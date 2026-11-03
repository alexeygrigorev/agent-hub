"""Incident 504d662b AC3: the alert guard for a vanished ~/.local/bin.

The PATH rm guard blocks the known deletion vector; check-local-bin catches
any other vector by auditing presence of the reference-backed critical
entries, and ops watchdog.sh turns a failure into exactly one alert per
episode (recovery resets the episode).
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "ops" / "guard-bin" / "check-local-bin"
ALERTER_SCRIPT = REPO / "ops" / "guard-bin" / "principal-alert.sh"
WATCHDOG = REPO / "ops" / "lib" / "watchdog.sh"
CRITICAL = ["aplexer", "a", "hub", "uv", "pocketshell-link", "victoria-metrics"]

ADAPTER = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
p=pathlib.Path(os.environ['HOME'])
f=p/'runtime.json'
s=json.loads(f.read_text())
with (p/'calls').open('a') as out: out.write(name+' '+ ' '.join(args)+'\n')
def save(): f.write_text(json.dumps(s))
if name=='a':
    cmd=args[0]
    if cmd=='list':
        if s.get('census_error'): sys.exit(1)
        print(json.dumps(s['sessions']))
    elif cmd=='status': print(json.dumps(next(x for x in s['sessions'] if x['id']==args[1])))
    elif cmd=='kill':
        if s.get('kill_ok'):
            s['sessions']=[x for x in s['sessions'] if x['id']!=args[1]]; save()
        else: sys.exit(1)
    elif cmd=='forget':
        s['sessions']=[x for x in s['sessions'] if x['id']!=args[1]];save()
    elif cmd=='start':
        if s.get('start_error'): sys.exit(1)
        s['sessions'].append(dict(id='new',tag='orchestrator-main',workspace=str(p/'git/agent-hub-wt-orchestrator-run/ops/orchestrator'),phase='running',worker_alive=True,worker_pid=os.getppid(),workload_pid=os.getppid()))
        save()
    elif cmd=='capture': print('❯')
    elif cmd=='send': sys.exit(s.get('send_error',0))
elif name=='systemd-run':
    import subprocess
    sys.exit(subprocess.call(args[args.index('--')+1:]) if '--' in args else 0)
elif name=='hub':
    sys.exit(s.get('hub_error',0))
elif name=='git':
    if 'rev-parse' in args:
        print(str(p/'exclude') if '--git-path' in args else 'abcdef')
'''


def make_live(tag, workspace, pid):
    return dict(id="live-" + tag, tag=tag, workspace=workspace, phase="running",
                worker_alive=True, worker_pid=pid, workload_pid=pid)


class CheckLocalBinTest(unittest.TestCase):
    """The guard itself: pure read-only audit of a (fake) home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        for name in CRITICAL:
            (self.bin / name).write_text("#!/bin/sh\n")
            (self.bin / name).chmod(0o755)

    def run_guard(self):
        return subprocess.run(
            [str(GUARD)], env={**os.environ, "BIN_WATCH_HOME": str(self.home)},
            text=True, capture_output=True, timeout=15)

    def test_intact_bin_passes_and_names_the_directory(self):
        res = self.run_guard()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("bin-watch: ok", res.stdout)
        self.assertIn(str(self.bin), res.stdout)

    def test_vanished_directory_fails_closed(self):
        shutil.rmtree(self.bin)
        res = self.run_guard()
        self.assertEqual(res.returncode, 1)
        self.assertIn("vanished", res.stdout + res.stderr)

    def test_lost_or_non_executable_critical_entry_fails_and_names_it(self):
        (self.bin / "uv").unlink()
        (self.bin / "victoria-metrics").chmod(0o644)
        res = self.run_guard()
        self.assertEqual(res.returncode, 1)
        self.assertIn("uv", res.stdout + res.stderr)
        self.assertIn("victoria-metrics", res.stdout + res.stderr)


