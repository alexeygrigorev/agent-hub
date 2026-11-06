#!/usr/bin/env python3
"""Legacy version rollback is disabled after the Hub2 clean start.

Cloudflare version IDs do not prove which database contract the Worker uses.
Deploying an arbitrary historic version could put Hub1 code over the fresh
Hub2 namespaces. This entry point is retained only to fail safely for old
operator commands. Use the Hub2 source-validation workflow and deploy only a
reviewed Hub2 commit through the production release path.
"""

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Disabled legacy Worker rollback helper")
    parser.add_argument("--list", action="store_true", help="Legacy version listing is disabled")
    parser.add_argument("--version-id", help="Legacy version deployment is disabled")
    parser.add_argument("--yes", "-y", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.list or args.version_id or args.yes:
        parser.error(
            "legacy Cloudflare version operations are disabled; validate and deploy a reviewed Hub2 source commit"
        )

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
