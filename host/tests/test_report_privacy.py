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
    def test_quoted_multiline_headers_redact_before_screen_and_report_selection(self):
        from agent_hub_host.launch_diagnostics import read_native_screen_crash_tail
        from agent_hub_host.log_redaction import redact_log
        for key in ['authorization', 'Cookie', 'Set-Cookie']:
            for quote in ['"', "'"]:
                with self.subTest(key=key, quote=quote), tempfile.TemporaryDirectory() as tmp:
                    text = key + ': ' + quote + 'prefix\npanic: short-test-value\nend' + quote
                    native = Path(tmp) / 'sessions' / NATIVE
                    native.mkdir(parents=True)
                    (native / 'screen.txt').write_text(text)
                    result = bound_finish_fields(dict(report=text, artifacts=[],
                                                       diagnostic=dict(message=text, tail=text)))
                    for rendered in [redact_log(text), json.dumps(result),
                                     read_native_screen_crash_tail(tmp, NATIVE)]:
                        self.assertNotIn('short-test-value', rendered)

    def test_uncertain_shared_log_offsets_and_oversized_logs_are_withheld(self):
        from agent_hub_host.fleet import Dispatcher
        from agent_hub_host import launch_diagnostics as ld
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'opencode.log'
            dest = Path(tmp) / '.local' / 'log.txt'
            d = Dispatcher(dict(version=1, subscriptions=[], workspaces={'main': tmp}))
            for text, offset in [('password="' + 'padding ' * 11000 + '\nshort-test-value"', 0),
                                 ('password="prefix\nshort-test-value\nend"', 18)]:
                with self.subTest(offset=offset):
                    log.write_text(text)
                    record = dict(workspace=tmp, offer_id=OFFER, engine='opencode',
                                  log_offsets={str(log): offset})
                    with patch.object(ld, 'get_opencode_log_paths', return_value=[log]), \
                            patch.object(ld, 'describe_partial_work', return_value={}):
                        self.assertIsNone(ld.retain_per_session_opencode_log(record['log_offsets'], dest))
                        result = bound_finish_fields(d._no_result_partial_report(record))
                    self.assertFalse(dest.exists())
                    self.assertNotIn('short-test-value', json.dumps(result))
                    self.assertEqual(result['status'], 'failed')

    def test_complete_bounded_new_log_is_retained_privately_after_redaction(self):
        from agent_hub_host import launch_diagnostics as ld
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'opencode.log'
            dest = Path(tmp) / '.local' / 'log.txt'
            log.write_text('password="short-test-value"\nSafe work evidence\n')
            with patch.object(ld, 'get_opencode_log_paths', return_value=[log]):
                self.assertEqual(ld.retain_per_session_opencode_log({str(log): 0}, dest), str(dest))
            self.assertNotIn('short-test-value', dest.read_text())
            self.assertIn('Safe work evidence', dest.read_text())
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

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


    def test_screen_selection_and_git_fallback_cannot_discard_secret_context(self):
        from agent_hub_host.fleet import Dispatcher
        from agent_hub_host.runtime_diagnostics import runtime_diagnostic
        from agent_hub_host.launch_diagnostics import read_native_screen_crash_tail
        import subprocess
        secret='short-test-value'
        screen='password="first line\npanic: '+secret+'\nlast line"\nTransport: The socket connection was closed unexpectedly'
        subject='abc1234 subject password="'+'ordinary words '*30+secret+' end"'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.git').mkdir()
            native=root/'native/sessions'/NATIVE;native.mkdir(parents=True)
            (native/'screen.txt').write_text(screen)
            d=Dispatcher(dict(version=1,subscriptions=[],workspaces={'main':tmp},aplexer_state_dir=str(root/'native')))
            record=dict(workspace=tmp,offer_id=OFFER,native_id=NATIVE,engine='opencode',
                        incarnation=1000,launched_at_ts=1,log_offsets={})
            def git(argv,**kwargs):
                return subprocess.CompletedProcess(argv,0,subject if 'log' in argv else '', '')
            with patch('subprocess.run',side_effect=git),patch.object(d,'_session_log_dest',return_value=(None,None)):
                partial=d._no_result_partial_report(record)
            diagnostic=runtime_diagnostic(record,dict(finished_at_ms=2000),query=lambda argv:'')
            final=bound_finish_fields(dict(report=partial['report'],artifacts=partial['artifacts'],diagnostic=diagnostic))
            self.assertNotIn(secret,read_native_screen_crash_tail(str(root/'native'),NATIVE))
            self.assertNotIn(secret,json.dumps(final))
            self.assertIn('Transport:',final['report'])

    def test_truncated_quoted_value_is_redacted_through_end_of_window(self):
        from agent_hub_host.log_redaction import redact_log
        self.assertNotIn('short-test-value',redact_log('password="prefix short-test-value'))


    def test_fast_retirement_recovers_and_retains_exact_output_without_catalog_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);native=root/'state/retired-sessions'/NATIVE;native.mkdir(parents=True)
            (native/'history.bin').write_text('Work completed\npassword="short-test-value"\n')
            record=dict(offer_id=OFFER,native_id=NATIVE,tag='task-hub-'+OFFER,incarnation=1000,
                        workspace=tmp,engine='opencode')
            tombstone=dict(exit=dict(code=0,signal=None,exited_at_ms=2000))
            evidence=terminal_evidence(record,tombstone,state_dir=str(root/'state'))
            self.assertEqual(evidence['exit_code'],0)
            self.assertEqual(evidence['wall_time_ms'],1000)
            self.assertIn('Work completed',evidence['log_tail'])
            self.assertNotIn('short-test-value',evidence['log_tail'])
            self.assertTrue((root/'.local/hub-session-logs'/(OFFER+'.json')).is_file())
            # Never clip an unlabeled interior of a large history: use the full
            # bounded native screen snapshot instead.
            (native/'history.bin').write_text('password="'+'secret fragment '*6000+'"')
            (native/'screen.txt').write_text('Latest safe output\n')
            record.pop('exit_evidence',None)
            evidence=terminal_evidence(record,tombstone,state_dir=str(root/'state'))
            self.assertIn('Latest safe output',evidence['log_tail'])
            self.assertNotIn('secret fragment',evidence['log_tail'])


    def test_routed_terminal_exit_includes_evidence_on_assigned_job_result(self):
        import test_tbm_routed_results as routed
        case=routed.RoutedResultsTests();case.setUp()
        try:
            case.d._active_launches[OFFER]['exit_evidence']=dict(exit_code=0,log_tail='Routed audit complete')
            results,_=case.reconcile()
            self.assertEqual(results[0]['state'],'done')
            payload=case.request.call_args.args[2]
            self.assertEqual(payload['runtime']['exit_code'],0)
            self.assertEqual(payload['runtime']['log_tail'],'Routed audit complete')
            self.assertTrue(payload['confirmed_exit'])
        finally:
            case.doCleanups()


    def test_non_repository_workspace_cannot_inherit_parent_repository_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            git=Mock(side_effect=AssertionError('must not inspect parent repository'))
            result=terminal_evidence(dict(workspace=tmp,incarnation=1000),{},git=git)
            self.assertEqual(result['observed_commits'],[])
            self.assertEqual(result['dirty_paths'],[])
            git.assert_not_called()
