"""Hold zcodex routing after a session dies; release only on a real probe pass.

A zcodex session whose leader died with surviving children (cgroup OOM kill,
reconnect exhaustion exit) is reported by the progress sampler as terminal
error `zcodex_agent_exited`. The first NEW such failure marks the engine
unavailable on this host (`zcodex_session_failed`); a bounded real
`zcodex exec` turn retires only that hold (`zcodex_probe_passed`). Intake
de6f2139.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

ENGINE = 'zcodex'
FAIL_REASON = 'zcodex_session_failed'
PASS_REASON = 'zcodex_probe_passed'
FAILURES = ('zcodex_agent_exited', 'zcodex_reconnect_failed')
PROBE_TIMEOUT_S = 150
PROBE_INTERVAL_S = 300


def real_probe(timeout=PROBE_TIMEOUT_S):
    """One tiny exec turn in its own capped scope; never inherits the session's."""
    exe = shutil.which('zcodex')
    if not exe:
        return False
    argv = [exe, 'exec', 'reply with the single word ok']
    if shutil.which('systemd-run'):
        argv = ['systemd-run', '--user', '--scope', '--quiet', '-p', 'MemoryMax=2G'] + argv
    env = dict(os.environ, CODEX_HOME=str(Path.home() / '.zcodex'))
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, env=env)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class ZcodexHealth:
    def __init__(self, request, state_path=None, probe=real_probe, now=time.time, threaded=True):
        self.request, self.probe, self.now, self.threaded = request, probe, now, threaded
        self._thread, self._result = None, None
        self.state_path = Path(state_path) if state_path else None
        self.seen, self.held, self.last_probe, self.baselined = set(), False, 0.0, False
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                self.seen = {tuple(x) for x in saved.get('seen', [])}
                self.held = saved.get('held') is True
                self.baselined = True
            except (OSError, ValueError, TypeError):
                pass

    def _save(self):
        if self.state_path:
            try:
                self.state_path.write_text(json.dumps({'seen': sorted(self.seen), 'held': self.held}))
            except OSError:
                pass

    def observe(self, catalog, progress):
        """catalog rows + sampler progress -> hold on new failure, probe to release."""
        failed = set()
        for row in catalog:
            if row.get('engine') != ENGINE:
                continue
            terminal = (progress.get(row.get('id')) or {}).get('terminal_error') in FAILURES
            # Headless sessions use the ordinary Fleet exit projection, not
            # the interactive-only terminal_error producer. A confirmed exit
            # 75 is reconnect exhaustion; a confirmed OOM is a runtime failure.
            # Capture silence, a live process and an operator SIGTERM are not.
            exit_fact = row.get('exit')
            confirmed = (row.get('phase') or row.get('state')) in ('exited', 'exiting')
            runtime_exit = (confirmed and isinstance(exit_fact, dict)
                            and ((type(exit_fact.get('code')) is int and exit_fact['code'] == 75)
                                 or exit_fact.get('oom_killed') is True))
            if terminal or runtime_exit:
                failed.add((row.get('id'), row.get('created_at_ms')))
        new = failed - self.seen
        if not self.baselined:
            # Corpses that predate this monitor must not hold routing.
            new = set()
            self.baselined = True
        self.seen = failed | (self.seen & {(r.get('id'), r.get('created_at_ms')) for r in catalog})
        if new and not self.held:
            try:
                self.request('POST', '/api/fleet/probe', {'engine': ENGINE, 'status': 'unavailable', 'reason': FAIL_REASON})
                self.held = True
            except Exception:
                self.seen -= new  # retry next cycle
        elif self.held:
            if self._result is None and self._thread is None and self.now() - self.last_probe >= PROBE_INTERVAL_S:
                self.last_probe = self.now()
                if self.threaded:
                    # The probe can take minutes; never stall the catalog cycle.
                    def run():
                        self._result = bool(self.probe())
                    self._thread = threading.Thread(target=run, daemon=True)
                    self._thread.start()
                else:
                    self._result = bool(self.probe())
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
            if self._result is not None and self._thread is None:
                passed, self._result = self._result, None
                if passed:
                    try:
                        self.request('POST', '/api/fleet/probe', {'engine': ENGINE, 'status': 'ok', 'reason': PASS_REASON})
                        self.held = False
                    except Exception:
                        pass
        self._save()
        return self.held
