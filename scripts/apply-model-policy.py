#!/usr/bin/env python3
"""Show or apply the committed Hub2 model policy (config/model-policy.json).

The policy is data in the Hub database; this script is the operator path that
keeps the live candidates in sync with the reviewed config file. It prints
policy data only and never echoes the token.

Usage:
  scripts/apply-model-policy.py show
  scripts/apply-model-policy.py apply [--expected-version N] [--file PATH]

Credentials: HUB2_TOKEN or --token-file (default ~/.config/agent-hub/cli.json
access_token field).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://hub.dtcdev.click"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "model-policy.json"


def load_token(args):
    if os.environ.get("HUB2_TOKEN"):
        return os.environ["HUB2_TOKEN"]
    path = Path(args.token_file) if args.token_file else Path.home() / ".config/agent-hub/cli.json"
    data = json.loads(path.read_text())
    return data["access_token"]


def request(url, token, method, body=None):
    req = urllib.request.Request(
        url.rstrip("/") + "/api/model-policy",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["show", "apply"], nargs="?", default="show")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--file", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--token-file", help="JSON file with an access_token field")
    parser.add_argument("--expected-version", type=int, default=None,
                        help="refuse to apply over a different live policy version")
    args = parser.parse_args()
    token = load_token(args)
    if args.action == "show":
        status, payload = request(args.url, token, "GET")
        print(json.dumps(payload, indent=2))
        sys.exit(0 if status == 200 else 1)
    candidates = json.loads(args.file.read_text())["candidates"]
    body = {"candidates": candidates}
    if args.expected_version is not None:
        body["expected_version"] = args.expected_version
    status, payload = request(args.url, token, "POST", body)
    print(json.dumps(payload, indent=2))
    sys.exit(0 if status == 200 else 1)


if __name__ == "__main__":
    main()
