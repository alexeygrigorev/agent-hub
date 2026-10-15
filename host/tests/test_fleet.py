import copy
import json
from pathlib import Path
import tempfile
import unittest
import io
import sys
from unittest.mock import MagicMock
from unittest.mock import patch

from agent_hub_host import fleet


def settings(workspace):
    return {'version': 1, 'workspaces': {'demo': str(workspace)}, 'subscriptions': [
        {'service': 'codex', 'account_label': 'shared-codex', 'plan': 'pro', 'engine': 'codex', 'profile': None, 'admission_argv': ['maintained-gate']}
    ]}


def offer(**kw):
    return {'id': '12345678-1234-1234-1234-123456789abc', 'engine': 'codex', 'profile': None, 'account': 'shared-codex', 'mode': 'interactive', 'workspace': 'demo', 'prompt': '', **kw}


class FleetTests(unittest.TestCase):
    def test_multi_account_config_and_secret_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'subscriptions.json'
            cfg = settings(Path(tmp))
            cfg['subscriptions'].append({**cfg['subscriptions'][0], 'account_label': 'second-codex', 'profile': 'second'})
            p.write_text(json.dumps(cfg))
            self.assertEqual(len(fleet.load_settings(p)['subscriptions']), 2)
            cfg['subscriptions'][0]['access_token'] = 'MUST_NOT_LEAK'
            p.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, 'metadata'):
                fleet.load_settings(p)

    def test_default_quota_is_not_copied_to_multiple_accounts_or_nondefault_profile(self):
        cfg = settings(Path('/repo'))
        cfg['subscriptions'].append({**cfg['subscriptions'][0], 'account_label': 'second-codex', 'profile': 'second'})
        calls = []
        reports = fleet.collect_subscriptions(cfg, lambda argv: calls.append(argv))
        self.assertFalse(calls)
        self.assertTrue(all(r['quota']['status'] == 'unknown' for r in reports))
        cfg['subscriptions'] = cfg['subscriptions'][1:]
        fleet.collect_subscriptions(cfg, lambda argv: calls.append(argv))
        self.assertFalse(calls)

    def test_collector_whitelists_quota_and_detected_tier_without_email_token_or_error_body(self):
        cfg = settings(Path('/repo')); cfg['subscriptions'][0].update(service='claude', engine='claude', plan=None)
        def runner(argv):
            if argv[0] == 'claude':
                return {'subscriptionType': 'max', 'email': 'private@example.invalid', 'token': 'MUST_NOT_LEAK'}
            return {'claude': {'status': 'ok', 'details': {'limit_reached': False, 'token': 'MUST_NOT_LEAK'}, 'windows': {'weekly': {'percent_remaining': 50, 'reset_at': '2099-01-01T00:00:00Z'}}, 'error': 'MUST_NOT_LEAK'}}
        report = fleet.collect_subscriptions(cfg, runner)
        self.assertEqual(report[0]['plan'], 'max')
        self.assertEqual(report[0]['quota']['windows']['weekly']['percent_remaining'], 50)
        text = json.dumps(report)
        self.assertNotIn('MUST_NOT_LEAK', text); self.assertNotIn('private@example', text)

    def test_probe_failure_and_malformed_route_remain_unknown(self):
        cfg = settings(Path('/repo'))
        def runner(_): raise RuntimeError('MUST_NOT_LEAK')
        report = fleet.collect_subscriptions(cfg, runner)
        self.assertEqual(report[0]['quota']['status'], 'unknown')
        self.assertNotIn('MUST_NOT_LEAK', json.dumps(report))
        self.assertEqual(fleet.quota_metadata({'codex': []}, 'codex')['status'], 'unknown')

    def test_sessions_include_workspace_profile_account_but_no_command_environment(self):
        cfg = settings(Path('/repo'))
        raw = [{'id': 'native-session', 'tag': 'owned', 'engine': 'codex', 'profile': None, 'phase': 'running', 'workspace': '/repo', 'reported_state': 'working', 'command': ['MUST_NOT_LEAK'], 'env': {'TOKEN': 'MUST_NOT_LEAK'}}]
        result = fleet.session_metadata(raw, cfg)
        self.assertEqual(result[0]['workspace'], '/repo'); self.assertEqual(result[0]['account'], 'shared-codex')
        self.assertNotIn('MUST_NOT_LEAK', json.dumps(result))
        cfg['subscriptions'].append(copy.deepcopy(cfg['subscriptions'][0]))
        self.assertIsNone(fleet.session_metadata(raw, cfg)[0]['account'])

    def test_interactive_uses_native_session_and_profile_not_headless_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp)); cfg['subscriptions'][0]['profile'] = 'secondary'
            calls, http = [], []
            def runner(argv, **kw):
                calls.append((argv, kw))
                if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                return {'id': 'native-session', 'workspace': tmp, 'tag': 'hub-' + offer()['id'], 'engine': 'codex', 'profile': 'secondary', 'phase': 'running'}
            dispatcher = fleet.Dispatcher(cfg, lambda *a: http.append(a) or {}, runner)
            result = dispatcher.dispatch(offer(profile='secondary'))
            self.assertEqual(result['state'], 'running')
            self.assertEqual(calls[1][0][0:2], ['aplexer', 'start'])
            self.assertIn('--profile', calls[1][0]); self.assertNotIn('exec', calls[1][0])
            self.assertTrue(http[0][1].endswith('/claim'))

    def test_unsupported_or_unknown_gate_never_claims_or_launches(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = []
            for gate in [{'launch_allowed': False}, {'launch_allowed': True, 'profile_supported': True, 'account_label': 'other', 'observed_at': fleet.utcnow()}, {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': '2000-01-01T00:00:00Z'}]:
                dispatcher = fleet.Dispatcher(settings(Path(tmp)), lambda *a: http.append(a), lambda *a, **kw: gate)
                with self.assertRaises(ValueError): dispatcher.dispatch(offer())
            self.assertFalse(http)

    def test_ambiguous_claim_never_executes_and_ambiguous_native_effect_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls, http = [], []
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                raise TimeoutError('MUST_NOT_LEAK')
            def request(*a):
                http.append(a)
                if a[1].endswith('/claim'): raise TimeoutError()
            d = fleet.Dispatcher(settings(Path(tmp)), request, runner)
            with self.assertRaises(TimeoutError): d.dispatch(offer())
            self.assertEqual(len(calls), 1)
            d.request = lambda *a: http.append(a) or {}
            result = d.dispatch(offer())
            self.assertEqual(result['state'], 'uncertain')
            self.assertEqual(http[-1][2], {'state': 'uncertain'})

    def test_wrong_native_receipt_is_not_reported_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            def runner(argv, **kw):
                return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()} if argv[0] == 'maintained-gate' else {'id': 'wrong', 'workspace': '/other', 'tag': 'impostor'}
            d = fleet.Dispatcher(settings(Path(tmp)), lambda *a: {}, runner)
            self.assertEqual(d.dispatch(offer())['state'], 'uncertain')

    def test_headless_uses_maintained_launcher_and_requires_account_aware_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp)); cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': tmp, 'config_dir': str(Path(tmp) / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6-sol')
            calls, requests = [], []
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv: return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'account_label': 'shared-codex', 'profile': None}]}
                if 'run' in argv: return {'receipt': {'unit_name': 'headless-unit', 'exit_code': 0}}
                return {}
            d = fleet.Dispatcher(cfg, lambda *a: requests.append(a) or {}, runner)
            result = d.dispatch(offer(mode='headless', prompt='Goal with $(literal shell text)'))
            self.assertEqual(result['state'], 'exited'); self.assertEqual(result['native_id'], 'headless-unit')
            self.assertIn('--backend', calls[-1]); self.assertIn('task-units', calls[-1]); self.assertNotIn('aplexer', calls[-1])
            def old_plan(argv, **kw): return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol'}]} if 'plan' in argv else runner(argv, **kw)
            d.runner = old_plan; requests.clear()
            with self.assertRaises(ValueError): d.dispatch(offer(mode='headless', prompt='Goal'))
            self.assertFalse(requests)

    def test_command_errors_never_echo_raw_stdout_stderr(self):
        with self.assertRaises(RuntimeError) as caught:
            fleet.run_json([sys.executable, '-c', 'import sys;print("MUST_NOT_LEAK");sys.stderr.write("MUST_NOT_LEAK");sys.exit(1)'])
        self.assertNotIn('MUST_NOT_LEAK', str(caught.exception))

    def test_stdout_stderr_and_time_are_bounded_during_capture(self):
        for script in ['import sys;sys.stderr.write("x"*20000);print("{}")', 'print("x"*200000)', 'import time;time.sleep(5)']:
            with self.assertRaises(RuntimeError): fleet.run_json([sys.executable, '-c', script], timeout=.1)
        self.assertEqual(fleet.run_json([sys.executable, '-c', 'print("{}")']), {})

    def test_provider_timestamp_is_preserved_and_stale_or_future_quota_is_unknown(self):
        for timestamp in ['2000-01-01T00:00:00Z', '2099-01-01T00:00:00Z']:
            raw = {'codex': {'status': 'ok', 'observed_at': timestamp, 'details': {'limit_reached': False}, 'windows': {}}}
            result = fleet.quota_metadata(raw, 'codex')
            self.assertEqual(result['observed_at'], timestamp)
            self.assertEqual(result['status'], 'unknown')

    def test_codex_plan_uses_only_readonly_account_metadata_and_closes_owned_process(self):
        proc = MagicMock()
        proc.stdout = io.StringIO(json.dumps({'id': 1, 'result': {}}) + '\n' + json.dumps({'id': 2, 'result': {'account': {'type': 'chatgpt', 'planType': 'pro', 'email': 'private@example.invalid', 'token': 'MUST_NOT_LEAK'}}}) + '\n')
        with patch('subprocess.Popen', return_value=proc):
            self.assertEqual(fleet.codex_plan(), 'pro')
        sent = [json.loads(call.args[0]) for call in proc.stdin.write.call_args_list]
        self.assertEqual([s['method'] for s in sent], ['initialize', 'initialized', 'account/read'])
        self.assertFalse(sent[-1]['params']['refreshToken'])
        proc.terminate.assert_called_once()

    def test_finite_cycle_registers_capabilities_and_reports_only_this_machine_metadata(self):
        cfg = settings(Path('/repo')); calls = []
        def request(method, path, body=None):
            calls.append((method, path, body))
            return {'offers': []}
        def runner(argv, **kw):
            if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            if argv[0] == 'aplexer': return []
            return {}
        with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []}), patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}):
            self.assertEqual(fleet.Dispatcher(cfg, request, runner).once(), [])
        self.assertEqual([c[1] for c in calls], ['/api/machines/register', '/api/host/poll', '/api/fleet/subscriptions', '/api/fleet/sessions', '/api/fleet/offers'])
        self.assertTrue(calls[1][2]['gate']['launch_allowed'])
        self.assertEqual(calls[0][2]['capabilities']['workspaces'], ['demo'])


if __name__ == '__main__': unittest.main()
