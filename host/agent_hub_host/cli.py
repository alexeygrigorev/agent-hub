import argparse
import sys
import json
from . import auth, api, system, config

def cmd_login(args):
    try:
        auth.device_login(args.base_url, "agent-hub-host", system.get_hostname(), "host")
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
        if not cfg.get("base_url"):
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

if __name__ == "__main__":
    main()
