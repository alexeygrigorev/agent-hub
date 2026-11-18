"""Dedicated entry and CLI runner for the staged same-process quota dispatcher."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types

from . import alert_authority as ram
from .quota_dispatch import configured_same_process, run_same_process_quota
from .quota_dispatch_manager import (
    FanotifyReader,
    SourceGate,
    StableRuntime,
    close_held_source,
    consume_handoff_fd,
    hold_source_fd,
    prepare_selected_quota,
)


def _run_verified_original_once(verified_quota_raw, quota_path, primary_args):
    """Execute already-verified retained original bytes once unqualified."""
    selected = types.ModuleType("selected_quota_monitor")
    selected.__file__ = quota_path
    exec(compile(verified_quota_raw, quota_path, "exec"), selected.__dict__)
    return selected.main(primary_args, _managed_observer=None)


def run_quota_dispatch_cli(args, config):
    """Run staged default-OFF same-process quota dispatch CLI command."""
    spec = configured_same_process(config.get("quota_same_process"))
    if not getattr(args, "enable", False) or spec is None:
        print(json.dumps(run_same_process_quota(enabled=False)))
        return 0

    src_dir = spec["source_directory"]
    gate_dir = spec["gate_directory"]
    unit = spec["unit"]
    cgroup = spec["cgroup"]
    primary_args = list(spec.get("primary_args") or ["--check"])
    quota_path = str(Path(src_dir) / "hub-quota-publish.py")
    client_path = str(Path(src_dir) / "hub_alert_client.py")
    prelude_path = str(Path(src_dir) / "hub-alert-quota-dispatch.py")

    held = []
    gate = None
    group = None
    interp_fd = None
    entry_fd = None
    verified_quota_raw = None
    try:
        if getattr(args, "handoff_fd", None) is not None:
            adopted = consume_handoff_fd(args.handoff_fd, gate_dir)
            quota_fd, client_fd, prelude_fd = adopted["source"], adopted["client"], adopted["prelude"]
            held.extend((quota_fd, client_fd, prelude_fd))
            verified_quota_raw = quota_fd.raw
            gate = adopted["gate"]
            group = adopted["fan_group"]
            interp_fd = adopted["interp_fd"]
            entry_fd = adopted["entry_fd"]
            interp_pin = adopted["interpreter_pin"]
            py_bin = interp_pin["path"]
            sealed_plan = adopted["plan"]
            ram.require(
                src_dir == sealed_plan["source_directory"]
                and gate_dir == sealed_plan["gate_directory"]
                and unit == sealed_plan["unit"]
                and cgroup == sealed_plan["cgroup"]
                and primary_args == sealed_plan["primary_args"]
            )
        else:
            quota_fd = hold_source_fd(src_dir, "hub-quota-publish.py")
            held.append(quota_fd)
            verified_quota_raw = quota_fd.raw
            client_fd = hold_source_fd(src_dir, "hub_alert_client.py")
            held.append(client_fd)
            prelude_fd = hold_source_fd(src_dir, "hub-alert-quota-dispatch.py")
            held.append(prelude_fd)
            gate = SourceGate(gate_dir)
            group = FanotifyReader.open_group((quota_fd,))
            interp_pin = None
            py_bin = "/usr/bin/python3"

        deadline = time.monotonic() + 40.0
        exec_argv = lambda dl_ns: [
            py_bin, "-I", "-S", prelude_path,
            str(dl_ns), quota_fd.pin.sha256, client_fd.pin.sha256,
            gate_dir, quota_path, client_path, "--", *primary_args,
        ]
        plan = {"units": {"healthy": unit}, "cgroups": {"healthy": cgroup}}

        def _close_handoff_extras():
            for extra in (interp_fd, entry_fd):
                if extra is not None:
                    try:
                        os.close(extra)
                    except OSError:
                        pass

        prepared = prepare_selected_quota(
            source=quota_fd, companions=(client_fd, prelude_fd), gate=gate, group=group,
            plan=plan, exec_argv=exec_argv, service_deadline=deadline,
            interpreter_pin=interp_pin, retained_source_fd=quota_fd.fd,
            extra_cleanup=_close_handoff_extras if (interp_fd is not None or entry_fd is not None) else None,
        )
    except (OSError, ValueError, ram.Unknown) as error:
        if gate is not None:
            gate.cleanup()
        for item in held:
            close_held_source(item)
        if group is not None:
            os.close(group)
        for extra in (interp_fd, entry_fd):
            if extra is not None:
                try:
                    os.close(extra)
                except OSError:
                    pass
        reason = error.args[0] if isinstance(error, ram.Unknown) and error.args else type(error).__name__
        if verified_quota_raw is None:
            print(json.dumps({"enabled": True, "dispatched": False, "source_rejected": True,
                              "error": str(reason), "complete": False}))
            return 1
        launch_rc = _run_verified_original_once(verified_quota_raw, quota_path, primary_args)
        print(json.dumps({"enabled": True, "dispatched": False, "fail_open_business": True,
                          "launch_rc": launch_rc, "error": str(reason), "complete": False}))
        return launch_rc

    def _launch_unit(ticket):
        if ticket is None:
            return _run_verified_original_once(verified_quota_raw, quota_path, primary_args)
        dl_ns = int(ticket.deadline * 1e9)
        retained_dup = os.dup(quota_fd.fd)
        try:
            os.set_inheritable(retained_dup, True)
            argv = ["/usr/bin/systemd-run", "--user", "--unit=" + unit, "--no-block", "--"] + exec_argv(dl_ns)
            child_env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_ALERT_")}
            child_env.update(StableRuntime.user_manager_env(os.getuid()))
            child_env["HUB_ALERT_RETAINED_FD"] = str(retained_dup)
            return subprocess.run(argv, capture_output=True, timeout=5, check=False,
                                  env=child_env, pass_fds=(retained_dup,)).returncode
        finally:
            os.close(retained_dup)

    result = run_same_process_quota(prepared, _launch_unit, enabled=True, broker_directory=spec["broker_directory"])
    print(json.dumps({"enabled": result["enabled"], "dispatched": result["dispatched"],
                      "reserved": result["reserved"], "phase": result["phase"], "complete": False}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-hub-quota-dispatcher")
    parser.add_argument("--config", default=str(Path.home() / ".config/agent-hub/v2.json"), type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    qd = sub.add_parser("quota-dispatch")
    qd.add_argument("--enable", action="store_true")
    qd.add_argument("--handoff-fd", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Quota dispatcher config error: {type(error).__name__}", file=sys.stderr)
        return 1
    return run_quota_dispatch_cli(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
