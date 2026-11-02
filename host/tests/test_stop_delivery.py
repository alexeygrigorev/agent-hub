"""Stop retirement regressions for deleted Linux workspaces."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from agent_hub_host.native_sessions import NativeTarget, SessionControlError
from agent_hub_host.retired_native import validate_retired_tombstone

class StopTombstoneTests(unittest.TestCase):
    def test_deleted_workspace_matches_original_but_other_workspace_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            target = NativeTarget(str(uuid.uuid4()), "owned-stop", directory + "/gone", 100)
            tombstone = Path(directory) / "tombstone.json"
            data = {"id": target.session_id, "tag":target.tag,
                    "workspace":target.workspace + " (deleted)",
                    "cause":"finished", "finished_at_ms":200}
            tombstone.write_text(json.dumps(data))
            self.assertEqual(validate_retired_tombstone(str(tombstone), target)["id"], target.session_id)
            data["workspace"] = directory + "/foreign (deleted)"
            tombstone.write_text(json.dumps(data))
            with self.assertRaisesRegex(SessionControlError, "workspace mismatch"):
                validate_retired_tombstone(str(tombstone), target)

class StopReceiptTests(unittest.TestCase):
    def test_old_stopped_row_never_masks_failed_stop_delivery(self):
        from agent_hub_host.session_stop import wait_for_confirmed_stop
        from unittest.mock import Mock
        request = Mock(return_value={'state':'stopped'})
        with self.assertRaisesRegex(ValueError, 'stop_host_unreachable; command stop-1'):
            wait_for_confirmed_stop('short-id','stop-1',10,1,
                wait_command=Mock(return_value={'state':'failed','ack':{'reason':'stop_host_unreachable'}}),request=request)
        request.assert_not_called()

    def test_wait_timeout_is_pending_not_host_unreachable(self):
        # Intake 1c8d915e: a short CLI wait must not claim the host is unreachable.
        from agent_hub_host.session_stop import wait_for_confirmed_stop
        from agent_hub_host.session_control import SessionHTTPError, SessionTransportError
        from unittest.mock import Mock
        cases = [(ValueError('Command wait timed out'), 'stop_pending'),
                 (SessionTransportError('connection reset'), 'stop_hub_unreachable'),
                 (SessionHTTPError(404, code='not_found'), 'not_found')]
        for error, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, '^' + reason + '; command stop-1; stop not yet confirmed') as raised:
                    wait_for_confirmed_stop('short-id', 'stop-1', 10, 1,
                        wait_command=Mock(side_effect=error), request=Mock())
                self.assertNotIn('stop_host_unreachable', str(raised.exception))

    def test_exact_stop_receipt_and_terminal_state_are_both_required(self):
        from agent_hub_host.session_stop import wait_for_confirmed_stop
        from unittest.mock import Mock
        wait = Mock(return_value={'state':'acked','ack':{'outcome':'delivered','confirmed_exit':True}})
        view = wait_for_confirmed_stop('short-id','stop-1',10,1,
            wait_command=wait,request=Mock(return_value={'state':'stopped'}))
        self.assertEqual(view['state'],'stopped')
        with self.assertRaisesRegex(ValueError,'stop_state_unconfirmed'):
            wait_for_confirmed_stop('short-id','stop-1',10,1,
                wait_command=wait,request=Mock(return_value={'state':'running'}))


class StopCliTests(unittest.TestCase):
    def test_stop_defaults_to_bounded_verified_receipt_and_no_wait_is_explicit(self):
        import argparse
        from unittest.mock import patch
        from agent_hub_host import session_control as cli
        parser = argparse.ArgumentParser()
        cli.add_commands(parser.add_subparsers(dest='cmd'))
        args = parser.parse_args(['sessions','stop','61bba72f','--key','stop-test'])
        self.assertTrue(args.wait)
        self.assertEqual(args.timeout,10)
        self.assertEqual(args.interval,1)
        with patch.object(cli,'request',side_effect=[
                {'command_id':'stop-1','state':'recorded'},
                {'commands':[{'id':'stop-1','state':'acked','ack':{'outcome':'delivered','confirmed_exit':True}}]},
                {'id':'61bba72f-full','state':'stopped'}]) as request:
            result = cli.execute(args)
        self.assertEqual(result['stop_wait']['state'],'stopped')
        self.assertEqual(request.call_count,3)
        args = parser.parse_args(['sessions','stop','61bba72f','--no-wait'])
        self.assertFalse(args.wait)

    def test_ambiguous_prefix_diagnostic_lists_matching_ids(self):
        import io
        import urllib.error
        from agent_hub_host import session_control as cli
        ids = ['61bba72f-e254-48d4-8166-0e11a16e99a5','61bba72f-aaaa-4aaa-8aaa-aaaaaaaaaaaa']
        body = json.dumps({'error':'ambiguous_session_prefix','matches':[{'id':sid} for sid in ids]}).encode()
        exc = urllib.error.HTTPError('http://localhost',409,'Conflict',{},io.BytesIO(body))
        message = str(cli.http_error(exc))
        for sid in ids:
            self.assertIn(sid,message)


class LiveDeletedWorkspaceStopTests(unittest.TestCase):
    def test_only_stop_matches_exact_kernel_suffix_and_uses_observed_workspace(self):
        from unittest.mock import patch
        from dataclasses import replace
        import subprocess
        from agent_hub_host import native_sessions as ns
        with tempfile.TemporaryDirectory() as directory:
            target = ns.NativeTarget(str(uuid.uuid4()), 'owned-stop', directory+'/gone', 100, engine='shell')
            before = ns.NativeSession(target.session_id, target.tag, target.workspace+' (deleted)',
                                      engine='shell', phase='running', created_at_ms=100)
            after = replace(before, phase='exited', containment_empty=True)
            with patch.object(ns, 'list_sessions', side_effect=[[before],[after]]), \
                 patch.object(ns, 'run_native', return_value=subprocess.CompletedProcess([],0,'{}','')) as run, \
                 patch.object(ns, '_confirm_workload_exit', return_value=(True,'verified empty')):
                result = ns.stop_session('aplexer',target)
            self.assertEqual(result.state,'stopped')
            argv = run.call_args.args[1]
            self.assertEqual(argv[argv.index('--workspace')+1],before.workspace)
            self.assertIn('--keep-record',argv)
            self.assertFalse(ns._stop_workspace_matches(directory+'/foreign (deleted)',target.workspace))
            literal = Path(directory)/'gone (deleted)'
            literal.mkdir()
            self.assertFalse(ns._stop_workspace_matches(str(literal),target.workspace))

    def test_kernel_suffix_never_waives_native_incarnation(self):
        from unittest.mock import patch
        from agent_hub_host import native_sessions as ns
        with tempfile.TemporaryDirectory() as directory:
            target = ns.NativeTarget(str(uuid.uuid4()), 'owned-stop', directory+'/gone', 100, engine='shell')
            row = ns.NativeSession(target.session_id,target.tag,target.workspace+' (deleted)',engine='shell',created_at_ms=200)
            with patch.object(ns,'list_sessions',return_value=[row]), patch.object(ns,'run_native') as run:
                with self.assertRaisesRegex(ns.SessionControlError,'generation mismatch'):
                    ns.stop_session('aplexer',target)
            run.assert_not_called()
