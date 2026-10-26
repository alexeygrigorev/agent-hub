#!/usr/bin/env python3
"""Build a complete host release tree from a git revision.

Copies exactly the paths a host release needs (host package, routing config,
supervisor entrypoint, dashboard export, build provenance) plus the release
manifest with sha256 hashes, then runs the install-time smoke check
(scripts/verify-host-release.py). A release that fails the smoke check is
removed and the build fails.

Usage:
  build-host-release.py --rev <sha> [--releases-dir DIR]

The target is <releases-dir>/<full-sha>. An existing target is verified in
place (idempotent rebuilds); a failed verification is never overwritten and
a failed smoke check discards the new tree.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST_TEMPLATE = REPO / "scripts" / "host-release-manifest.json"
VERIFY = REPO / "scripts" / "verify-host-release.py"
DEFAULT_RELEASES = Path.home() / ".local" / "share" / "agent-hub" / "host-releases"

TRACKED_PATHS = [
    "host",
    "config/model-routing.json",
    "scripts/session-host-supervisor.py",
    "scripts/metrics-dashboard-export.py",
    "scripts/host-release-manifest.json",
    "pyproject.toml",
    "uv.lock",
]


def run(*argv, cwd=None):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=cwd)
    if proc.returncode != 0:
        raise SystemExit("build-host-release: command failed: " + " ".join(argv[:3]))
    return proc


def write_pth(target):
    site_dirs = sorted((target / ".venv").glob("lib/python*/site-packages"))
    if not site_dirs:
        raise SystemExit("build-host-release: venv site-packages not found")
    # Belt and braces next to the drop-in PYTHONPATH=<release>/host: an
    # offline .pth entry (no pip, no network) so the release venv always
    # resolves its own host package.
    (site_dirs[0] / "agent_hub_host_release.pth").write_text(str(target / "host") + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", required=True, help="git revision to build from")
    parser.add_argument("--releases-dir", default=str(DEFAULT_RELEASES))
    args = parser.parse_args()
    rev = run("git", "rev-parse", args.rev, cwd=str(REPO)).stdout.strip()
    if len(rev) != 40 or any(c not in "0123456789abcdef" for c in rev):
        raise SystemExit("build-host-release: invalid revision")
    short = rev
    releases = Path(args.releases_dir)
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / short
    if target.exists():
        print("build-host-release: target exists, verifying in place: " + short)
        rc = subprocess.run([sys.executable, str(VERIFY), str(target)]).returncode
        if rc != 0:
            raise SystemExit("build-host-release: existing release failed verification")
        write_pth(target)
        print("build-host-release: OK: " + str(target))
        return 0
    template = json.loads(MANIFEST_TEMPLATE.read_text(encoding="utf-8"))
    staging = Path(tempfile.mkdtemp(prefix="host-release-", dir=str(releases)))
    # git archive streams to stdout; extract explicitly so failures are loud.
    proc = subprocess.run(["git", "archive", rev, "--", *TRACKED_PATHS],
                          capture_output=True, timeout=120, cwd=str(REPO))
    if proc.returncode != 0 or not proc.stdout:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("build-host-release: git archive failed")
    unpack = subprocess.run(["tar", "-x", "-C", str(staging)], input=proc.stdout,
                            capture_output=True, timeout=120)
    if unpack.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("build-host-release: archive extract failed")
    for name in TRACKED_PATHS:
        if not (staging / name).exists():
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemExit("build-host-release: archived rev is missing: " + name)
    hashes = {}
    for name in template["files"]:
        data = (staging / name).read_bytes()
        if len(data) > 4 * 1024 * 1024:
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemExit("build-host-release: file unexpectedly large: " + name)
        hashes[name] = hashlib.sha256(data).hexdigest()
    for tree in template.get("hashed_trees", []):
        count = 0
        for path in sorted((staging / tree).rglob("*")):
            if path.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
                raise SystemExit("build-host-release: package tree has a link: " + tree)
            if path.is_dir():
                continue
            if not path.is_file():
                shutil.rmtree(staging, ignore_errors=True)
                raise SystemExit("build-host-release: package tree has a special: " + tree)
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            count += 1
            if count > 2000:
                shutil.rmtree(staging, ignore_errors=True)
                raise SystemExit("build-host-release: package tree too large: " + tree)
            data = path.read_bytes()
            if len(data) > 4 * 1024 * 1024:
                shutil.rmtree(staging, ignore_errors=True)
                raise SystemExit("build-host-release: file unexpectedly large: " + path.name)
            hashes[path.relative_to(staging).as_posix()] = hashlib.sha256(data).hexdigest()
    if "host/agent_hub_host/__init__.py" not in hashes:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("build-host-release: archived rev has no host package entry")
    (staging / "release-manifest.json").write_text(json.dumps(
        {"manifest_version": template["version"], "release_rev": rev,
         "files_sha256": hashes}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Drop-ins point TMPDIR at <release>/.local/tmp; ship the directory.
    (staging / ".local" / "tmp").mkdir(parents=True, exist_ok=True)
    venv = staging / ".venv"
    vproc = subprocess.run(["uv", "venv", str(venv)],
                           capture_output=True, text=True, timeout=300)
    if vproc.returncode != 0:
        vproc = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                               capture_output=True, text=True, timeout=300)
    if vproc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("build-host-release: venv creation failed")
    try:
        staging.rename(target)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit("build-host-release: target appeared concurrently, aborting")
    # Written after the rename so the path points at the final release dir.
    write_pth(target)
    rc = subprocess.run([sys.executable, str(VERIFY), str(target)]).returncode
    if rc != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise SystemExit("build-host-release: smoke check failed, release discarded")
    print("build-host-release: OK: " + str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
