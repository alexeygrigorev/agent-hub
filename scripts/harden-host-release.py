#!/usr/bin/env python3
"""Remove group/world write bits from one verified installed host release.

This is an explicit repair tool. Its target must be a direct child of the
fixed Agent Hub host-releases directory and contain a regular release
manifest. It never follows symlinks and changes permissions only.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_release_modes import harden_tree, validate_installed_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    try:
        release = validate_installed_release(args.release)
        changed = harden_tree(release)
    except (OSError, ValueError) as exc:
        raise SystemExit("harden-host-release: REFUSED: " + str(exc))
    print("harden-host-release: OK: " + str(release) + " (entries changed: " + str(len(changed)) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
