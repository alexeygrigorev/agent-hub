"""Existing exact-SHA Python prerequisite with one fixed observation deadline."""
import argparse
import json
import os
import selectors
import re
import subprocess
import sys
import time

# One fixed observation deadline, sized for the contabo-hub runner: it executes
# one job at a time, so the Python suite's declared job timeouts alone sum to
# 125 minutes (20 + 15 + 4x20 + 10) and the earlier 25 minutes expired before
# a normal merge went green (run 45247689745, intake e19faefa).
WAIT_SECONDS = 4 * 60 * 60
POLL_SECONDS = 10
REQUEST_SECONDS = 20
MAX_RESPONSE_BYTES = 1048576


class WaitRefused(RuntimeError):
    pass


def decision(payload, sha):
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise WaitRefused("python_ci_metadata_invalid")
    runs = [r for r in payload["workflow_runs"] if isinstance(r, dict)
            and r.get("head_sha") == sha and r.get("head_branch") == "main"
            and r.get("event") == "push"]
    # Preserve the existing gate's same-commit success rule, not a new rerun policy.
    if any(r.get("status") == "completed" and r.get("conclusion") == "success" for r in runs):
        return "green"
    if runs and all(r.get("status") == "completed" for r in runs):
        raise WaitRefused("python_ci_terminal_failure")
    return "pending"


def fetch_runs(repo, sha, timeout):
    path = ("repos/"+repo+"/actions/workflows/python-tests.yml/runs"
            "?branch=main&event=push&head_sha="+sha+"&per_page=20")
    deadline = time.monotonic() + timeout
    child = None
    try:
        # Unused stderr is discarded in the kernel, never buffered in Python.
        child = subprocess.Popen(["gh", "api", path], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WaitRefused("python_ci_metadata_unavailable")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, MAX_RESPONSE_BYTES + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE_BYTES:
                            raise WaitRefused("python_ci_metadata_unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0:
            raise WaitRefused("python_ci_metadata_unavailable")
        payload = json.loads(output)
        if time.monotonic() >= deadline:
            raise WaitRefused("python_ci_metadata_unavailable")
        return payload
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        raise WaitRefused("python_ci_metadata_unavailable") from None
    finally:
        if child is not None:
            # Exact direct child only. Settlement has a separate finite 1s bound;
            # it never grants late eligibility or promises descendant closure.
            try:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=0.5)
            except (OSError, subprocess.SubprocessError):
                raise WaitRefused("python_ci_metadata_unavailable") from None
            finally:
                child.stdout.close()


def wait_for_python(repo, sha, *, fetch=fetch_runs, monotonic=time.monotonic, sleep=time.sleep):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise WaitRefused("python_ci_selection_invalid")
    deadline = monotonic() + WAIT_SECONDS
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise WaitRefused("python_ci_wait_deadline")
        payload = fetch(repo, sha, min(REQUEST_SECONDS, remaining))
        if monotonic() >= deadline:
            raise WaitRefused("python_ci_wait_deadline")
        state = decision(payload, sha)
        # Classification itself never renews the original observation deadline.
        if monotonic() >= deadline:
            raise WaitRefused("python_ci_wait_deadline")
        if state == "green":
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise WaitRefused("python_ci_wait_deadline")
        sleep(min(POLL_SECONDS, remaining))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    try:
        wait_for_python(args.repo, args.sha)
    except WaitRefused as error:
        # Only fixed codes; never print API payload, stderr, or auth context.
        print("refusing automatic deploy without green Python host tests: "+str(error), file=sys.stderr)
        return 1
    print("Python host tests are green for "+args.sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
