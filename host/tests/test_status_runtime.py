"""Tests for the maintained StatusCollector runtime (native collection + probes).

Fake native list + fake probe runner; no network, no real aplexer, no Hub.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host import status_runtime
from agent_hub_host.native_sessions import NativeSession


def allowlist(machine='host-rt', roots=(), sessions=(), extra=None, **kw):
    al = {'machine_id': machine, 'session_roots': list(roots),
          'report_roots': list(roots), 'sessions': list(sessions), 'reports': []}
    if extra:
        al.update(extra)
    al.update(kw)
    return al


def native_row(sid, ws, agent_state=None, **kw):
    return NativeSession(session_id=sid, tag='t-' + sid[-4:], workspace=ws,
                         engine='opencode', profile=None, phase='running',
                         agent_state=agent_state, last_activity_ms=None,
                         created_at_ms=None, cwd=None, containment_empty=None,
                         workload_cgroup=None, worker_pid=None,
                         workload_pid=None, processes=None, **kw)


class TestResolveAllowlist(unittest.TestCase):
    def test_explicit_wins_over_env(self):
        with patch.dict(os.environ, {'HUB_STATUS_COLLECT_ALLOWLIST': '/env/allow.json'}):
            self.assertEqual(status_runtime.resolve_allowlist_path('/x/allow.json'), '/x/allow.json')
            self.assertEqual(status_runtime.resolve_allowlist_path(), '/env/allow.json')


class TestNativeMapping(unittest.TestCase):
    def test_exact_agent_state_maps_otherwise_unknown(self):
        self.assertTrue(status_runtime.native_to_entry(
            SimpleNamespace(session_id='s1', workspace='/w', agent_state='working'))['working'])
        self.assertTrue(status_runtime.native_to_entry(
            SimpleNamespace(session_id='s1', workspace='/w', agent_state='idle'))['idle'])
        entry = status_runtime.native_to_entry(
            SimpleNamespace(session_id='s1', workspace='/w', agent_state='busy-typing'))
        self.assertNotIn('working', entry)
        self.assertEqual(set(entry), {'native_id', 'workspace'})

    def test_no_draft_synthesis(self):
        entry = status_runtime.native_to_entry(
            SimpleNamespace(session_id='s1', workspace='/w', agent_state='working'))
        self.assertNotIn('draft_pending', entry)
        self.assertNotIn('finished', entry)


class TestCollectorStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='status-runtime-')
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name))
        self.ws = os.path.join(self.root, 'ws')
        os.mkdir(self.ws)

    def collector(self, sessions, **kw):
        extra = kw.pop('extra_al', {})
        al = allowlist(roots=[self.root],
                       sessions=[{'native_id': s} for s in sessions], extra=extra)
        rows = [native_row(s, self.ws, kw.pop('agent_state', None)) for s in sessions]
        kw.setdefault('allowlist', al)
        kw.setdefault('list_sessions_fn', lambda: rows)
        kw.setdefault('min_interval_s', 0)
        return status_runtime.StatusCollector(**kw), al

    def test_native_collection_registered_only_unknown_default(self):
        col, _ = self.collector(['n1'])
        # Inject an unregistered row: must be skipped with zero observations.
        col.list_sessions_fn = lambda: [native_row('n1', self.ws, 'working'),
                                        native_row('ghost', self.ws, 'working')]
        with patch('agent_hub_host.native_sessions.qualify_sender_binding',
                   side_effect=Exception('unbound')):
            result = col.step()
        self.assertEqual([o['native_id'] for o in result['observations']
                          if o['kind'] == 'session'], ['n1'])
        self.assertEqual(result['observations'][0]['payload'], {'state': 'working'})
        self.assertTrue(any(s.get('native_id') == 'ghost' for s in result['skipped']))
        self.assertIn('sender_unbound:Exception', result['errors'])

    def test_limit_only_on_explicit_marker(self):
        al = allowlist(roots=[self.root], sessions=[{'native_id': 'n1'}])
        col = status_runtime.StatusCollector(
            allowlist=al, list_sessions_fn=lambda: [], min_interval_s=0,
            time_fn=lambda: 0.0)
        with patch('agent_hub_host.native_sessions.qualify_sender_binding',
                   side_effect=Exception('unbound')):
            result = col.step(prepared_sessions=[
                {'native_id': 'n1', 'workspace': self.ws, 'limit_exhausted': True}])
        self.assertEqual(result['observations'][0]['payload'], {'state': 'limit_hit'})
        with patch('agent_hub_host.native_sessions.qualify_sender_binding',
                   side_effect=Exception('unbound')):
            result2 = col.step(force=True, prepared_sessions=[
                {'native_id': 'n1', 'workspace': self.ws, 'agent_state': 'busy'}])
        self.assertEqual(result2['observations'][0]['payload'], {'state': 'unknown'})

    def test_rate_limited_step(self):
        col, _ = self.collector(['n1'], min_interval_s=60, time_fn=lambda: 100.0)
        with patch('agent_hub_host.native_sessions.qualify_sender_binding',
                   side_effect=Exception('unbound')):
            first = col.step()
            second = col.step()
            third = col.step(force=True)
        self.assertFalse(first['rate_limited'])
        self.assertTrue(second['rate_limited'])
        self.assertEqual(second['observations'], [])
        self.assertFalse(third['rate_limited'])

    def test_isolated_list_failure(self):
        al = allowlist(roots=[self.root], sessions=[{'native_id': 'n1'}])
        col = status_runtime.StatusCollector(
            allowlist=al, list_sessions_fn=lambda: (_ for _ in ()).throw(RuntimeError('boom')),
            min_interval_s=0)
        with patch('agent_hub_host.native_sessions.qualify_sender_binding',
                   side_effect=Exception('unbound')):
            result = col.step()
        self.assertEqual(result['observations'], [])
        self.assertTrue(any('native_list_failed' in e for e in result['errors']))

    def test_probes_integrated(self):
        sha = 'a' * 40
        extra = {'repo_roots': [self.root],
                 'probes': [{'id': 'g1', 'kind': 'git_ancestry', 'cwd': self.root,
                             'for_native_id': 'n1', 'sha': sha,
                             'argv': ['git', 'rev-parse', 'HEAD']}]}
        col, _ = self.collector(['n1'], extra_al=extra)
        mapping = {(('git', 'rev-parse', 'HEAD')): (0, 'x\n')}
        # Fake probe runner answering the ancestry sub-calls.
        def fake_probe(argv, cwd, timeout_s):
            key = tuple(argv)
            if key == ('git', 'rev-parse', 'HEAD'):
                return {'exit': 0, 'stdout': sha + '\n', 'stderr': ''}
            if key == ('git', 'rev-parse', 'origin/main'):
                return {'exit': 0, 'stdout': sha + '\n', 'stderr': ''}
            if key == ('git', 'ls-remote', 'origin', 'refs/heads/main'):
                return {'exit': 0, 'stdout': sha + '\trefs/heads/main\n', 'stderr': ''}
            if key[:2] == ('git', 'merge-base'):
                return {'exit': 0, 'stdout': '', 'stderr': ''}
            return {'exit': 99, 'stdout': '', 'stderr': 'unexpected'}
        col.probe_runner = fake_probe
        with patch('agent_hub_host.native_sessions.qualify_sender_binding',
                   side_effect=Exception('unbound')):
            result = col.step()
        kinds = {o['kind'] for o in result['observations']}
        self.assertIn('session', kinds)
        self.assertIn('probe_git', kinds)

    def test_main_native_default_uses_adapter(self):
        import io
        from contextlib import redirect_stdout
        from agent_hub_host import status_collect
        al = allowlist(roots=[self.root], sessions=[{'native_id': 'n1'}])
        allow_path = os.path.join(self.root, 'allow.json')
        with open(allow_path, 'w', encoding='utf-8') as handle:
            json.dump(al, handle)
        rows = [native_row('n1', self.ws, 'idle')]
        out = io.StringIO()
        with patch('agent_hub_host.native_sessions.list_sessions', return_value=rows):
            with patch('agent_hub_host.native_sessions.qualify_sender_binding',
                       side_effect=Exception('unbound')):
                with redirect_stdout(out):
                    result = status_collect.main(['--once', '--allowlist', allow_path])
        self.assertEqual(result['observations'][0]['payload'], {'state': 'idle'})
        self.assertEqual(json.loads(out.getvalue())['observations'][0]['native_id'], 'n1')


if __name__ == '__main__':
    unittest.main()
