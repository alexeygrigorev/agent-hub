import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_hub_host.exit_evidence import snapshot_native, terminal_evidence, bound_finish_fields
from agent_hub_host import late_reports
import test_late_reports as fixtures

OFFER, NATIVE = fixtures.OFFER, fixtures.NATIVE


class ReportPrivacy(unittest.TestCase):
    def test_snapshot_and_terminal_publication_redact_full_quoted_and_cookie_values(self):
        cases = ['{"password":"short-test-value"}', "refresh_token='short-test-value'",
                 'Cookie: session=short-test-value; theme=light', 'Set-Cookie: sid=short-test-value',
                 '{"password":"short-test-value \\\" embedded quote"}',
                 'password="' + 'short-test-value ' * 600 + '"']
        for text in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                history = root / NATIVE / 'history.bin'
                history.parent.mkdir()
                history.write_text(text + '\nUnicode 😀 preserved\n')
                row = dict(id=NATIVE, tag='task-hub-' + OFFER, created_at_ms=1000,
                           workspace=tmp, engine='opencode', history_path=str(history))
                record = dict(offer_id=OFFER, native_id=NATIVE, tag=row['tag'], incarnation=1000,
                              workspace=tmp, engine='opencode')
                record['exit_evidence'] = snapshot_native(record, row)
                result = terminal_evidence(record, {'finished_at_ms':2000})
                payload = bound_finish_fields(dict(report=text, artifacts=[dict(label=text,text=text)],
                                                   diagnostic=dict(message=text,tail=text), runtime=result))
                retained = (root / '.local/hub-session-logs' / (OFFER + '.json')).read_text()
                self.assertNotIn('short-test-value', retained)
                self.assertNotIn('short-test-value', json.dumps(payload))
                self.assertIn('😀', result['log_tail'])

    def test_json_lone_surrogates_cannot_block_terminal_journal_or_post(self):
        for key in ['report', 'label', 'text']:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                d,_,record = fixtures.LateReports().fixture(tmp)
                record.update(state='running',settled=False)
                result = dict(status='done',report='Completed',artifacts=[dict(label='proof',text='audit')])
                if key == 'report': result[key] = 'Completed \ud800'
                else: result['artifacts'][0][key] += '\udfff'
                path=Path(record['result_path']);path.parent.mkdir(parents=True)
                path.write_text(json.dumps(result))
                report=d._read_hub_finish_report(record)
                d.request=Mock(return_value=dict(id=OFFER,state='exited'))
                payload=dict(state='exited',confirmed_exit=True,native_id=NATIVE,
                             launcher_task_id=record['task_id'],**report)
                with patch.object(d,'_maybe_cleanup_worktree'):
                    self.assertTrue(late_reports.acknowledge(d,record,payload,True))
                self.assertTrue(record['settled'])
                wire=json.dumps(d.request.call_args.args[2],ensure_ascii=False).encode('utf-8')
                self.assertIn('Completed'.encode(),wire)
