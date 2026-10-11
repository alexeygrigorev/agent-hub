import argparse
import sys
import signal
from . import api, config

def main():
    parser = argparse.ArgumentParser(description="Agent Hub Host Daemon")
    args = parser.parse_args()
    
    cfg = config.load_config()
    if not cfg.get("access_token"):
        print("Not logged in. Please run `hub login <base_url>` first.", file=sys.stderr)
        sys.exit(1)
        
    agent = api.HostAgent()
    
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
