"""Synthetic fixtures for the M14b allowlisted host status collector.

No operator configuration, no native CLIs, no network: sessions arrive as
structured input, report files live in a per-test TMPDIR.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import status_collect


def allowlist(machine='host-fixture', session_roots=(), report_roots=(), sessions=(), reports=()):
    return {'machine_id': machine, 'session_roots': list(session_roots),
            'report_roots': list(report_roots),
            'sessions': list(sessions), 'reports': list(reports)}


class TestStatusCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='status-collect-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'ws'
        self.ws.mkdir()

    def test_classify_precedence_and_unknown_default(self):
        now = '2026-10-17T00:00:00.000Z'
        al = allowlist(session_roots=[str(self.root)],
                       sessions=[{'native_id': 'n%d' % i, 'workspace': str(self.ws)} for i in range(6)])
        sessions = [
            {'native_id': 'n0', 'workspace': str(self.ws), 'limit_exhausted': True, 'working': True},
            {'native_id': 'n1', 'workspace': str(self.ws), 'context_low': True, 'working': True},
            {'native_id': 'n2', 'workspace': str(self.ws), 'draft_pending': True},
            {'native_id': 'n3', 'workspace': str(self.ws), 'working': True},
            {'native_id': 'n4', 'workspace': str(self.ws)},
            {'native_id': 'n5', 'workspace': str(self.ws), 'transcript': 'Weekly Limit Exhausted!!!', 'phase': 'running'},
        ]
        result = status_collect.collect_once(sessions, al, 'host-fixture', now)
        states = {o['native_id']: o['payload']['state'] for o in result['observations']}
        self.assertEqual(states, {'n0': 'limit_hit', 'n1': 'context_low', 'n2': 'draft_pending',
                                 'n3': 'working', 'n4': 'unknown', 'n5': 'unknown'})
        # Free-text transcript markers must never drive classification.
        self.assertNotIn('transcript', json.dumps(result['observations']))

    def test_unregistered_and_outside_allowlist_sessions_skipped(self):
        now = '2026-10-17T00:00:00.000Z'
        outside = self.root / 'elsewhere'
        outside.mkdir()
        al = allowlist(session_roots=[str(self.ws)],
                       sessions=[{'native_id': 'ok', 'workspace': str(self.ws)},
                                 {'native_id': 'escape', 'workspace': str(outside)}])
        sessions = [
            {'native_id': 'ok', 'workspace': str(self.ws), 'working': True},
            {'native_id': 'stranger', 'workspace': str(self.ws), 'working': True},
            {'native_id': 'escape', 'workspace': str(outside), 'working': True},
        ]
        result = status_collect.collect_once(sessions, al, 'host-fixture', now)
        self.assertEqual([o['native_id'] for o in result['observations']], ['ok'])
        reasons = {s.get('native_id'): s['reason'] for s in result['skipped']}
        self.assertEqual(reasons['stranger'], 'unregistered_session')
        self.assertEqual(reasons['escape'], 'workspace_outside_allowlist')

    def test_headline_first_heading_only_sanitized(self):
        report = self.root / 'report.md'
        report.write_text('# Synthetic headline \x1b[2J with hubt_leaked_secret_token and more\n\n' + 'x' * 5000 + '\nbody hubr_other_secret\n', encoding='utf-8')
        al = allowlist(session_roots=[str(self.root)], report_roots=[str(self.root)],
                       sessions=[{'native_id': 'n1', 'workspace': str(self.ws)}],
                       reports=[{'path': str(report), 'for_native_id': 'n1', 'verdict': 'done'}])
        result = status_collect.collect_once(
            [{'native_id': 'n1', 'workspace': str(self.ws), 'working': True}], al, 'host-fixture',
            '2026-10-17T00:00:00.000Z')
        kinds = {o['kind']: o for o in result['observations']}
        self.assertIn('report', kinds)
        headline = kinds['report']['payload']['headline']
        self.assertTrue(headline.startswith('Synthetic headline'))
        self.assertNotIn('\x1b', headline)
        self.assertNotIn('hubt_leaked_secret_token', headline)
        self.assertNotIn('hubr_other_secret', json.dumps(result['observations']))
        self.assertLessEqual(len(headline), status_collect.MAX_HEADLINE)
        self.assertEqual(kinds['report']['payload']['verdict'], 'done')

    def test_report_outside_allowlist_or_missing_skipped(self):
        other = Path(tempfile.mkdtemp(prefix='status-collect-outside-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(other, ignore_errors=True))
        stray = other / 'stray.md'
        stray.write_text('# Stray\n', encoding='utf-8')
        al = allowlist(session_roots=[str(self.root)], report_roots=[str(self.root)],
                       sessions=[{'native_id': 'n1', 'workspace': str(self.ws)}],
                       reports=[{'path': str(stray), 'for_native_id': 'n1'},
                                {'path': str(self.root / 'absent.md'), 'for_native_id': 'n1'},
                                {'path': str(self.root / 'orphan.md'), 'for_native_id': 'ghost'}])
        (self.root / 'orphan.md').write_text('# Orphan\n', encoding='utf-8')
        result = status_collect.collect_once(
            [{'native_id': 'n1', 'workspace': str(self.ws)}], al, 'host-fixture',
            '2026-10-17T00:00:00.000Z')
        self.assertEqual([o['kind'] for o in result['observations']], ['session'])
        reasons = sorted(s['reason'] for s in result['skipped'])
        self.assertIn('report_outside_allowlist', reasons)
        self.assertIn('headline_unreadable', reasons)
        self.assertIn('report_session_not_collected', reasons)

    def test_allowlist_absent_fails_closed_in_main(self):
        sessions_file = self.root / 'sessions.json'
        sessions_file.write_text('[]', encoding='utf-8')
        with self.assertRaises(SystemExit) as ctx:
            status_collect.main(['--once', '--allowlist', str(self.root / 'absent.json'),
                                 '--sessions-json', str(sessions_file)])
        self.assertEqual(ctx.exception.code, 2)

    def test_once_entrypoint_prints_bounded_observations(self):
        al = allowlist(machine='host-cli', session_roots=[str(self.root)],
                       sessions=[{'native_id': 'n1', 'workspace': str(self.ws)}])
        allow_path = self.root / 'allow.json'
        allow_path.write_text(json.dumps(al), encoding='utf-8')
        sessions_path = self.root / 'sessions.json'
        sessions_path.write_text(json.dumps([{'native_id': 'n1', 'workspace': str(self.ws),
                                                      'limit_exhausted': True}]), encoding='utf-8')
        out = io.StringIO()
        with redirect_stdout(out):
            result = status_collect.main(['--once', '--allowlist', str(allow_path),
                                          '--sessions-json', str(sessions_path)])
        printed = json.loads(out.getvalue())
        self.assertEqual(printed['observations'][0]['payload'], {'state': 'limit_hit'})
        self.assertEqual(printed['observations'][0]['machine_id'], 'host-cli')
        self.assertEqual(result, printed)

    def test_submit_posts_each_observation_without_token_leak(self):
        seen = []

        class FakeResp:
            status = 200

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append(req)
            return FakeResp()

        obs = [{'machine_id': 'm', 'native_id': 'n', 'kind': 'session',
                'observed_at': '2026-10-17T00:00:00.000Z', 'payload': {'state': 'working'}}]
        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            results = status_collect.submit(obs, 'https://hub.test', 'hubt_secret_submit_token')
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].full_url.endswith('/api/status/observations'))
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer hubt_secret_submit_token')
        sent = json.loads(seen[0].data.decode('utf-8'))
        self.assertNotIn('transcript', json.dumps(sent))
        self.assertEqual(results[0]['status'], 200)


if __name__ == '__main__':
    unittest.main()
