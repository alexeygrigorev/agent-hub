"""Offline parser/request tests; real public-Node composition is in task-completion-cli.test.ts."""
import argparse
import contextlib
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from agent_hub_host import session_control as C

SID='11111111-1111-4111-8111-111111111111'
IID='22222222-2222-4222-8222-222222222222'


class SessionFinishedCliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file=Path(self.temp.name)/'claim.json'
        self.record={'kind':'session_finished','session_id':SID,'summary':'Bounded completion claim.',
                     'acceptance':[{'status':'met','proof':'actual synthetic fixture proof'}]}
        self.file.write_text(json.dumps(self.record))

    def parse(self,*extra):
        p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True);C.add_commands(sub)
        return p.parse_args(['sessions','finish',SID,'--intake-task-id',IID,'--key','finished-once',
                             '--file',str(self.file),*extra])

    def test_exact_advertised_syntax_to_claim_route(self):
        with patch.object(C,'request',return_value={'appended':True}) as request:
            result=C.execute(self.parse())
        self.assertTrue(result['appended'])
        request.assert_called_once_with('POST','/api/intakes/'+IID+'/finished',
                                        {**self.record,'key':'finished-once'})
        self.assertNotIn('native_exit_confirmed',request.call_args.args[2])

    def test_optional_json_does_not_change_payload(self):
        with patch.object(C,'request',return_value={'appended':True}) as request:C.execute(self.parse('--json'))
        self.assertEqual(request.call_args.args[2],{**self.record,'key':'finished-once'})

    def test_wrong_session_or_evidence_kind_is_local_refusal(self):
        for change in ({'session_id':IID},{'kind':'acceptance_verification'},{'kind':'native_finished'}):
            self.file.write_text(json.dumps({**self.record,**change}))
            with self.subTest(change=change),patch.object(C,'request') as request:
                with self.assertRaises(ValueError):C.execute(self.parse())
                request.assert_not_called()

    def test_no_host_or_verification_fields_allowed(self):
        for field in ['native_exit_confirmed','containment_empty','host_actor','evidence','verification','key']:
            self.file.write_text(json.dumps({**self.record,field:True}))
            with self.subTest(field=field),patch.object(C,'request') as request:
                with self.assertRaises(ValueError):C.execute(self.parse())
                request.assert_not_called()

    def test_malformed_acceptance_refused_before_request(self):
        for value in [[],[{'status':'unmet','proof':'x'}],[{'status':'met','proof':''}],
                      [{'status':'met','proof':'x','native_exit':True}],{'status':'met'}]:
            self.file.write_text(json.dumps({**self.record,'acceptance':value}))
            with self.subTest(case=value),patch.object(C,'request') as request:
                with self.assertRaises(ValueError):C.execute(self.parse())
                request.assert_not_called()

    def test_strict_json_duplicates_nonfinite_and_capacity(self):
        values=[json.dumps(self.record)[:-1]+',"kind":"session_finished"}',
                json.dumps(self.record).replace('"Bounded completion claim."','NaN'),
                '{', 'x'*16385]
        for raw in values:
            self.file.write_text(raw)
            with patch.object(C,'request') as request:
                with self.assertRaises(ValueError):C.execute(self.parse())
                request.assert_not_called()

    def test_uuid_aliases_and_unsafe_keys_refuse(self):
        for key,value in [('id','owned-alias'),('intake_task_id','../foreign'),('key','bad/key')]:
            args=self.parse();setattr(args,key,value)
            with self.subTest(key=key),patch.object(C,'request') as request:
                with self.assertRaises(ValueError):C.execute(args)
                request.assert_not_called()

    def test_server_forbidden_and_stale_propagate_without_retry(self):
        for reason in ['403 completion_claim_forbidden','409 session_finish_stale','400 invalid_session_finished']:
            with patch.object(C,'request',side_effect=RuntimeError(reason)) as request:
                with self.assertRaises(RuntimeError):C.execute(self.parse())
                self.assertEqual(request.call_count,1)

    def test_required_arguments_not_replaced_with_random_key(self):
        p=argparse.ArgumentParser();C.add_commands(p.add_subparsers(dest='cmd',required=True))
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            p.parse_args(['sessions','finish',SID,'--intake-task-id',IID,'--file',str(self.file)])

    def test_body_bound_includes_added_key(self):
        record={**self.record,'acceptance':[{'status':'met','proof':'x'*16200}]}
        self.file.write_text(json.dumps(record,separators=(',',':')))
        with patch.object(C,'request') as request:
            with self.assertRaises(ValueError):C.execute(self.parse())
            request.assert_not_called()


if __name__=='__main__':unittest.main()
