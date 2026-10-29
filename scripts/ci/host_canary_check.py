#!/usr/bin/env python3
"""Canary regression: the host suite must never touch the real user profile.

Reproduces the 2026-10-27 cli.json clobber conditions (intake 6b624e65 AC4/AC5):
HOME points at a canary home holding a valid human cli.json and a paired
host.json (the exact profile kind the close-gate session's suite run
destroyed), with only TMPDIR set on top - the shape the incident run used.
The full host suite is then executed twice (pytest from the repo root, and
`python -m unittest discover` from host/), and both canary files must be
byte-identical afterwards.

Host tests are hermetic by default since intake 6b624e65 (host/tests scrubs
HOME/XDG before agent_hub_host is imported, and agent_hub_host.config
hard-refuses the recorded real user profile), so any mutation of the canary
or any suite attempt to resolve it fails this check.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CANARY_CLI = {
    "client_kind": "human",
    "client_name": "canary-human",
    "access_token": "canary-human-access-token-000001",
    "refresh_token": "canary-human-refresh-token-00001",
    "base_url": "https://hub.example",
}
CANARY_HOST = {
    "client_kind": "agent",
    "client_name": "project:worker",
    "access_token": "canary-host-access-token-0001",
    "refresh_token": "canary-host-refresh-token-0001",
    "base_url": "https://hub.example",
    "machine_id": "canary-machine",
}

# Ambient selectors that would make config resolution leave the canary home
# (same class as the incident; the suite scrubs these itself, this keeps the
# harness independent of the launching session's environment).
SCRUB = (
    "HUB_CONFIG_FILE", "HUB_URL", "HUB_BASE_URL", "HUB_TOKEN", "HUB_JOB_TOKEN",
    "HUB_WORKER_CONFIG_FILE", "HUB_TEST_GUARD_HOME", "HUB_TEST_GUARD_USER_HOME", "HUB_TEST_GUARD_XDG_CONFIG_HOME",
    "HUB_TEST_GUARD_APPDATA", "APLEXER_TAG", "APLEXER_WORKSPACE",
)


def write_canary(home: Path):
    config_dir = home / ".config" / "agent-hub"
    config_dir.mkdir(parents=True)
    os.chmod(config_dir, 0o700)
    for name, profile in (("cli.json", CANARY_CLI), ("host.json", CANARY_HOST)):
        path = config_dir / name
        path.write_text(json.dumps(profile) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)


def digest(config_dir: Path) -> dict:
    return {
        name: hashlib.sha256((config_dir / name).read_bytes()).hexdigest()
        for name in ("cli.json", "host.json")
    }


def suite_env(run_tmp: Path) -> dict:
    # Python 3.11 can return a relative mkdtemp path. Both runners must use
    # the same canary and scratch directories even when cwd changes to host/.
    run_tmp = run_tmp.resolve()
    env = dict(os.environ)
    for name in SCRUB:
        env.pop(name, None)
    # Keep uv's cache with the invoking user, not inside the canary home.
    env.setdefault("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv"))
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("APPDATA", None)
    env["HOME"] = str(run_tmp / "canary-home")
    env["TMPDIR"] = str(run_tmp / "tmp")
    # Bare unittest has no pytest fixture to bound Git discovery. Synthetic
    # non-repositories below scratch must never discover this real checkout.
    env["GIT_CEILING_DIRECTORIES"] = env["TMPDIR"]
    # Cross-test imports ("from test_fleet import ...") need the test dir on
    # the path for both runners, like scripts/test-full-local.sh; absolute so
    # the unittest leg can run from host/ with the same value.
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "host"), str(REPO_ROOT / "host/tests")])
    env["DTC_AUTH_HOME"] = env.get("DTC_AUTH_HOME") or str(REPO_ROOT.parent / "auth")
    env["CODEX_LAUNCH_WRAPPER"] = ""
    return env


def uv_prefix(env: dict):
    uv = shutil_which("uv")
    if uv:
        return [uv, "run", "--frozen", "--with", "pytest", "python"]
    return [sys.executable]


def shutil_which(name: str):
    from shutil import which
    return which(name)


def run_suite(name: str, cwd: Path, args: list, env: dict, log_path: Path = None) -> int:
    print(f"=== canary suite run: {name} (cwd {cwd}) ===", flush=True)
    if log_path is None:
        result = subprocess.run(args, cwd=cwd, env=env)
        code = result.returncode
    else:
        with open(log_path, "wb") as log:
            code = subprocess.run(args, cwd=cwd, env=env, stdout=log, stderr=log).returncode
    if code != 0:
        print(f"NOTE: {name} exited with code {code}", flush=True)
    return code


def unittest_failures(log_path: Path):
    lines = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(("FAIL: ", "ERROR: ")):
            lines.append(line)
    return lines


def main() -> int:
    run_parent = Path(os.environ.get("TMPDIR") or REPO_ROOT / ".local" / "tmp")
    run_parent.mkdir(parents=True, exist_ok=True)
    run_tmp = Path(tempfile.mkdtemp(prefix="hub-canary-", dir=str(run_parent)))
    try:
        home = run_tmp / "canary-home"
        write_canary(home)
        config_dir = home / ".config" / "agent-hub"
        before = digest(config_dir)
        env = suite_env(run_tmp)
        (run_tmp / "tmp").mkdir()
        prefix = uv_prefix(env)

        # Same prerequisite ordering as CI and scripts/test-full-local.sh:
        # build the CLI zipapp manifest some suite assertions expect.
        build_code = run_suite(
            "hub CLI zipapp build",
            REPO_ROOT,
            prefix + [str(REPO_ROOT / "scripts" / "build-hub-cli.py"), "--out-dir", "public/cli"],
            env,
        )
        if build_code != 0:
            return 1
        pytest_code = run_suite(
            "pytest discovery (repo root)",
            REPO_ROOT,
            prefix + ["-m", "pytest", "-q", "-o", "tmp_path_retention_count=0",
                      "-o", "tmp_path_retention_policy=none"],
            env,
        )
        after = digest(config_dir)
        if after != before:
            print(f"FAIL: canary mutated after pytest run: {before} -> {after}", flush=True)
            return 1

        unittest_log = run_tmp / "unittest-discover.log"
        unittest_code = run_suite(
            "unittest discover (host/)",
            REPO_ROOT / "host",
            prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            env,
            log_path=unittest_log,
        )
        after = digest(config_dir)
        if after != before:
            print(f"FAIL: canary mutated after unittest run: {before} -> {after}", flush=True)
            return 1
        if pytest_code != 0 or unittest_code != 0:
            for line in unittest_failures(unittest_log):
                print("  " + line, flush=True)
            print(f"FAIL: suite exit codes pytest={pytest_code}, unittest={unittest_code}", flush=True)
            return 1

        print("PASS: canary cli.json and host.json byte-identical after the full host suite "
              "(pytest and unittest discover).", flush=True)
        return 0
    finally:
        shutil.rmtree(run_tmp)


if __name__ == "__main__":
    raise SystemExit(main())
