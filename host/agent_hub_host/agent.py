import argparse
import sys
import signal
from . import api, config

def main():
    parser = argparse.ArgumentParser(description="Agent Hub Host Daemon")
    args = parser.parse_args()
    
    cfg = config.load_config()
    if not cfg.get("access_token"):
        base_url = cfg.get("base_url")
        if base_url:
            from . import system, auth
            auth.device_login(base_url, "host", "agent-hub-host", system.get_hostname(), ["host"])
        else:
            print("Not logged in. Please run `hub login <base_url>` first.", file=sys.stderr)
            sys.exit(1)
    base_url = cfg.get("base_url")
    from . import jobs, system
    dispatcher = jobs.JobDispatcher(base_url=base_url, host_id=system.get_hostname()) if base_url else None
    agent = api.HostAgent(dispatcher=dispatcher)
    
    def handle_sigint(sig, frame):
        print("Shutdown requested...")
        agent.shutdown_requested = True
        
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)
    
    try:
        agent.run()
    except Exception as e:
        print(f"Agent stopped due to error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
