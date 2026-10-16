import argparse
import sys
import json
from . import auth, api, system, config

def cmd_login(args):
    try:
        kind = getattr(args, 'kind', 'host')
        scopes = getattr(args, 'scope', None) or (auth.AGENT_SCOPES if kind == 'agent' else auth.HOST_SCOPES)
        auth.device_login(args.base_url, kind, getattr(args, 'name', None) or 'agent-hub-' + kind, system.get_hostname(), scopes)
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
                auth.device_login(base_url, "host", "agent-hub-host", system.get_hostname(), auth.HOST_SCOPES)
            else:
                print("Not logged in.", file=sys.stderr)
                sys.exit(1)
        agent = api.HostAgent()
        agent.run()
    except Exception as e:
        print(f"Agent run failed: {e}", file=sys.stderr)
        sys.exit(1)

MAX_BRIDGE_CYCLES = 1000
MAX_BRIDGE_PAGES = 100


def cmd_bridge(args):
    """Bounded foreground mailbox bridge (SPEC §7.5) for tags the hub registers for this credential.

    Runs one cycle by default, at most --cycles cycles; it installs no service, timer or autostart.
    Exits 1 when any cycle reports an error (inbox, native send, hub ack/reply, exhausted inbox
    window), so automation never reads a failed bridge run as success.
    """
    import time
    from . import bus
    from .bridge import MailboxBridge, poll_once, registered_bridge_targets
    from .native_bridge import BridgeCapabilityGap

    if not 1 <= args.cycles <= MAX_BRIDGE_CYCLES:
        print(f"Error: --cycles must be between 1 and {MAX_BRIDGE_CYCLES}", file=sys.stderr)
        sys.exit(2)
    if args.interval < 0 or not 1 <= args.limit <= 100 or not 1 <= args.max_pages <= MAX_BRIDGE_PAGES:
        print(f"Error: --interval must be >= 0, --limit between 1 and 100, --max-pages between 1 and {MAX_BRIDGE_PAGES}", file=sys.stderr)
        sys.exit(2)
    try:
        targets = registered_bridge_targets(bus._do_request("GET", "/api/bus/actors"), args.tag or None)
        bridge = MailboxBridge(
            args.workspace,
            aplexer_bin=args.aplexer_bin,
            state_dir=args.state_dir,
            runtime_dir=args.runtime_dir,
            service_tag=args.service_tag,
            service_session_id=args.service_session_id,
        )
    except (BridgeCapabilityGap, ValueError) as e:
        print(f"Bridge refused: {e}", file=sys.stderr)
        sys.exit(2)

    if not targets:
        print("Bridge refused: no agent tags are registered on the hub for this credential", file=sys.stderr)
        sys.exit(2)

    summaries = []
    for cycle in range(args.cycles):
        if cycle:
            time.sleep(args.interval)
        summaries.append(poll_once(bridge, targets, limit=args.limit, max_pages=args.max_pages))
    print(json.dumps({"targets": [{"address": a, "tag": t} for a, t in targets], "cycles": summaries}))
    if any(s["errors"] for s in summaries):
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Agent Hub CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    from . import routing
    routing.add_commands(sub)

    for command in ('start-agent', 'start-session', 'plan-agent'):
        launch = sub.add_parser(command, help='Queue a headless agent or a real interactive session')
        launch.add_argument('--machine', required=command == 'start-session')
        launch.add_argument('--engine')
        launch.add_argument('--workspace', required=True, help='Configured workspace label on the selected host')
        launch.add_argument('--prompt-file', help='UTF-8 goal file for headless work')
        launch.add_argument('--summary', default='Agent task', help='Non-secret task summary shown on dashboard')
        launch.add_argument('--parent', help='Parent hub launch id, omitted for a root request')
        launch.add_argument('--key', help='Stable idempotency key for retries')
    sub.add_parser('fleet', help='Authenticated machines, agents, sessions and account dashboard data')
    heartbeat = sub.add_parser('heartbeat', help='Report that this agent is alive (agent credential with health:report)')
    heartbeat.add_argument('--status', default='working', help='Short state word, for example working, idle or waiting')
    heartbeat.add_argument('--summary', help='Non-secret one-line summary shown on the dashboard (max 240 chars)')
    subscriptions = sub.add_parser('subscriptions', help='Inspect safe machine subscription metadata')
    subscriptions.add_argument('--config')
    subscriptions.add_argument('--report', action='store_true', help='Post metadata to the hub using this host credential')
    dispatch = sub.add_parser('fleet-host', help='Opt-in bounded host polling; no service is installed')
    dispatch.add_argument('--config')
    dispatch.add_argument('--cycles', type=int, default=1)
    dispatch.add_argument('--interval', type=int, default=60)
    
    p_login = sub.add_parser("login", help="Login to Agent Hub")
    p_login.add_argument("base_url", help="Base URL of the Agent Hub")
    p_login.add_argument('--kind', choices=['host', 'agent'], default='host')
    p_login.add_argument('--name', help='Human-visible name of this paired host or agent')
    p_login.add_argument('--scope', action='append', help='Requested scope; administrator approval is required')
    
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

    p_bridge = sub.add_parser("bridge", help="Bridge hub messages to native aplexer mailboxes (bounded, foreground)")
    p_bridge.add_argument("--workspace", required=True, help="aplexer workspace whose mailboxes are bridged")
    p_bridge.add_argument("--tag", action="append", help="Registered tag to bridge (repeatable; default: all registered)")
    p_bridge_runs = p_bridge.add_mutually_exclusive_group()
    p_bridge_runs.add_argument("--once", dest="cycles", action="store_const", const=1, help="Run one cycle (default)")
    p_bridge_runs.add_argument("--cycles", type=int, help=f"Run N cycles, 1..{MAX_BRIDGE_CYCLES}")
    p_bridge.set_defaults(cycles=1)
    p_bridge.add_argument("--interval", type=float, default=30.0, help="Seconds between cycles")
    p_bridge.add_argument("--limit", type=int, default=50, help="Inbox page size per tag")
    p_bridge.add_argument("--max-pages", type=int, default=10, help=f"Inbox pages read per tag and cycle, 1..{MAX_BRIDGE_PAGES}")
    p_bridge.add_argument("--aplexer-bin", default="aplexer")
    p_bridge.add_argument("--state-dir", default=None)
    p_bridge.add_argument("--runtime-dir", default=None)
    p_bridge.add_argument("--service-tag", default=None)
    p_bridge.add_argument("--service-session-id", default=None)

    args = parser.parse_args()
    if args.cmd in ('submit', 'jobs', 'job', 'task-result', 'routing-machines', 'model-policy', 'task-artifact', 'artifact'):
        try:
            print(json.dumps(routing.execute(args), indent=2))
        except Exception:
            print('Hub task operation failed; check inputs, authentication and host admission.', file=sys.stderr)
            sys.exit(1)
        return

    
    if args.cmd == 'heartbeat':
        from . import bus
        payload = {'status': args.status}
        if args.summary: payload['summary'] = args.summary
        try:
            print(json.dumps(bus._do_request('POST', '/api/heartbeat', payload)))
        except Exception:
            print('Heartbeat failed; check the agent credential has the health:report scope.', file=sys.stderr)
            sys.exit(1)
        return
    if args.cmd in ('start-agent', 'start-session', 'plan-agent', 'fleet', 'subscriptions', 'fleet-host'):
        from . import fleet, bus
        from pathlib import Path
        import uuid
        try:
            if args.cmd == 'fleet':
                result = bus._do_request('GET', '/api/fleet')
            elif args.cmd in ('subscriptions', 'fleet-host'):
                settings = fleet.load_settings(args.config)
                if args.cmd == 'fleet-host':
                    fleet.run_cycles(settings, args.cycles, args.interval)
                    return
                result = {'subscriptions': fleet.collect_subscriptions(settings)}
                if args.report:
                    bus._do_request('PUT', '/api/fleet/subscriptions', result)
            else:
                payload = {'mode': 'interactive' if args.cmd == 'start-session' else 'headless', 'workspace': args.workspace,
                           'summary': args.summary, 'idempotency_key': args.key or str(uuid.uuid4())}
                if args.machine: payload['machine'] = args.machine
                if args.engine: payload['engine'] = args.engine
                if args.parent: payload['parent_id'] = args.parent
                if args.prompt_file:
                    if args.cmd == 'start-session': raise ValueError('Interactive sessions start at an empty native prompt')
                    with Path(args.prompt_file).open('r', encoding='utf-8') as source:
                        prompt = source.read(16001)
                    if len(prompt) > 16000: raise ValueError('Goal exceeds 16000 characters')
                    payload['prompt'] = prompt
                result = bus._do_request('POST', '/api/fleet/plan' if args.cmd == 'plan-agent' else '/api/fleet/launches', payload)
            print(json.dumps(result, indent=2))
        except Exception:
            # Server/provider output may contain sensitive data; fail without echoing it.
            print('Hub fleet operation failed; check authentication, local configuration and admission.', file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "login":
        cmd_login(args)
    elif args.cmd == "machines":
        if args.m_cmd == "available":
            cmd_machines_available(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "bridge":
        cmd_bridge(args)
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
