#!/usr/bin/env python3
"""Release-slot gate for main pushes and deploys (intake 2c87c231).

The main-push check validates a Release-Slot trailer against a Hub-issued
record without consuming it. The manual production deploy repeats that check
and atomically consumes the record before any deploy action. Rollback dispatch
passes an explicit version target and slot. Every refusal is fail-closed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HUB_URL = "https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev"
# A slot id is a random UUID v4 capability. Keep this in sync with Hub SLOT_RE.
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(r"^" + UUID_PATTERN + r"$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
TRAILER_RE = re.compile(r"(?im)^\s*release-slot:\s*(" + UUID_PATTERN + r")\s*$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
ATTEMPTS = 4
RETRY_SLEEP_SECONDS = (0, 5, 10)
# Cloudflare error 1010 blocks Python's default User-Agent before the Worker
# runs; identify every request explicitly (same rule as the deploy smokes).
USER_AGENT = "agent-hub-ci-release-slot-gate"

REFUSALS = {
    "release_slot_not_found": "slot id was never issued by the Hub (typo, or the orchestrator has not issued it yet)",
    "release_slot_target_mismatch": "slot is bound to a different deploy target (issue a slot for this exact SHA)",
    "release_slot_already_consumed": "slot is single-use and was already consumed by another deploy",
    "release_slot_revoked": "slot was revoked by the orchestrator",
    "release_slot_expired": "slot expired before this deploy ran; ask the orchestrator for a fresh one",
    "invalid_request": "request rejected by the Hub (malformed slot id or target)",
}


def head_sha(explicit=None):
    if explicit:
        return explicit
    from_env = os.environ.get("GITHUB_SHA")
    if from_env:
        return from_env
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def commit_message(sha):
    result = subprocess.run(["git", "show", "-s", "--format=%B", sha], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout


def trailer_slot(sha):
    """Last Release-Slot trailer on the head commit, or None (AC1)."""
    message = commit_message(sha)
    if message is None:
        return None, "cannot read head commit message for %s" % sha[:12]
    matches = TRAILER_RE.findall(message)
    if len(matches) != 1:
        if len(matches) > 1:
            return None, "head commit %s carries multiple Release-Slot trailers; keep exactly one" % sha[:12]
        return None, ("head commit %s carries no Release-Slot trailer; ask the orchestrator "
                      "(hub releases issue --sha %s --id <random-uuid-v4>) and amend the trailer before pushing" % (sha[:12], sha))
    return matches[0], None


def post_verify(base_url, payload, timeout=15):
    """One POST to the Hub verify endpoint. Returns (status, body_dict, transport_error)."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/releases/slots/verify",
        data=json.dumps(payload).encode(),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(8192).decode() or "{}"), None
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(8192).decode() or "{}")
        except Exception:
            body = {}
        return error.code, body if isinstance(body, dict) else {}, None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {}, exc


def verify(base_url, slot, target, actor, check_only=False, clock=time.sleep, attempts=ATTEMPTS):
    """Verify with bounded retry; consumption is requested only for real deploys."""
    payload = {"slot": slot, "target": target, "actor": actor, "consume": not check_only}
    last_reason = "no attempt made"
    for attempt in range(attempts):
        status, body, transport_error = post_verify(base_url, payload)
        if (status == 200 and body.get("ok") is True and body.get("target") == target
                and body.get("consumed") is (not check_only)):
            return True, body
        if status is not None and status < 500:
            slug = body.get("error") if isinstance(body, dict) else None
            reason = REFUSALS.get(slug, "Hub refused the slot (HTTP %s%s)" % (status, ": " + slug if slug else ""))
            return False, reason
        last_reason = ("Hub unreachable (%s)" % transport_error) if transport_error else ("Hub error HTTP %s" % status)
        if attempt + 1 < attempts:
            clock(RETRY_SLEEP_SECONDS[min(attempt, len(RETRY_SLEEP_SECONDS) - 1)])
    return False, "release gate unavailable after %d attempts (%s) - refusing to deploy, fail closed" % (attempts, last_reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check or consume the orchestrator release slot for a main push/deploy.")
    parser.add_argument("--sha", help="Deploy target commit SHA (default: GITHUB_SHA or git HEAD)")
    parser.add_argument("--target", help="Explicit non-commit target, used by rollback dispatches")
    parser.add_argument("--slot", help="Slot id paired with --target for a rollback dispatch")
    parser.add_argument("--check-only", action="store_true", help="Validate a main-push grant without consuming it")
    parser.add_argument("--hub-url", default=os.environ.get("RELEASE_SLOT_HUB_URL") or DEFAULT_HUB_URL)
    parser.add_argument("--actor", help="Consumer marker recorded on the slot (bounded)")
    args = parser.parse_args(argv)

    try:
        sha = head_sha(args.sha)
    except subprocess.CalledProcessError:
        print("REFUSED: cannot determine the deploy target SHA (no GITHUB_SHA, git failed)", file=sys.stderr)
        return 1
    target = args.target or sha
    if args.target and SHA_RE.fullmatch(args.target):
        print("REFUSED: commit deploys must use the head commit Release-Slot trailer", file=sys.stderr)
        return 1
    if not TARGET_RE.match(target):
        print("REFUSED: deploy target %r is not a valid slot target (40-hex SHA or version id)" % target[:24], file=sys.stderr)
        return 1

    if args.target:
        slot = args.slot
        if not slot:
            print("REFUSED (no release slot): explicit version target requires the workflow release_slot input", file=sys.stderr)
            return 1
    else:
        if not SHA_RE.fullmatch(sha):
            print("REFUSED: deploy target is not a full 40-hex commit SHA", file=sys.stderr)
            return 1
        if args.slot:
            print("REFUSED: a commit deploy must use its Release-Slot trailer", file=sys.stderr)
            return 1
        slot, reason = trailer_slot(sha)
        if not slot:
            print("REFUSED (no release slot): " + reason, file=sys.stderr)
            return 1
    if not UUID_RE.fullmatch(slot):
        print("REFUSED (release slot): id is not a random UUID v4", file=sys.stderr)
        return 1

    actor = args.actor or os.environ.get("RELEASE_SLOT_ACTOR")
    if not actor:
        run = os.environ.get("GITHUB_RUN_ID")
        actor = ("github-actions:" + run) if run else "ci-deploy"

    ok, result = verify(args.hub_url, slot, target, actor, check_only=args.check_only)
    if not ok:
        print("REFUSED (release slot): " + str(result), file=sys.stderr)
        return 1
    action = "checked without consumption" if args.check_only else "verified and consumed"
    print("Release slot %s for target %s (deploy may proceed)" % (action, str(result.get("target"))[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
