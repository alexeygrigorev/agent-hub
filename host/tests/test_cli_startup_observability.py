"""Synthetic startup diagnostics: no real service, journal, config or database."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def admission_env():
    return {k: v for k, v in os.environ.items() if k.startswith('AGENT_HUB_HEAVY_TEST_')}


def admission_fds():
    try:
        os.fstat(9)
    except OSError:
        return ()
    return (9,)


class StartupObservabilityTests(unittest.TestCase):
    def run_diagnostic(self, scenario='normal'):
        source = (ROOT / 'deploy/vm/install-hub2-node.sh').read_text()
        body = source.split('report_hub2_startup_failure() {', 1)[1].split('\n}\n', 1)[0]
        parent = Path(os.environ.get('TMPDIR') or ROOT / '.local/tmp')
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent, prefix='startup-observer-') as tmp:
            work = Path(tmp)
            commands = work / 'bin'
            commands.mkdir(mode=0o700)
            systemctl = commands / 'systemctl'
            systemctl.write_text("""#!/bin/bash
if [[ "$*" == *InvocationID* ]]; then
  count=0; [[ ! -f "$COUNTER" ]] || read -r count < "$COUNTER"
  count=$((count+1)); printf '%s\\n' "$count" > "$COUNTER"
  if [[ "$SCENARIO" == unavailable ]]; then exit 1; fi
  if [[ "$SCENARIO" == drift && "$count" == 2 ]]; then printf '%032d\\n' 2
  else printf '%032d\\n' 1; fi
else
  printf '%s\\n' 'ActiveState=active' 'SubState=running' 'MainPID=123' 'ExecMainStatus=0' 'Result=success' 'Environment=SECRET_MUST_NOT_LEAK' 'ActiveState=SECRET_MUST_NOT_LEAK'
fi
""")
            journal = commands / 'journalctl'
            journal.write_text("""#!/bin/bash
