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

    def test_session_activity_is_reported_as_utc_iso_or_unknown(self):
        cfg = settings(Path('/repo'))
        base = {'id': 'native-session', 'tag': 'owned', 'engine': 'codex', 'profile': None, 'phase': 'running', 'workspace': '/repo', 'reported_state': 'working'}
        self.assertEqual(fleet.session_metadata([dict(base, last_activity_ms=1792046739719)], cfg)[0]['last_activity_at'], '2026-10-15T06:45:39.719000Z')
        for bad in [None, 'yesterday', -5, 1.5, True]:
            self.assertIsNone(fleet.session_metadata([dict(base, last_activity_ms=bad)], cfg)[0]['last_activity_at'])

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
            # Bounded diagnostic only (type+stage, never argv/goal/secrets).
            self.assertIn("TimeoutError", result.get("reason", ""))
            posted = http[-1][2]
            self.assertEqual(posted, {'state': 'uncertain'})
            self.assertTrue(set(posted.keys()).issubset({'state', 'native_id', 'launcher_task_id', 'confirmed_exit'}))
            self.assertNotIn("reason", posted)
            self.assertNotIn("stage", posted)
            self.assertNotIn("error", posted)

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
                if 'plan' in argv: return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'task_fit': 1.0, 'account_label': 'shared-codex', 'profile': None}]}
                if 'run' in argv: return {'receipt': {'unit_name': 'headless-unit', 'exit_code': 0}}
                return {}
            d = fleet.Dispatcher(cfg, lambda *a: requests.append(a) or {}, runner)
            result = d.dispatch(offer(mode='headless', prompt='Goal with $(literal shell text)'))
            self.assertEqual(result['state'], 'exited'); self.assertEqual(result['native_id'], 'headless-unit')
            self.assertIn('--backend', calls[-1]); self.assertIn('task-units', calls[-1]); self.assertNotIn('aplexer', calls[-1])
            def wrong_model_plan(argv, **kw): return {'eligible': [{'provider': 'codex', 'model': 'some-other-model', 'task_fit': 1.0}]} if 'plan' in argv else runner(argv, **kw)
            d.runner = wrong_model_plan; requests.clear()
            with self.assertRaises(ValueError): d.dispatch(offer(mode='headless', prompt='Goal'))
            self.assertFalse(requests)

    def test_antigravity_headless_requires_native_first_action_and_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp))
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': tmp, 'config_dir': str(Path(tmp) / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='antigravity', launcher_model='gemini-3.8-flash-high')
            calls = []
            receipt = {'session_id': 'genuine-native-uuid', 'state': 'running', 'first_action_verified': True}
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'antigravity', 'model': 'gemini-3.8-flash-high', 'task_fit': 1.0}]}
                if 'run' in argv: return {'receipt': dict(receipt)}
                return {}
            d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
            result = d.dispatch(offer(mode='headless', prompt='Useful monitoring task'))
            self.assertEqual(result['state'], 'running')
            self.assertEqual(result['native_id'], 'genuine-native-uuid')
            self.assertIn('aplexer', calls[-1])
            receipt.pop('session_id'); receipt['unit_name'] = 'not-native'
            self.assertEqual(d.dispatch(offer(mode='headless', prompt='Goal'))['state'], 'uncertain')
            receipt.update(session_id='genuine-native-uuid', first_action_verified=False)
            self.assertEqual(d.dispatch(offer(mode='headless', prompt='Goal'))['state'], 'uncertain')

    def test_zai_headless_native_backend_binds_exact_zcodex_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp))
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': tmp, 'config_dir': str(Path(tmp) / 'config')}
            cfg['subscriptions'][0].update(engine='zcodex', launcher_provider='zai', launcher_model='glm-5.3-flash')
            calls = []
            receipt = {'session_id': 'c420fcbe-36bc-4235-8bc7-224691d0bb65', 'state': 'running', 'first_action_verified': True,
                       'engine': 'zcodex', 'workspace': tmp, 'tag': 'task-hub-' + offer()['id']}
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'zai', 'model': 'glm-5.3-flash', 'task_fit': 1.0}]}
                if 'run' in argv: return {'receipt': dict(receipt)}
                return {}
            d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
            request = offer(engine='zcodex', mode='headless', prompt='Owned lifecycle test')
            result = d.dispatch(request)
            self.assertEqual(result['state'], 'running')
            self.assertEqual(result['native_id'], receipt['session_id'])
            self.assertIn('aplexer', calls[-1])
            self.assertNotIn('task-units', calls[-1])
            # Launcher --cwd is its owned workspace/evidence root; payload.cwd
            # independently selects the confined child's working directory.
            nested = Path(tmp) / 'nested'
            nested.mkdir()
            self.assertEqual(d.dispatch(dict(request, cwd='nested'))['state'], 'running')
            run_argv = calls[-1]
            self.assertEqual(run_argv[run_argv.index('--cwd') + 1], tmp)
            self.assertEqual(run_argv[run_argv.index('--tmpdir') + 1], str(Path(tmp) / '.local' / 'tmp'))
            submitted = next(c for c in reversed(calls) if 'submit' in c)
            self.assertEqual(json.loads(submitted[submitted.index('--payload') + 1])['cwd'], str(nested))
            valid = dict(receipt)
            for field, bad in [('session_id', 'unit-name'), ('session_id', 'c420fcbe-36bc-1235-8bc7-224691d0bb65'), ('session_id', None), ('engine', 'shell'),
                               ('workspace', '/other'), ('tag', 'other'), ('first_action_verified', False)]:
                with self.subTest(field=field, bad=bad):
                    receipt.clear(); receipt.update(valid); receipt[field] = bad
                    self.assertEqual(d.dispatch(request)['state'], 'uncertain')
            receipt.clear(); receipt.update(valid)
            cfg['subscriptions'][0]['profile'] = 'unqualified'
            with self.assertRaises(ValueError):
                d.dispatch(dict(request, profile='unqualified'))

    def test_command_errors_never_echo_raw_stdout_stderr(self):
        with self.assertRaises(RuntimeError) as caught:
            fleet.run_json([sys.executable, '-c', 'import sys;print("MUST_NOT_LEAK");sys.stderr.write("MUST_NOT_LEAK");sys.exit(1)'])
        self.assertNotIn('MUST_NOT_LEAK', str(caught.exception))

    def test_stdout_stderr_and_time_are_bounded_during_capture(self):
        for script in ['import sys;sys.stderr.write("x"*20000);print("{}")', 'print("x"*200000)', 'import time;time.sleep(5)']:
            with self.assertRaises(RuntimeError): fleet.run_json([sys.executable, '-c', script], timeout=.1)
        self.assertEqual(fleet.run_json([sys.executable, '-c', 'print("{}")']), {})

    def test_verbose_native_catalog_has_separate_bounded_output_capacity(self):
        script = 'import json;print(json.dumps([{"id":str(i),"metadata":"x"*4096} for i in range(500)]))'
        rows = fleet.run_json([sys.executable, '-c', script, 'list', '--json'])
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[-1]['id'], '499')
        with self.assertRaises(RuntimeError):
            fleet.run_json([sys.executable, '-c', 'print("x" * (9 * 1024 * 1024))', 'list', '--json'])

    def test_provider_timestamp_is_preserved_and_stale_or_future_quota_is_unknown(self):
        for timestamp in ['2000-01-01T00:00:00Z', '2099-01-01T00:00:00Z']:
            raw = {'codex': {'status': 'ok', 'observed_at': timestamp, 'details': {'limit_reached': False}, 'windows': {}}}
            result = fleet.quota_metadata(raw, 'codex')
            self.assertEqual(result['observed_at'], timestamp if timestamp.startswith('2000') else None)
            self.assertEqual(result['status'], 'unknown')

    def test_explicit_provider_and_window_errors_never_become_healthy(self):
        base = {'status': 'ok', 'details': {'limit_reached': False}, 'windows': {'weekly': {'percent_remaining': 80, 'reset_at': '2099-01-01T00:00:00Z'}}}
        cases = [dict(base, stale=True), dict(base, stale='true'), dict(base, stale=None), dict(base, error=''), dict(base, error='MUST_NOT_LEAK'), dict(base, details={'limit_reached': False, 'stale': True}), dict(base, windows={**base['windows'], 'five_hour': {'error': 'unavailable'}}), dict(base, windows={**base['windows'], 'five_hour': {'error': ''}}), dict(base, observed_at=None)]
        for route in cases:
            result = fleet.quota_metadata({'codex': route}, 'codex')
            self.assertEqual(result['status'], 'unknown')
            self.assertNotIn('MUST_NOT_LEAK', json.dumps(result))
        for windows in [{**base['windows'], 'contradiction': {'present': False, 'percent_remaining': 0, 'reset_at': None}}, {**{f'w{i}': {'present': False, 'percent_remaining': None, 'reset_at': None} for i in range(32)}, 'last': {'error': 'unavailable'}}]:
            self.assertEqual(fleet.quota_metadata({'codex': dict(base, windows=windows)}, 'codex')['status'], 'unknown')

    def test_many_failed_probes_do_not_close_later_probe_descriptors(self):
        for _ in range(30):
            with self.assertRaises(RuntimeError):
                fleet.run_json([sys.executable, '-c', 'import sys;sys.stderr.write("x"*20000);print("{}")'], timeout=.03)
            self.assertEqual(fleet.run_json([sys.executable, '-c', 'import json;print(json.dumps({"ok": True}))']), {'ok': True})

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
        self.assertEqual([c[1] for c in calls], ['/api/machines/register', '/api/host/poll', '/api/fleet/subscriptions', '/api/fleet/sessions', '/api/sessions/host-bindings', '/api/fleet/offers'])
        self.assertTrue(calls[1][2]['gate']['launch_allowed'])
        self.assertEqual(calls[0][2]['capabilities']['workspaces'], ['demo'])



    def test_absolute_workspace_keys_allowed_and_malformed_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'repo'
            target.mkdir()
            p = Path(tmp) / 'subscriptions.json'
            cfg = settings(target)
            cfg['workspaces'] = {'demo': str(target), '/srv/hub/demo': str(target)}
            p.write_text(json.dumps(cfg))
            loaded = fleet.load_settings(p)
            self.assertEqual(loaded['workspaces']['/srv/hub/demo'], str(target))
            for bad_key in ['/srv/../escape', '/srv//double', '/srv/./dot',
                            '/srv/hub/demo/', '/', '/srv/hub/de mo', '']:
                bad = dict(cfg)
                bad['workspaces'] = {'demo': str(target), bad_key: str(target)}
                p.write_text(json.dumps(bad))
                with self.assertRaises(ValueError, msg=bad_key or '<empty>'):
                    fleet.load_settings(p)
            # Non-absolute values still refused, labels still fine.
            bad = dict(cfg)
            bad['workspaces'] = {'demo': 'relative/path'}
            p.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                fleet.load_settings(p)

    def test_absolute_offer_routes_only_when_explicitly_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            cfg = settings(target)
            cfg['workspaces'] = {'demo': str(target), '/srv/hub/demo': str(target)}
            route = fleet.Dispatcher(cfg)._route({**offer(), 'workspace': '/srv/hub/demo'})
            self.assertEqual(route['account_label'], 'shared-codex')
            with self.assertRaisesRegex(ValueError, 'not configured'):
                fleet.Dispatcher(cfg)._route({**offer(), 'workspace': '/srv/hub/unmapped'})
    def test_native_start_argv_configuration_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'subscriptions.json'
            valid_prefix = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect", "--property=KillMode=process", "--"]
            # Top-level configuration succeeds
            cfg = settings(Path(tmp))
            cfg['native_start_argv'] = valid_prefix
            p.write_text(json.dumps(cfg))
            loaded = fleet.load_settings(p)
            self.assertEqual(loaded['native_start_argv'], valid_prefix)

            # Subscription-level configuration succeeds
            cfg2 = settings(Path(tmp))
            cfg2['subscriptions'][0]['native_start_argv'] = valid_prefix
            p.write_text(json.dumps(cfg2))
            loaded2 = fleet.load_settings(p)
            self.assertEqual(loaded2['subscriptions'][0]['native_start_argv'], valid_prefix)

            # Invalid configurations are rejected strictly
            for bad_prefix in [
                "not-a-list",
                [],
                [""],
                [123],
                ["systemd-run\0bad"],
                [None],
                ["valid", ""],
            ]:
                bad_cfg = settings(Path(tmp))
                bad_cfg['native_start_argv'] = bad_prefix
                p.write_text(json.dumps(bad_cfg))
                with self.assertRaises(ValueError, msg=f"Should reject bad prefix: {bad_prefix!r}"):
                    fleet.load_settings(p)

                bad_sub_cfg = settings(Path(tmp))
                bad_sub_cfg['subscriptions'][0]['native_start_argv'] = bad_prefix
                p.write_text(json.dumps(bad_sub_cfg))
                with self.assertRaises(ValueError, msg=f"Should reject bad sub prefix: {bad_prefix!r}"):
                    fleet.load_settings(p)

            # Unknown secret fields are rejected
            secret_cfg = settings(Path(tmp))
            secret_cfg['native_start_argv'] = valid_prefix
            secret_cfg['secret_token'] = 'MUST_NOT_EXIST'
            p.write_text(json.dumps(secret_cfg))
            with self.assertRaisesRegex(ValueError, 'Invalid subscription configuration'):
                fleet.load_settings(p)

    def test_interactive_start_prepends_native_start_argv_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect", "--property=KillMode=process", "--"]
            cfg = settings(Path(tmp))
            cfg['native_start_argv'] = prefix
            calls, http = [], []

            def runner(argv, **kw):
                calls.append((argv, kw))
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                return {'id': 'native-session', 'workspace': tmp, 'tag': 'hub-' + offer()['id'], 'engine': 'codex', 'profile': None, 'phase': 'running'}

            dispatcher = fleet.Dispatcher(cfg, lambda *a: http.append(a) or {}, runner)
            result = dispatcher.dispatch(offer())
            self.assertEqual(result['state'], 'running')
            start_argv = calls[1][0]
            # Exact argv prefix applied
            expected_prefix = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect", "--property=KillMode=process", "--", "aplexer", "start"]
            self.assertEqual(start_argv[:len(expected_prefix)], expected_prefix)
            self.assertEqual(start_argv[len(expected_prefix):len(expected_prefix) + 2], ["--workspace", tmp])

            # Subscription-level overrides top-level
            sub_prefix = ["sudo", "-u", "nobody", "--"]
            cfg['subscriptions'][0]['native_start_argv'] = sub_prefix
            calls.clear()
            dispatcher2 = fleet.Dispatcher(cfg, lambda *a: {}, runner)
            result2 = dispatcher2.dispatch(offer())
            self.assertEqual(result2['state'], 'running')
            self.assertEqual(calls[1][0][:len(sub_prefix) + 2], sub_prefix + ["aplexer", "start"])

            # Default unchanged when prefix omitted
            cfg_no_prefix = settings(Path(tmp))
            calls.clear()
            dispatcher3 = fleet.Dispatcher(cfg_no_prefix, lambda *a: {}, runner)
            result3 = dispatcher3.dispatch(offer())
            self.assertEqual(result3['state'], 'running')
            self.assertEqual(calls[1][0][:2], ["aplexer", "start"])


if __name__ == '__main__': unittest.main()

