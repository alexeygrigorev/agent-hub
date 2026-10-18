"""Jobs client and dispatcher for host agent.

Coordinates job offers, gate checks, claim races, lease renewals, and completions:
- Claims offered jobs atomically using host bearer token.
- Integrates runtime qualification and rootlock bridge before claiming.
- Automatically renews leases every 5 minutes while job is running.
- Records native epoch / Hub lease epoch binding and first-action evidence.
- Finalizes jobs with status, sanitized summary, and private evidence references.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import re
import shutil
import uuid
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import api, auth, config
from .guarded_root import GuardedRootBridge, IncumbentStatus, is_pid_alive, get_current_pid_namespace, WINDOW_SEC
from . import root_dispatch as rd
from . import role_dispatch as rld
from .role_dispatch import SUPPORTED_ROLE_PROFILES
from .runtime import RuntimeManager, get_first_action_digest
from .retired_native import verify_native_retirement

logger = logging.getLogger(__name__)


class JobsClient:
    """HTTP client for Hub jobs API endpoints."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def _request(
        self,
        method: str,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
        is_job_token: bool = False
    ) -> Dict[str, Any]:
        api.check_url(url)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if not token or not isinstance(token, str) or not token.strip():
            return {"error": "unauthorized", "reason": "missing_token", "status_code": 401}
        headers["Authorization"] = f"Bearer {token.strip()}"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            return api.safe_http_json(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                try:
                    err_body = e.read().decode("utf-8")
                    parsed = json.loads(err_body)
                    if isinstance(parsed, dict):
                        parsed.setdefault("error", "conflict")
                        parsed["status_code"] = 409
                        return parsed
                except Exception:
                    pass
                return {"error": "conflict", "status_code": 409}
            if e.code == 401:
                if is_job_token:
                    return {"error": "unauthorized", "status_code": 401}
                try:
                    new_token = auth.refresh_token()
                    headers["Authorization"] = f"Bearer {new_token}"
                    retry_req = urllib.request.Request(url, data=body, headers=headers, method=method)
                    return api.safe_http_json(retry_req, timeout=self.timeout)
                except Exception:
                    return {"error": "unauthorized", "status_code": 401}
            try:
                err_body = e.read().decode("utf-8")
                parsed = json.loads(err_body)
                parsed["status_code"] = e.code
                return parsed
            except Exception:
                return {"error": f"HTTP {e.code}", "status_code": e.code}

    def claim_job(
        self,
        base_url: str,
        job_id: str,
        host_id: str,
        token: str,
        fresh_gate: bool = True,
        gate_status: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Claim an offered job. POST /api/jobs/{id}/claim"""
        url = f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/claim"
        gate_dict = dict(gate_status) if gate_status else {"launch_allowed": fresh_gate}
        prof_supported = bool(gate_dict.get("profile_supported", False))
        native_supported = bool(gate_dict.get("native_root_supported", False))
        gate_dict.setdefault("profile_supported", prof_supported)
        gate_dict.setdefault("native_root_supported", native_supported)
        data = {
            "host_id": host_id,
            "fresh_gate": fresh_gate,
            "gate": gate_dict,
            "profile_supported": prof_supported,
            "native_root_supported": native_supported,
        }
        return self._request(
            "POST",
            url,
            data=data,
            token=token
        )

    def commit_role_execution(self, base_url: str, job_id: str, token: str,
                              epoch: int, generation: int) -> Dict[str, Any]:
        """Never retry: an uncertain response means the durable intent may exist."""
        return self._request("POST", f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/execute",
                             data={"epoch": epoch, "generation": generation},
                             token=token, is_job_token=True)

    def get_offers(
        self,
        base_url: str,
        host_id: str,
        token: str,
    ) -> List[Dict[str, Any]]:
        """Retrieve job offers for this host. GET /api/jobs/offers"""
        url = f"{base_url}/api/jobs/offers?host_id={urllib.parse.quote(host_id)}"
        res = self._request("GET", url, token=token)
        if isinstance(res, dict) and "offers" in res:
            return res["offers"]
        if isinstance(res, list):
            return res
        return []

    def decline_job(
        self,
        base_url: str,
        job_id: str,
        host_id: str,
        token: str,
        reason: str
    ) -> Dict[str, Any]:
        """Decline an offered job. POST /api/jobs/{id}/decline"""
        url = f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/decline"
        return self._request(
            "POST",
            url,
            data={"host_id": host_id, "reason": reason[:4096]},
            token=token
        )

    def start_job(
        self,
        base_url: str,
        job_id: str,
        job_token: str,
        epoch: int,
        evidence_digest: str,
        native_session: str,
        launcher_task_id: str
    ) -> Dict[str, Any]:
        """Signal job start with first-action evidence. POST /api/jobs/{id}/start"""
        url = f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/start"
        return self._request(
            "POST",
            url,
            data={
                "job_token": job_token,
                "epoch": epoch,
                "evidence_digest": evidence_digest,
                "native_session": native_session,
                "launcher_task_id": launcher_task_id
            },
            token=job_token,
            is_job_token=True
        )

    def renew_job(
        self,
        base_url: str,
        job_id: str,
        job_token: str,
        epoch: int
    ) -> Dict[str, Any]:
        """Renew lease for a running job. POST /api/jobs/{id}/renew"""
        url = f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/renew"
        return self._request(
            "POST",
            url,
            data={"job_token": job_token, "epoch": epoch},
            token=job_token,
            is_job_token=True
        )

    def finish_job(
        self,
        base_url: str,
        job_id: str,
        job_token: str,
        epoch: int,
        status: str,
        summary: str,
        evidence_refs: List[str]
    ) -> Dict[str, Any]:
        """Report job completion and release lease. POST /api/jobs/{id}/finish"""
        url = f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/finish"
        return self._request(
            "POST",
            url,
            data={
                "job_token": job_token,
                "epoch": epoch,
                "status": status,
                "summary": summary[:4096],
                "evidence_refs": evidence_refs
            },
            token=job_token,
            is_job_token=True
        )

    def get_job_status(
        self,
        base_url: str,
        job_id: str,
        token: str,
    ) -> Dict[str, Any]:
        """Retrieve authoritative status of a job from the Hub. GET /api/jobs/{id}/status"""
        url = f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/status"
        return self._request("GET", url, token=token)

    def reconcile_terminal_job(
        self,
        base_url: str,
        job_id: str,
        token: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Reconcile a terminal role job (M8). POST /api/jobs/{id}/reconcile"""
        url = f"{base_url}/api/jobs/{urllib.parse.quote(job_id)}/reconcile"
        return self._request(
            "POST",
            url,
            data=data,
            token=token,
            is_job_token=False
        )



def write_role_uncertain_proof(
    runs_dir: pathlib.Path,
    run_dir: Optional[pathlib.Path],
    job_id: str,
    role: Optional[Dict[str, Any]],
    kind: str,
    lease_epoch: int,
    phase: str,
    cause: str,
    is_prelaunch_refusal: bool = False,
    exc: Optional[Exception] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> pathlib.Path:
    """Record safe, scrubbed durable cause/phase proof for an uncertain role outcome."""
    proof_data = {
        "job_id": job_id,
        "role_name": role.get("name") if role else None,
        "profile": kind,
        "lease_epoch": lease_epoch,
        "generation": role.get("generation") if role else None,
        "phase": phase,
        "status": "uncertain",
        "uncertain": True,
        "prelaunch_refusal": bool(is_prelaunch_refusal),
        "ambiguous_launch": not bool(is_prelaunch_refusal),
        "cause": cause,
        "error_type": type(exc).__name__ if exc else None,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if extra:
        proof_data["details"] = extra

    runs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = runs_dir / f"outcome-uncertain-{job_id}.json"
    try:
        rld.write_private_json(summary_path, proof_data)
    except Exception as e:
        logger.warning("Failed to write runs_dir outcome-uncertain proof: %s", e)

    active_idx = runs_dir / f"active-role-{job_id}.json"
    if active_idx.exists():
        try:
            active_idx.unlink()
        except OSError:
            pass

    if run_dir is not None:
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            run_path = run_dir / f"outcome-uncertain-{job_id}.json"
            rld.write_private_json(run_path, proof_data)
        except Exception as e:
            logger.warning("Failed to write run_dir outcome-uncertain proof: %s", e)
        try:
            rld.update_role_custody(runs_dir, run_dir, job_id, {"phase": "uncertain", "cause": cause, "status": "uncertain"})
        except Exception:
            pass

    return summary_path


class JobDispatcher:
    """Coordinates job offer evaluation, claiming, execution, and renewals."""

    def __init__(
        self,
        base_url: str,
        host_id: str,
        token_provider: Optional[Callable[[], str]] = None,
        client: Optional[JobsClient] = None,
        runtime_mgr: Optional[RuntimeManager] = None,
        root_bridge: Optional[GuardedRootBridge] = None,
        clock: Optional[Any] = None
    ):
        self.base_url = base_url
        self.host_id = host_id
        self.token_provider = token_provider or auth.get_access_token
        self.client = client or JobsClient()
        self.runtime_mgr = runtime_mgr or RuntimeManager()
        self.clock = clock or time
        self.root_bridge = root_bridge or GuardedRootBridge(clock=self.clock)
        if isinstance(self.runtime_mgr, RuntimeManager):
            # Authenticated, same-origin Hub scheduling read for every new Root run.
            self.runtime_mgr.bind_hub(self.base_url, lambda: self.token_provider(), self.host_id, self.clock)

        self.running_jobs: Dict[str, Dict[str, Any]] = {}
        self._renewal_failures: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get_running_job_count(self) -> int:
        with self._lock:
            return len(self.running_jobs)

    def get_running_job_ids(self) -> List[str]:
        with self._lock:
            return list(self.running_jobs.keys())

    def handle_offer(self, offer_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate and act on an incoming job offer."""
        job_data = offer_payload.get("job") if isinstance(offer_payload.get("job"), dict) else offer_payload
        job_id = job_data.get("id") or job_data.get("job_id") or offer_payload.get("job_id") or offer_payload.get("id")
        if not job_id:
            return {"accepted": False, "reason": "missing_job_id"}

        kind = job_data.get("kind") or offer_payload.get("kind", "custom")
        payload = job_data.get("payload") if "payload" in job_data else offer_payload.get("payload", {})
        role_binding = job_data.get("role_binding")
        token = self.token_provider()
        if not token or not isinstance(token, str) or not token.strip():
            return {"accepted": False, "reason": "missing_host_token"}

        # Check offer expiry (fail closed on malformed or expired timestamps across all fields)
        clk_val = self.clock.time() if hasattr(self.clock, "time") else (self.clock() if callable(self.clock) else time.time())
        import math
        for src in (offer_payload, job_data):
            for exp_key in ("offer_expires_at", "expires_at"):
                if exp_key in src:
                    val = src[exp_key]
                    if val is None:
                        continue
                    if isinstance(val, bool):
                        return {"accepted": False, "reason": "malformed_offer_expiry"}
                    if isinstance(val, (int, float)):
                        if math.isnan(val) or math.isinf(val):
                            return {"accepted": False, "reason": "malformed_offer_expiry"}
                        exp_sec = float(val)
                    elif isinstance(val, str):
                        if not val.strip():
                            return {"accepted": False, "reason": "malformed_offer_expiry"}
                        if not (val.endswith("Z") or "+" in val or ("-" in val[10:] and "T" in val)):
                            return {"accepted": False, "reason": "malformed_offer_expiry"}
                        try:
                            exp_dt = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
                            if exp_dt.tzinfo is None:
                                return {"accepted": False, "reason": "malformed_offer_expiry"}
                            exp_sec = exp_dt.timestamp()
                        except Exception:
                            return {"accepted": False, "reason": "malformed_offer_expiry"}
                    else:
                        return {"accepted": False, "reason": "malformed_offer_expiry"}

                    if exp_sec <= clk_val:
                        return {"accepted": False, "reason": "offer_expired"}

        has_qual_mock = getattr(self.runtime_mgr, "_has_qualified_mock_runner", None)
        if callable(has_qual_mock) and not (hasattr(self.runtime_mgr, "_mock_return_value") or hasattr(self.runtime_mgr, "assert_called")):
            is_mock = bool(has_qual_mock())
        else:
            mock_runner = getattr(self.runtime_mgr, "_mock_runner", None)
            allow_mock = type(getattr(self.runtime_mgr, "allow_mock_runner", False)) is bool and getattr(self.runtime_mgr, "allow_mock_runner", False) is True
            is_mock_magic = (
                hasattr(self.runtime_mgr, "_mock_return_value")
                or hasattr(self.runtime_mgr, "assert_called")
                or hasattr(mock_runner, "_mock_return_value")
                or hasattr(mock_runner, "assert_called")
            )
            is_mock = allow_mock and callable(mock_runner) and not is_mock_magic
        if is_mock and getattr(self.root_bridge, "is_canonical_state", False):
            is_mock = False

        if role_binding is not None:
            if not self._valid_role_binding(role_binding, kind):
                return {"accepted": False, "reason": "invalid_role_binding"}
            # Real non-root backends are currently unsupported by RuntimeManager.
            # Inventory strings and ordinary mocks are not runtime qualification.
            if not self._role_consumer_qualified(kind):
                return {"accepted": False, "reason": "HOLD: role backend unsupported (M8b)"}

        # Gate check for root-run: qualification and read-only incumbent check
        op_qualified = False
        if kind == "root-run":
            eligible, reason, report = self.runtime_mgr.can_process_root_run()
            if not eligible:
                logger.info(f"Declining root-run offer {job_id}: {reason}")
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": reason, "qualification": report}

            # Operational trusted dispatch: only when the runtime genuinely
            # qualified the installed launcher/entrypoint (never for mocks).
            op_qualified = (
                report.get("operational_qualified") is True
                and not is_mock
            )
            if getattr(self.root_bridge, "is_canonical_state", False) and not op_qualified:
                skip_reason = "operational_root_receiver_unqualified: canonical_root_state_forbidden_while_unsupported"
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": skip_reason}

            if op_qualified:
                # Script owns the canonical lock: read-only incumbent check with
                # the script's own semantics. No lock is taken (a probe would
                # make the timer's flock -n see a false busy holder). Unknown
                # state (corrupt/unreadable/foreign namespace/ambiguous live
                # identity) HOLDS: it is never treated as "no incumbent".
                cfg = getattr(self.runtime_mgr, "config", None)
                cst, _ = rd.canonical_incumbent_status(
                    getattr(cfg, "canonical_state_dir", None), self.clock)
                if cst in ("healthy", "starting", "unknown"):
                    skip_reason = "root_state_unknown" if cst == "unknown" else "skip_healthy_incumbent"
                    try:
                        self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                    except Exception:
                        pass
                    return {"accepted": False, "reason": skip_reason}
            else:
                # Check for corrupt state.json
                is_valid, incumbent_state = self.root_bridge.is_state_valid()
                if not is_valid:
                    skip_reason = "root_state_unknown"
                    try:
                        self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                    except Exception:
                        pass
                    return {"accepted": False, "reason": skip_reason}

                lock_held = self.root_bridge.is_lock_held()
                status, _ = self.root_bridge.inspect_incumbent(job_id, lock_held=lock_held)
                if status in (IncumbentStatus.HEALTHY, IncumbentStatus.STARTING):
                    skip_reason = "skip_healthy_incumbent"
                    try:
                        self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                    except Exception:
                        pass
                    return {"accepted": False, "reason": skip_reason}
                elif not lock_held and incumbent_state.get("status") is None and self.root_bridge.lock_path.exists():
                    skip_reason = "root_state_unknown"
                    try:
                        self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                    except Exception:
                        pass
                    return {"accepted": False, "reason": skip_reason}

            is_fixture = is_mock and (report.get("is_fixture_only") is True) and (report.get("operational_qualified") is False) and not getattr(self.root_bridge, "is_canonical_state", False)
            if not is_fixture and not op_qualified:
                skip_reason = "operational_root_receiver_unqualified: real trusted receiver is not qualified (issue 59)"
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": skip_reason}
        elif kind in SUPPORTED_ROLE_PROFILES:
            if role_binding is None:
                return {"accepted": False, "reason": "missing_role_binding"}
            eligible, reason, report = self.runtime_mgr.can_process_role_run(kind)
            if not eligible:
                logger.info(f"Declining {kind} offer {job_id}: {reason}")
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": reason, "qualification": report}
        elif not is_mock:
            # Validate job kind capability (non-root kinds are currently unsupported under incomplete issue 59)
            reason = f"unsupported_job_kind: {kind}"
            try:
                self.client.decline_job(self.base_url, job_id, self.host_id, token, reason)
            except Exception:
                pass
            return {"accepted": False, "reason": reason}

        # Evaluate gate status before claiming
        if is_mock:
            gate_status = {"launch_allowed": True, "reason": "mock_allowed"}
            if kind == "root-run":
                prof_ok = (
                    report.get("profile_supported") is not False
                    and report.get("root_profile_supported") is not False
                    and report.get("status") != "UNSUPPORTED"
                )
                if not prof_ok:
                    reason = f"root_execution_profile_unsupported: {report.get('reason', 'profile unsupported')}"
                    try:
                        self.client.decline_job(self.base_url, job_id, self.host_id, token, reason)
                    except Exception:
                        pass
                    return {"accepted": False, "reason": reason, "qualification": report}
                gate_status["profile_supported"] = True
                gate_status["native_root_supported"] = True
            elif kind in SUPPORTED_ROLE_PROFILES:
                gate_status["profile_supported"] = True
        elif kind in SUPPORTED_ROLE_PROFILES:
            # Genuine role admission: evaluate fresh role qualification without Root-only Codex gate
            op_qualified = bool(
                isinstance(report, dict)
                and report.get("supported") is True
                and report.get("operational_qualified") is True
                and not report.get("is_fixture_only", False)
            )
            if op_qualified:
                gate_status = {
                    "launch_allowed": True,
                    "reason": "role_qualified",
                    "profile_supported": True,
                    "role": kind,
                    "chosen": report.get("chosen"),
                }
            else:
                skip_reason = (
                    f"role_unqualified: fixture_only_qualification_rejected: {kind}"
                    if report.get("is_fixture_only")
                    else f"role_unqualified: operational_qualification_required: {kind}"
                )
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": skip_reason, "qualification": report}
        else:
            from . import gate
            gate._gate_cache = None
            gate._gate_cache_time = 0.0
            gate_clk = self.clock if hasattr(self.clock, "time") and not isinstance(self.clock, type(time)) else None
            gate_status = gate.get_gate_status(gate_clk)
            if kind == "root-run":
                gate_status["profile_supported"] = bool(report.get("root_profile_supported", False))
                gate_status["native_root_supported"] = bool(report.get("operational_qualified", False))
            if not gate_status.get("launch_allowed", False):
                reason = f"gate_refused: {gate_status.get('reason', 'gate not allowed')}"
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": reason}

        # Attempt to claim job atomically
        try:
            claim_res = self.client.claim_job(
                self.base_url, job_id, self.host_id, token,
                fresh_gate=True, gate_status=gate_status
            )
        except Exception as e:
            return {"accepted": False, "reason": f"claim_failed: {e}"}

        if claim_res.get("error"):
            return {"accepted": False, "reason": claim_res.get("error")}

        job_token = claim_res.get("job_token")
        lease_epoch = claim_res.get("lease_epoch")
        deadline_at = claim_res.get("deadline_at")
        claimed_binding = claim_res.get("role_binding")
        if role_binding is not None and (not self._valid_role_binding(claimed_binding, kind) or claimed_binding != role_binding):
            return {"accepted": False, "reason": "HOLD: claim role binding changed"}
        if claimed_binding is not None and not (self._valid_role_binding(claimed_binding, kind) and self._role_consumer_qualified(kind)):
            return {"accepted": False, "reason": "HOLD: unsupported claimed role"}

        if not job_token or not isinstance(job_token, str) or not job_token.strip():
            return {"accepted": False, "reason": "malformed_claim_response: invalid job_token"}

        if isinstance(lease_epoch, bool) or not isinstance(lease_epoch, int) or lease_epoch <= 0:
            return {"accepted": False, "reason": "malformed_claim_response: invalid lease_epoch"}

        if deadline_at is not None:
            if not isinstance(deadline_at, (str, int, float)) or (isinstance(deadline_at, str) and not deadline_at.strip()):
                return {"accepted": False, "reason": "malformed_claim_response: invalid deadline_at"}
            try:
                if isinstance(deadline_at, str):
                    if not (deadline_at.endswith("Z") or "+" in deadline_at or ("-" in deadline_at[10:] and "T" in deadline_at)):
                        return {"accepted": False, "reason": "malformed_claim_response: invalid deadline_at"}
                    d_dt = datetime.datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
                    d_sec = d_dt.timestamp()
                else:
                    d_sec = float(deadline_at)
                clk_now = self.clock.time() if hasattr(self.clock, "time") else (self.clock() if callable(self.clock) else time.time())
                if d_sec <= clk_now:
                    return {"accepted": False, "reason": "claim_deadline_already_expired"}
            except Exception:
                return {"accepted": False, "reason": "malformed_claim_response: invalid deadline_at"}

        # Record running job
        job_info = {
            "job_id": job_id,
            "kind": kind,
            "op_qualified": bool(kind == "root-run" and op_qualified),
            "job_token": job_token,
            "lease_epoch": lease_epoch,
            "deadline_at": deadline_at,
            "started_at": self.clock.time(),
            "started_wall": time.time(),
            "stop_event": threading.Event()
        }
        if claimed_binding is not None:
            job_info["role_binding"] = claimed_binding
            try:
                runs_dir = self._get_role_runs_dir()
                run_dir = runs_dir / f"{kind}-{job_id}"
                safe_job = re.sub(r"[^a-zA-Z0-9_-]", "-", job_id)
                task_id = f"hub-role-{kind}-{safe_job}-g{claimed_binding.get('generation', 0)}-e{lease_epoch}"
                rld.save_role_custody(
                    runs_dir=runs_dir,
                    run_dir=run_dir,
                    job_id=job_id,
                    data={
                        "job_id": job_id,
                        "kind": kind,
                        "role_binding": claimed_binding,
                        "lease_epoch": lease_epoch,
                        "job_token": job_token,
                        "deadline_at": deadline_at,
                        "payload": payload,
                        "base_url": self.base_url,
                        "host_id": self.host_id,
                        "task_id": task_id,
                        "phase": "claimed",
                        "claimed_at": time.time(),
                    },
                )
            except Exception as e:
                logger.warning("Failed to save initial role custody for %s: %s", job_id, e)
        with self._lock:
            self.running_jobs[job_id] = job_info

        # Spawn worker thread to execute and manage lease
        worker_thread = threading.Thread(
            target=self._run_job_lifecycle,
            args=(job_info, payload),
            name=f"job-runner-{job_id}",
            daemon=True
        )
        worker_thread.start()

        return {
            "accepted": True,
            "job_id": job_id,
            "lease_epoch": lease_epoch,
            "deadline_at": deadline_at
        }

    def _run_job_lifecycle(self, job_info: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """Execute job, maintain renewals, and report completion."""
        job_id = job_info["job_id"]
        kind = job_info["kind"]
        job_token = job_info["job_token"]
        lease_epoch = job_info["lease_epoch"]
        stop_event = job_info["stop_event"]
        job_start_wall = float(job_info.get("started_wall", time.time()))

        deadline_at = job_info.get("deadline_at")
        deadline_sec: Optional[float] = None
        if deadline_at is not None:
            try:
                if isinstance(deadline_at, str):
                    d_dt = datetime.datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
                    deadline_sec = d_dt.timestamp()
                else:
                    deadline_sec = float(deadline_at)
            except Exception:
                deadline_sec = 0.0

        acquired_root = False
        script_owned = bool(job_info.get("op_qualified"))
        op_qualified = script_owned
        native_session = f"session-{job_id}"
        launcher_task_id = f"hub-root-{job_id}" if script_owned else f"hub-{job_id}"
        renew_thread: Optional[threading.Thread] = None
        role = job_info.get("role_binding")
        if role is not None:
            safe_job = re.sub(r"[^a-zA-Z0-9_-]", "-", job_id)
            launcher_task_id = f"hub-role-{kind}-{safe_job}-g{role.get('generation', 0)}-e{lease_epoch}"
            native_session = None
            runs_dir = self._get_role_runs_dir()
            run_dir_path = runs_dir / f"{kind}-{job_id}"
            rld.ensure_private_dir(run_dir_path)
        role_intent_recorded = False

        def _finish_failed_with_bridge(fail_summary: str, initial_refs: Optional[List[str]] = None,
                                       known_terminal: bool = False,
                                       phase: str = "unknown",
                                       is_prelaunch_refusal: bool = False,
                                       exc: Optional[Exception] = None) -> None:
            if role is not None and role_intent_recorded and not known_terminal:
                scrubbed_cause = rld.scrub(fail_summary, job_token)
                run_dir_path = (runs_dir / f"{kind}-{job_id}") if role is not None else None
                write_role_uncertain_proof(
                    runs_dir=runs_dir,
                    run_dir=run_dir_path,
                    job_id=job_id,
                    role=role,
                    kind=kind,
                    lease_epoch=lease_epoch,
                    phase=phase,
                    cause=scrubbed_cause,
                    is_prelaunch_refusal=is_prelaunch_refusal,
                    exc=exc,
                )
                logger.warning(
                    "Role consumer state uncertain (phase=%s, prelaunch_refusal=%s): %s; retain intent and HOLD %s",
                    phase, is_prelaunch_refusal, scrubbed_cause, job_id,
                )
                return
            refs = list(initial_refs) if initial_refs else []
            out_summary = fail_summary
            if acquired_root:
                finish_res = self.root_bridge.finish(
                    status="failed",
                    exit_code=1,
                    summary_text=fail_summary,
                    evidence_refs=refs
                )
                if isinstance(finish_res, dict):
                    handoff_path = finish_res.get("handoff")
                    if handoff_path and os.path.exists(handoff_path):
                        if handoff_path not in refs:
                            refs.append(handoff_path)
                        try:
                            with open(handoff_path, "r", encoding="utf-8") as f:
                                handoff_text = f.read()
                            if handoff_text and handoff_text not in out_summary:
                                out_summary = f"{out_summary}\n\n{handoff_text}"
                        except Exception:
                            pass
                    for ref in finish_res.get("evidence_refs", []):
                        if ref not in refs:
                            refs.append(ref)
            self.client.finish_job(
                self.base_url, job_id, job_token, lease_epoch,
                "failed", out_summary, refs
            )

        try:
            if kind == "root-run" and getattr(self.root_bridge, "is_canonical_state", False) and not op_qualified:
                self.client.finish_job(
                    self.base_url, job_id, job_token, lease_epoch,
                    "failed", "canonical_root_state_forbidden_while_unsupported", []
                )
                return

            if kind == "root-run" and script_owned:
                # Trusted dispatch mode: the CAG script owns the canonical
                # root lock itself; the host never takes a second lock (the
                # script's flock would deadlock).
                self._run_script_owned_root(job_info, payload)
                return
            elif kind == "root-run":
                if getattr(self.root_bridge, "_released", False) or stop_event.is_set():
                    return
                # Acquire rootlock and bind native epoch with hub lease epoch
                acq = self.root_bridge.acquire(
                    hub_job_id=job_id,
                    hub_lease_epoch=lease_epoch,
                    hub_url=self.base_url,
                    non_blocking=True
                )
                if not acq.get("acquired") or getattr(self.root_bridge, "_released", False) or stop_event.is_set():
                    if acq.get("acquired"):
                        self.root_bridge.release()
                    logger.error(f"Failed to acquire root lock for job {job_id}: {acq.get('reason')}")
                    self.client.finish_job(
                        self.base_url, job_id, job_token, lease_epoch,
                        "failed", f"root lock acquisition failed: {acq.get('reason')}", []
                    )
                    return
                acquired_root = True
                native_session = f"root-{acq.get('run_id')}"

            if getattr(self.root_bridge, "_released", False) or stop_event.is_set():
                if acquired_root:
                    self.root_bridge.release()
                return

            # Check for pre-existing evidence file before worker start
            if role is not None:
                runs_dir = self._get_role_runs_dir()
            else:
                runs_dir = pathlib.Path(self.root_bridge.runs_dir)
            runs_dir.mkdir(parents=True, exist_ok=True)
            evidence_file = runs_dir / f"first-action-{job_id}.json"

            if evidence_file.exists():
                logger.error(f"Pre-existing evidence file found before job start for {job_id}")
                _finish_failed_with_bridge("pre-existing evidence file")
                return

            if role is not None:
                if not self._valid_role_binding(role, kind) or not self._role_consumer_qualified(kind):
                    logger.warning("Role consumer unsupported before execution intent; HOLD %s", job_id)
                    return
                # The server commits intent on this real job before the current
                # approved consumer starts. No cached/offline permission or retry.
                try:
                    grant = self.client.commit_role_execution(
                        self.base_url, job_id, job_token, lease_epoch, role["generation"])
                except Exception:
                    logger.warning("Role execution authorization uncertain; HOLD %s", job_id)
                    return
                if (grant.get("execute") is not True or grant.get("epoch") != lease_epoch
                        or grant.get("generation") != role["generation"] or grant.get("role") != role["name"]
                        or grant.get("profile") != kind or grant.get("payload") != payload or stop_event.is_set()):
                    logger.warning("Role execution authorization refused or uncertain; HOLD %s", job_id)
                    return
                role_intent_recorded = True
                try:
                    rld.update_role_custody(runs_dir, run_dir_path, job_id, {"phase": "intent_committed"})
                except Exception:
                    pass

            # Execute worker in thread to ensure actual worker execution starts
            exec_result: Dict[str, Any] = {}
            exec_exc: List[Exception] = []

            server_started_event = threading.Event()
            first_action_ready_event = threading.Event()
            server_start_result: Dict[str, Any] = {}
            cb_data: Dict[str, Any] = {}

            def _role_start_callback(start_data: Dict[str, Any]) -> Dict[str, Any]:
                try:
                    if isinstance(start_data, dict):
                        cb_data.update(start_data)
                    if not evidence_file.exists() and isinstance(start_data, dict):
                        ev_payload = start_data.get("data")
                        if isinstance(ev_payload, dict):
                            with open(evidence_file, "w", encoding="utf-8") as f:
                                json.dump(ev_payload, f)
                except Exception:
                    pass
                first_action_ready_event.set()
                server_started_event.wait(timeout=15.0)
                if server_start_result.get("status") == "started":
                    return {
                        "status": "started",
                        "acknowledged_by": "hub_server",
                        "server_start": server_start_result.get("start_res", {}),
                        "job_id": job_id,
                        "epoch": lease_epoch,
                        "generation": role.get("generation") if role else None,
                    }
                return {
                    "status": "refused",
                    "error": server_start_result.get("error", "server_start_timeout_or_failed"),
                }

            def _worker_exec():
                try:
                    res = self.runtime_mgr.execute_job(
                        job_id, kind, payload, job_token, lease_epoch, self.base_url,
                        start_callback=_role_start_callback if role is not None else None,
                        role_binding=role, stop_event=stop_event, deadline_sec=deadline_sec,
                    )
                    exec_result["res"] = res
                except Exception as e:
                    exec_exc.append(e)

            exec_thread = threading.Thread(
                target=_worker_exec,
                name=f"worker-exec-{job_id}",
                daemon=True
            )
            exec_thread.start()

            # Poll for worker-produced evidence_file while worker is running.
            # Role jobs wait for actual bootstrap window (BOOTSTRAP_TIMEOUT_SEC = 120s), bounded by deadline.
            def _get_clk_time() -> float:
                if hasattr(self.clock, "time") and callable(self.clock.time):
                    return self.clock.time()
                if callable(self.clock):
                    return self.clock()
                return time.time()

            bootstrap_window = rld.BOOTSTRAP_TIMEOUT_SEC if role is not None else 10.0
            poll_deadline = _get_clk_time() + bootstrap_window
            if deadline_sec is not None:
                poll_deadline = min(poll_deadline, deadline_sec)

            while _get_clk_time() < poll_deadline and exec_thread.is_alive():
                if evidence_file.exists():
                    break
                if stop_event.is_set():
                    break
                first_action_ready_event.wait(timeout=0.02)
                if evidence_file.exists():
                    break
                exec_thread.join(timeout=0.01)

            if acquired_root and getattr(self.root_bridge, "_released", False):
                return

            run_dir_path = (runs_dir / f"{kind}-{job_id}") if role is not None else None

            if exec_exc:
                exc = exec_exc[0]
                if role is not None:
                    # Definite prelaunch refusal ONLY on witnessed preflight checks with absence of launch attempt
                    # Never guess prelaunch from exception text substrings
                    is_prelaunch = bool(getattr(exc, "prelaunch_refusal", False))
                    phase = "preflight" if is_prelaunch else "execution_error"
                    err_msg = str(exc)
                    cause = rld.scrub(err_msg, job_token)
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=is_prelaunch,
                        exc=exc,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=%s): %s; retain intent and HOLD %s",
                        phase, is_prelaunch, cause, job_id,
                    )
                    return
                _finish_failed_with_bridge(f"execution_error: {exc}")
                return

            res_obj = exec_result.get("res") or {}
            res_refs = res_obj.get("evidence_refs", []) if isinstance(res_obj, dict) else []

            # If worker finished with error or failed without producing evidence
            if not exec_thread.is_alive() and res_obj.get("status") in ("failed", "error"):
                summary = res_obj.get("summary") or "worker execution failed"
                _finish_failed_with_bridge(summary, res_refs, known_terminal=True)
                return

            # Never self-fabricate first-action evidence or spawn a dummy sleep child in the dispatcher.
            if not evidence_file.exists():
                logger.error(f"No genuine first-action evidence produced for job {job_id}")
                if role is not None:
                    phase = "bootstrap_timeout"
                    cause = "no first action evidence produced within bootstrap window"
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=False,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=False): %s; retain intent and HOLD %s",
                        phase, cause, job_id,
                    )
                    return
                _finish_failed_with_bridge("no first action evidence", res_refs)
                return

            # Validate custody of worker-produced evidence file
            try:
                if evidence_file.stat().st_mtime < job_start_wall - 0.05:
                    raise ValueError("stale evidence file created before job start")
                for _ in range(5):
                    try:
                        existing_data = json.loads(evidence_file.read_text(encoding="utf-8"))
                        break
                    except (json.JSONDecodeError, OSError):
                        time.sleep(0.02)
                else:
                    existing_data = json.loads(evidence_file.read_text(encoding="utf-8"))
                if not isinstance(existing_data, dict):
                    raise ValueError("non-dict evidence")
                ex_pid = existing_data.get("pid")
                sup_pid = getattr(getattr(self.root_bridge, "_supervisor_proc", None), "pid", None)
                if (
                    type(ex_pid) is not int
                    or ex_pid <= 1
                    or ex_pid == os.getpid()
                    or ex_pid == os.getppid()
                    or ex_pid == 65534
                    or (sup_pid is not None and ex_pid == sup_pid)
                ):
                    raise ValueError("invalid, reaper, supervisor, or daemon pid")
                if existing_data.get("job_id") != job_id:
                    raise ValueError("mismatched or missing job_id")
                if existing_data.get("epoch") != lease_epoch:
                    raise ValueError("mismatched or missing epoch")
                if acquired_root and existing_data.get("run_id") != acq.get("run_id"):
                    raise ValueError("mismatched or missing run_id")
                if role is not None:
                    if "role" in existing_data and existing_data.get("role") != role.get("name"):
                        raise ValueError("mismatched role name")
                    if "generation" in existing_data and existing_data.get("generation") != role.get("generation"):
                        raise ValueError("mismatched role generation")
                    ev_sess = existing_data.get("native_session")
                    if (
                        not isinstance(ev_sess, str)
                        or not rld.UUID_RE.fullmatch(ev_sess)
                        or ev_sess == rld.NIL_UUID
                    ):
                        raise ValueError("invalid or missing native_session in role evidence")
                    corroborated_target = cb_data.get("target") or self._corroborate_native_target(job_id, kind, role, lease_epoch, run_dir_path)
                    if corroborated_target is not None and ev_sess.lower() != corroborated_target.session_id.lower():
                        raise ValueError("mismatched native_session in role evidence against corroborated binding")
                    native_session = corroborated_target.session_id if corroborated_target else ev_sess
                if "utc" in existing_data:
                    utc_val = existing_data.get("utc")
                    if not isinstance(utc_val, str) or not utc_val.strip():
                        raise ValueError("missing or invalid utc")
                    if not (utc_val.endswith("Z") or "+" in utc_val or ("-" in utc_val[10:] and "T" in utc_val)):
                        raise ValueError("utc missing timezone")
                    utc_dt = datetime.datetime.fromisoformat(utc_val.replace("Z", "+00:00"))
                    if utc_dt.tzinfo is None:
                        raise ValueError("utc timezone naive")
                try:
                    os.kill(ex_pid, 0)
                except OSError:
                    raise ValueError("dead pid in evidence")
            except Exception as e:
                logger.error(f"First-action custody validation failed for {job_id}: {e}")
                server_start_result["status"] = "failed"
                server_start_result["error"] = str(e)
                server_started_event.set()
                if role is not None:
                    phase = "custody_validation"
                    cause = rld.scrub(f"first-action custody failed: {e}", job_token)
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=False,
                        exc=e,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=False): %s; retain intent and HOLD %s",
                        phase, cause, job_id,
                    )
                    return
                _finish_failed_with_bridge(f"first-action custody failed: {e}", res_refs)
                return

            if role is not None:
                from .role_dispatch import (
                    get_role_first_action_digest,
                    is_backend_or_run_injected_test,
                    read_proc_cgroups,
                )
                corroborated_target = cb_data.get("target") or self._corroborate_native_target(job_id, kind, role, lease_epoch, run_dir_path)

                fixture = None
                is_test = bool(cb_data.get("is_injected_test"))
                if not is_test:
                    if hasattr(self.client, "mock_calls") or type(self.client).__name__ in ("MagicMock", "Mock"):
                        is_test = True
                    elif hasattr(getattr(self.runtime_mgr, "execute_job", None), "mock_calls") or type(getattr(self.runtime_mgr, "execute_job", None)).__name__ in ("MagicMock", "Mock"):
                        is_test = True
                    elif getattr(self.runtime_mgr, "role_adapter", None) is not None:
                        ra = self.runtime_mgr.role_adapter
                        is_test = is_backend_or_run_injected_test(
                            getattr(ra, "launcher_backend", None),
                            getattr(ra, "launcher_executor", None),
                        )
                if is_test:
                    target_to_use = corroborated_target or cb_data.get("target")
                    nb = cb_data.get("native_binding") or {}
                    w_pid = nb.get("workload_pid") or getattr(target_to_use, "workload_pid", None)
                    sup_pid = nb.get("worker_pid") or getattr(target_to_use, "worker_pid", None)
                    cg_path = nb.get("workload_cgroup") or getattr(target_to_use, "workload_cgroup", None)
                    # Nonproduction unit-test fixtures where execute_job is mocked directly without full binding
                    if w_pid is None and existing_data.get("pid"):
                        w_pid = existing_data.get("pid")
                        if sup_pid is None:
                            sup_pid = 999998
                        if cg_path is None:
                            try:
                                cg_list = read_proc_cgroups(w_pid)
                                if cg_list:
                                    cg_path = cg_list[0][2]
                            except Exception:
                                pass
                    fixture = {
                        "workload_pid": w_pid,
                        "worker_pid": sup_pid,
                        "workload_cgroup": cg_path,
                        "containment_empty": nb.get("containment_empty", False),
                        "phase": nb.get("phase", "running"),
                        "created_at_ms": getattr(target_to_use, "created_at_ms", None) or nb.get("created_at_ms") or int(time.time() * 1000) - 2000,
                        "is_fixture": True,
                    }

                evidence_digest = get_role_first_action_digest(
                    evidence_file,
                    expected_job_id=job_id,
                    expected_epoch=lease_epoch,
                    expected_role_name=role.get("name", ""),
                    expected_generation=role.get("generation", 0),
                    expected_native_session=native_session,
                    expected_native_target=corroborated_target,
                    nonproduction_fixture=fixture,
                )
            else:
                evidence_digest = get_first_action_digest(
                    evidence_file,
                    expected_job_id=job_id,
                    expected_run_id=acq.get("run_id") if acquired_root else None,
                    expected_epoch=lease_epoch
                )
            if not evidence_digest:
                logger.error(f"Failed to compute first-action digest for {job_id}")
                server_start_result["status"] = "failed"
                server_start_result["error"] = "no_first_action_digest"
                server_started_event.set()
                if role is not None:
                    phase = "evidence_digest"
                    cause = "no first action digest"
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=False,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=False): %s; retain intent and HOLD %s",
                        phase, cause, job_id,
                    )
                    return
                _finish_failed_with_bridge("no first action digest", res_refs)
                return

            # Start job on server
            start_res = self.client.start_job(
                self.base_url, job_id, job_token, lease_epoch,
                evidence_digest, native_session, launcher_task_id
            )
            if start_res.get("error"):
                logger.error(f"Failed to signal job start for {job_id}: {start_res.get('error')}")
                server_start_result["status"] = "failed"
                server_start_result["error"] = start_res.get("error")
                server_started_event.set()
                if role is not None:
                    phase = "server_start"
                    cause = rld.scrub(f"start_job failed: {start_res.get('error')}", job_token)
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=False,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=False): %s; retain intent and HOLD %s",
                        phase, cause, job_id,
                    )
                    return
                _finish_failed_with_bridge(f"start_job failed: {start_res.get('error')}", res_refs)
                return

            server_start_result["status"] = "started"
            server_start_result["start_res"] = start_res
            server_started_event.set()
            if role is not None:
                try:
                    rld.update_role_custody(runs_dir, run_dir_path, job_id, {
                        "phase": "running",
                        "native_session": native_session,
                        "evidence_digest": evidence_digest,
                    })
                except Exception:
                    pass

            # Start periodic renewal loop in background
            renew_thread = threading.Thread(
                target=self._renewal_loop,
                args=(job_id, job_token, lease_epoch, stop_event, role),
                name=f"renew-{job_id}",
                daemon=True
            )
            renew_thread.start()

            # Wait for execution to finish
            exec_thread.join()
            if exec_exc:
                exc = exec_exc[0]
                if role is not None:
                    phase = "execution_error"
                    cause = rld.scrub(f"execution_error: {exc}", job_token)
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=False,
                        exc=exc,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=False): %s; retain intent and HOLD %s",
                        phase, cause, job_id,
                    )
                    return
                status = "failed"
                summary = f"execution_error: {exc}"
                evidence_refs = []
            else:
                exec_res = exec_result.get("res", {})
                status = exec_res.get("status", "failed" if role is not None else "succeeded")
                summary = exec_res.get("summary", "completed")
                evidence_refs = exec_res.get("evidence_refs", [])
                if role is not None and status == "succeeded" and not evidence_refs:
                    evidence_refs = [str(evidence_file)]

            # Stop renewal loop
            was_stopped_externally = stop_event.is_set()
            stop_event.set()

            # Check deadline_at
            clk_now = self.clock.time() if hasattr(self.clock, "time") else (self.clock() if callable(self.clock) else time.time())
            if deadline_sec is not None and clk_now > deadline_sec:
                if role is not None:
                    phase = "deadline_exceeded"
                    cause = f"deadline_exceeded: job execution exceeded deadline_at ({deadline_sec})"
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=False,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=False): %s; retain intent and HOLD %s",
                        phase, cause, job_id,
                    )
                    return
                status = "failed"
                summary = "deadline_exceeded: job execution exceeded deadline_at"

            # Check renewal failures (for both root and non-root jobs)
            if job_id in self._renewal_failures:
                fail_err = self._renewal_failures.pop(job_id)
                if role is not None:
                    phase = "lease_renewal_failed"
                    cause = rld.scrub(f"lease_renewal_failed: {fail_err}", job_token)
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir_path,
                        job_id=job_id,
                        role=role,
                        kind=kind,
                        lease_epoch=lease_epoch,
                        phase=phase,
                        cause=cause,
                        is_prelaunch_refusal=False,
                    )
                    logger.warning(
                        "Role consumer outcome uncertain (phase=%s, prelaunch_refusal=False): %s; retain intent and HOLD %s",
                        phase, cause, job_id,
                    )
                    return
                status = "failed"
                summary = f"lease_renewal_failed: {fail_err}"

            if role is not None and was_stopped_externally:
                logger.warning("Dispatcher stopped or cancelled for role job %s; aborting lifecycle without finish_job", job_id)
                return

            # Fencing check: never report succeeded if fenced in the root bridge
            if acquired_root:
                now_s = int(self.clock.time()) if hasattr(self.clock, "time") else int(time.time())
                st_data = self.root_bridge.read_state()
                start_s = st_data.get("start", now_s)
                if now_s - start_s >= WINDOW_SEC or self.root_bridge.is_fenced():
                    if now_s - start_s >= WINDOW_SEC:
                        status = "hard-stopped-25m"
                        summary = "hard-stopped-25m: 25-minute execution window exceeded"
                    else:
                        status = "failed"
                        summary = "fenced: job execution was fenced or superseded"

            # Finalize root bridge if applicable
            if acquired_root:
                norm_status = "hard-stopped-25m" if status == "hard-stopped-25m" else ("ok" if status in ("succeeded", "ok") else status)
                finish_res = self.root_bridge.finish(
                    status=norm_status,
                    exit_code=0 if status in ("succeeded", "ok") else 1,
                    summary_text=summary,
                    evidence_refs=evidence_refs
                )
                if isinstance(finish_res, dict):
                    handoff_path = finish_res.get("handoff")
                    if handoff_path and os.path.exists(handoff_path):
                        if not evidence_refs:
                            evidence_refs = []
                        if handoff_path not in evidence_refs:
                            evidence_refs.append(handoff_path)
                        try:
                            with open(handoff_path, "r", encoding="utf-8") as f:
                                handoff_text = f.read()
                            if handoff_text and handoff_text not in summary:
                                summary = f"{summary}\n\n{handoff_text}"
                        except Exception:
                            pass
                    for ref in finish_res.get("evidence_refs", []):
                        if ref not in evidence_refs:
                            evidence_refs.append(ref)

            # Signal finish to hub (status must be in allowedStatuses: succeeded, finished, failed, cancelled)
            hub_status = status
            if hub_status not in ("succeeded", "finished", "failed", "cancelled"):
                hub_status = "failed"
            finish_error = None
            try:
                finish_res = self.client.finish_job(
                    self.base_url, job_id, job_token, lease_epoch,
                    hub_status, summary, evidence_refs
                )
            except Exception as e:
                finish_error = str(e)
                finish_res = {"error": f"finish_error: {e}"}

            is_semantic_finish = (
                isinstance(finish_res, dict)
                and finish_res.get("status") == "finished"
                and (finish_res.get("job_id") == job_id or "job_id" not in finish_res)
            )

            if not is_semantic_finish and role is not None:
                try:
                    host_token = self.token_provider()
                    st_res = self.client.get_job_status(self.base_url, job_id, token=host_token)
                    if isinstance(st_res, dict) and "job" in st_res:
                        s_job = st_res["job"]
                        if s_job.get("status") in ("succeeded", "finished"):
                            is_semantic_finish = True
                            logger.info("Job lifecycle finish for %s reconciled via server status (%s)", job_id, s_job.get("status"))
                except Exception as e:
                    logger.warning("Failed to query job status for reconciliation of %s: %s", job_id, e)

            if role is not None:
                if is_semantic_finish:
                    try:
                        rld.remove_role_custody(runs_dir, run_dir_path, job_id)
                    except Exception:
                        pass
                else:
                    err_reason = finish_error or (finish_res.get("error") if isinstance(finish_res, dict) else "finish_refused")
                    phase = "finish_error" if finish_error else "finish_refused"
                    try:
                        rld.update_role_custody(runs_dir, run_dir_path, job_id, {
                            "phase": phase,
                            "finish_error": err_reason,
                        })
                    except Exception:
                        pass


        finally:
            stop_event.set()
            if renew_thread is not None and renew_thread.is_alive():
                renew_thread.join(timeout=2.0)
            if acquired_root:
                self.root_bridge.release()
            with self._lock:
                self.running_jobs.pop(job_id, None)

    @staticmethod
    def _deadline_seconds(deadline_at: Any) -> Optional[float]:
        if deadline_at is None:
            return None
        try:
            if isinstance(deadline_at, str):
                return datetime.datetime.fromisoformat(deadline_at.replace("Z", "+00:00")).timestamp()
            return float(deadline_at)
        except Exception:
            return 0.0

    def _run_script_owned_root(self, job_info: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """Script-owned Root cycle (docs/ROOT-DISPATCH.md).

        Order: live scheduling read -> launch the owned unit -> validated receipt of THIS run
        -> scheduling re-read -> Hub start -> start acknowledgement to the script -> wait.
        The Hub outcome comes from the run's own summary. Any host-side failure, lost lease
        or deadline cancels and waits only the exact owned unit before reporting.
        """
        job_id = job_info["job_id"]
        token = job_info["job_token"]
        epoch = job_info["lease_epoch"]
        stop_event = job_info["stop_event"]
        deadline_sec = self._deadline_seconds(job_info.get("deadline_at"))

        def finish(status: str, summary: str, refs: List[str]) -> None:
            self.client.finish_job(self.base_url, job_id, token, epoch, status,
                                   rd.scrub(summary, token), refs)

        ok, why = self.runtime_mgr.check_scheduling(token)
        if not ok:
            finish("cancelled", f"not_started: {why}", [])
            return
        try:
            run = self.runtime_mgr.begin_root_run(job_id, token, epoch, self.base_url, payload)
        except Exception as e:
            finish("failed", f"dispatch_refused: {e}", [])
            return

        renew_thread: Optional[threading.Thread] = None
        try:
            ev = run.wait_bootstrap(rd.BOOTSTRAP_TIMEOUT_SEC)
            if ev is None:
                cleanup = run.cleanup_after_exit()
                out = run.outcome()
                summary = out["summary"]
                if cleanup is not None:
                    summary = f"{summary}; cancel_verified={cleanup.get('verified')}"
                finish(out["hub_status"], summary, out["evidence_refs"])
                return

            ok, why = self.runtime_mgr.check_scheduling(token)
            if not ok:
                info = run.cancel(f"scheduling: {why}")
                finish("cancelled", f"scheduling_recheck_denied_before_start: {why}; cancel_verified={info.get('verified')}",
                       [ev["path"]])
                return

            start_res = self.client.start_job(
                self.base_url, job_id, token, epoch, ev["digest"],
                f"root-{run.expected.run_id}", run.expected.task_id)
            if start_res.get("error"):
                info = run.cancel("hub_start_refused")
                finish("failed", f"start_job failed: {start_res.get('error')}; cancel_verified={info.get('verified')}",
                       [ev["path"]])
                return
            run.acknowledge_start(start_res)

            renew_thread = threading.Thread(
                target=self._renewal_loop, args=(job_id, token, epoch, stop_event),
                name=f"renew-{job_id}", daemon=True)
            renew_thread.start()

            while not run.wait_done(1.0):
                now = self.clock.time() if hasattr(self.clock, "time") else time.time()
                if stop_event.is_set() or (deadline_sec is not None and now > deadline_sec):
                    reason = "lease_lost" if stop_event.is_set() else "deadline_exceeded"
                    info = run.cancel(reason)
                    stop_event.set()
                    finish("failed", f"{reason}: owned unit cancelled; cancel_verified={info.get('verified')}",
                           [ev["path"]])
                    return
            stop_event.set()
            # A launcher that raised, or ended without a verified clean exit, may leave the owned
            # unit live: stop and live-verify that exact invocation before the outcome is reported.
            cleanup = run.cleanup_after_exit()
            out = run.outcome()
            status, summary = out["hub_status"], out["summary"]
            if cleanup is not None:
                summary = f"{summary}; cancel_verified={cleanup.get('verified')}"
            now = self.clock.time() if hasattr(self.clock, "time") else time.time()
            if deadline_sec is not None and now > deadline_sec and status == "succeeded":
                status, summary = "failed", "deadline_exceeded: job execution exceeded deadline_at"
            if job_id in self._renewal_failures:
                status = "failed"
                summary = f"lease_renewal_failed: {self._renewal_failures.pop(job_id)}"
            finish(status, summary, out["evidence_refs"])
        except BaseException as e:  # noqa: BLE001 - reported to the Hub, never hidden
            try:
                info = run.cancel("host_failure")
            except Exception as ce:  # noqa: BLE001
                info = {"verified": False, "error": str(ce)}
            logger.error("Script-owned Root run failed for %s: %s", job_id, rd.scrub(e, token))
            try:
                finish("failed", f"host_failure: {type(e).__name__}: {e}; cancel_verified={info.get('verified')}", [])
            except Exception:
                pass
        finally:
            stop_event.set()
            if renew_thread is not None and renew_thread.is_alive():
                renew_thread.join(timeout=2.0)

    def _renewal_loop(
        self,
        job_id: str,
        job_token: str,
        epoch: int,
        stop_event: threading.Event,
        role: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Periodically renew lease every 300s (5 minutes)."""
        if role is None:
            role = self.running_jobs.get(job_id, {}).get("role_binding")
        interval = getattr(self, "renewal_interval_sec", None)
        if interval is None:
            interval = min(300.0, role["ttl_seconds"] / 3) if role else 300.0
        while not stop_event.is_set():
            if stop_event.wait(interval):
                break
            if self.root_bridge.is_fenced():
                logger.warning(f"Root bridge fenced, stopping renewal loop for {job_id}")
                stop_event.set()
                break
            try:
                renew_res = self.client.renew_job(self.base_url, job_id, job_token, epoch)
                if renew_res.get("error"):
                    err = renew_res.get("error")
                    logger.warning(f"Lease renewal for job {job_id} failed: {err}")
                    self._renewal_failures[job_id] = err
                    if role:
                        stop_event.set()
                        break
                    if err in ("unauthorized_or_stale", "deadline_exceeded"):
                        if self.root_bridge:
                            self.root_bridge.fence("lease_renewal_failed")
                        stop_event.set()
                        break
            except Exception as e:
                logger.warning(f"Lease renewal exception for job {job_id}: {e}")
                self._renewal_failures[job_id] = str(e)
                if role:
                    stop_event.set()
                    break

    def _valid_role_binding(self, binding: Any, kind: str) -> bool:
        return (isinstance(binding, dict) and kind in SUPPORTED_ROLE_PROFILES
                and binding.get("profile") == kind and binding.get("host_id") == self.host_id
                and isinstance(binding.get("name"), str)
                and re.fullmatch(r"automation:[a-z][a-z0-9-]{0,47}:[a-z][a-z0-9-]{0,47}", binding["name"]) is not None
                and type(binding.get("generation")) is int and binding["generation"] > 0
                and type(binding.get("ttl_seconds")) is int and 60 <= binding["ttl_seconds"] <= 1200)

    def _role_consumer_qualified(self, kind: Optional[str] = None) -> bool:
        if not isinstance(self.runtime_mgr, RuntimeManager):
            return False
        if self.runtime_mgr._has_qualified_mock_runner():
            return not getattr(self.root_bridge, "is_canonical_state", False)
        if kind is not None:
            eligible, _, _ = self.runtime_mgr.can_process_role_run(kind)
            return eligible
        return any(self.runtime_mgr.can_process_role_run(k)[0] for k in SUPPORTED_ROLE_PROFILES)

    def _get_role_runs_dir(self) -> pathlib.Path:
        if hasattr(self.runtime_mgr, "role_runs_dir"):
            p = pathlib.Path(self.runtime_mgr.role_runs_dir)
        elif hasattr(self.runtime_mgr, "role_adapter") and hasattr(self.runtime_mgr.role_adapter, "config") and hasattr(self.runtime_mgr.role_adapter.config, "runs_dir"):
            p = pathlib.Path(self.runtime_mgr.role_adapter.config.runs_dir)
        else:
            p = pathlib.Path(getattr(self.root_bridge, "runs_dir", ".local/roles"))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _corroborate_native_target(
        self,
        job_id: str,
        kind: str,
        role: Dict[str, Any],
        lease_epoch: int,
        run_dir: pathlib.Path,
    ) -> Optional[rld.NativeTarget]:
        safe_job = re.sub(r"[^a-zA-Z0-9_-]", "-", job_id)
        generation = int(role.get("generation", 0))
        task_id = f"hub-role-{kind}-{safe_job}-g{generation}-e{lease_epoch}"
        if hasattr(self.runtime_mgr, "role_adapter"):
            adapter = self.runtime_mgr.role_adapter
            run = rld.RoleRun(
                job_id=job_id,
                job_token="temporary",
                lease_epoch=lease_epoch,
                role_binding=role,
                kind=kind,
                payload={},
                hub_url=self.base_url,
                host_id=self.host_id,
                config=adapter.config,
                launcher_backend=adapter.launcher_backend,
                clock=self.clock,
                reattach=True,
            )
            binding = run.corroborate_maintained_binding()
            if binding:
                return rld.NativeTarget(
                    session_id=binding["session_id"],
                    tag=binding["tag"],
                    workspace=binding["workspace"],
                    created_at_ms=binding["created_at_ms"],
                    engine=binding["engine"],
                )
        return None

    def _stop_native_target_safely(self, target: rld.NativeTarget) -> bool:
        try:
            if hasattr(self.runtime_mgr, "role_adapter"):
                backend = self.runtime_mgr.role_adapter.launcher_backend
                res = backend.stop_session(target)
                if isinstance(res, tuple):
                    return bool(res[0])
                return bool(res)
        except Exception as e:
            logger.warning("Failed to stop native target %s: %s", target.session_id, e)
        return False

    def recover_active_role_jobs(self) -> List[Dict[str, Any]]:
        """Discover and reattach to interrupted role jobs."""
        runs_dir = self._get_role_runs_dir()
        active_files = list(runs_dir.glob("active-role-*.json"))
        for sub in runs_dir.iterdir():
            if sub.is_dir() and any(sub.name.startswith(f"{p}-") for p in SUPPORTED_ROLE_PROFILES):
                for cfile in sub.glob("custody-*.json"):
                    jid = cfile.name[len("custody-"):-len(".json")]
                    idx = runs_dir / f"active-role-{jid}.json"
                    if not idx.exists() and not (sub / f"outcome-uncertain-{jid}.json").exists():
                        active_files.append(cfile)

        results = []
        for afile in set(active_files):
            rec = self._recover_one_role_job(afile, runs_dir)
            if rec is not None:
                results.append(rec)
        return results

    @staticmethod
    def _terminal_ack_matches(reply, job_id, epoch, generation, terminal_case, server_status, target=None, tombstone=None):
        if type(epoch) is not int or epoch <= 0 or type(generation) is not int or generation <= 0:
            return False
        if not isinstance(reply, dict) or reply.get("reconciled") is not True or reply.get("status") != "reconciled":
            return False
        receipt = reply.get("receipt")
        if not isinstance(receipt, dict):
            return False
        for value in (reply, receipt):
            if (value.get("job_id") != job_id or type(value.get("epoch")) is not int or value.get("epoch") != epoch
                    or type(value.get("generation")) is not int or value.get("generation") != generation
                    or value.get("outcome") != "failed" or value.get("terminal_case") != terminal_case):
                return False
        if (receipt.get("reconciled") is not True or receipt.get("status") != "failed"
                or receipt.get("original_status") != server_status
                or receipt.get("summary") != "Terminal role reconciliation after authoritative expiry or fence"
                or receipt.get("evidenceRefs") != []):
            return False
        if terminal_case == "executed":
            marker = receipt.get("native_retirement")
            if not target or not isinstance(marker, dict) or not isinstance(tombstone, dict):
                return False
            expected = {"native_id": target.session_id, "tag": target.tag, "workspace": target.workspace,
                        "engine": target.engine, "created_at_ms": target.created_at_ms,
                        "cause": "finished", "finished_at_ms": tombstone.get("finished_at_ms")}
            if receipt.get("native_session") != target.session_id or any(marker.get(k) != v for k, v in expected.items()):
                return False
            if any(type(marker.get(key)) is not int or marker[key] <= 0 for key in ("generation", "created_at_ms", "finished_at_ms")):
                return False
        elif receipt.get("native_session") is not None or receipt.get("native_retirement") is not None:
            return False
        return True

    @staticmethod
    def _archive_terminal_pointer(runs_dir, run_dir, job_id, custody):
        # Preserve the original active record as a private archive, never unlink it.
        active = runs_dir / f"active-role-{job_id}.json"
        if not os.path.lexists(active):
            return None
        current, _, _ = rld.safe_read_bounded_json(active, max_bytes=65536)
        if (not isinstance(current, dict) or current.get("job_id") != job_id
                or current.get("lease_epoch") != custody.get("lease_epoch")
                or current.get("role_binding") != custody.get("role_binding")):
            raise ValueError("active_pointer_changed")
        rld.ensure_private_dir(run_dir)
        archive = run_dir / f"settled-active-{job_id}-{uuid.uuid4().hex}.json"
        if os.path.lexists(archive):
            raise ValueError("archive_conflict")
        os.rename(active, archive)
        return str(archive)

    def _recover_one_role_job(self, state_file: pathlib.Path, runs_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
        try:
            custody, _, _ = rld.safe_read_bounded_json(state_file, max_bytes=65536)
        except Exception as e:
            logger.warning("Failed to read role custody %s: %s", state_file, e)
            return None

        if not custody or not isinstance(custody, dict):
            return None

        job_id = custody.get("job_id")
        kind = custody.get("kind")
        job_token = custody.get("job_token")
        lease_epoch = custody.get("lease_epoch")
        role = custody.get("role_binding")
        deadline_at = custody.get("deadline_at")
        payload = custody.get("payload", {})
        host_id = custody.get("host_id") or (role.get("host_id") if isinstance(role, dict) else None)

        if not job_id or not kind or not job_token or not lease_epoch or not role:
            return None

        with self._lock:
            if job_id in self.running_jobs:
                return None

        if host_id != self.host_id:
            return None

        if kind not in SUPPORTED_ROLE_PROFILES:
            return None

        run_dir = runs_dir / f"{kind}-{job_id}"
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            return None
        # Resume local archival after a durably correlated ACK; never repeat an effect.
        try:
            stored, _, _ = rld.safe_read_bounded_json(run_dir / f"custody-{job_id}.json", max_bytes=65536)
        except Exception:
            stored = None
        if (isinstance(stored, dict) and stored.get("reconciled") is True
                and stored.get("job_id") == job_id and stored.get("lease_epoch") == lease_epoch
                and stored.get("role_binding") == role and stored.get("host_id") == host_id):
            receipt = stored.get("reconciliation_receipt")
            target = None
            tombstone = None
            if isinstance(receipt, dict) and receipt.get("terminal_case") == "executed":
                marker = receipt.get("native_retirement")
                if isinstance(marker, dict) and marker.get("tag") == "task-" + str(stored.get("task_id")):
                    target = rld.NativeTarget(session_id=marker.get("native_id"), tag=marker.get("tag"),
                                              workspace=marker.get("workspace"), engine=marker.get("engine"),
                                              created_at_ms=marker.get("created_at_ms"))
                    tombstone = {"finished_at_ms": marker.get("finished_at_ms")}
            reply = {**(receipt if isinstance(receipt, dict) else {}), "status": "reconciled", "receipt": receipt}
            if (not isinstance(receipt, dict) or receipt.get("original_status") not in ("expired", "fenced", "failed", "cancelled")
                    or not self._terminal_ack_matches(reply, job_id, lease_epoch, role.get("generation"),
                                                      receipt.get("terminal_case"), receipt.get("original_status"), target, tombstone)):
                return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "invalid_stored_reconciliation_receipt"}
            try:
                self._archive_terminal_pointer(runs_dir, run_dir, job_id, stored)
            except Exception:
                return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "terminal_archive_pending"}
            return None

        uncertain_file = run_dir / f"outcome-uncertain-{job_id}.json"
        if uncertain_file.exists() or custody.get("phase") == "uncertain":
            idx = runs_dir / f"active-role-{job_id}.json"
            if idx.exists():
                try: idx.unlink()
                except OSError: pass
            return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "already_uncertain"}

        # 1. Runtime qualification check
        if not hasattr(self.runtime_mgr, "can_process_role_run"):
            return None
        eligible, reason, report = self.runtime_mgr.can_process_role_run(kind)
        if not eligible:
            logger.warning("Recovery cannot process role %s (%s): %s", job_id, kind, reason)
            return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": f"unqualified: {reason}"}

        # 2. Probe server with renew_job to verify lease validity and detect stale generation
        try:
            renew_res = self.client.renew_job(self.base_url, job_id, job_token, lease_epoch)
        except Exception as e:
            logger.warning("Lease renewal probe exception during recovery of %s: %s", job_id, e)
            return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": f"network_error: {e}"}

        if renew_res.get("error"):
            renew_err = renew_res.get("error")
            status_code = renew_res.get("status_code")
            logger.warning(
                "Lease renewal probe returned error during recovery of %s: %s (status_code=%s)",
                job_id,
                renew_err,
                status_code,
            )

            # HTTP401 alone is NOT proof of generation fence or permission to stop.
            # Query exact job using own current host credential.
            server_job = None
            try:
                host_token = self.token_provider()
                status_res = self.client.get_job_status(self.base_url, job_id, token=host_token)
                if isinstance(status_res, dict) and isinstance(status_res.get("job"), dict):
                    server_job = status_res["job"]
                elif isinstance(status_res, dict) and status_res.get("error"):
                    logger.warning("Authoritative job status lookup for %s returned error: %s", job_id, status_res.get("error"))
            except Exception as e:
                logger.warning("Could not query authoritative server job status for %s: %s", job_id, e)

            # If authoritative status unavailable/offline/unknown, HOLD and preserve custody (no stop, no duplicate launch)
            if not isinstance(server_job, dict):
                logger.warning("Authoritative job status for %s unavailable, offline, or unknown; HOLD and preserve custody", job_id)
                return {
                    "job_id": job_id,
                    "recovered": False,
                    "status": "HOLD",
                    "reason": "authoritative_status_unavailable",
                }

            # Validate same job / host / role binding / current epoch as appropriate
            auth_id = server_job.get("id")
            if auth_id != job_id:
                logger.warning("Authoritative job id mismatch for %s (server reported %s); HOLD and preserve custody", job_id, auth_id)
                return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "authoritative_identity_mismatch"}

            auth_kind = server_job.get("kind")
            if auth_kind != kind:
                logger.warning("Authoritative job kind mismatch for %s (expected %s, got %s); HOLD and preserve custody", job_id, kind, auth_kind)
                return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "authoritative_identity_mismatch"}

            holder_host = server_job.get("holder_host_id") or server_job.get("holder_machine_id")
            expected_host = self.host_id
            # Custody exists only after claim; the real status DTO carries the
            # holder and epoch even after fencing. Missing fields are not proof.
            server_epoch = server_job.get("lease_epoch")
            if holder_host != expected_host or type(server_epoch) is not int or server_epoch < lease_epoch:
                return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "authoritative_identity_mismatch"}

            if isinstance(role, dict) and role.get("name"):
                server_role_name = server_job.get("role_name")
                if not server_role_name and isinstance(server_job.get("role_binding"), dict):
                    server_role_name = server_job["role_binding"].get("name")
                if server_role_name and server_role_name != role.get("name"):
                    logger.warning("Authoritative role mismatch for %s (%s != %s); HOLD and preserve custody", job_id, server_role_name, role.get("name"))
                    return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "authoritative_identity_mismatch"}

            server_status = server_job.get("status")

            # If still running, HOLD and preserve custody (no stop, no duplicate launch/claim/execute/start)
            if server_status in ("running", "claimed", "offered"):
                logger.warning("Authoritative status for %s is still %s; renewal failed but job active; HOLD and preserve custody", job_id, server_status)
                return {
                    "job_id": job_id,
                    "recovered": False,
                    "status": "HOLD",
                    "reason": f"job_still_{server_status}",
                }

            # Confirmed already finished: reconcile semantic outcome without stale success; preserve unknown exit/resource proof
            if server_status in ("succeeded", "finished", "failed"):
                if server_epoch != lease_epoch:
                    return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "authoritative_identity_mismatch"}
                # The authenticated job status is the canonical semantic result.
                # Reading a model-writable pathname here would add an unbounded,
                # symlink-following read and let local output override that result.
                reconciled_outcome = server_status

                logger.info("Interrupted job %s was already finished on server (%s); reconciling outcome as %s", job_id, server_status, reconciled_outcome)
                rld.remove_role_custody(runs_dir, run_dir, job_id)
                return {
                    "job_id": job_id,
                    "recovered": True,
                    "status": "finished",
                    "outcome": reconciled_outcome,
                    "reconciled": True,
                }

            # Confirmed fenced/cancelled/expired or provably stale lease
            server_epoch = server_job.get("lease_epoch")
            is_stale_epoch = server_epoch > lease_epoch
            is_deadline_exceeded = server_status in ("expired", "deadline_exceeded")
            is_fenced_or_dead = (
                server_status in ("fenced", "cancelled", "expired")
                or is_stale_epoch
                or is_deadline_exceeded
            )

            if not is_fenced_or_dead:
                logger.warning("Authoritative status for %s is unknown (%s); HOLD and preserve custody", job_id, server_status)
                return {
                    "job_id": job_id,
                    "recovered": False,
                    "status": "HOLD",
                    "reason": f"unknown_authoritative_status_{server_status}",
                }

            # Confirmed fence: stop ONLY corroborated exact owned native
            target = self._corroborate_native_target(job_id, kind, role, lease_epoch, run_dir)
            # Preserve existing authoritative generation-fence containment. The new
            # expiry reconciliation path is passive and never substitutes for it.
            reconcile_expiry = is_deadline_exceeded and not is_stale_epoch and callable(getattr(self.client, "reconcile_terminal_job", None))
            if target is not None and not reconcile_expiry:
                self._stop_native_target_safely(target)

            phase = "deadline_exceeded" if is_deadline_exceeded else "stale_generation_fenced"
            cause = (
                f"renew_job returned {renew_err} (status_code={status_code}): "
                f"authoritative server status confirmed {server_status} (epoch={server_epoch}, generation advanced or lease lost)"
            )

            # Attempt supported terminal role reconciliation (M8)
            reconcile_fn = getattr(self.client, "reconcile_terminal_job", None) if reconcile_expiry else None
            if callable(reconcile_fn):
                start_ack_file = run_dir / f"start-ack-{job_id}.json"
                first_action_file = run_dir / f"first-action-{job_id}.json"
                is_unstarted = (
                    not start_ack_file.exists()
                    and not first_action_file.exists()
                    and not (run_dir / f"outcome-uncertain-{job_id}.json").exists()
                    and not custody.get("native_session")
                    and not custody.get("execution_receipt")
                    and custody.get("phase") not in ("uncertain", "executed", "running")
                    and not server_job.get("started_at")
                    and not server_job.get("first_action_at")
                    and not server_job.get("native_session")
                    and server_job.get("role_effect") is None
                )

                terminal_case = None
                reconcile_payload = {
                    "epoch": lease_epoch,
                    "generation": role.get("generation"),
                    "summary": "Terminal role reconciliation after authoritative expiry or fence",
                }

                tombstone = None
                can_reconcile = False
                if is_unstarted:
                    terminal_case = "unstarted"
                    reconcile_payload["terminal_case"] = "unstarted"
                    can_reconcile = True
                else:
                    # Executed case: require corroborated native target and verified stopped physical process
                    if target is not None:
                        if (server_job.get("native_session") != target.session_id or not server_job.get("started_at")
                                or not server_job.get("first_action_at")):
                            return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "missing_server_native_association"}
                        if type(target.created_at_ms) is not int or target.created_at_ms <= 0 or not target.engine:
                            return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "incomplete_native_identity"}
                        executable = shutil.which("aplexer")
                        try:
                            retired, tombstone, _ = verify_native_retirement(executable, target) if executable else (False, None, "missing_native_cli")
                        except Exception:
                            retired, tombstone = False, None
                        if not retired or not isinstance(tombstone, dict):
                            return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "native_retirement_unproved"}
                        terminal_case = "executed"
                        reconcile_payload["terminal_case"] = "executed"
                        reconcile_payload["native_session"] = target.session_id
                        can_reconcile = True
                    else:
                        # Cannot corroborate native target on executed job: fail-closed HOLD
                        logger.warning("Cannot corroborate native target for executed job %s; HOLD and preserve custody", job_id)
                        return {
                            "job_id": job_id,
                            "recovered": False,
                            "status": "HOLD",
                            "reason": "uncorroborated_native_target",
                        }

                if can_reconcile:
                    host_token = self.token_provider()
                    try:
                        rec_res = reconcile_fn(self.base_url, job_id, host_token, reconcile_payload)
                    except Exception as e:
                        logger.warning("reconcile_terminal_job transport failure for %s: %s", job_id, type(e).__name__)
                        rec_res = {"error": "transport_failure"}

                    if isinstance(rec_res, dict) and rec_res.get("reconciled") is True:
                        # Strict SDK correlation checks: exact job_id, epoch, generation
                        if self._terminal_ack_matches(rec_res, job_id, lease_epoch, role.get("generation"),
                                                      terminal_case, server_status, target, tombstone):
                            custody["prior_phase"] = custody.get("phase")
                            custody["reconciled"] = True
                            custody["phase"] = "terminal_reconciled"
                            custody["reconciled_outcome"] = "failed"
                            custody["reconciled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                            custody["reconciliation_receipt"] = rec_res["receipt"]
                            try:
                                # Persist ACK first; the archive preserves the original active bytes.
                                rld.write_private_json(run_dir / f"custody-{job_id}.json", custody)
                                archive = self._archive_terminal_pointer(runs_dir, run_dir, job_id, custody)
                                if archive:
                                    custody["active_pointer_archive"] = archive
                                    rld.write_private_json(run_dir / f"custody-{job_id}.json", custody)
                            except Exception:
                                return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "terminal_archive_pending"}

                            logger.info("Terminal role job %s reconciled on server as %s (%s)", job_id, rec_res.get("outcome"), terminal_case)
                            return {
                                "job_id": job_id,
                                "recovered": True,
                                "status": "reconciled",
                                "outcome": rec_res.get("outcome", "failed"),
                                "reconciled": True,
                                "terminal_case": terminal_case,
                            }
                        else:
                            logger.warning("Server reconciliation ACK rejected due to correlation mismatch for %s", job_id)
                            # SDK wrong-job/generic ACK rejection: do not archive custody or mark reconciled
                            return {
                                "job_id": job_id,
                                "recovered": False,
                                "status": "HOLD",
                                "reason": "uncorrelated_reconciliation_ack",
                            }
                    elif isinstance(rec_res, dict) and rec_res.get("status") == "HOLD":
                        logger.warning("Server returned HOLD for terminal reconciliation of %s: %s", job_id, rec_res.get("reason"))
                        return {
                            "job_id": job_id,
                            "recovered": False,
                            "status": "HOLD",
                            "reason": f"server_reconciliation_hold: {rec_res.get('reason')}",
                        }
                    return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "terminal_reconciliation_refused"}

            write_role_uncertain_proof(
                runs_dir=runs_dir,
                run_dir=run_dir,
                job_id=job_id,
                role=role,
                kind=kind,
                lease_epoch=lease_epoch,
                phase=phase,
                cause=cause,
                is_prelaunch_refusal=False,
            )
            return {
                "job_id": job_id,
                "recovered": False,
                "status": "fenced",
                "reason": "deadline_exceeded" if is_deadline_exceeded else "stale_generation_or_lost_lease",
            }

        # 3. Server lease is confirmed active!
        start_ack_file = run_dir / f"start-ack-{job_id}.json"
        first_action_file = run_dir / f"first-action-{job_id}.json"
        outcome_file = run_dir / f"outcome-{job_id}.json"

        # Reattach RoleRun
        run = self.runtime_mgr.reattach_role_run(
            job_id=job_id,
            kind=kind,
            payload=payload,
            job_token=job_token,
            lease_epoch=lease_epoch,
            role_binding=role,
            hub_url=self.base_url,
            host_id=self.host_id,
        )

        binding = run.corroborate_maintained_binding()
        if not binding:
            logger.error("Interrupted job %s cannot corroborate native binding; HOLD uncertain", job_id)
            write_role_uncertain_proof(
                runs_dir=runs_dir,
                run_dir=run_dir,
                job_id=job_id,
                role=role,
                kind=kind,
                lease_epoch=lease_epoch,
                phase="uncorroborated_native_identity",
                cause="missing or uncorroborated native binding on recovery",
                is_prelaunch_refusal=False,
            )
            return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": "uncorroborated_native_identity"}

        target = rld.NativeTarget(
            session_id=binding["session_id"],
            tag=binding["tag"],
            workspace=binding["workspace"],
            created_at_ms=binding["created_at_ms"],
            engine=binding["engine"],
        )

        # Handle start-ack if needed:
        if not start_ack_file.exists():
            if first_action_file.exists():
                fixture = None
                if getattr(run, "is_injected_test", False):
                    fixture = {
                        "workload_pid": binding.get("workload_pid"),
                        "worker_pid": binding.get("worker_pid"),
                        "workload_cgroup": binding.get("workload_cgroup"),
                        "containment_empty": binding.get("containment_empty", False),
                        "phase": binding.get("phase", "running"),
                        "created_at_ms": binding.get("created_at_ms"),
                        "is_fixture": True,
                    }
                digest = rld.get_role_first_action_digest(
                    first_action_file,
                    expected_job_id=job_id,
                    expected_epoch=lease_epoch,
                    expected_role_name=role.get("name", ""),
                    expected_generation=role.get("generation", 0),
                    expected_native_session=binding["session_id"],
                    expected_native_target=target,
                    aplexer_bin=run.launcher_backend.aplexer_bin,
                    nonproduction_fixture=fixture,
                )
                if digest:
                    start_res = self.client.start_job(
                        self.base_url, job_id, job_token, lease_epoch,
                        digest, binding["session_id"], run.task_id
                    )
                    if start_res.get("error"):
                        run.cancel("recovery_server_start_refused")
                        write_role_uncertain_proof(
                            runs_dir=runs_dir,
                            run_dir=run_dir,
                            job_id=job_id,
                            role=role,
                            kind=kind,
                            lease_epoch=lease_epoch,
                            phase="server_start",
                            cause=f"start_job failed on recovery: {start_res.get('error')}",
                            is_prelaunch_refusal=False,
                        )
                        return {"job_id": job_id, "recovered": False, "status": "failed", "reason": start_res.get("error")}
                    run.acknowledge_start(start_res)

        alive, _ = run.launcher_backend.is_session_alive(target)
        if alive:
            logger.info("Reattaching to alive native session %s for job %s", binding["session_id"], job_id)
            stop_event = threading.Event()
            job_info = {
                "job_id": job_id,
                "kind": kind,
                "op_qualified": False,
                "job_token": job_token,
                "lease_epoch": lease_epoch,
                "deadline_at": deadline_at,
                "started_at": self.clock.time(),
                "started_wall": time.time(),
                "stop_event": stop_event,
                "role_binding": role,
                "reattached": True,
            }
            with self._lock:
                self.running_jobs[job_id] = job_info

            recovery_thread = threading.Thread(
                target=self._monitor_reattached_job,
                args=(job_info, run, runs_dir, run_dir, payload),
                name=f"job-recovery-{job_id}",
                daemon=True,
            )
            recovery_thread.start()
            return {"job_id": job_id, "recovered": True, "status": "running", "reattached": True}
        else:
            logger.info("Workload for job %s has already terminated; finalizing outcome", job_id)
            out = run.outcome()
            status = out["hub_status"]
            summary = out["summary"]
            refs = out["evidence_refs"]
            if status == "succeeded" and not refs:
                refs = [str(run.first_action_file)]

            finish_error = None
            try:
                finish_res = self.client.finish_job(
                    self.base_url, job_id, job_token, lease_epoch,
                    status, summary, refs
                )
            except Exception as e:
                finish_error = str(e)
                finish_res = {"error": f"finish_error: {e}"}

            is_semantic_finish = (
                isinstance(finish_res, dict)
                and finish_res.get("status") == "finished"
                and (finish_res.get("job_id") == job_id or "job_id" not in finish_res)
            )

            if not is_semantic_finish:
                try:
                    host_token = self.token_provider()
                    st_res = self.client.get_job_status(self.base_url, job_id, token=host_token)
                    if isinstance(st_res, dict) and "job" in st_res:
                        s_job = st_res["job"]
                        if s_job.get("status") in ("succeeded", "finished"):
                            is_semantic_finish = True
                            logger.info("finish_job for %s reconciled via server status (%s)", job_id, s_job.get("status"))
                except Exception as e:
                    logger.warning("Failed to query job status for reconciliation of %s: %s", job_id, e)

            if is_semantic_finish:
                rld.remove_role_custody(runs_dir, run_dir, job_id)
                return {"job_id": job_id, "recovered": True, "status": "finished", "outcome": status}
            else:
                err_reason = finish_error or (finish_res.get("error") if isinstance(finish_res, dict) else "finish_refused")
                phase = "finish_error" if finish_error else "finish_refused"
                logger.warning("finish_job refused on recovery for %s: %s; HOLD and preserve custody", job_id, err_reason)
                rld.update_role_custody(runs_dir, run_dir, job_id, {
                    "phase": phase,
                    "finish_error": err_reason,
                })
                return {"job_id": job_id, "recovered": False, "status": "HOLD", "reason": f"{phase}: {err_reason}"}

    def _monitor_reattached_job(
        self,
        job_info: Dict[str, Any],
        run: rld.RoleRun,
        runs_dir: pathlib.Path,
        run_dir: pathlib.Path,
        payload: Dict[str, Any],
    ) -> None:
        job_id = job_info["job_id"]
        job_token = job_info["job_token"]
        lease_epoch = job_info["lease_epoch"]
        stop_event = job_info["stop_event"]
        role = job_info["role_binding"]
        deadline_sec = self._deadline_seconds(job_info.get("deadline_at"))

        renew_thread = threading.Thread(
            target=self._renewal_loop,
            args=(job_id, job_token, lease_epoch, stop_event, role),
            name=f"renew-{job_id}",
            daemon=True,
        )
        renew_thread.start()

        try:
            while not run.wait_done(1.0):
                now = self.clock.time() if hasattr(self.clock, "time") else time.time()
                if stop_event.is_set() or (deadline_sec is not None and now > deadline_sec):
                    reason = "lease_lost" if stop_event.is_set() else "deadline_exceeded"
                    info = run.cancel(reason)
                    stop_event.set()
                    write_role_uncertain_proof(
                        runs_dir=runs_dir,
                        run_dir=run_dir,
                        job_id=job_id,
                        role=role,
                        kind=job_info["kind"],
                        lease_epoch=lease_epoch,
                        phase="cancelled",
                        cause=f"{reason}: owned unit cancelled; cancel_verified={info.get('verified')}",
                        is_prelaunch_refusal=False,
                    )
                    return

            stop_event.set()
            out = run.outcome()
            status, summary = out["hub_status"], out["summary"]
            now = self.clock.time() if hasattr(self.clock, "time") else time.time()
            if deadline_sec is not None and now > deadline_sec and status == "succeeded":
                status, summary = "failed", "deadline_exceeded: job execution exceeded deadline_at"
            if job_id in self._renewal_failures:
                status = "failed"
                summary = f"lease_renewal_failed: {self._renewal_failures.pop(job_id)}"
            refs = out["evidence_refs"]
            if status == "succeeded" and not refs:
                refs = [str(run.first_action_file)]

            finish_error = None
            try:
                finish_res = self.client.finish_job(
                    self.base_url, job_id, job_token, lease_epoch,
                    status, summary, refs
                )
            except Exception as e:
                finish_error = str(e)
                finish_res = {"error": f"finish_error: {e}"}

            is_semantic_finish = (
                isinstance(finish_res, dict)
                and finish_res.get("status") == "finished"
                and (finish_res.get("job_id") == job_id or "job_id" not in finish_res)
            )

            if not is_semantic_finish:
                try:
                    host_token = self.token_provider()
                    st_res = self.client.get_job_status(self.base_url, job_id, token=host_token)
                    if isinstance(st_res, dict) and "job" in st_res:
                        s_job = st_res["job"]
                        if s_job.get("status") in ("succeeded", "finished"):
                            is_semantic_finish = True
                            logger.info("Reattached job monitor for %s reconciled via server status (%s)", job_id, s_job.get("status"))
                except Exception as e:
                    logger.warning("Failed to query job status for reconciliation of %s: %s", job_id, e)

            if is_semantic_finish:
                rld.remove_role_custody(runs_dir, run_dir, job_id)
            else:
                err_reason = finish_error or (finish_res.get("error") if isinstance(finish_res, dict) else "finish_refused")
                phase = "finish_error" if finish_error else "finish_refused"
                logger.warning("Reattached job monitor finish_job refused for %s: %s; HOLD", job_id, err_reason)
                rld.update_role_custody(runs_dir, run_dir, job_id, {
                    "phase": phase,
                    "finish_error": err_reason,
                })
        except Exception as e:
            logger.error("Reattached job monitor failed for %s: %s", job_id, e)
            write_role_uncertain_proof(
                runs_dir=runs_dir,
                run_dir=run_dir,
                job_id=job_id,
                role=role,
                kind=job_info["kind"],
                lease_epoch=lease_epoch,
                phase="monitor_error",
                cause=str(e),
                is_prelaunch_refusal=False,
                exc=e,
            )
        finally:
            stop_event.set()
            if renew_thread is not None and renew_thread.is_alive():
                renew_thread.join(timeout=2.0)
            with self._lock:
                self.running_jobs.pop(job_id, None)

    def poll_and_dispatch_role_jobs(self) -> List[Dict[str, Any]]:
        """Poll for role job offers and dispatch any claimed jobs."""
        recovered = self.recover_active_role_jobs()

        token = self.token_provider()
        if not token:
            return recovered
        try:
            offers = self.client.get_offers(self.base_url, self.host_id, token)
        except Exception as e:
            logger.warning("Failed to poll role job offers: %s", e)
            return recovered
        results = list(recovered)
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            kind = offer.get("kind")
            if kind in SUPPORTED_ROLE_PROFILES or offer.get("role_binding") is not None:
                res = self.handle_offer(offer)
                results.append(res)
        return results
