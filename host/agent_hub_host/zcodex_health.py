"""Hold zcodex routing after a session dies; release only on a real probe pass.

Confirmed reconnect-exhaustion exits and native OOM failures hold this engine
until a real probe passes. Catalog silence and an unqualified dead-leader marker
are never failure proof. Exact-incarnation retirement evidence comes from the
existing Fleet verifier, including sessions already removed from the catalog.
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
MAX_SEEN = 500
_INIT_LOCK = threading.Lock()
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


def monitor_for(dispatcher):
    """One monitor for both the catalog and terminal-reconciliation threads."""
    with _INIT_LOCK:
        monitor = getattr(dispatcher, '_zcodex_health', None)
        if monitor is None:
            journal = getattr(dispatcher, 'journal_dir', None)
            monitor = ZcodexHealth(dispatcher.request,
                Path(journal) / 'zcodex-health.json' if journal else None)
            dispatcher._zcodex_health = monitor
        return monitor


def observe_verified_terminal(dispatcher, record, target, evidence):
    """Called only after Fleet's positive retirement/retained-exit verifier.

    Recheck the exact launch tuple; a missing native or a journal assertion
    alone can never reach this path or establish failure.
    """
    if record.get('engine') != ENGINE:
        return False
    from .retired_native import launch_workspace_targets
    from .native_sessions import SessionControlError
    try:
        target = target.validated()
        if (record.get('engine') != ENGINE or target.engine != ENGINE
                or target.session_id != record.get('native_id')
                or target.tag != record.get('tag')
                or target.created_at_ms != record.get('incarnation')
                or type(target.created_at_ms) is not int or target.created_at_ms <= 0
                or target.workspace not in launch_workspace_targets(record)
                or not isinstance(evidence, dict)
                or type(evidence.get('finished_at_ms')) is not int
                or evidence['finished_at_ms'] < target.created_at_ms):
            return False
    except (ValueError, TypeError, AttributeError, SessionControlError):
        return False
    return monitor_for(dispatcher).observe_verified_exit(target, evidence)


class ZcodexHealth:
    def __init__(self, request, state_path=None, probe=real_probe, now=time.time, threaded=True):
        self.request, self.probe, self.now, self.threaded = request, probe, now, threaded
        self._thread, self._result = None, None
        self._generation = 0
        self.pending = False
        self._lock = threading.RLock()
        self.state_path = Path(state_path) if state_path else None
        self.seen, self.held, self.last_probe, self.baselined = set(), False, 0.0, False
        if self.state_path and self.state_path.exists():
            try:
                from . import private_fs
                saved = json.loads(private_fs.read_bytes(self.state_path, 128 * 1024, private=False))
                self.seen = {tuple(x) for x in saved.get('seen', [])[-MAX_SEEN:]
                             if isinstance(x, list) and len(x) == 2 and isinstance(x[0], str)
                             and 0 < len(x[0]) <= 160 and x[0].isascii()
                             and all(c.isalnum() or c in '-_.:' for c in x[0])
                             and type(x[1]) is int and x[1] > 0}
                self.held = saved.get('held') is True
                self.pending = saved.get('pending') is True
                self.baselined = True
            except (OSError, ValueError, TypeError):
                pass

    def _save(self):
        if self.state_path:
            try:
                from . import private_fs
                raw = json.dumps({'seen': sorted(self.seen), 'held': self.held, 'pending': self.pending}).encode()
                private_fs.atomic_write(self.state_path, raw, 128 * 1024, engine_owned=True)
            except (OSError, ValueError):
                pass

    @staticmethod
    def _runtime_failure(exit_fact):
        return (isinstance(exit_fact, dict)
                and ((type(exit_fact.get('code')) is int and exit_fact['code'] == 75)
                     or exit_fact.get('oom_killed') is True))

    def observe(self, catalog, progress):
        """Only positive terminal exit facts qualify; never a progress marker alone."""
        with self._lock:
            failed = set()
            for row in catalog:
                sid, incarnation = row.get('id'), row.get('created_at_ms')
                if (row.get('engine') == ENGINE and isinstance(sid, str)
                        and 0 < len(sid) <= 160 and sid.isascii()
                        and all(c.isalnum() or c in '-_.:' for c in sid)
                        and type(incarnation) is int and incarnation > 0
                        and (row.get('phase') or row.get('state')) in ('exited', 'exiting', 'failed', 'dead')
                        and self._runtime_failure(row.get('exit'))):
                    failed.add((sid, incarnation))
            return self._update(failed, baseline=True)

    def observe_verified_exit(self, target, evidence):
        """Fresh qualified owned retirement receipts are not historical catalog corpses."""
        with self._lock:
            if not self._runtime_failure(evidence.get('exit')):
                return self.held
            return self._update({(target.session_id, target.created_at_ms)}, baseline=False)

    def _update(self, failed, *, baseline):
        new = failed - self.seen
        if baseline and not self.baselined:
            # Corpses that predate this monitor must not hold routing.
            new = set()
            self.baselined = True
        # Retired identities must survive catalog disappearance and ACK replay.
        self.seen = set(sorted(self.seen | failed, key=lambda key: (key[1], key[0]))[-MAX_SEEN:])
        if new:
            self._generation += 1
            self.held, self.pending = True, True
            # The owned launch may settle immediately after this observation.
            # Persist the publication obligation before attempting the RPC.
            self._save()
        if self.pending:
            try:
                self.request('POST', '/api/fleet/probe', {'engine': ENGINE, 'status': 'unavailable', 'reason': FAIL_REASON})
                self.pending = False
            except Exception:
                pass  # durable pending hold is retried even without the old receipt
        elif self.held:
            if self._result is None and self._thread is None and self.now() - self.last_probe >= PROBE_INTERVAL_S:
                self.last_probe = self.now()
                if self.threaded:
                    # The probe can take minutes; never stall the catalog cycle.
                    generation = self._generation
                    def run():
                        self._result = (generation, bool(self.probe()))
                    self._thread = threading.Thread(target=run, daemon=True)
                    self._thread.start()
                else:
                    self._result = (self._generation, bool(self.probe()))
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
            if self._result is not None and self._thread is None:
                (generation, passed), self._result = self._result, None
                if passed and generation == self._generation:
                    try:
                        self.request('POST', '/api/fleet/probe', {'engine': ENGINE, 'status': 'ok', 'reason': PASS_REASON})
                        self.held = False
                    except Exception:
                        pass
        self._save()
        return self.held