printf '%s\\n' "$*" > "$JOURNAL_ARGS"
if [[ "$SCENARIO" == journal_failure ]]; then exit 1; fi
printf '%s\\n' 'SECRET_MUST_NOT_LEAK' '[hub2-startup-v1] stage=schema_probe_begin elapsed_ms=12' '[hub2-startup-v1] stage=preimport elapsed_ms=0 path=SECRET_MUST_NOT_LEAK' '[hub2-startup-v1] stage=runtime elapsed_ms=19 code=startup_failed' '[hub2-startup-v1] stage=SECRET_MUST_NOT_LEAK elapsed_ms=1'
""")
            for f in (systemctl, journal):
                f.chmod(0o700)
            env = {'PATH': str(commands) + ':/usr/bin:/bin', 'HOME': str(work),
                   'SCENARIO': scenario, 'COUNTER': str(work/'count'),
                   'JOURNAL_ARGS': str(work/'journal-args'), **admission_env()}
            result = subprocess.run(['/bin/bash', '-c',
                'set -euo pipefail\nreport_hub2_startup_failure() {' + body +
                '\n}\nreport_hub2_startup_failure\nexit 37\n'],
                env=env, text=True, capture_output=True, timeout=5, pass_fds=admission_fds())
            args = (work/'journal-args').read_text() if (work/'journal-args').exists() else None
            return result, args

    def test_only_closed_status_and_current_invocation_markers_are_emitted(self):
        result, args = self.run_diagnostic()
        self.assertEqual(result.returncode, 37)
        self.assertIn('MainPID=123', result.stdout)
        self.assertIn('stage=schema_probe_begin elapsed_ms=12', result.stdout)
        self.assertIn('code=startup_failed', result.stdout)
        self.assertNotIn('SECRET', result.stdout + result.stderr)
        self.assertIn('_SYSTEMD_UNIT=agent-hub-hub2.service', args)
        self.assertIn('_SYSTEMD_INVOCATION_ID=' + '0'*31 + '1', args)
        self.assertIn('--lines=100', args)
        self.assertNotIn('--invocation', args)

    def test_changed_invocation_discards_previous_projection(self):
        result, _ = self.run_diagnostic('drift')
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, '[hub2-startup-diagnostic-v1] invocation_changed\n')

    def test_missing_invocation_never_queries_journal(self):
        result, args = self.run_diagnostic('unavailable')
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, '[hub2-startup-diagnostic-v1] unavailable\n')
        self.assertIsNone(args)

    def test_journal_failure_preserves_original_exit_and_no_fabricated_stage(self):
        result, _ = self.run_diagnostic('journal_failure')
        self.assertEqual(result.returncode, 37)
        self.assertNotIn('[hub2-startup-v1]', result.stdout)
        self.assertNotIn('SECRET', result.stdout + result.stderr)

    def test_actual_marker_block_does_not_stringify_errors_or_throw_on_log_failure(self):
        source = (ROOT / 'hub2/node/main.ts').read_text()
        block = source.split('// BEGIN bounded startup diagnostics', 1)[1]
        block = '// BEGIN bounded startup diagnostics' + block.split('// END bounded startup diagnostics.', 1)[0]
        parent = Path(os.environ.get('TMPDIR') or ROOT / '.local/tmp')
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent, prefix='startup-marker-') as tmp:
            script = Path(tmp) / 'marker.ts'
            script.write_text("import {writeSync as realWriteSync} from 'node:fs'; let writeSync = realWriteSync;\n" + block + "\nstartupMarker('schema_probe_end', 4);\n"
                "process.emit('uncaughtExceptionMonitor', new Error('SECRET_MUST_NOT_LEAK'));\n"
                "writeSync = () => { throw new Error('SECRET_MUST_NOT_LEAK'); };\n"
                "startupMarker('runtime');\nprocess.stdout.write('survived');\n")
            result = subprocess.run(['node', '--experimental-strip-types', str(script)],
                env={'PATH': os.environ['PATH'], 'HOME': tmp, 'NODE_NO_WARNINGS': '1', **admission_env()},
                text=True, capture_output=True, timeout=10, pass_fds=admission_fds())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'survived')
        self.assertIn('objects=4', result.stderr)
        self.assertIn('code=startup_failed', result.stderr)
        self.assertNotIn('SECRET', result.stderr)
        for line in result.stderr.splitlines():
            self.assertRegex(line, r'^\[hub2-startup-v1\] stage=(preimport|schema_probe_end) elapsed_ms=[0-9]+( objects=4)?( code=startup_failed)?$')

    def test_actual_timeout_prefix_settles_term_resistant_owned_child(self):
        # Exercise the actual invocation-read timeout argv, not a fake timeout.
        import shlex
        import signal
        import sys
        import time
        source = (ROOT / 'deploy/vm/install-hub2-node.sh').read_text()
        prefix = re.search(r'invocation="\$\((timeout [^\n]+?) systemctl ', source).group(1)
        argv = shlex.split(prefix) + [sys.executable, '-I', '-S', '-B', '-c',
            'import signal,time,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); sys.stdout.write("READY\\n"); sys.stdout.flush(); time.sleep(30)']
        child = None
        timed_out = False
        stdout = b''
        started = time.monotonic()
        try:
            # Cleanup registered before spawn. This Popen is the timeout leader
            # and new session/group owner; no real service or journal is used.
            child = subprocess.Popen(argv, start_new_session=True,
                env={'PATH': os.environ['PATH'], **admission_env()},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=admission_fds())
            try:
                stdout, _ = child.communicate(timeout=2.5)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                stdout, _ = child.communicate(timeout=2)
        self.assertEqual(stdout, b'READY\n')
        self.assertFalse(timed_out, 'TERM-resistant actual diagnostic read did not settle; owned group killed/reaped')
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertNotEqual(child.returncode, 0)
        self.assertEqual(source.count('timeout --kill-after=0.5s 0.5s '), 4)

    def test_stage_order_and_existing_readiness_budget_remain(self):
        source = (ROOT / 'hub2/node/main.ts').read_text()
        markers = ["startupMarker('" + s + "'" for s in
                   ('preimport', 'imports_env', 'schema_probe_begin', 'schema_probe_end', 'runtime', 'listening')]
        self.assertEqual(sorted(source.index(m) for m in markers), [source.index(m) for m in markers])
        self.assertLess(source.index("startupMarker('listening');"),
                        source.index("process.removeListener('uncaughtExceptionMonitor', startupFailure);"))
        installer = (ROOT / 'deploy/vm/install-hub2-node.sh').read_text()
        ready = installer.split('wait_for_hub2_readiness() {', 1)[1]
        self.assertIn('max_wait=15', ready)
        self.assertEqual(ready.count('report_hub2_startup_failure'), 3)
        self.assertEqual(ready.count('return 1'), 3)
        self.assertEqual(ready.count('return 0'), 1)


if __name__ == '__main__':
    unittest.main()
