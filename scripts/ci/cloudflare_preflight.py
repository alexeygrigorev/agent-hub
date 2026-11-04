#!/usr/bin/env python3
"""Read-only credential check: never uploads code or accesses Durable Objects.

Supports both account-owned and user API tokens. Error output deliberately
contains only endpoint labels, HTTP statuses and API error codes, never raw
Cloudflare response bodies, account IDs, or credentials.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"


def request_json(path, token):
    request = urllib.request.Request(
        API + path,
        headers={"Authorization": "Bearer " + token,
                 "User-Agent": "agent-hub-ci-preflight", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except (ValueError, OSError):
            payload = {}
        return error.code, payload


def failure(label, status, payload):
    codes = [str(item.get("code")) for item in payload.get("errors", [])
             if isinstance(item, dict) and isinstance(item.get("code"), int)]
    return f"{label}: HTTP {status}; API codes {','.join(codes) or 'unavailable'}"


def verify(environ, request=request_json):
    token = environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account = environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not token:
        raise RuntimeError("Missing GitHub secret CLOUDFLARE_API_TOKEN")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise RuntimeError("Missing or invalid GitHub secret CLOUDFLARE_ACCOUNT_ID")
    errors = []
    for kind, path in (("account", f"/accounts/{account}/tokens/verify"),
                       ("user", "/user/tokens/verify")):
        status, payload = request(path, token)
        if status == 200 and payload.get("success") is True:
            if (payload.get("result") or {}).get("status") != "active":
                raise RuntimeError("Cloudflare API token is not active; replace CLOUDFLARE_API_TOKEN")
            break
        errors.append(failure(kind + " token verification", status, payload))
    else:
        raise RuntimeError("Invalid or inaccessible CLOUDFLARE_API_TOKEN. " + "; ".join(errors))
    for script in ("agent-hub-dev", "agent-hub"):
        status, payload = request(f"/accounts/{account}/workers/services/{script}", token)
        if status != 200 or payload.get("success") is not True:
            raise RuntimeError(failure(script + " service access", status, payload)
                               + ". Check token Workers Scripts permissions and account scope")
    return kind


def main():
    try:
        kind = verify(os.environ)
    except RuntimeError as error:
        print(f"Cloudflare credential preflight FAILED: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Cloudflare credential preflight FAILED: network or malformed API response", file=sys.stderr)
        return 1
    print(f"Cloudflare credential preflight passed ({kind}-owned token; both existing services readable; no deployment)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
