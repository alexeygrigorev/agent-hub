"""Regression: engine exits without result JSON -> failed-with-partial-work.

Reproduces intake a19cac9e: opencode/muse exits with no .local/hub-results
file after doing work. Asserts the new behavior:
- generic "exact runtime cause is unavailable" never appears,
- runtime diagnostic carries exit code, signal and wall time,
- missing result JSON yields failed (never done) with a partial-work report
  listing commits/dirty files and the per-session log location.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host.runtime_diagnostics import runtime_diagnostic, _sanitize_tail
from agent_hub_host import launch_diagnostics as ld
from agent_hub_host.fleet import Dispatcher, _terminal_fleet_state


NATIVE = '2505c5ae-605e-426b-8908-a5d293bdc4c8'
RECORD = dict(native_id=NATIVE, incarnation=1000000)
TOMB = dict(finished_at_ms=3000000)
FORBIDDEN = 'exact runtime cause is unavailable'


def _dispatcher(tmp):
    settings = {
        "version": 1,
        "workspaces": {"main": tmp},
        "subscriptions": [],
        "aplexer_bin": "aplexer",
    }
    d = Dispatcher(settings)
    d._active_launches = {}
    return d


class NoResultRegression(unittest.TestCase):
    def test_generic_phrase_gone_and_wall_time_present(self):
        for info in ({}, {'code': 1}, {'signal': 9}, {'code': 0}):
            result = runtime_diagnostic(dict(RECORD, runtime_exit=info), TOMB,
                                       query=lambda a: '')
            self.assertNotIn(FORBIDDEN, result['message'])
            self.assertIn('wall_time_ms', result)
            self.assertEqual(result['wall_time_ms'], 2000000)
        result = runtime_diagnostic(dict(RECORD, runtime_exit={'code': 3}), TOMB,
                                   query=lambda a: '')
        self.assertEqual(result['exit_code'], 3)
        result = runtime_diagnostic(dict(RECORD, runtime_exit={'signal': 9}), TOMB,
                                   query=lambda a: '')
        self.assertEqual(result['signal'], 9)
        self.assertEqual(result['reason'], 'engine_signal_exit')

    def test_tail_is_sanitized(self):
        result = runtime_diagnostic(
            dict(RECORD, runtime_exit={'code': 1},
                 runtime_tail='Bearer abcdefghijklmnop123456\nnormal line'),
            TOMB, query=lambda a: '')
        self.assertNotIn('abcdefghijklmnop123456', result.get('tail', ''))
        self.assertIn('normal line', result.get('tail', ''))

    def test_tail_matches_server_charset_and_redacts_secret_shapes(self):
        # Server rejects [\x00-\x1f\x7f] in diagnostic.tail: the host tail
        # must never contain newlines, tabs or other control chars.
        tail = _sanitize_tail('line one\nline two\ttabbed\nBearer abcdefghijklmnop\n'
                              'key=ghp_abcdefghijklmnop1234\nAKIAIOSFODNN7EXAMPLE\n'
                              'xoxb-123456789012-abcdefghij\nsk-ant-abcdef1234567890')
        self.assertNotRegex(tail, r'[\x00-\x1f\x7f]')
        self.assertNotIn('\n', tail)
        self.assertNotIn('\t', tail)
        self.assertIn('line one | line two', tail)
        for secret in ('abcdefghijklmnop', 'ghp_abcdefghijklmnop1234', 'AKIAIOSFODNN7EXAMPLE',
                       'xoxb-123456789012-abcdefghij', 'sk-ant-abcdef1234567890'):
            self.assertNotIn(secret, tail)
        # runtime_diagnostic tails also satisfy the server predicate.
        result = runtime_diagnostic(
            dict(RECORD, runtime_exit={'code': 1}, runtime_tail='a\nb'),
            TOMB, query=lambda a: '')
        self.assertNotRegex(result.get('tail', 'x'), r'[\x00-\x1f\x7f]')
        self.assertLessEqual(len(result.get('tail', '')), 1024)

    def test_terminal_state_never_done_without_result(self):
        self.assertEqual(_terminal_fleet_state('failed', True), 'failed')
        self.assertEqual(_terminal_fleet_state('failed', False), 'failed')
        self.assertEqual(_terminal_fleet_state('completed-awaiting-review', False), 'failed')
        self.assertEqual(_terminal_fleet_state('accepted', False), 'failed')
        self.assertEqual(_terminal_fleet_state('completed-awaiting-review', True), 'exited')
        self.assertEqual(_terminal_fleet_state('accepted', True), 'exited')
        self.assertEqual(_terminal_fleet_state('completed-awaiting-review', True,
                                               'engine_tool_permission_refused'), 'failed')

    def test_retain_is_opencode_scoped(self):
        self.assertIsNone(ld.retain_per_session_opencode_log({}, '/tmp/x.log', engine='opencode'))
        self.assertIsNone(ld.retain_per_session_opencode_log(
            {'/tmp/log': 0}, '/tmp/x.log', engine='codex'))
        self.assertIsNone(ld.retain_per_session_opencode_log(None, '/tmp/x.log'))

    def test_cleanup_roots_send_log_to_journal(self):
        import tempfile
        from agent_hub_host.fleet import _sanitize_launcher_slug
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name) / 'ws'
        ws.mkdir()
        d = Dispatcher({"version": 1, "workspaces": {"main": tmp.name},
                        "subscriptions": [], "aplexer_bin": "aplexer",
                        "cleanup_roots": [str(ws)], "journal_dir": tmp.name})
        self.assertTrue(d._cleanup_will_remove(str(ws)))
        self.assertFalse(d._cleanup_will_remove(tmp.name))
        dest, label = d._session_log_dest({'workspace': str(ws), 'offer_id': 'abc'})
        self.assertIn('fleet-logs', dest)
        self.assertIn('host journal', label)
        dest2, label2 = Dispatcher({"version": 1, "workspaces": {"main": tmp.name},
                                    "subscriptions": [], "aplexer_bin": "aplexer"}
                                   )._session_log_dest({'workspace': str(ws), 'offer_id': 'abc'})
        self.assertIn('.local/hub-logs', dest2)
        # Secret-shaped launcher slugs are dropped, plain slugs survive.
        self.assertEqual(_sanitize_launcher_slug('ghp_abcdefghijklmnop1234'), '')
        self.assertEqual(_sanitize_launcher_slug('sk-ant-abcdef1234567890'), '')
        self.assertEqual(_sanitize_launcher_slug('quota_exhausted'), 'quota_exhausted')

    def test_missing_result_json_is_failed_with_partial_work(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name) / 'ws'
        ws.mkdir()
        subprocess.run(['git', 'init', '-q', str(ws)], check=True)
        subprocess.run(['git', 'config', 'user.email', 't@t.t'], cwd=str(ws), check=True)
        subprocess.run(['git', 'config', 'user.name', 't'], cwd=str(ws), check=True)
        (ws / 'work.txt').write_text('partial work')
        subprocess.run(['git', 'add', 'work.txt'], cwd=str(ws), check=True)
        subprocess.run(['git', 'commit', '-qm', 'partial commit'], cwd=str(ws), check=True)
        (ws / 'dirty.txt').write_text('uncommitted')
        d = _dispatcher(tmp.name)
        offer = '11111111-2222-4333-8444-555555555555'
        record = {'offer_id': offer, 'workspace': str(ws),
                  'result_path': str(ws / '.local' / 'hub-results' / (offer + '.json')),
                  'log_offsets': {}, 'launched_at_ts': 1000.0}
        partial = d._no_result_partial_report(record)
        self.assertEqual(partial['status'], 'failed')
        self.assertNotEqual(partial['status'], 'done')
        self.assertIn('partial', partial['report'].lower())
        self.assertIn('partial commit', partial['report'])
        self.assertIn('dirty.txt', partial['report'])
        # Per-session log dir helper never raises and returns None/str.
        dest = str(ws / '.local' / 'hub-logs' / (offer + '-opencode.log'))
        self.assertIn(ld.retain_per_session_opencode_log({}, dest), (None, dest))
        # describe_partial_work sees the dirty file and commit.
        info = ld.describe_partial_work(str(ws))
        self.assertTrue(any('dirty.txt' in f for f in info['dirty']))
        self.assertTrue(any('partial commit' in c for c in info['commits']))

    def test_no_secrets_in_partial_artifacts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = _dispatcher(tmp.name)
        record = {'offer_id': 'x', 'workspace': tmp.name,
                  'result_path': tmp.name + '/nope.json', 'log_offsets': {}}
        with patch.object(ld, 'describe_partial_work',
                          return_value={'dirty': ['sk-abcdef1234567890 file'], 'commits': []}):
            partial = d._no_result_partial_report(record)
        blob = json.dumps(partial)
        self.assertNotIn('sk-abcdef1234567890', blob)

    def test_children_running_reason_when_cgroup_not_empty(self):
        # Intake a19cac9e ddf55d00: terminal row retired with a non-empty
        # cgroup and no exit code/signal -> engine_children_running, never
        # the generic phrase, with wall time and tail preserved.
        rec = dict(RECORD, containment_empty=False)
        result = runtime_diagnostic(rec, TOMB, query=lambda a: '')
        self.assertEqual(result['reason'], 'engine_children_running')
        self.assertNotIn(FORBIDDEN, result['message'])
        self.assertIn('wall_time_ms', result)
        self.assertNotIn('exit_code', result)
        # A real signal keeps its own reason (precedence).
        rec_sig = dict(RECORD, containment_empty=False, runtime_exit={'signal': 6})
        self.assertEqual(runtime_diagnostic(rec_sig, TOMB, query=lambda a: '')['reason'], 'engine_signal_exit')
        # A real exit code keeps its own reason (precedence).
        rec_code = dict(RECORD, containment_empty=False, runtime_exit={'code': 1})
        self.assertEqual(runtime_diagnostic(rec_code, TOMB, query=lambda a: '')['reason'], 'engine_exit_code')
        # Clean containment without metadata stays generic (but never the
        # forbidden phrase).
        rec_clean = dict(RECORD, containment_empty=True)
        generic = runtime_diagnostic(rec_clean, TOMB, query=lambda a: '')
        self.assertEqual(generic['reason'], 'engine_exited_without_result')
        self.assertNotIn(FORBIDDEN, generic['message'])

    def test_crash_tail_extracts_bun_panic_and_redacts(self):
        screen = ('Args: "/node/opencode" run --standalone\n'
                  'Elapsed: 555503ms | User: 1521ms | Sys: 634ms\n'
                  'RSS: 97.25 MB | Peak: 98.84 MB\n'
                  'panic(main thread): abort() called\n'
                  'oh no: Bun has crashed. report Bearer abcdefghijklmnop123456\n')
        tail = ld.extract_crash_tail(screen)
        self.assertIn('Bun has crashed', tail)
        self.assertIn('panic', tail)
        self.assertNotIn('abcdefghijklmnop123456', tail)
        self.assertNotRegex(tail, r'[\x00-\x1f\x7f]')
        self.assertLessEqual(len(tail), 512)
        self.assertEqual(ld.extract_crash_tail('watcher started\nall quiet\n'), '')
        self.assertEqual(ld.extract_crash_tail(''), '')

    def test_read_screen_crash_tail_guards_paths(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        nid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        sdir = Path(tmp.name) / 'state'
        (sdir / 'sessions' / nid).mkdir(parents=True)
        (sdir / 'sessions' / nid / 'screen.txt').write_text(
            'Elapsed: 10ms\npanic(main thread): abort() called\noh no: Bun has crashed.\n')
        tail = ld.read_native_screen_crash_tail(str(sdir), nid)
        self.assertIn('Bun has crashed', tail)
        # Unknown UUID, malformed UUID and symlinks yield nothing.
        self.assertEqual(ld.read_native_screen_crash_tail(str(sdir), 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff'), '')
        self.assertEqual(ld.read_native_screen_crash_tail(str(sdir), 'not-a-uuid'), '')
        link_dir = sdir / 'retired-sessions' / nid
        link_dir.mkdir(parents=True)
        (sdir / 'sessions' / nid / 'screen.txt').rename(link_dir / 'real.txt')
        try:
            (link_dir / 'screen.txt').symlink_to(link_dir / 'real.txt')
        except OSError:
            pass
        else:
            self.assertEqual(ld.read_native_screen_crash_tail(str(sdir), nid), '')

    def test_partial_report_carries_native_crash_evidence(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name) / 'ws'
        ws.mkdir()
        subprocess.run(['git', 'init', '-q', str(ws)], check=True)
        nid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        sdir = Path(tmp.name) / 'state'
        (sdir / 'sessions' / nid).mkdir(parents=True)
        (sdir / 'sessions' / nid / 'screen.txt').write_text(
            'panic(main thread): abort() called\noh no: Bun has crashed.\n')
        d = Dispatcher({"version": 1, "workspaces": {"main": tmp.name},
                        "subscriptions": [], "aplexer_bin": "aplexer",
                        "aplexer_state_dir": str(sdir)})
        offer = '11111111-2222-4333-8444-555555555555'
        record = {'offer_id': offer, 'workspace': str(ws), 'native_id': nid,
                  'engine': 'opencode',
                  'result_path': str(ws / '.local' / 'hub-results' / (offer + '.json')),
                  'log_offsets': {}, 'launched_at_ts': 1000.0}
        partial = d._no_result_partial_report(record)
        self.assertEqual(partial['status'], 'failed')
        self.assertIn('Native crash evidence', partial['report'])
        self.assertIn('Bun has crashed', partial['report'])
        self.assertIn('Bun has crashed', record.get('runtime_tail', ''))
        blob = json.dumps(partial)
        self.assertNotRegex(blob, r'[\x00-\x1f\x7f]')


if __name__ == '__main__':
    unittest.main()
