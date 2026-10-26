#!/usr/bin/env python3
"""Check host subscriptions against the Hub registry routes (relabel safety).

Compares one host settings file with a `hub fleet` JSON snapshot and verifies
that every registry route for --machine has a host subscription with the same
engine/profile whose account label matches by canonical identity (the local
label or any alias from config/model-routing.json `account_aliases`).

Safe relabel order (enforced by this check, exit 1 otherwise):
  1. add the canonical route to the registry (both labels then route safely),
  2. flip the host subscription label,
  3. remove the legacy registry route.

Usage:
  check-host-routes.py --settings <subscriptions.json> --fleet <hub-fleet.json> --machine <id>
  hub fleet > /tmp/fleet.json   # snapshot first; this tool never calls the Hub
"""
import argparse
import json
import sys
from pathlib import Path


def load_settings(path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))
    from agent_hub_host.fleet import load_settings as load
    try:
        return load(str(path))
    finally:
        sys.path.pop(0)


def load_aliases():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))
    try:
        from agent_hub_host.model_contract import account_aliases, canonical_account_label
        return account_aliases(), canonical_account_label
    finally:
        sys.path.pop(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--fleet", required=True)
    parser.add_argument("--machine", required=True)
    args = parser.parse_args()
    try:
        settings = load_settings(args.settings)
    except Exception as exc:
        print("check-host-routes: FAIL: host settings invalid: " + type(exc).__name__)
        return 1
    try:
        fleet = json.loads(Path(args.fleet).read_text(encoding="utf-8"))
    except Exception:
        print("check-host-routes: FAIL: fleet snapshot unreadable")
        return 1
    aliases, canonical = load_aliases()
    subs = settings.get("subscriptions", [])
    problems = []
    matched = 0
    for route in fleet.get("routes", []):
        if not isinstance(route, dict) or route.get("machine_id") != args.machine:
            continue
        matched += 1
        engine, account, profile = route.get("engine"), route.get("account"), route.get("profile")
        hit = [s for s in subs
               if s.get("engine", s.get("service")) == engine
               and s.get("profile") == profile
               and canonical(s.get("account_label")) == canonical(account)]
        if len(hit) != 1:
            problems.append("route %s/%s/%s has %d host matches" % (engine, account, profile, len(hit)))
            continue
        local = hit[0].get("account_label")
        if local != account:
            print("check-host-routes: info: route %s/%s accepted via alias (host label %s)" % (engine, account, local))
    if problems:
        for problem in problems:
            print("check-host-routes: FAIL: " + problem)
        return 1
    if matched == 0:
        print("check-host-routes: FAIL: no registry routes for " + args.machine)
        return 1
    print("check-host-routes: OK: %d route(s) for %s all match by canonical identity (aliases: %s)"
          % (matched, args.machine, aliases if aliases else "{}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
