"""Runtime manager and launcher capability qualification.

Enforces strict launcher interface qualification for root runs and job execution:
- Inspects maintained launcher interfaces (e537 commit, task profiles, per-job env, adapter routes).
- Reports actual supported/unsupported/unknown status and required minimal dependency packet (issue59).
- Hard fail-closed gate: if root-run is unsupported or Codex quota window is unavailable/<=15%,
  refuses before claim or processing. Never allows direct codex/API launch bypass.
- Manages first-action evidence verification, native session binding, and execution tracking.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

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


class LauncherQualification:
    """Inspects and qualifies the installed agent-quota-launcher interface."""

    def __init__(self, launcher_repo_path: Optional[str] = None):
        self.launcher_path = pathlib.Path(
            launcher_repo_path or os.path.expanduser("~/git/agent-quota-launcher")
        ).resolve()

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
            return res.stdout.strip()
        except Exception:
            return "unknown"

    def inspect_interfaces(self) -> Dict[str, Any]:
        """Inspect launcher code interfaces for root profile and per-job env."""
        report: Dict[str, Any] = {
            "launcher_repo": str(self.launcher_path),
            "installed_sha": self.get_installed_sha(),
            "root_profile_supported": False,
            "per_job_env_supported": False,
            "codex_adapter_supported": False,
            "status": "UNKNOWN",
            "reasons": [],
            "dependency_packet": None
        }

        # Check task profiles
        try:
            sys_path_inserted = False
            if str(self.launcher_path) not in sys.path:
                sys.path.insert(0, str(self.launcher_path))
                sys_path_inserted = True

            try:
                from launcher import task_profiles  # type: ignore
                available_profiles = sorted(list(task_profiles.TASK_PROFILES.keys()))
                report["available_profiles"] = available_profiles
                if "root-run" in task_profiles.TASK_PROFILES:
                    report["root_profile_supported"] = True
                else:
                    report["reasons"].append(
                        f"root-run profile absent in launcher.task_profiles.TASK_PROFILES (available: {available_profiles})"
                    )
            except Exception as e:
                report["reasons"].append(f"failed to import launcher.task_profiles: {e}")

            # Check admission adapter routes
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

            # Check per-job env injection in cli / task_units
            cli_py = self.launcher_path / "launcher" / "cli.py"
            if cli_py.exists():
                cli_content = cli_py.read_text(encoding="utf-8")
                # Look for dynamic job env forwarding in spawn_ql_controller, excluding comments and docstrings
                env_found = False
                try:
                    import ast
                    tree = ast.parse(cli_content)
                    spawn_fn = None
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "spawn_ql_controller":
                            spawn_fn = node
                            break
                    if spawn_fn is not None:
                        # 1. Parameter signature: require explicit job_env or env_file parameter (never generic 'env')
                        for arg in spawn_fn.args.args + spawn_fn.args.kwonlyargs:
                            if arg.arg in ("job_env", "env_file", "job_env_file"):
                                env_found = True
                                break
                        # 2. Body inspection: require all three HUB_* keys in non-raise/non-docstring AST nodes
                        if not env_found:
                            found_hub_keys = set()
                            for node in ast.walk(spawn_fn):
                                if isinstance(node, ast.Raise):
                                    continue
                                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                                    continue
                                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                                    targets = getattr(node, "targets", [getattr(node, "target", None)])
                                    for target in targets:
                                        if isinstance(target, ast.Subscript):
                                            if isinstance(target.slice, ast.Constant) and target.slice.value in ("HUB_JOB_ID", "HUB_URL", "HUB_LEASE_EPOCH"):
                                                found_hub_keys.add(target.slice.value)
                                if isinstance(node, ast.Dict):
                                    for k in node.keys:
                                        if isinstance(k, ast.Constant) and k.value in ("HUB_JOB_ID", "HUB_URL", "HUB_LEASE_EPOCH"):
                                            found_hub_keys.add(k.value)
                            if {"HUB_JOB_ID", "HUB_URL", "HUB_LEASE_EPOCH"}.issubset(found_hub_keys):
                                env_found = True
                except Exception:
                    env_found = False

                if env_found:
                    report["per_job_env_supported"] = True
                else:
                    report["reasons"].append(
                        "per-job environment propagation (HUB_URL, HUB_JOB_ID, HUB_LEASE_EPOCH) absent in spawn_ql_controller"
                    )
            else:
                report["reasons"].append(f"launcher/cli.py not found at {cli_py}")

            if sys_path_inserted:
                try:
                    sys.path.remove(str(self.launcher_path))
                except ValueError:
                    pass

        except Exception as e:
            report["status"] = "UNKNOWN"
            report["reasons"].append(f"exception during interface inspection: {e}")
            return report

        report["status"] = "UNSUPPORTED"
        report["reasons"].append("Runtime Root execution remains UNSUPPORTED until launcher issue #59 qualification is complete.")
        report["dependency_packet"] = {
            "target_repo": "alexeygrigorev/agent-quota-launcher",
            "issue_ref": "issue59",
            "required_changes": [
                {
                    "component": "launcher.task_profiles",
                    "change": "Define 'root-run' profile: timeout=1500.0, memory_mb=1500, description='Headless root run bounded cycle'"
                },
                {
                    "component": "launcher.cli.spawn_ql_controller",
                    "change": "Propagate per-job credentials and environment via secure mode 0600 job env file (never systemd-run -E or argv)"
                },
                {
                    "component": "launcher.admission",
                    "change": "Enable codex execution adapter route or support wrapped root execution with protected quota gate"
                }
            ]
        }

        return report


class RuntimeManager:
    """Manages task execution gates, launcher qualification, and execution tracking."""

    def __init__(
        self,
        launcher_repo_path: Optional[str] = None,
        quota_gate_script: Optional[str] = None,
        allow_mock_runner: bool = False,
        mock_runner: Optional[Any] = None,
    ):
        self.qualification = LauncherQualification(launcher_repo_path)
        self.quota_gate_script = pathlib.Path(
            quota_gate_script or os.path.expanduser("~/git/cloudflare-agent-git/scripts/quota-gate.py")
        ).resolve()
        self.allow_mock_runner = allow_mock_runner if type(allow_mock_runner) is bool else False
        self._mock_runner: Optional[Any] = mock_runner

    def set_mock_runner(self, runner: Any) -> None:
        """Configure mock runner for test isolation."""
        self._mock_runner = runner

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

        if report["status"] != "SUPPORTED":
            reason = f"launcher_unsupported: {'; '.join(report['reasons'])}"
            return False, reason, report

        # Operational Root qualification:
        # Real trusted receiver is NOT qualified until issue 59 is complete.
        # Native Root must remain UNSUPPORTED before claim/start/effect.
        report["operational_qualified"] = False
        report["is_fixture_only"] = False
        reason = "operational_root_receiver_unqualified: real trusted receiver is not qualified (issue 59)"
        return False, reason, report

    def execute_job(
        self,
        job_id: str,
        kind: str,
        payload: Dict[str, Any],
        job_token: str,
        lease_epoch: int,
        hub_url: str
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

        raise NotImplementedError("Real execution requires accepted launcher issue59 dependency packet.")
