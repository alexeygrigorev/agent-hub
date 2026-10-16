import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_hub_host import routing, fleet


class RoutingClientTests(unittest.TestCase):
    def parse(self, argv):
        parser = argparse.ArgumentParser()
        routing.add_commands(parser.add_subparsers(dest='cmd', required=True))
        return parser.parse_args(argv)

    def test_prompt_is_explicit_approved_action_by_default_and_tier_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'prompt.md'; p.write_text('Read inbox and report count only')
            args = self.parse(['submit', '--prompt-file', str(p), '--needs', 'browser:orin,phone-sms', '--tier', 'daily', '--key', 'stable'])
            with patch.object(routing.bus, '_do_request', return_value={'id':'test'}) as request:
                self.assertEqual(routing.execute(args), {'id':'test'})
            payload = request.call_args.args[2]
            self.assertEqual(payload['prompt'], payload['approved_action'])
            self.assertEqual(payload['needs'], ['browser:orin','phone-sms'])
            self.assertEqual(payload['tier'], 'daily')
            self.assertEqual(payload['idempotency_key'], 'stable')

    def test_batch_and_prompt_cannot_mix_and_input_size_is_bounded(self):
        args = self.parse(['submit', '--batch-file', '/unused', '--prompt-file', '/unused'])
        with self.assertRaises(ValueError): routing.execute(args)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'large'; p.write_text('x' * 24001)
            with self.assertRaises(ValueError): routing.read_text(p)

    def test_job_id_cannot_inject_a_path(self):
        with self.assertRaises(ValueError): routing.execute(self.parse(['job', '../credentials']))

    def test_selected_model_mismatch_never_runs_admission_or_claim(self):
        runner = lambda *a, **kw: self.fail('must not invoke native command')
        settings = {'subscriptions':[{'service':'zai','engine':'zcodex','profile':None,'account_label':'a','launcher_model':'glm-5.3-flash','launcher_effort':'high','admission_argv':['gate']}], 'workspaces':{'tasks':'/repo'}}
        offer = {'task_id':'job','needs':[],'account':'a','engine':'zcodex','profile':None,'workspace':'tasks','selection':{'model':'glm-5.3','effort':'high'}}
        with self.assertRaisesRegex(ValueError, 'model/effort'):
            fleet.Dispatcher(settings, request=runner, runner=runner).dispatch(offer)

    def test_settings_reject_unknown_capabilities_and_bad_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'settings.json'
            for extra in [{'capabilities':['auth token=bad']},{'max_agents':True},{'max_agents':0}]:
                p.write_text(json.dumps({'version':1,'subscriptions':[],**extra}))
                with self.assertRaises(ValueError): fleet.load_settings(p)
