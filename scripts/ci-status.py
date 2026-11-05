#!/usr/bin/env python3
"""Read GitHub Actions status through a host-shared cache.

Sessions on one host share a 60-second cache and a per-key lock, so concurrent
requests for the same repository and commit produce at most one ``gh`` call.
Repeated 403 responses activate a shared cooldown; a Retry-After value is
honored when gh includes one in its diagnostic.
"""

from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

CACHE_TTL_SECONDS = 60
MAX_COOLDOWN_SECONDS = 900
INITIAL_COOLDOWN_SECONDS = 30
MAX_OUTPUT_BYTES = 1024 * 1024
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}\Z")


def cache_dir() -> Path:
    """Return a private cache location shared by sessions running as this user."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime) / "agent-hub" if runtime else Path.home() / ".cache" / "agent-hub"
    path = root / "gh-ci-status"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def atomic_json(path: Path, value: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".ci-status-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def cached_result(path: Path, now: float) -> dict | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(entry, dict) and isinstance(entry.get("saved_at"), (int, float))
                and now - entry["saved_at"] < CACHE_TTL_SECONDS
                and isinstance(entry.get("runs"), list)):
            return entry
    except (OSError, ValueError):
        pass
    return None


def retry_after_seconds(message: str) -> int | None:
    match = re.search(r"(?i)retry[-_ ]after\s*[:=]\s*([^\r\n]+)", message)
    if not match:
        return None
    value = match.group(1).strip()
    try:
        seconds = int(value)
    except ValueError:
        try:
            seconds = int(parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None
    return max(1, min(MAX_COOLDOWN_SECONDS, seconds))


def run(repo: str, sha: str) -> int:
    if not REPO_RE.fullmatch(repo) or not SHA_RE.fullmatch(sha):
        print("repository must be OWNER/REPO and commit must be a 7-40 digit hex SHA", file=sys.stderr)
        return 2

    directory = cache_dir()
    key = hashlib.sha256(f"{repo.lower()}:{sha.lower()}".encode()).hexdigest()
    result_path = directory / f"{key}.json"
    lock_path = directory / f"{key}.lock"
    global_lock_path = directory / "poller.lock"
    cooldown_path = directory / "cooldown.json"
    failure_path = directory / "failure.json"

    # The global lock makes the shared cooldown authoritative across different
    # repositories/commits as well as duplicate requests for one cache key.
    with global_lock_path.open("a", encoding="utf-8") as global_lock:
        os.chmod(global_lock_path, 0o600)
        fcntl.flock(global_lock, fcntl.LOCK_EX)
        with lock_path.open("a", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _run_locked(repo, sha, result_path, cooldown_path, failure_path)


def _run_locked(repo: str, sha: str, result_path: Path,
                cooldown_path: Path, failure_path: Path) -> int:
        now = time.time()
        entry = cached_result(result_path, now)
        if entry is not None:
            print(json.dumps({"source": "shared-cache", "age_seconds": int(now - entry["saved_at"]),
                              "repo": repo, "sha": sha, "runs": entry["runs"]}, separators=(",", ":")))
            return 0

        try:
            cooldown = json.loads(cooldown_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cooldown = {}
        until = cooldown.get("until", 0) if isinstance(cooldown, dict) else 0
        if isinstance(until, (int, float)) and until > now:
            wait = int(until - now + 0.999)
            print(f"shared GitHub status cooldown active; retry in {wait}s", file=sys.stderr)
            return 75

        command = ["gh", "run", "list", "--repo", repo, "--commit", sha,
                   "--limit", "50", "--json",
                   "databaseId,name,status,conclusion,headSha,createdAt,updatedAt"]
        try:
            proc = subprocess.run(command, check=False, capture_output=True, text=True,
                                  timeout=30, env={**os.environ, "GH_PROMPT_DISABLED": "1"})
        except (OSError, subprocess.TimeoutExpired):
            print("GitHub status request failed; retry after 60 seconds", file=sys.stderr)
            return 1

        if proc.returncode != 0:
            diagnostic = (proc.stderr or "")[:MAX_OUTPUT_BYTES]
            if "403" in diagnostic or "secondary rate limit" in diagnostic.lower():
                try:
                    failures = json.loads(failure_path.read_text(encoding="utf-8"))
                    count = failures.get("count", 0) if isinstance(failures, dict) else 0
                except (OSError, ValueError):
                    count = 0
                count = min(8, count + 1) if isinstance(count, int) and count >= 0 else 1
                delay = retry_after_seconds(diagnostic)
                if delay is None:
                    delay = min(MAX_COOLDOWN_SECONDS, INITIAL_COOLDOWN_SECONDS * (2 ** (count - 1)))
                atomic_json(failure_path, {"count": count})
                atomic_json(cooldown_path, {"until": time.time() + delay})
                print(f"GitHub rate limit response; shared cooldown {delay}s", file=sys.stderr)
                return 75
            print("GitHub status request failed; retry after 60 seconds", file=sys.stderr)
            return 1

        if len(proc.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            print("GitHub status response exceeded the size limit", file=sys.stderr)
            return 1
        try:
            runs = json.loads(proc.stdout)
        except ValueError:
            print("GitHub status response was not valid JSON", file=sys.stderr)
            return 1
        if not isinstance(runs, list):
            print("GitHub status response had an unexpected shape", file=sys.stderr)
            return 1

        atomic_json(result_path, {"saved_at": time.time(), "runs": runs})
        atomic_json(failure_path, {"count": 0})
        try:
            cooldown_path.unlink()
        except FileNotFoundError:
            pass
        print(json.dumps({"source": "github", "age_seconds": 0,
                          "repo": repo, "sha": sha, "runs": runs}, separators=(",", ":")))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub OWNER/REPO")
    parser.add_argument("--sha", required=True, help="commit SHA")
    args = parser.parse_args()
    return run(args.repo, args.sha)


if __name__ == "__main__":
    raise SystemExit(main())
