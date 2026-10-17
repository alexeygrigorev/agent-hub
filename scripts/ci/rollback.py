#!/usr/bin/env python3
"""Manual rollback and version inspection helper for Agent Hub Worker.

Interacts with Cloudflare Workers Versioning via Wrangler.
Can list active versions or deploy a prior version ID at 100%.

Usage:
  # List recent versions:
  python3 scripts/ci/rollback.py --list

  # Deploy specific version at 100%:
  python3 scripts/ci/rollback.py --version-id <VERSION_ID> [--message "Reason"] [--yes]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "wrangler.jsonc"


def get_cloudflare_env() -> dict:
    """Retrieve Cloudflare credentials from environment or agent-branches.env."""
    env = os.environ.copy()
    if "CLOUDFLARE_API_TOKEN" in env and "CLOUDFLARE_ACCOUNT_ID" in env:
        return env

    env_file = Path.home() / ".config" / "cloudflare" / "agent-branches.env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
                    env[k] = v

    if "CLOUDFLARE_API_TOKEN" not in env or "CLOUDFLARE_ACCOUNT_ID" not in env:
        print("WARNING: CLOUDFLARE_API_TOKEN or CLOUDFLARE_ACCOUNT_ID missing from environment.", file=sys.stderr)
    return env


def list_versions():
    env = get_cloudflare_env()
    cmd = ["npx", "wrangler", "versions", "list", "--config", str(CONFIG_PATH)]
    print(f"Executing: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env)
    return res.returncode


def deploy_version(version_id: str, message: str, yes: bool):
    env = get_cloudflare_env()
    spec = f"{version_id}@100%"
    cmd = ["npx", "wrangler", "versions", "deploy", spec, "--config", str(CONFIG_PATH)]
    if message:
        cmd.extend(["--message", message])
    if yes:
        cmd.append("--yes")

    print(f"Executing rollback deployment: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env)
    return res.returncode


def main():
    parser = argparse.ArgumentParser(description="Agent Hub Worker Rollback Helper")
    parser.add_argument("--list", action="store_true", help="List recent Worker versions")
    parser.add_argument("--version-id", type=str, help="Worker version ID to deploy at 100 percent")
    parser.add_argument("--message", type=str, default="Manual rollback via CLI", help="Annotation message")
    parser.add_argument("--yes", "-y", action="store_true", help="Automatically accept confirmation prompts")

    args = parser.parse_args()

    if args.list:
        sys.exit(list_versions())

    if args.version_id:
        if not args.yes:
            confirm = input(f"Are you sure you want to rollback to {args.version_id} at 100%? (y/N): ")
            if confirm.lower() != "y":
                print("Rollback aborted.")
                sys.exit(0)
        sys.exit(deploy_version(args.version_id, args.message, args.yes))

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
