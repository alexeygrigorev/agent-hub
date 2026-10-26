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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host.runtime_diagnostics import runtime_diagnostic
from agent_hub_host import launch_diagnostics as ld
from agent_hub_host.fleet import Dispatcher


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


if __name__ == '__main__':
    unittest.main()
