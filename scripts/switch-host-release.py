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

--dropin must be the shared zz~~hub-desired-release.conf managed override.
--restart reloads user units and restarts the service,
then waits for it to report active.
"""
import argparse
import shlex
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
from host_release_modes import verify_tree
sys.path.insert(0, str(REPO / 'host'))
from agent_hub_host.immutable_release import (legacy_switches, migration_lock,
    retire_switches, validate_switches, UpdateRefused)

VERIFY = REPO / "scripts" / "verify-host-release.py"
DROPIN = 'zz~~hub-desired-release.conf'
MARKER = '# Managed by Agent Hub desired host release.\n'
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


# Companion units with managed drop-ins are updated during the same switch.
# The fleet timer is a Hub2 heartbeat-only cycle and uses the separately
# installed Hub2 host package; the legacy quota publisher still follows the
# verified Hub1 host release but remains disabled on Hub2-managed hosts.
COMPANION_SERVICES = ("agent-hub-fleet-host.service", "agent-hub-quota-server.service")


def hub2_fleet_cycle_dropin():
    """Keep the managed fleet unit on the Hub2-only heartbeat path."""
    return (MARKER + "[Service]\n"
            "WorkingDirectory=%h\n"
            "ExecStart=\n"
            "ExecStart=%h/.local/share/agent-hub2/venv/bin/hub2 heartbeat --aplexer a\n"
            "StandardOutput=null\n"
            "TimeoutStartSec=60\n")


def refresh_companions(dropin, release):
    """Refresh managed companion drop-ins; returns the paths written."""
    written = []
    for service in COMPANION_SERVICES:
        path = dropin.parent.parent / (service + ".d") / DROPIN
        if path.is_symlink() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if not text.startswith(MARKER):
            continue
        if service == "agent-hub-fleet-host.service":
            new_text = hub2_fleet_cycle_dropin()
        else:
            try:
                new_text = substitute_release(text, str(release))
            except SystemExit:
                continue
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(new_text, encoding="utf-8")
        temporary.replace(path)
        written.append(path)
    return written


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
    parser.add_argument('--migration-lock-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    dropin = Path(args.dropin)
    if dropin.name != DROPIN:
        raise SystemExit('switch-host-release: REFUSED: use managed ' + DROPIN)
    if dropin.is_symlink() or (dropin.exists() and
            (not dropin.is_file() or not dropin.read_text(encoding='utf-8').startswith(MARKER))):
        raise SystemExit('switch-host-release: REFUSED: unmanaged destination')
    dropin.parent.mkdir(parents=True, exist_ok=True)
    try:
        with migration_lock(dropin.parent.resolve(), args.migration_lock_fd):
            return activate(args, dropin)
    except UpdateRefused as exc:
        raise SystemExit('switch-host-release: REFUSED: ' + exc.reason)


def activate(args, dropin):
    if dropin.is_symlink() or (dropin.exists() and
            (not dropin.is_file() or not dropin.read_text(encoding='utf-8').startswith(MARKER))):
        raise SystemExit('switch-host-release: REFUSED: unmanaged destination')
    release = Path(args.release).resolve()
    try:
        verify_tree(release)
    except (OSError, ValueError):
        raise SystemExit('switch-host-release: REFUSED: release contains group/world-writable or unsupported entries')
    template_text = Path(args.template).read_text(encoding='utf-8')
    old_dirs = sorted(set(m.group(0) for m in SHA.finditer(template_text)))
    if len(old_dirs) != 1:
        raise SystemExit('switch-host-release: REFUSED: template must reference exactly one release dir')
    old_root = Path(old_dirs[0])
    paths = run('systemctl', '--user', 'show', args.service,
                '--property=DropInPaths', '--value').stdout
    # Include files not yet loaded by systemd as well as reported search paths.
    dropins = set(Path(p) for p in shlex.split(paths)) | set(dropin.parent.glob('*.conf'))
    legacy = legacy_switches(dropins, dropin.parent.resolve(), old_root)
    rc = subprocess.run([sys.executable, str(VERIFY), str(release)]).returncode
    if rc != 0:
        raise SystemExit("switch-host-release: REFUSED: release failed verification")
    template_text = Path(args.template).read_text(encoding="utf-8")
    new_text = substitute_release(template_text, str(release))
    if not new_text.startswith(MARKER):
        new_text = MARKER + new_text
    if "PYTHONDONTWRITEBYTECODE" not in new_text:
        # Keep the release tree verifiable at runtime: no bytecode caches
        # are ever written next to the hashed sources (startup cost only).
        lines = new_text.splitlines(keepends=True)
        insert_at = next((i for i, line in enumerate(lines) if line.startswith("ExecStart=")), len(lines))
        lines.insert(insert_at, "Environment=PYTHONDONTWRITEBYTECODE=1\n")
        new_text = "".join(lines)
    validate_switches(legacy)
    dropin.write_text(new_text, encoding="utf-8")
    retire_switches(legacy, release.parent / '.host-release-switch-journal.jsonl')
    print("switch-host-release: wrote " + str(dropin))
    for companion in refresh_companions(dropin, release):
        print("switch-host-release: wrote " + str(companion))
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
