import argparse
import sys
import json
from . import auth, api, system, config

def cmd_login(args):
    try:
        auth.device_login(args.base_url, "host", "agent-hub-host", system.get_hostname(), ["host_agent"])
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_machines_available(args):
    try:
        cfg = config.load_config()
        if not cfg.get("base_url"):
            print("Not logged in.", file=sys.stderr)
            sys.exit(1)
        machines = api.get_available_machines(cfg["base_url"])
        
        # machines.py compatible output:
        # {"reachable": [ {machine} ], "count": N}
        if isinstance(machines, list):
            output = {"reachable": machines, "count": len(machines)}
        elif isinstance(machines, dict) and "reachable" in machines:
            output = machines
        else:
            output = {"reachable": machines, "count": len(machines)} if isinstance(machines, list) else machines

        if args.json:
            print(json.dumps(output, indent=2, sort_keys=True))
        else:
            reachable = output.get("reachable", [])
            if not reachable:
                print("reachable: none (all probes failed or unknown)", file=sys.stderr)
            for m in reachable:
                print(m.get("id", m.get("hostname", "unknown")))
    except Exception as e:
        print(f"Failed to get available machines: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_status(args):
    try:
        cfg = config.load_config()
        if not cfg.get("access_token"):
            print("Status: Not logged in.")
            sys.exit(1)
        print("Status: Logged in.")
        print(f"Base URL: {cfg.get('base_url')}")
    except Exception as e:
        print(f"Error checking status: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_run(args):
    from . import api
    try:
        cfg = config.load_config()
        if not cfg.get("access_token"):
            base_url = cfg.get("base_url")
            if base_url:
                from . import system, auth
                auth.device_login(base_url, "host", "agent-hub-host", system.get_hostname(), ["host_agent"])
            else:
                print("Not logged in.", file=sys.stderr)
                sys.exit(1)
        agent = api.HostAgent()
        agent.run()
    except Exception as e:
        print(f"Agent run failed: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Agent Hub CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    p_login = sub.add_parser("login", help="Login to Agent Hub")
    p_login.add_argument("base_url", help="Base URL of the Agent Hub")
    
    p_machines = sub.add_parser("machines", help="Machines operations")
    m_sub = p_machines.add_subparsers(dest="m_cmd", required=True)
    p_available = m_sub.add_parser("available", help="List available machines")
    p_available.add_argument("--json", action="store_true", help="Output JSON")
    
    p_status = sub.add_parser("status", help="Check status")
    
    p_run = sub.add_parser("run", help="Run the host agent in the foreground")

    p_send = sub.add_parser("send", help="Send a message")
    p_send.add_argument("--from", dest="from_actor", required=True)
    p_send.add_argument("--to", dest="to_actor", required=True)
    p_send.add_argument("--kind", required=True)
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--idempotency-key", default=None)
    p_send.add_argument("--thread-id", help="Thread ID")
    p_send.add_argument("--reply-to", help="Reply to message ID")
    p_send.add_argument("--lease-epoch", type=int, help="Root lease epoch", default=None)

    p_inbox = sub.add_parser("inbox", help="List inbox messages")
    p_inbox.add_argument("--actor", required=True)
    p_inbox.add_argument("--limit", type=int, default=50)
    p_inbox.add_argument("--cursor", help="Pagination cursor")
    
    p_show = sub.add_parser("show", help="Show message")
    p_show.add_argument("id", help="Message ID")
    
    p_ack = sub.add_parser("ack", help="Ack message")
    p_ack.add_argument("id", help="Message ID")
    p_ack.add_argument("--lease-epoch", type=int, help="Root lease epoch", default=None)
    
    p_accept = sub.add_parser("accept", help="Accept message")
    p_accept.add_argument("id", help="Message ID")
    p_accept.add_argument("--lease-epoch", type=int, help="Root lease epoch", default=None)
    
    p_complete = sub.add_parser("complete", help="Complete message")
    p_complete.add_argument("id", help="Message ID")
    p_complete.add_argument("--lease-epoch", type=int, help="Root lease epoch", default=None)
    
    p_reply = sub.add_parser("reply", help="Reply to message")
    p_reply.add_argument("--reply-to", required=True)
    p_reply.add_argument("--from", dest="from_actor", required=True)
    p_reply.add_argument("--to", dest="to_actor", required=True)
    p_reply.add_argument("--kind", required=True)
    p_reply.add_argument("--body", required=True)
    p_reply.add_argument("--idempotency-key", default=None)
    p_reply.add_argument("--thread-id", help="Thread ID")
    p_reply.add_argument("--lease-epoch", type=int, help="Root lease epoch", default=None)
    
    p_claim = sub.add_parser("claim", help="Create a claim")
    p_claim.add_argument("--project", required=True)
    p_claim.add_argument("--scope", required=True)
    p_claim.add_argument("--holder", required=True)
    p_claim.add_argument("--ttl-ms", type=int, default=3600000)
    p_claim.add_argument("--lease-epoch", type=int, help="Root lease epoch", default=None)
    
    p_release = sub.add_parser("release", help="Release a claim")
    p_release.add_argument("id", help="Claim ID or project:scope")
    p_release.add_argument("--lease-epoch", type=int, help="Root lease epoch", default=None)

    p_claims = sub.add_parser("claims", help="List claims")
    p_claims.add_argument("--project", help="Filter by project")
    p_claims.add_argument("--holder", help="Filter by holder")
    p_claims.add_argument("--all", action="store_true", help="Include released/expired claims")

    args = parser.parse_args()
    
    if args.cmd == "login":
        cmd_login(args)
    elif args.cmd == "machines":
        if args.m_cmd == "available":
            cmd_machines_available(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "send":
        from . import bus
        if args.body == "-":
            raw = sys.stdin.read(16385)
            if len(raw.encode("utf-8")) > 16384:
                print("Error: stdin body exceeds 16 KiB limit", file=sys.stderr)
                sys.exit(1)
            args.body = raw
        res = bus.send(
            args.from_actor,
            args.to_actor,
            args.kind,
            args.body,
            idempotency_key=getattr(args, 'idempotency_key', None),
            thread_id=getattr(args, 'thread_id', None),
            reply_to=getattr(args, 'reply_to', None),
            lease_epoch=getattr(args, 'lease_epoch', None)
        )
        print(json.dumps(res))
    elif args.cmd == "inbox":
        from . import bus
        print(json.dumps(bus.inbox(args.actor, limit=getattr(args, 'limit', 50), cursor=getattr(args, 'cursor', None))))
    elif args.cmd == "show":
        from . import bus
        print(json.dumps(bus.show(args.id)))
    elif args.cmd == "ack":
        from . import bus
        print(json.dumps(bus.ack(args.id, lease_epoch=getattr(args, 'lease_epoch', None))))
    elif args.cmd == "accept":
        from . import bus
        print(json.dumps(bus.accept(args.id, lease_epoch=getattr(args, 'lease_epoch', None))))
    elif args.cmd == "complete":
        from . import bus
        print(json.dumps(bus.complete(args.id, lease_epoch=getattr(args, 'lease_epoch', None))))
    elif args.cmd == "reply":
        from . import bus
        if args.body == "-":
            raw = sys.stdin.read(16385)
            if len(raw.encode("utf-8")) > 16384:
                print("Error: stdin body exceeds 16 KiB limit", file=sys.stderr)
                sys.exit(1)
            args.body = raw
        res = bus.reply(
            args.reply_to,
            args.from_actor,
            args.to_actor,
            args.kind,
            args.body,
            idempotency_key=getattr(args, 'idempotency_key', None),
            thread_id=getattr(args, 'thread_id', None),
            lease_epoch=getattr(args, 'lease_epoch', None)
        )
        print(json.dumps(res))
    elif args.cmd == "claim":
        from . import bus
        print(json.dumps(bus.claim(args.project, args.scope, args.holder, args.ttl_ms, lease_epoch=getattr(args, 'lease_epoch', None))))
    elif args.cmd == "release":
        from . import bus
        print(json.dumps(bus.release(args.id, lease_epoch=getattr(args, 'lease_epoch', None))))
    elif args.cmd == "claims":
        from . import bus
        print(json.dumps(bus.list_claims(project=getattr(args, 'project', None), holder=getattr(args, 'holder', None), active=not getattr(args, 'all', False))))

if __name__ == "__main__":
    main()
