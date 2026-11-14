"""Small native CLI. Config secrets are read from a private file or environment."""
import argparse
import getpass
import json
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
    args = parser.parse_args(argv)
    if args.command == "self-update":
        import subprocess
        command = [sys.executable, str(args.root / "updater.py"), "--root", str(args.root)]
        if args.base_url:
            command.extend(["--base-url", args.base_url])
        if args.no_restart:
            command.append("--no-restart")
        return subprocess.run(command).returncode
    try:
        config = json.loads(args.config.read_text()) if args.config.exists() else {}
        if args.command == "configure-host":
            from .advertisement import configure
            updated = configure(config, args.capability, args.workspace)
            save_config(args.config, updated)
            print(json.dumps({"configured": True, "restart_required": True}))
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
                      "roots": [str(Path(root).resolve(strict=True)) for root in args.root],
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
    except (ApiError, OSError, ValueError, KeyError, RuntimeError):
        # Do not echo exceptions that can contain a token, prompt, local path or response.
        print("Hub operation failed; no successful result recorded.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
