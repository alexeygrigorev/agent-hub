"""Small native CLI. Config secrets are read from a private file or environment."""
import argparse
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path

from .agent import Bridge, Journal
from .client import ApiError, Client, save_config
from .commands import add_commands, execute

DEFAULT_SERVER = "https://hub.dtcdev.click"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hub2")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/agent-hub/v2.json")
    parser.add_argument("--server")
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--code-env", default="HUB2_PAIRING_CODE")
    enroll.add_argument("--root", action="append", required=True)
    enroll.add_argument("--engine", action="append", default=[])
    serve = commands.add_parser("serve")
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--aplexer", default="a")
    serve.add_argument("--label", action="append", default=[])
    serve.add_argument("--log-file", type=Path, default=None,
                       help="Append daemon logs here (default on Windows: %%LOCALAPPDATA%%\\agent-hub2\\serve.log)")
    commands.add_parser("version", help="Print this host's release identity and credential mode")
    mode = commands.add_parser("credential-mode", help="Show or set host-owned (local) or Hub-delivered (central) provider logins")
    mode.add_argument("mode", nargs="?", choices=["local", "central"])
    heartbeat = commands.add_parser("heartbeat", help="Report machine health without reading offers or changing sessions")
    heartbeat.add_argument("--aplexer", default="a")
    heartbeat.add_argument("--label", action="append", default=[])
    configure_host = commands.add_parser("configure-host", help="Replace resource tags and workspace aliases in an existing host enrollment")
    configure_host.add_argument("--capability", action="append", default=[])
    configure_host.add_argument("--workspace", action="append", default=[], metavar="LABEL=PATH")
    request = commands.add_parser("request")
    request.add_argument("method", choices=["GET", "POST", "PATCH", "DELETE"])
    request.add_argument("path")
    request.add_argument("--body-file", type=Path)
    request.add_argument("--key")
    add_commands(commands)
    updater = commands.add_parser("self-update", help="Install the verified published host release")
    updater.add_argument("--base-url")
    updater.add_argument("--root", type=Path, default=Path.home() / ".local/share/agent-hub2")
    updater.add_argument("--no-restart", action="store_true")
    updater.add_argument("--force", action="store_true", help="Windows: reinstall even when already current")
    quota_dispatch_cmd = commands.add_parser("quota-dispatch", help="Staged opt-in same-process quota dispatch (default OFF)")
    quota_dispatch_cmd.add_argument("--enable", action="store_true", default=False,
                                    help="Opt in to staged same-process quota dispatch (default OFF)")
    args = parser.parse_args(argv)
    if args.command == "self-update" and os.name == "nt":
        from .windows_host import windows_self_update
        try:
            config = json.loads(args.config.read_text()) if args.config.exists() else {}
            return windows_self_update(args.base_url or config.get("server") or DEFAULT_SERVER, force=args.force)
        except (OSError, ValueError) as error:
            print(f"Hub2 self-update failed: {type(error).__name__}: {str(error)[:200]}", file=sys.stderr)
            return 1
    if args.command == "self-update":
        import subprocess
        command = [sys.executable, str(args.root / "updater.py"), "--root", str(args.root)]
        if args.base_url:
            command.extend(["--base-url", args.base_url])
        if args.no_restart:
            command.append("--no-restart")
        return subprocess.run(command).returncode
    try:
        if args.command == "enroll":
            roots = [str(Path(root).resolve(strict=True)) for root in args.root]
            if not all(Path(root).is_dir() for root in roots):
                raise ValueError("workspace roots must be directories")
        config = json.loads(args.config.read_text()) if args.config.exists() else {}
        if args.command == "version":
            from .windows_host import credential_mode, host_release
            version, protocol = host_release()
            print(json.dumps({"host_version": version, "protocol_version": protocol,
                              "machine_id": config.get("machine_id"), "credential_mode": credential_mode(config)}))
            return 0
        if args.command == "credential-mode":
            from .windows_host import credential_mode, set_credential_mode
            if args.mode is not None:
                config = set_credential_mode(config, args.mode)
                save_config(args.config, config)
            print(json.dumps({"credential_mode": credential_mode(config),
                              "restart_required": args.mode is not None}))
            return 0
        if args.command == "configure-host":
            from .advertisement import configure
            updated = configure(config, args.capability, args.workspace)
            save_config(args.config, updated)
            print(json.dumps({"configured": True, "restart_required": True}))
            return 0
        if args.command == "quota-dispatch":
            import subprocess
            from . import alert_authority as ram
            from .quota_dispatch import configured_same_process, run_same_process_quota
            from .quota_dispatch_manager import (
                FanotifyReader, SourceGate, close_held_source, hold_source_fd, prepare_selected_quota,
            )
            spec = configured_same_process(config.get("quota_same_process"))
            if not args.enable or spec is None:
                print(json.dumps(run_same_process_quota(enabled=False)))
                return 0
            src_dir = spec["source_directory"]
            quota_path = str(Path(src_dir) / "hub-quota-publish.py")
            client_path = str(Path(src_dir) / "hub_alert_client.py")
            prelude_path = str(Path(src_dir) / "hub-alert-quota-dispatch.py")
            primary_args = ["--check"]
            held = []
            gate = None
            group = None
            verified_quota_raw = None
            try:
                quota_fd = hold_source_fd(src_dir, "hub-quota-publish.py")
                held.append(quota_fd)
                verified_quota_raw = quota_fd.raw
                client_fd = hold_source_fd(src_dir, "hub_alert_client.py")
                held.append(client_fd)
                prelude_fd = hold_source_fd(src_dir, "hub-alert-quota-dispatch.py")
                held.append(prelude_fd)
                gate = SourceGate(spec["gate_directory"])
                group = FanotifyReader.open_group((quota_fd,))
                deadline = time.monotonic() + 40.0
                exec_argv = lambda dl_ns: [
                    "/usr/bin/python3", "-I", "-S", prelude_path,
                    str(dl_ns), quota_fd.pin.sha256, client_fd.pin.sha256,
                    spec["gate_directory"], quota_path, client_path, "--", *primary_args,
                ]
                plan = {"units": {"healthy": spec["unit"]}, "cgroups": {"healthy": spec["cgroup"]}}
                prepared = prepare_selected_quota(
                    source=quota_fd, companions=(client_fd, prelude_fd), gate=gate, group=group,
                    plan=plan, exec_argv=exec_argv, service_deadline=deadline,
                )
            except (OSError, ram.Unknown) as error:
                if gate is not None:
                    gate.cleanup()
                for item in held:
                    close_held_source(item)
                if group is not None:
                    os.close(group)
                reason = error.args[0] if isinstance(error, ram.Unknown) and error.args else type(error).__name__
                if verified_quota_raw is None:
                    # Source pin/open itself rejected: never execute unverified/rejected quota_path.
                    print(json.dumps({"enabled": True, "dispatched": False, "source_rejected": True,
                                      "error": str(reason), "complete": False}))
                    return 1
                # Source was verified; execute retained verified original bytes once unqualified.
                import types
                selected = types.ModuleType("selected_quota_monitor")
                selected.__file__ = quota_path
                exec(compile(verified_quota_raw, quota_path, "exec"), selected.__dict__)
                launch_rc = selected.main(primary_args, _managed_observer=None)
                print(json.dumps({"enabled": True, "dispatched": False, "fail_open_business": True,
                                  "launch_rc": launch_rc, "error": str(reason), "complete": False}))
                return launch_rc

            def _launch_unit(ticket):
                dl_ns = int(ticket.deadline * 1e9) if ticket is not None else 0
                argv = ["/usr/bin/systemd-run", "--user", "--unit=" + spec["unit"], "--no-block", "--"] + exec_argv(dl_ns)
                return subprocess.run(argv, capture_output=True, timeout=5, check=False).returncode

            result = run_same_process_quota(prepared, _launch_unit, enabled=True, broker_directory=spec["broker_directory"])
            print(json.dumps({"enabled": result["enabled"], "dispatched": result["dispatched"],
                              "reserved": result["reserved"], "phase": result["phase"], "complete": False}))
            return 0
        server = args.server or config.get("server") or DEFAULT_SERVER
        client = Client(server, os.environ.get("HUB2_TOKEN") or config.get("token", ""))
        if args.command not in ('auth', 'heartbeat') and not os.environ.get('HUB2_TOKEN') and config.get('refresh_token') and config.get('expires_at',float('inf'))<=time.time()+30:
            from .auth import refresh
            client=refresh(config,args.config,server)
        if args.command == "enroll":
            code = os.environ.get(args.code_env) or getpass.getpass("Pairing code: ")
            enrolled = client.request("POST", "/api/enrollments/complete", {"code": code})
            config = {"server": server, "token": enrolled["token"], "machine_id": enrolled["machine_id"],
                      "roots": roots,
                      "engines": args.engine}
            save_config(args.config, config)
            print(json.dumps({"enrolled": config["machine_id"]}))
        elif args.command == "request":
            body = json.loads(args.body_file.read_text()) if args.body_file else None
            print(json.dumps(client.request(args.method, args.path, body, args.key)))
        elif args.command == "heartbeat":
            bridge = Bridge(client, None, config["roots"], config["engines"], args.aplexer,
                            launcher_config=config.get("launcher"), machine_id=config["machine_id"],
                            capabilities=config.get("capabilities"), workspaces=config.get("workspaces"))
            bridge.heartbeat(args.label, include_native_sessions=False)
            print(json.dumps({"ok": True}))
        elif args.command == "serve":
            from .windows_host import configure_logging, credential_mode, default_log_file, host_release
            log_file = configure_logging(args.log_file or default_log_file())
            version, protocol = host_release()
            logging.getLogger("agent_hub_host").info(
                "Hub2 host starting machine=%s host_version=%s protocol=%s credential_mode=%s engines=%s log=%s",
                config.get("machine_id"), version or "unreleased", protocol, credential_mode(config),
                ",".join(config.get("engines") or []), log_file or "stderr")
            observer = None
            if 'alert_observer' in config:
                try:
                    from .alert_broker import configured
                    observer = configured(config['alert_observer'])
                except Exception:
                    pass  # Optional evidence never prevents ordinary serve.
            bridge = Bridge(client, Journal(args.config.parent / "v2-journal.sqlite"),
                            config["roots"], config["engines"], args.aplexer,
                            launcher_config=config.get("launcher"),machine_id=config['machine_id'],
                            capabilities=config.get("capabilities"), workspaces=config.get("workspaces"),
                            alert_observer=observer)
            if args.once:
                bridge.heartbeat(args.label)
                bridge.drain()
            else:
                bridge.serve(config["machine_id"], args.label)
        else:
            print(json.dumps(execute(client, args, config)))
        return 0
    except (ApiError, OSError, ValueError, KeyError, RuntimeError) as error:
        # Do not echo exceptions that can contain a token, prompt, local path or
        # response; name the failure class and Hub's typed code so it is diagnosable.
        reason = (f"{error.code} (HTTP {error.status})" if isinstance(error, ApiError) and error.code
                  else type(error).__name__)
        if args.command == "serve":
            logging.getLogger("agent_hub_host").error("Hub2 serve stopped: %s", reason)
        print(f"Hub operation failed ({args.command}: {reason}); no successful result recorded.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