class WatchdogWiringTest(unittest.TestCase):
    """watchdog.sh fails and alerts once per vanish episode without blocking session checks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for cmd in ("a", "git", "gh", "sleep", "systemd-run", "systemctl", "hub"):
            file = self.bin / cmd
            file.write_text(ADAPTER)
            file.chmod(0o755)
        self.alert_log = self.home / "alert-log"
        # Audit the SAME $HOME/.local/bin so a vanished ~/.local/bin also removes
        # any alerter inside ~/.local/bin — proving delivery from guard-bin outside it.
        self.audit_bin = self.home / ".local" / "bin"
        self.fill_audit_bin()
        (self.home / "git/bots-management-ops/.git").mkdir(parents=True)
        (self.home / "exclude").touch()
        for role, tag in (("orchestrator", "orchestrator-main"),
                          ("oncall", "oncall-engineer")):
            workspace = self.home / f"git/agent-hub-wt-{role}-run/ops/{role}"
            workspace.mkdir(parents=True)
            (workspace.parents[1] / ".git").touch()
        self.runtime = self.home / "runtime.json"
        self.ticks = self.home / "tmp/agent-reports/orchestrator-ticks.jsonl"
        self.ticks.parent.mkdir(parents=True)
        self.ticks.write_text("{}\n")  # fresh: both sessions are progressing
        self.state_dir = self.home / "watch-state"
        self.env = {**os.environ, "HOME": str(self.home),
                    "PATH": str(self.bin) + ":" + os.environ["PATH"],
                    "LOOP_INTERVAL": "off", "MIN_RESTART_GAP": "0",
                    "OPS_LOCK_DIR": str(self.home),
                    "ALERT_LOG": str(self.alert_log),
                    "PRINCIPAL_ALERT_LOG": str(self.home / "principal-alerts.log"),
                    "BIN_WATCH_STATE": str(self.state_dir),
                    "BIN_WATCH_HOME": str(self.home)}

    def fill_audit_bin(self):
        self.audit_bin.mkdir(parents=True, exist_ok=True)
        for name in CRITICAL:
            (self.audit_bin / name).write_text("#!/bin/sh\n")
            (self.audit_bin / name).chmod(0o755)

    def set_live_sessions(self):
        self.runtime.write_text(json.dumps({"sessions": [
            make_live("orchestrator-main",
                      str(self.home / "git/agent-hub-wt-orchestrator-run/ops/orchestrator"),
                      os.getpid()),
            make_live("oncall-engineer",
                      str(self.home / "git/agent-hub-wt-oncall-run/ops/oncall"),
                      os.getpid()),
        ]}))

    def run_watchdog(self, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        return subprocess.run(["bash", str(WATCHDOG)], env=env, text=True,
                              capture_output=True, timeout=30)

    def alerts(self):
        return self.alert_log.read_text().splitlines() if self.alert_log.exists() else []

    def test_intact_bin_keeps_the_watchdog_green(self):
        self.set_live_sessions()
        res = self.run_watchdog()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("bin-watch: ok", res.stdout)
        self.assertEqual(self.alerts(), [])
        self.assertTrue(self.state_dir.joinpath("bin-watch.state").exists())

    def test_vanish_of_same_home_bin_alerts_via_guard_bin_once_per_episode(self):
        self.set_live_sessions()
        shutil.rmtree(self.audit_bin)  # whole $HOME/.local/bin gone, including any alerter inside it
        first = self.run_watchdog()
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("vanished", first.stdout + first.stderr)
        self.assertEqual(len(self.alerts()), 1)
        self.assertIn("bin-watch", self.alerts()[0])
        self.assertTrue(self.state_dir.joinpath("bin-watch.state").read_text().startswith("fail "))
        second = self.run_watchdog()  # same episode: fail again, do not re-alert
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(len(self.alerts()), 1)
        # Recovery resets the episode; a fresh vanish alerts again.
        self.fill_audit_bin()
        recovered = self.run_watchdog()
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertTrue(self.state_dir.joinpath("bin-watch.state").read_text().startswith("ok "))
        shutil.rmtree(self.audit_bin)
        third = self.run_watchdog()
        self.assertNotEqual(third.returncode, 0)
        self.assertEqual(len(self.alerts()), 2)

    def test_failed_alerter_marks_fail_unalerted_and_retries_next_tick(self):
        self.set_live_sessions()
        shutil.rmtree(self.audit_bin)
        bad_alerter = self.home / "bad-alerter.sh"
        bad_alerter.write_text("#!/usr/bin/env bash\nexit 1\n")
        bad_alerter.chmod(0o755)
        first = self.run_watchdog({"BIN_WATCH_ALERTER": str(bad_alerter)})
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self.alerts(), [])
        self.assertTrue(self.state_dir.joinpath("bin-watch.state").read_text().startswith("fail-unalerted "))
        # Next tick with working alerter must retry and succeed instead of muting the episode.
        second = self.run_watchdog()
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(len(self.alerts()), 1)
        self.assertTrue(self.state_dir.joinpath("bin-watch.state").read_text().startswith("fail "))

    def test_session_checks_run_before_bin_watch_and_alerter_timeout_is_bounded(self):
        self.set_live_sessions()
        shutil.rmtree(self.audit_bin)
        slow_alerter = self.home / "slow-alerter.sh"
        slow_alerter.write_text("#!/usr/bin/env bash\n/bin/sleep 10\n")
        slow_alerter.chmod(0o755)
        res = self.run_watchdog({"BIN_WATCH_ALERTER": str(slow_alerter), "BIN_WATCH_ALERT_TIMEOUT": "1"})
        self.assertNotEqual(res.returncode, 0)
        # Both orchestrator and oncall session checks completed before bin_watch timed out.
        self.assertIn("orchestrator: OK session=live-orchestrator-main", res.stdout)
        self.assertIn("oncall: OK session=live-oncall-engineer", res.stdout)
        self.assertTrue(self.state_dir.joinpath("bin-watch.state").read_text().startswith("fail-unalerted "))

    def test_missing_guard_script_never_breaks_the_watchdog(self):
        # Installed-layout resolution: without a guard next to watchdog.sh the
        # watchdog must still run its session checks and stay green.
        self.set_live_sessions()
        watchdog = self.home / "standalone-watchdog.sh"
        watchdog.write_text(WATCHDOG.read_text())
        watchdog.chmod(0o755)
        shutil.copy2(REPO / "ops" / "lib" / "session-ctl.sh", self.home / "session-ctl.sh")
        res = subprocess.run(["bash", str(watchdog)], env=self.env, text=True,
                             capture_output=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("bin-watch", res.stdout)


if __name__ == "__main__":
    unittest.main()
