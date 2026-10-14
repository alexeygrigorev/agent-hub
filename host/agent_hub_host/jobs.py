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
from .runtime import RuntimeManager, get_first_action_digest

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

        # Gate check for root-run: qualification and read-only incumbent check
        if kind == "root-run":
            eligible, reason, report = self.runtime_mgr.can_process_root_run()
            if not eligible:
                logger.info(f"Declining root-run offer {job_id}: {reason}")
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": reason, "qualification": report}

            if getattr(self.root_bridge, "is_canonical_state", False):
                skip_reason = "operational_root_receiver_unqualified: canonical_root_state_forbidden_while_unsupported"
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": skip_reason}

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
            if not is_fixture:
                skip_reason = "operational_root_receiver_unqualified: real trusted receiver is not qualified (issue 59)"
                try:
                    self.client.decline_job(self.base_url, job_id, self.host_id, token, skip_reason)
                except Exception:
                    pass
                return {"accepted": False, "reason": skip_reason}
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
            "job_token": job_token,
            "lease_epoch": lease_epoch,
            "deadline_at": deadline_at,
            "started_at": self.clock.time(),
            "started_wall": time.time(),
            "stop_event": threading.Event()
        }
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
        native_session = f"session-{job_id}"
        launcher_task_id = f"hub-{job_id}"
        renew_thread: Optional[threading.Thread] = None

        def _finish_failed_with_bridge(fail_summary: str, initial_refs: Optional[List[str]] = None) -> None:
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
            if getattr(self.root_bridge, "is_canonical_state", False):
                self.client.finish_job(
                    self.base_url, job_id, job_token, lease_epoch,
                    "failed", "canonical_root_state_forbidden_while_unsupported", []
                )
                return

            if kind == "root-run":
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
            runs_dir = pathlib.Path(self.root_bridge.runs_dir)
            runs_dir.mkdir(parents=True, exist_ok=True)
            evidence_file = runs_dir / f"first-action-{job_id}.json"

            if evidence_file.exists():
                logger.error(f"Pre-existing evidence file found before job start for {job_id}")
                _finish_failed_with_bridge("pre-existing evidence file")
                return

            # Execute worker in thread to ensure actual worker execution starts
            exec_result: Dict[str, Any] = {}
            exec_exc: List[Exception] = []

            def _worker_exec():
                try:
                    res = self.runtime_mgr.execute_job(
                        job_id, kind, payload, job_token, lease_epoch, self.base_url
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

            # Poll for worker-produced evidence_file while worker is running
            poll_deadline = time.time() + 10.0
            while time.time() < poll_deadline and exec_thread.is_alive():
                if evidence_file.exists():
                    break
                if stop_event.is_set():
                    break
                exec_thread.join(timeout=0.005)

            if acquired_root and getattr(self.root_bridge, "_released", False):
                return

            if exec_exc:
                _finish_failed_with_bridge(f"execution_error: {exec_exc[0]}")
                return

            res_obj = exec_result.get("res") or {}
            res_refs = res_obj.get("evidence_refs", []) if isinstance(res_obj, dict) else []

            # If worker finished with error or failed without producing evidence
            if not exec_thread.is_alive() and res_obj.get("status") in ("failed", "error"):
                summary = res_obj.get("summary") or "worker execution failed"
                _finish_failed_with_bridge(summary, res_refs)
                return

            # Never self-fabricate first-action evidence or spawn a dummy sleep child in the dispatcher.
            if not evidence_file.exists():
                logger.error(f"No genuine first-action evidence produced for job {job_id}")
                _finish_failed_with_bridge("no first action evidence", res_refs)
                return

            # Validate custody of worker-produced evidence file
            try:
                if evidence_file.stat().st_mtime < job_start_wall - 0.05:
                    raise ValueError("stale evidence file created before job start")
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
                _finish_failed_with_bridge(f"first-action custody failed: {e}", res_refs)
                return

            evidence_digest = get_first_action_digest(
                evidence_file,
                expected_job_id=job_id,
                expected_run_id=acq.get("run_id") if acquired_root else None,
                expected_epoch=lease_epoch
            )
            if not evidence_digest:
                logger.error(f"Failed to compute first-action digest for {job_id}")
                _finish_failed_with_bridge("no first action digest", res_refs)
                return

            # Start job on server
            start_res = self.client.start_job(
                self.base_url, job_id, job_token, lease_epoch,
                evidence_digest, native_session, launcher_task_id
            )
            if start_res.get("error"):
                logger.error(f"Failed to signal job start for {job_id}: {start_res.get('error')}")
                _finish_failed_with_bridge(f"start_job failed: {start_res.get('error')}", res_refs)
                return

            # Start periodic renewal loop in background
            renew_thread = threading.Thread(
                target=self._renewal_loop,
                args=(job_id, job_token, lease_epoch, stop_event),
                name=f"renew-{job_id}",
                daemon=True
            )
            renew_thread.start()

            # Wait for execution to finish
            exec_thread.join()
            if exec_exc:
                status = "failed"
                summary = f"execution_error: {exec_exc[0]}"
                evidence_refs = []
            else:
                exec_res = exec_result.get("res", {})
                status = exec_res.get("status", "succeeded")
                summary = exec_res.get("summary", "completed")
                evidence_refs = exec_res.get("evidence_refs", [])

            # Stop renewal loop
            stop_event.set()

            # Check deadline_at
            clk_now = self.clock.time() if hasattr(self.clock, "time") else (self.clock() if callable(self.clock) else time.time())
            if deadline_sec is not None and clk_now > deadline_sec:
                status = "failed"
                summary = "deadline_exceeded: job execution exceeded deadline_at"

            # Check renewal failures (for both root and non-root jobs)
            if job_id in self._renewal_failures:
                status = "failed"
                summary = f"lease_renewal_failed: {self._renewal_failures.pop(job_id)}"

            # Fencing check: never report succeeded if fenced in state.json or root bridge
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
            self.client.finish_job(
                self.base_url, job_id, job_token, lease_epoch,
                hub_status, summary, evidence_refs
            )

        finally:
            stop_event.set()
            if renew_thread is not None and renew_thread.is_alive():
                renew_thread.join(timeout=2.0)
            if acquired_root:
                self.root_bridge.release()
            with self._lock:
                self.running_jobs.pop(job_id, None)

    def _renewal_loop(
        self,
        job_id: str,
        job_token: str,
        epoch: int,
        stop_event: threading.Event
    ) -> None:
        """Periodically renew lease every 300s (5 minutes)."""
        interval = 300.0
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
                    if err in ("unauthorized_or_stale", "deadline_exceeded"):
                        if self.root_bridge:
                            self.root_bridge.fence("lease_renewal_failed")
                        stop_event.set()
                        break
            except Exception as e:
                logger.warning(f"Lease renewal exception for job {job_id}: {e}")
