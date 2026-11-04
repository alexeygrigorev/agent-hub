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
    request = commands.add_parser("request")
    request.add_argument("method", choices=["GET", "POST", "PATCH", "DELETE"])
    request.add_argument("path")
    request.add_argument("--body-file", type=Path)
    request.add_argument("--key")
    add_commands(commands)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text()) if args.config.exists() else {}
        server = args.server or config.get("server")
        if not server:
            parser.error("--server is required for enrollment")
        client = Client(server, os.environ.get("HUB2_TOKEN") or config.get("token", ""))
        if args.command!='auth' and not os.environ.get('HUB2_TOKEN') and config.get('refresh_token') and config.get('expires_at',float('inf'))<=time.time()+30:
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
        elif args.command == "serve":
            bridge = Bridge(client, Journal(args.config.parent / "v2-journal.sqlite"),
                            config["roots"], config["engines"], args.aplexer,
                            launcher_config=config.get("launcher"),machine_id=config['machine_id'])
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
