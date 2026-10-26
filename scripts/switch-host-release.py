#!/usr/bin/env python3
"""Switch a systemd user drop-in to a verified host release (or refuse).

Gate: scripts/verify-host-release.py must pass on --release first. A release
that fails verification is never written into any drop-in.

The new drop-in is cloned from --template (normally the currently active
drop-in) with every occurrence of the old release directory replaced by the
new one, so supervisor --config/--credential-config/--journal-dir arguments
and environment are preserved and only the release path changes.

Usage:
  switch-host-release.py --release <dir> --template <conf> --dropin <conf> [--restart]

--dropin should sort after every other drop-in for the unit (systemd applies
the last ExecStart=). --restart reloads user units and restarts the service,
then waits for it to report active.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "scripts" / "verify-host-release.py"
# Full old release directory path: any non-space run ending in a 40-hex
# release segment, followed by /, whitespace, quote or end of line. Matching
# the full path (not just the sha segment) keeps the substitution exact.
SHA = re.compile(r"/[^\s\"']+?/[0-9a-f]{40}(?=[/\"'\s]|$)")


def substitute_release(template_text, new_release):
    """Clone a drop-in template at a new release dir (pure, testable).

    Raises SystemExit when the template does not reference exactly one
    release directory or the substitution changes nothing.
    """
    old_dirs = sorted(set(m.group(0) for m in SHA.finditer(template_text)))
    if len(old_dirs) != 1:
        raise SystemExit("switch-host-release: REFUSED: template must reference exactly one release dir")
    new_text = template_text.replace(old_dirs[0], new_release)
    if new_text == template_text:
        raise SystemExit("switch-host-release: REFUSED: template unchanged")
    return new_text


def run(*argv):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit("switch-host-release: command failed: " + " ".join(argv[:4])
                         + ((("\n" + proc.stderr.strip()) if proc.stderr.strip() else ""))[:500])
    return proc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--dropin", required=True)
    parser.add_argument("--service", default="agent-hub-session-host.service")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    release = Path(args.release).resolve()
    rc = subprocess.run([sys.executable, str(VERIFY), str(release)]).returncode
    if rc != 0:
        raise SystemExit("switch-host-release: REFUSED: release failed verification")
    template_text = Path(args.template).read_text(encoding="utf-8")
    new_text = substitute_release(template_text, str(release))
    if "PYTHONDONTWRITEBYTECODE" not in new_text:
        # Keep the release tree verifiable at runtime: no bytecode caches
        # are ever written next to the hashed sources (startup cost only).
        lines = new_text.splitlines(keepends=True)
        insert_at = next((i for i, line in enumerate(lines) if line.startswith("ExecStart=")), len(lines))
        lines.insert(insert_at, "Environment=PYTHONDONTWRITEBYTECODE=1\n")
        new_text = "".join(lines)
    dropin = Path(args.dropin)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(new_text, encoding="utf-8")
    print("switch-host-release: wrote " + str(dropin))
    run("systemctl", "--user", "daemon-reload")
    if args.restart:
        run("systemctl", "--user", "restart", args.service)
        active = run("systemctl", "--user", "is-active", args.service).stdout.strip()
        print("switch-host-release: service state: " + active)
        if active != "active":
            raise SystemExit("switch-host-release: service did not become active")
    else:
        print("switch-host-release: daemon reloaded (service not restarted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
