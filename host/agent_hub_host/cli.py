import argparse
import json
import os
import sys
from . import auth, api, system, config

try:
    from . import human_auth
except ImportError:  # pragma: no cover - vendored fallback always present
    human_auth = None

HUB_VERSION = "0.1.0"
try:
    from . import __version__ as _pkg_version
    HUB_VERSION = _pkg_version
except Exception:
    pass


def get_build_info() -> dict:
    try:
        from . import _build_info
        commit = getattr(_build_info, "BUILD_COMMIT", "dev")
        date = getattr(_build_info, "BUILD_DATE", "dev")
    except Exception:
        commit = "dev"
        date = "dev"
    if commit == "dev":
        try:
            import subprocess
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=2,
            )
            sha = out.stdout.strip()
            if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
                commit = sha
        except Exception:
            pass
    if date == "dev":
        try:
            from datetime import datetime, timezone
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass
    return {
        "version": HUB_VERSION,
        "commit": commit,
        "build_date": date,
    }


def format_version() -> str:
    info = get_build_info()
    commit_str = info["commit"][:12] if info["commit"] != "dev" else "dev"
    return f"{info['version']} ({commit_str}, {info['build_date']})"


def cmd_update(args):
    import hashlib
    import urllib.request
    from pathlib import Path

    clean_base = (
        getattr(args, "url", None)
        or os.environ.get("HUB_URL")
        or os.environ.get("HUB_BASE_URL")
        or config.DEFAULT_BASE_URL
    ).rstrip("/")
    api.check_url(clean_base)
    manifest_url = f"{clean_base}/cli/manifest.json"

    try:
        req = urllib.request.Request(manifest_url, headers={"User-Agent": "hub-cli"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                print(f"Failed to fetch CLI manifest from {manifest_url} (HTTP {resp.status})", file=sys.stderr)
                sys.exit(1)
            manifest = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Failed to check for updates from {manifest_url}: {exc}", file=sys.stderr)
        sys.exit(1)

    curr_info = get_build_info()
    curr_commit = curr_info.get("commit", "unknown")
    server_commit = manifest.get("built_from_commit", "")
    server_version = manifest.get("version", "0.1.0")
    server_sha256 = manifest.get("sha256", "")

    is_up_to_date = (
        curr_commit != "dev"
        and server_commit
        and curr_commit.lower() == server_commit.lower()
    )

    if getattr(args, "check", False):
        if is_up_to_date:
            print(f"hub is up to date: {curr_info['version']} ({curr_commit[:12]})")
            return
        else:
            print(f"Warning: hub CLI is stale! Current: {format_version()}, Server: {server_version} ({server_commit[:12]}). Run 'hub update' to update.")
            return

    if is_up_to_date and not getattr(args, "force", False):
        print(f"hub is already up to date ({format_version()}). Use --force to reinstall.")
        return

    pyz_url = f"{clean_base}/cli/hub.pyz"
    print(f"Downloading hub CLI from {pyz_url}...")
    try:
        req = urllib.request.Request(pyz_url, headers={"User-Agent": "hub-cli"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                print(f"Download failed: {pyz_url} returned HTTP {resp.status}", file=sys.stderr)
                sys.exit(1)
            data = resp.read()
    except Exception as exc:
        print(f"Download failed from {pyz_url}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not data:
        print("Downloaded file is empty", file=sys.stderr)
        sys.exit(1)

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if server_sha256 and actual_sha256.lower() != server_sha256.lower():
        print(f"Checksum mismatch: expected {server_sha256}, got {actual_sha256}", file=sys.stderr)
        sys.exit(1)

    install_dir_arg = getattr(args, "install_dir", None)
    if install_dir_arg:
        install_dir = Path(install_dir_arg).expanduser()
        target_path = install_dir / "hub"
    else:
        prog = Path(sys.argv[0]).resolve()
        if prog.is_file() and prog.name == "hub" and os.access(prog.parent, os.W_OK):
            target_path = prog
            install_dir = prog.parent
        else:
            install_dir = Path.home() / ".local" / "bin"
            target_path = install_dir / "hub"

    install_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".hub.tmp.{os.getpid()}"
    try:
        tmp_path.write_bytes(data)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, target_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    print(f"Updated hub at {target_path} to {server_version} ({server_commit[:12]})")



def cmd_login(args):
    try:
        kind = getattr(args, 'kind', 'host')
        scopes = getattr(args, 'scope', None) or (auth.AGENT_SCOPES if kind == 'agent' else auth.HOST_SCOPES)
        auth.device_login(args.base_url, kind, getattr(args, 'name', None) or 'agent-hub-' + kind, system.get_hostname(), scopes)
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_auth_login(args):
    if human_auth is None:
        print("Login failed: human auth module unavailable", file=sys.stderr)
        sys.exit(1)
    try:
        human_auth.human_login(getattr(args, 'url', None), getattr(args, 'label', None))
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_auth_status(args):
    if human_auth is None:
        print("Error checking status: human auth module unavailable", file=sys.stderr)
        sys.exit(1)
    try:
        status = human_auth.human_status()
    except config.ConfigPermissionError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("Status operation failed; check authentication and filters.", file=sys.stderr)
        sys.exit(1)
    if getattr(args, 'json', False):
        print(json.dumps(status, indent=2, sort_keys=True))
        return
    if not status.get("logged_in"):
        print("Status: Not logged in as human. Run `hub auth login`.")
        sys.exit(1)
    print("Status: Logged in as human.")
    if status.get("base_url"):
        print(f"Base URL: {status['base_url']}")
    for key in ("actor", "email", "sub", "label"):
        if status.get(key):
            print(f"{key}: {status[key]}")


def cmd_auth_logout(args):
    if human_auth is None:
        print("Logout failed: human auth module unavailable", file=sys.stderr)
        sys.exit(1)
    try:
        result = human_auth.human_logout()
    except Exception:
        print("Logout failed; check authentication.", file=sys.stderr)
        sys.exit(1)
    if result.get("already"):
        print("Already logged out (no human session). Host profile preserved.")
    else:
        print("Logged out. Human profile removed; host profile preserved.")

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


def add_bridge_resume_command(sub):
    parser = sub.add_parser('bridge-resume', help='Explicitly acknowledge a missing conversation history range (local journal only)',
                            description='HOLD is the default. Advance one existing bridge journal only across a verified empty retained-history range. Never marks missing messages delivered/read. Equal-target replays are refused; inspect the journal after an uncertain result.')
    parser.add_argument('--enable-native', action='store_true', help='Require the same genuine own native binding as the normal conversation bridge')
    for flag in ('actor', 'project', 'workspace', 'machine-id', 'conversation'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--resume-after-seq', type=int, required=True, help='Explicit new LOCAL cursor, greater than current cursor and at most current server last_seq')
    parser.add_argument('--acknowledge-gap', action='store_true', help='Acknowledge that missing history cannot be delivered; retain a durable local gap audit record')


def main():
    parser = argparse.ArgumentParser(description="Agent Hub CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {format_version()}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_update = sub.add_parser("update", help="Update hub CLI to the latest deployed version or check staleness")
    p_update.add_argument("--url", default=None, help="Release URL (default: HUB_URL/HUB_BASE_URL or https://hub.dtcdev.click; ignores stored profile URL)")
    p_update.add_argument("--check", action="store_true", help="Check for updates without installing")
    p_update.add_argument("--force", action="store_true", help="Force update/reinstall even if already at latest commit")
    p_update.add_argument("--install-dir", default=None, help="Directory to install the updated hub binary (default: current executable location or ~/.local/bin)")
    from . import routing
    routing.add_commands(sub)
    from . import conversations
    conversations.add_commands(sub)
    from . import intake
    intake.add_commands(sub)
    from . import session_control
    session_control.add_commands(sub)
    from . import quota as quota_cmd
    quota_cmd.add_commands(sub)
    from . import metrics as metrics_cmd
    metrics_cmd.add_commands(sub)
    from . import provisioning
    provisioning.add_commands(sub)
    add_bridge_resume_command(sub)

    for command in ('start-agent', 'start-session', 'plan-agent'):
        launch = sub.add_parser(command, help='Queue a headless agent or a real interactive session')
        launch.add_argument('--account', help='Exact central account/profile label (shared quota across machines)')
        launch.add_argument('--machine', required=command == 'start-session')
        launch.add_argument('--engine', help='Engine override (requires --reason; default start is policy-driven)')
        launch.add_argument('--reason', help='Required explanation why this specific model/engine must be chosen')
        launch.add_argument('--tier', choices=['quick', 'daily', 'deep'], help='Routing policy tier (default auto)')
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

    p_auth = sub.add_parser("auth", help="Human Developer CLI auth (device login, status, logout)")
    a_sub = p_auth.add_subparsers(dest="auth_cmd", required=True)
    p_auth_login = a_sub.add_parser("login", help="Human device login (prints URL + user code)")
    p_auth_login.add_argument("--url", default=None, help="Hub base URL (default: baked prod URL or stored human base)")
    p_auth_login.add_argument("--label", default=None, help="Human label for this CLI (default: developer-cli)")
    p_auth_status = a_sub.add_parser("status", help="Human auth status (safe identity, no tokens)")
    p_auth_status.add_argument("--json", action="store_true", help="Output JSON")
    a_sub.add_parser("logout", help="Revoke human family + remove human config (preserves host)")
    
    p_machines = sub.add_parser("machines", help="Machines operations")
    m_sub = p_machines.add_subparsers(dest="m_cmd", required=True)
    p_available = m_sub.add_parser("available", help="List available machines")
    p_available.add_argument("--json", action="store_true", help="Output JSON")
    
    p_status = sub.add_parser("status", help="One-call authorized status (single GET /api/status)")
    p_status.add_argument("--since", default=None, help="Filter to changes since duration (e.g. 2h) or ISO time")
    p_status.add_argument("--json", dest="json", action="store_true", help="Output raw JSON DTO")
    p_status.add_argument("--task", default=None, help="Single task id")
    p_status.add_argument("--project", default=None, help="Project scope filter")
    p_status.add_argument("--host", default=None, help="Host/machine filter")
    p_status.add_argument("--after", default=None, help="Pagination cursor (task id)")
    p_status.add_argument("--limit", type=int, default=None, help="Page size 1..25")
    p_status.add_argument("--local-only", action="store_true", help="Local config diagnostics only, no API call")

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

    p_activity = sub.add_parser("activity", help="Show active work declarations and recent bus messages (who is already on this)")
    p_activity.add_argument("--workspace", help="Workspace to check (resolved to a claim project via claim policy)")
    p_activity.add_argument("--project", help="Filter by claim project")
    p_activity.add_argument("--holder", help="Filter by claim holder")
    p_activity.add_argument("--actor", help="Filter recent bus messages by actor")
    p_activity.add_argument("--limit", type=int, default=20, help="Recent bus messages to include (1-100)")
    p_activity.add_argument("--all", action="store_true", help="Include released/expired claims")

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

    p_workclaim = sub.add_parser("workclaim-sync", help="Mirror local aplexer work declarations and work.* events into Hub (one cycle)")
    p_workclaim.add_argument("--aplexer-bin", default="aplexer")
    p_workclaim.add_argument("--state-dir", default=None)
    p_workclaim.add_argument("--machine-id", default=None, help="Attested machine id (default: configured machine_id)")
    p_workclaim.add_argument("--dry-run", action="store_true", help="Scan and redact without POSTing to Hub")

    args = parser.parse_args()
    if args.cmd == 'engines':
        try:
            print(json.dumps(provisioning.command(args)))
        except Exception:
            print('Hub engine operation failed; check authentication, bundle permissions and host prerequisites.', file=sys.stderr)
            sys.exit(1)
        return
    if args.cmd == 'update':
        cmd_update(args)
        return
    if args.cmd == 'bridge-resume':
        try:
            from .conversation_bridge import resume_from_args
            print(json.dumps(resume_from_args(args), indent=2))
        except Exception:
            print('Bridge recovery HOLD: check native opt-in, acknowledgement, current authority, '
                  'existing journal and empty retained range. Preserve uncertain intents. '
                  'After an uncertain result inspect the journal; equal-target replays are refused.', file=sys.stderr)
            sys.exit(1)
        return
    if args.cmd in ('intake', 'handoff'):
        try:
            print(json.dumps(intake.execute(args), indent=2, ensure_ascii=True))
        except Exception as exc:
            print(intake.diagnostic(exc), file=sys.stderr)
            sys.exit(1)
        return
    if args.cmd == 'conversations':
        try:
            print(json.dumps(conversations.execute(args), indent=2))
        except Exception:
            # Do not echo message text, paths, tokens, or server-controlled errors.
            if args.conversation_cmd == 'bridge':
                from .conversation_bridge import HOLD_REASON
                print(HOLD_REASON, file=sys.stderr)
            else:
                print('Conversation operation failed; check inputs, authentication and membership. '
                      'Retry create/send with the same key and payload after uncertain results.', file=sys.stderr)
            sys.exit(1)
        return
    if args.cmd == 'sessions':
        try:
            result = session_control.execute(args)
            rendered = session_control.render_output(args, result)
            if isinstance(rendered, str):
                print(rendered)
            else:
                print(json.dumps(rendered, indent=2, ensure_ascii=True))
        except Exception as exc:
            print(session_control.diagnostic(exc), file=sys.stderr)
            sys.exit(1)
        return
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
                result = session_control.request('GET', '/api/fleet')
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
                if args.account: payload['account'] = args.account
                if getattr(args, 'tier', None): payload['tier'] = args.tier
                if getattr(args, 'engine', None):
                    reason = getattr(args, 'reason', None)
                    if not reason or not reason.strip():
                        print('Engine override requires an explanation why this specific model/engine must be chosen (use --reason). Casual use should prefer auto/policy without --engine.', file=sys.stderr)
                        sys.exit(2)
                    payload['engine'] = args.engine
                    payload['engine_override_reason'] = reason.strip()
                if args.parent: payload['parent_id'] = args.parent
                if args.prompt_file:
                    with Path(args.prompt_file).open('r', encoding='utf-8') as source:
                        prompt = source.read(16001)
                    if len(prompt) > 16000: raise ValueError('Goal exceeds 16000 characters')
                    payload['prompt'] = prompt
                if args.cmd == 'start-session':
                    # Interactive starts bind an empty terminal first. Deliver the
                    # uploaded prompt through the normal correlated command path.
                    prompt = payload.pop('prompt', None)
                    start = {k: v for k, v in payload.items() if k not in ('summary', 'idempotency_key', 'account')}
                    if args.account:
                        raise ValueError('start-session uses policy routing; omit --account')
                    start['key'] = payload['idempotency_key']
                    result = session_control.request('POST', '/api/sessions', start)
                    if prompt is not None:
                        bound = session_control.wait_for_session(result['id'], 300, 5)
                        result['binding_wait'] = bound
                        if bound.get('state') != 'running':
                            raise ValueError('Interactive session did not become running; inspect sessions show before retrying')
                        sent = session_control.request('POST', '/api/sessions/' + result['id'] + '/send',
                                                       {'key': payload['idempotency_key'], 'body': prompt})
                        result['prompt_command'] = sent
                else:
                    result = session_control.request('POST', '/api/fleet/plan' if args.cmd == 'plan-agent' else '/api/fleet/launches', payload)
            print(json.dumps(result, indent=2))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        except Exception:
            # Server/provider output may contain sensitive data; fail without echoing it.
            print('Hub fleet operation failed; check authentication, local configuration and admission.', file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "login":
        cmd_login(args)
    elif args.cmd == "auth":
        if getattr(args, 'auth_cmd', None) == "login":
            cmd_auth_login(args)
        elif getattr(args, 'auth_cmd', None) == "status":
            cmd_auth_status(args)
        elif getattr(args, 'auth_cmd', None) == "logout":
            cmd_auth_logout(args)
        else:
            parser.error("auth requires login/status/logout")
    elif args.cmd == "machines":
        if args.m_cmd == "available":
            cmd_machines_available(args)
    elif args.cmd == "status":
        if getattr(args, 'local_only', False):
            cmd_status(args)
        else:
            from . import status as status_cmd
            try:
                status_cmd.execute(args)
            except SystemExit:
                raise
            except Exception:
                print('Status operation failed; check authentication, scope and filters. No native input was submitted.', file=sys.stderr)
                sys.exit(1)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "quota":
        from . import quota as quota_cmd
        try:
            data = quota_cmd.execute(args)
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print(quota_cmd.render_text(data))
        except Exception:
            print('Hub quota operation failed; check authentication, filters and quota grants.', file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "metrics":
        from . import metrics as metrics_mod
        try:
            data = metrics_mod.execute(args)
            if not getattr(args, "dry_run", False):
                print(json.dumps(data, indent=2, sort_keys=True))
        except SystemExit:
            raise
        except Exception:
            print('Metrics operation failed; check authentication and report files. Retry post with the same key and payload after uncertain results.', file=sys.stderr)
            sys.exit(1)
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
    elif args.cmd == "activity":
        from . import bus
        print(json.dumps(bus.activity(workspace=getattr(args, 'workspace', None), project=getattr(args, 'project', None), holder=getattr(args, 'holder', None), actor=getattr(args, 'actor', None), limit=getattr(args, 'limit', 20), active=not getattr(args, 'all', False))))
    elif args.cmd == "workclaim-sync":
        from . import bus, workclaim
        from .fleet import load_settings
        try:
            settings = load_settings()
        except Exception:
            settings = {"workspaces": {}, "subscriptions": [], "capabilities": [], "max_agents": 1}
        if getattr(args, "dry_run", False):
            import time
            from .bridge import get_authoritative_state_dir
            try:
                state_dir = args.state_dir or settings.get("aplexer_state_dir") or get_authoritative_state_dir(None)
            except Exception:
                state_dir = args.state_dir
            canonicals = workclaim.canonical_workspaces_from_settings(settings)
            rows = workclaim._list_rows(workclaim.subprocess_runner, getattr(args, "aplexer_bin", "aplexer"))
            declarations = workclaim.scan_declarations(state_dir, rows, getattr(args, "machine_id", None) or settings.get("machine_id", ""))
            envelopes, _cursors, scan_stats = workclaim.scan_work_messages(state_dir, canonicals, {})
            moment = int(time.time() * 1000)
            machine = getattr(args, "machine_id", None) or settings.get("machine_id", "") or "local"
            claim_items, claim_stats = workclaim.build_claim_items(declarations, canonicals, machine, moment)
            message_items, message_stats = workclaim.build_message_items(envelopes, machine)
            print(json.dumps({"dry_run": True, "claims": claim_items, "messages": message_items,
                              "declaration_stats": claim_stats, "envelope_stats": {**scan_stats, "invalid": message_stats.get("invalid", 0)}}))
        else:
            print(json.dumps(workclaim.sync_once(
                bus._do_request, settings=settings, runner=None,
                aplexer_bin=getattr(args, "aplexer_bin", "aplexer"),
                state_dir=getattr(args, "state_dir", None),
                machine_id=getattr(args, "machine_id", None))))

if __name__ == "__main__":
    main()
