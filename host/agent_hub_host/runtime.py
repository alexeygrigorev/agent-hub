"""Runtime manager and launcher capability qualification.

Enforces strict launcher interface qualification for root runs and job execution:
- Inspects maintained launcher interfaces (pinned commit, task profiles, the explicit parameters of
  ``execute_transient_task_unit``, adapter routes) and the pinned CAG Root script.
- Reports actual supported/unsupported/unknown status and required minimal dependency packet (issue59).
- Hard fail-closed gate: if root-run is unsupported or Codex quota window is unavailable/<=15%,
  refuses before claim or processing. Never allows direct codex/API launch bypass.
- Manages first-action evidence verification, native session binding, and execution tracking.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import root_dispatch as rd
from .root_dispatch import DispatchError
from . import role_dispatch as rld

CODEX_MIN_PERCENT: float = 15.0


def get_first_action_digest(
    artifact_path_or_content: str | bytes | os.PathLike,
    expected_job_id: Optional[str] = None,
    expected_run_id: Optional[str] = None,
    expected_epoch: Optional[int] = None,
) -> Optional[str]:
    """Compute standard SHA-256 digest of first-action evidence.
    
    Refuses non-JSON files, raw strings, daemon PID, and requires genuine valid JSON evidence.
    Returns None for invalid files, non-JSON strings, or JSON evidence failing custody.
    """
    if isinstance(artifact_path_or_content, os.PathLike):
        p = pathlib.Path(artifact_path_or_content)
        if p.exists() and p.is_file():
            try:
                content = p.read_bytes()
            except Exception:
                return None
        else:
            return None
    elif isinstance(artifact_path_or_content, str):
        p = pathlib.Path(artifact_path_or_content)
        if p.exists() and p.is_file():
            try:
                content = p.read_bytes()
            except Exception:
                return None
        else:
            try:
                # Must parse as JSON string
                json.loads(artifact_path_or_content)
                content = artifact_path_or_content.encode("utf-8")
            except Exception:
                return None
    elif isinstance(artifact_path_or_content, bytes):
        content = artifact_path_or_content
    else:
        return None

    # Content must parse as valid JSON dictionary
    try:
        data = json.loads(content.decode("utf-8"))
        if not isinstance(data, dict):
            return None
    except Exception:
        return None

    # Validate PID: must be int > 1 and NOT init/reaper (1), host daemon PID, parent PID, or 65534
    pid_val = data.get("pid")
    if (
        type(pid_val) is not int
        or pid_val <= 1
        or pid_val == os.getpid()
        or pid_val == os.getppid()
        or pid_val == 65534
    ):
        return None

    # Required core fields
    job_id = data.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        return None

    epoch_val = data.get("epoch")
    if type(epoch_val) is not int or epoch_val <= 0:
        return None

    if "stage" in data and data.get("stage") != "first_action":
        return None

    if "utc" in data:
        utc_val = data.get("utc")
        if not isinstance(utc_val, str) or not utc_val.strip():
            return None
        if not (utc_val.endswith("Z") or "+" in utc_val or ("-" in utc_val[10:] and "T" in utc_val)):
            return None
        try:
            import datetime
            utc_dt = datetime.datetime.fromisoformat(utc_val.replace("Z", "+00:00"))
            if utc_dt.tzinfo is None:
                return None
        except Exception:
            return None

    ns_val = data.get("private_pid_namespace")
    if not isinstance(ns_val, str) or not re.match(r"^pid:\[[0-9]+\]$", ns_val) or ns_val == "pid:[unknown]":
        return None

    procs = data.get("numeric_proc_entries")
    if not isinstance(procs, list) or not all(type(x) is int and x > 0 for x in procs) or pid_val not in procs:
        return None

    # Task / session custody validation (fail closed when expected values provided)
    if expected_job_id is not None:
        if data.get("job_id") != expected_job_id:
            return None
    if expected_run_id is not None:
        if data.get("run_id") != expected_run_id:
            return None
    if expected_epoch is not None:
        if data.get("epoch") != expected_epoch:
            return None

    return hashlib.sha256(content).hexdigest()


# Adapter seams only tests inject: production RuntimeManager() passes none of them, so any one of
# them present can never be operational (scheduling_check/quota_check are only defaulted by us).
FIXTURE_SEAMS = ("launcher_executor", "proc_probe", "invocation_probe", "host_namespace", "unit_controller",
                 "scheduling_check", "quota_check", "quse_provider", "clock", "launcher_repo")


@contextlib.contextmanager
def _inspected_launcher(path: pathlib.Path):
    """Import ``launcher.*`` from exactly ``path``: cached modules of another checkout are
    set aside so they cannot answer for it, and restored afterwards."""
    with rd.LAUNCHER_IMPORT_LOCK:
        saved = {k: v for k, v in sys.modules.items() if k == "launcher" or k.startswith("launcher.")}
        for k in saved:
            del sys.modules[k]
        sys.path.insert(0, str(path))
        try:
            yield
        finally:
            try:
                sys.path.remove(str(path))
            except ValueError:
                pass
            for k in [k for k in sys.modules if k == "launcher" or k.startswith("launcher.")]:
                del sys.modules[k]
            sys.modules.update(saved)


class LauncherQualification:
    """Inspects and qualifies the installed agent-quota-launcher and CAG Root script."""

    def __init__(
        self,
        launcher_repo_path: Optional[str] = None,
        config: Optional[rd.TrustedRootConfig] = None,
        cag_pins: Optional[frozenset] = None,
        launcher_pins: Optional[frozenset] = None,
    ):
        self.launcher_path = pathlib.Path(
            launcher_repo_path or os.path.expanduser("~/git/agent-quota-launcher")
        ).resolve()
        self.config = config
        self._cag_pins = cag_pins
        self._launcher_pins = launcher_pins

    @property
    def cag_pins(self) -> frozenset:
        return self._cag_pins if self._cag_pins is not None else rd.ACCEPTED_CAG_SCRIPT_SHA256

    @property
    def launcher_pins(self) -> frozenset:
        return self._launcher_pins if self._launcher_pins is not None else rd.ACCEPTED_LAUNCHER_COMMITS

    def get_installed_sha(self) -> str:
        """Get git commit SHA of launcher repository if available."""
        if not (self.launcher_path / ".git").exists():
            return "unknown"
        try:
            res = subprocess.run(
                ["git", "-C", str(self.launcher_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=True
            )
            sha = str(res.stdout).strip()
            return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else "unknown"
        except Exception:
            return "unknown"

    def inspect_interfaces(self) -> Dict[str, Any]:
        """Inspect launcher code interfaces for the root profile, per-job env and pins."""
        report: Dict[str, Any] = {
            "launcher_repo": str(self.launcher_path),
            "installed_sha": self.get_installed_sha(),
            "root_profile_supported": False,
            "per_job_env_supported": False,
            "codex_adapter_supported": False,
            "launcher_pin_accepted": False,
            "cag_script_pin_accepted": False,
            "status": "UNKNOWN",
            "reasons": [],
            "dependency_packet": None
        }

        try:
            with _inspected_launcher(self.launcher_path):
                try:
                    from launcher import task_profiles  # type: ignore
                    available_profiles = sorted(list(task_profiles.TASK_PROFILES.keys()))
                    report["available_profiles"] = available_profiles
                    if rd.ROOT_PROFILE in task_profiles.TASK_PROFILES:
                        report["root_profile_supported"] = True
                    else:
                        report["reasons"].append(
                            f"root-run profile absent in launcher.task_profiles.TASK_PROFILES (available: {available_profiles})"
                        )
                except Exception as e:
                    report["reasons"].append(f"failed to import launcher.task_profiles: {e}")

                try:
                    from launcher import admission  # type: ignore
                    routes = getattr(admission, "ADAPTER_ROUTES", ())
                    report["adapter_routes"] = list(routes)
                    if "codex" in routes:
                        report["codex_adapter_supported"] = True
                    else:
                        report["reasons"].append(
                            f"codex route absent from launcher.admission.ADAPTER_ROUTES (active: {list(routes)})"
                        )
                except Exception as e:
                    report["reasons"].append(f"failed to import launcher.admission: {e}")

            # The route the adapter really calls: explicit parameters of
            # execute_transient_task_unit (AST of the source; docstrings/comments cannot count).
            boundary = rd.inspect_launcher_exec_boundary(self.launcher_path)
            report["exec_boundary"] = boundary
            if boundary["present"] and not boundary["missing"]:
                report["per_job_env_supported"] = True
            else:
                report["reasons"].append(
                    "per-job environment/Root boundary absent in launcher.task_units.execute_transient_task_unit "
                    f"(missing explicit parameters: {boundary['missing']})"
                )
        except Exception as e:
            report["status"] = "UNKNOWN"
            report["reasons"].append(f"exception during interface inspection: {e}")
            return report

        # Pins: committed constants accepted by the head (empty until accepted).
        if report["installed_sha"] != "unknown" and report["installed_sha"] in self.launcher_pins:
            report["launcher_pin_accepted"] = True
        else:
            report["reasons"].append(
                f"launcher_commit_not_pinned: {report['installed_sha']} (no accepted Quota Launcher Root commit pin)"
            )

        # Trusted configuration: the same absolute resolved paths the adapter uses.
        entrypoint_status = "verified"
        try:
            cfg = self.config or rd.TrustedRootConfig.build()
            entry = cfg.validated_entrypoint()
            report["entrypoint"] = str(entry)
            sha = rd.file_sha256(entry)
            report["entrypoint_sha256"] = sha
            if sha in self.cag_pins:
                report["cag_script_pin_accepted"] = True
            else:
                report["reasons"].append(
                    f"cag_script_not_pinned: sha256 {sha[:16]} (no accepted CAG Root script hook pin)"
                )
        except DispatchError as e:
            entrypoint_status = f"unverified: {e}"
            report["reasons"].append(f"root_entrypoint_unverified: {e}")
        report["entrypoint_status"] = entrypoint_status
        report["namespace_policy"] = (
            "UNKNOWN until pinned: the trusted host PID namespace is additionally verified per run "
            "from the receipt, state.json and live /proc"
        )

        root_route_supported = report.get("codex_adapter_supported", False) or (
            "root-run" in report.get("adapter_routes", [])
        )
        if not root_route_supported:
            report["reasons"].append(
                "no root execution adapter route in launcher.admission.ADAPTER_ROUTES "
                f"(active: {report.get('adapter_routes', [])}; needs codex route or root-run route)"
            )

        fully_supported = (
            report.get("installed_sha") != "unknown"
            and report.get("root_profile_supported") is True
            and report.get("per_job_env_supported") is True
            and root_route_supported
            and entrypoint_status == "verified"
            and report["launcher_pin_accepted"] is True
            and report["cag_script_pin_accepted"] is True
        )

        report["dependency_packet"] = None
        if fully_supported:
            report["status"] = "SUPPORTED"
        else:
            report["status"] = "UNSUPPORTED"
            report["dependency_packet"] = {
                "target_repo": "alexeygrigorev/agent-quota-launcher",
                "issue_ref": "issue59",
                "required_changes": [
                    {
                        "component": "launcher.task_profiles",
                        "change": "Define 'root-run' profile: timeout=1500.0, memory_mb=1500, description='Headless root run bounded cycle'"
                    },
                    {
                        "component": "launcher.task_units.execute_transient_task_unit",
                        "change": "Name explicit parameters profile and job_env (or job_env_file): per-job credentials via "
                                  "a 0600 EnvironmentFile (never systemd-run -E or argv); trusted host PID namespace and "
                                  "ReadWritePaths for the Root state dir under the root-run profile; receipt with "
                                  "unit_name, invocation_id, exit_code, cleanup_verified"
                    },
                    {
                        "component": "launcher.admission",
                        "change": "Enable codex execution adapter route or a root-run route with protected quota gate"
                    },
                    {
                        "component": "cloudflare-agent-git scripts/root/headless-root-run.sh",
                        "change": "Honour HUB_* env: HUB_EXPECTED_RUN_ID as run id, bound state.json fields, first-action "
                                  "receipt at HUB_FIRST_ACTION_FILE, wait for HUB_START_ACK_FILE, distinct skipped summary"
                    },
                    {
                        "component": "agent-hub host pins",
                        "change": "Add accepted launcher commit and CAG script sha256 to root_dispatch.ACCEPTED_*"
                    },
                ]
            }

        return report


def operational_root_profile_proof(
    launcher_repo_path: Optional[str] = None,
    qualification: Optional[LauncherQualification] = None,
) -> Tuple[bool, str]:
    """Genuine operational Root profile proof for gate.py: derived from the installed
    launcher, the pinned CAG script and trusted paths. There is no caller-supplied input."""
    try:
        report = (qualification or LauncherQualification(launcher_repo_path)).inspect_interfaces()
    except Exception as e:  # missing SDK / unreadable launcher => deny
        return False, f"root_profile_unproven: {type(e).__name__}"
    if report.get("status") == "SUPPORTED":
        return True, "root_profile_proven"
    return False, "root_profile_unproven: " + "; ".join(report.get("reasons", []))[:500]


class RuntimeManager:
    """Manages task execution gates, launcher qualification, and execution tracking."""

    def __init__(
        self,
        launcher_repo_path: Optional[str] = None,
        quota_gate_script: Optional[str] = None,
        allow_mock_runner: bool = False,
        mock_runner: Optional[Any] = None,
        config: Optional[rd.TrustedRootConfig] = None,
        adapter_kwargs: Optional[Dict[str, Any]] = None,
        role_config: Optional[rld.TrustedRoleConfig] = None,
    ):
        self._config_error: Optional[str] = None
        if config is None:
            try:
                config = rd.TrustedRootConfig.build()
            except DispatchError as e:
                self._config_error = str(e)
        self.config = config
        self.qualification = LauncherQualification(launcher_repo_path, config=config)
        self.quota_gate_script = pathlib.Path(
            quota_gate_script or os.path.expanduser("~/git/cloudflare-agent-git/scripts/quota-gate.py")
        ).resolve()
        self.allow_mock_runner = allow_mock_runner if type(allow_mock_runner) is bool else False
        self._mock_runner: Optional[Any] = mock_runner
        self._adapter_kwargs: Dict[str, Any] = dict(adapter_kwargs or {})
        self._scheduling: Optional[rd.HubSchedulingReader] = None
        self._bound_clock_fixture = False
        self.host_id: str = ""

        self._role_config: rld.TrustedRoleConfig = role_config if role_config is not None else rld.TrustedRoleConfig.disabled()
        self.role_adapter = rld.RoleDispatchAdapter(
            config=self._role_config,
            quota_checker=self.check_codex_window,
            launcher_backend=self._adapter_kwargs.get("role_launcher_backend"),
            launcher_executor=self._adapter_kwargs.get("role_launcher_executor"),
        )

    @property
    def role_config(self) -> rld.TrustedRoleConfig:
        return self._role_config

    @role_config.setter
    def role_config(self, new_cfg: rld.TrustedRoleConfig) -> None:
        self._role_config = new_cfg if new_cfg is not None else rld.TrustedRoleConfig.disabled()
        self.role_adapter = rld.RoleDispatchAdapter(
            config=self._role_config,
            quota_checker=self.check_codex_window,
            launcher_backend=self._adapter_kwargs.get("role_launcher_backend"),
            launcher_executor=self._adapter_kwargs.get("role_launcher_executor"),
        )

    def set_mock_runner(self, runner: Any) -> None:
        """Configure mock runner for test isolation."""
        self._mock_runner = runner

    def bind_hub(self, base_url: str, token_provider: Any, host_id: str, clock: Any = None) -> None:
        """Bind the authenticated Hub scheduling read (same origin as the paired hub)."""
        self.host_id = host_id
        # A dispatcher may retain this clock after a later trusted binding. Once injected,
        # this manager remains fixture-only; rebinding cannot restore operational authority.
        self._bound_clock_fixture |= clock is not None and clock is not time
        if self.config is None:
            return
        self._scheduling = rd.HubSchedulingReader(
            base_url, token_provider, host_id=host_id, clock=clock,
            cache=rd.SchedulingCache(self.config.host_state_dir),
            canonical_state_dir=self.config.canonical_state_dir,
        )

    def check_scheduling(self, job_token: Optional[str] = None) -> Tuple[bool, str]:
        """Live verified scheduling read. Unbound, missing, unreachable, stale or corrupt => deny."""
        if self._scheduling is None:
            return False, "scheduling_reader_unbound"
        return self._scheduling.check(job_token)

    def _quota_gate_present(self) -> bool:
        """Check the CAG quota gate script exists and is readable."""
        try:
            return self.quota_gate_script.is_file() and os.access(self.quota_gate_script, os.R_OK)
        except Exception:
            return False

    def _has_qualified_mock_runner(self) -> bool:
        """Return True only when allow_mock_runner is strictly True and _mock_runner is a real callable (not MagicMock/Mock)."""
        if type(self.allow_mock_runner) is not bool or self.allow_mock_runner is not True:
            return False
        runner = self._mock_runner
        if runner is None or not callable(runner):
            return False
        if hasattr(runner, "_mock_return_value") or hasattr(runner, "assert_called") or hasattr(runner, "_mock_name"):
            return False
        return True

    def check_codex_window(self) -> Tuple[bool, str]:
        """Evaluate fresh quota gate for Codex window remaining."""
        if not self.quota_gate_script.exists():
            return False, f"quota gate script missing: {self.quota_gate_script}"

        try:
            res = subprocess.run(
                [sys.executable, str(self.quota_gate_script)],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False
            )
            if res.returncode != 0 and res.returncode != 75:
                return False, f"quota gate returned rc={res.returncode}: {res.stderr.strip()}"

            try:
                data = json.loads(res.stdout)
            except Exception:
                return False, "quota gate output is not valid JSON"

            allowed = bool(data.get("launch_allowed", False))
            reason = str(data.get("reason", "unknown"))

            if not allowed:
                return False, f"quota gate refused: {reason}"

            # Check percentage in windows if present
            windows = data.get("windows", {})
            for win_name, win in windows.items():
                if isinstance(win, dict) and win.get("present") is not False:
                    rem = win.get("percent_remaining")
                    if rem is not None and isinstance(rem, (int, float)):
                        if rem <= CODEX_MIN_PERCENT:
                            return False, f"codex window {win_name} below safety threshold"

            return True, "quota ok"
        except subprocess.TimeoutExpired:
            return False, "quota gate check timed out"
        except Exception as e:
            return False, f"quota gate check exception: {e}"

    def can_process_root_run(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Validate all prerequisites before claiming or processing a root-run job.

        Returns:
            Tuple of (eligible: bool, reason: str, qualification_report: dict)
        """
        # 1. Interface qualification
        report = self.qualification.inspect_interfaces()

        if self._has_qualified_mock_runner():
            # Under explicit mock runner in private test state, qualification passes for testing
            mock_report = {
                "launcher_repo": report.get("launcher_repo"),
                "installed_sha": report.get("installed_sha"),
                "status": "MOCK_FIXTURE",
                "is_fixture_only": True,
                "operational_qualified": False,
            }
            return True, "mock_runner_active", mock_report

        if self._config_error:
            report["operational_qualified"] = False
            report["is_fixture_only"] = False
            return False, f"operational_root_receiver_unqualified: {self._config_error}", report

        if report["status"] != "SUPPORTED":
            reason = f"launcher_unsupported: {'; '.join(report['reasons'])}"
            return False, reason, report

        if not self._quota_gate_present():
            report["operational_qualified"] = False
            report["is_fixture_only"] = False
            reason = f"operational_root_receiver_unqualified: quota gate script missing or unreadable: {self.quota_gate_script}"
            return False, reason, report

        # Operational Root qualification: every concrete installed fact must
        # verify before the trusted receiver is qualified. Any failure keeps
        # Root forbidden before claim/start/effect (fail closed). An injected
        # fixture executor can never be operational.
        report["operational_qualified"] = False
        injected = sorted(k for k in FIXTURE_SEAMS if self._adapter_kwargs.get(k) is not None)
        if self._bound_clock_fixture:
            injected.append("bound_clock")
        report["is_fixture_only"] = bool(injected)
        if injected:
            return False, f"operational_root_receiver_unqualified: fixture seams injected: {', '.join(injected)}", report
        allowed, sched_reason = self.check_scheduling()
        report["scheduling_status"] = sched_reason
        if not allowed:
            reason = f"operational_root_receiver_unqualified: {sched_reason}"
            return False, reason, report
        report["operational_qualified"] = True
        return True, "operationally_qualified", report

    @property
    def role_runs_dir(self) -> pathlib.Path:
        return self.role_config.runs_dir

    def can_process_role_run(self, kind: str) -> Tuple[bool, str, Dict[str, Any]]:
        """Validate prerequisites before claiming or processing a role job."""
        if self._has_qualified_mock_runner():
            mock_report = {
                "kind": kind,
                "status": "MOCK_FIXTURE",
                "is_fixture_only": True,
                "operational_qualified": False,
            }
            return True, "mock_runner_active", mock_report

        if kind not in rld.SUPPORTED_ROLE_PROFILES:
            return False, f"unsupported_role_profile: {kind}", {"kind": kind, "supported": False}

        return self.role_adapter.can_process(kind)

    def _make_adapter(self) -> rd.RootDispatchAdapter:
        kwargs = dict(self._adapter_kwargs)
        kwargs.setdefault("scheduling_check", self.check_scheduling)
        kwargs.setdefault("quota_check", self.check_codex_window)
        # Execution must use the checkout whose interfaces and pins were qualified.
        kwargs["launcher_repo"] = str(self.qualification.launcher_path)
        return rd.RootDispatchAdapter(config=self.config, **kwargs)

    def begin_root_run(
        self,
        job_id: str,
        job_token: str,
        lease_epoch: int,
        hub_url: str,
        payload: Dict[str, Any],
    ) -> rd.RootRun:
        """Start one Root cycle for a claimed job (script-owned lock; see ROOT-DISPATCH.md)."""
        eligible, reason, _ = self.can_process_root_run()
        if not eligible:
            raise RuntimeError(f"Refusing to execute root-run: {reason}. Direct codex/API bypass forbidden.")
        return self._make_adapter().begin(job_id, job_token, lease_epoch, hub_url, self.host_id or "host")

    def execute_job(
        self,
        job_id: str,
        kind: str,
        payload: Dict[str, Any],
        job_token: str,
        lease_epoch: int,
        hub_url: str,
        start_callback: Optional[Any] = None,
        role_binding: Optional[Dict[str, Any]] = None,
        stop_event: Optional[Any] = None,
        deadline_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute a claimed job using maintained launcher or test mock."""
        if self._has_qualified_mock_runner():
            return self._mock_runner(job_id, kind, payload, job_token, lease_epoch, hub_url)

        if kind == "root-run":
            eligible, reason, report = self.can_process_root_run()
            if not eligible:
                # Strictly refuse: never bypass launcher
                raise RuntimeError(
                    f"Refusing to execute root-run: {reason}. Direct codex/API bypass forbidden."
                )
            adapter = self._make_adapter()
            return adapter.dispatch(
                job_id=job_id,
                job_token=job_token,
                lease_epoch=lease_epoch,
                hub_url=hub_url,
                payload=payload,
                workspace=None,
                host_id=self.host_id or "host",
                start_callback=start_callback,
            )

        if kind in rld.SUPPORTED_ROLE_PROFILES:
            eligible, reason, report = self.can_process_role_run(kind)
            if not eligible:
                raise RuntimeError(
                    f"Refusing to execute {kind}: {reason}. Direct model/API bypass forbidden."
                )
            return self.role_adapter.dispatch(
                job_id=job_id,
                kind=kind,
                payload=payload,
                job_token=job_token,
                lease_epoch=lease_epoch,
                role_binding=role_binding or {},
                hub_url=hub_url,
                host_id=self.host_id or "host",
                start_callback=start_callback,
                stop_event=stop_event,
                deadline_sec=deadline_sec,
            )

        raise NotImplementedError("Real execution requires accepted launcher issue59 dependency packet.")
