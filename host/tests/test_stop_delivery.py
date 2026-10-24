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
