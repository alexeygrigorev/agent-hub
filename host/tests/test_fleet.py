import copy
import json
from pathlib import Path
import tempfile
import unittest
import io
import os
import stat
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
    def test_session_metadata_uses_live_liveness_over_persisted_phase(self):
        raw = [{'id': 'id', 'tag': 'tag', 'workspace': '/repo', 'engine': 'codex',
                'phase': 'running', 'state': 'broken', 'worker_alive': False}]
        self.assertEqual(fleet.session_metadata(raw, settings(Path('/repo')))[0]['phase'], 'broken')

    def test_session_metadata_marks_deleted_worktree_as_exited(self):
        raw = [{'id': 'id', 'tag': 'tag', 'workspace': '/repo/deleted-wt (deleted)', 'engine': 'codex',
                'phase': 'running', 'state': 'running', 'worker_alive': True}]
        self.assertEqual(fleet.session_metadata(raw, settings(Path('/repo')))[0]['phase'], 'exited')

    def test_session_metadata_marks_enoent_home_workspace_as_exited(self):
        raw = [{'id': 'id', 'tag': 'tag', 'workspace': '/home/alexey/nonexistent-workspace-path-12345', 'engine': 'codex',
                'phase': 'running', 'state': 'running', 'worker_alive': True}]
        self.assertEqual(fleet.session_metadata(raw, settings(Path('/repo')))[0]['phase'], 'exited')

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

    def test_configured_journal_survives_bounded_dispatcher_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp))
            cfg['journal_dir'] = str(Path(tmp) / 'journal')
            Path(cfg['journal_dir']).mkdir(mode=0o700)
            path = Path(tmp) / 'subscriptions.json'
            path.write_text(json.dumps(cfg))
            loaded = fleet.load_settings(path)
            first = fleet.Dispatcher(loaded)
            first._set_imported_cursor('next-page')
            self.assertEqual(fleet.Dispatcher(fleet.load_settings(path))._get_imported_cursor(), 'next-page')
            for bad in [None, 123, '', 'relative', '/bad\0path']:
                cfg['journal_dir'] = bad
                path.write_text(json.dumps(cfg))
                with self.assertRaisesRegex(ValueError, 'journal_dir'):
                    fleet.load_settings(path)

    def test_repository_roots_precede_home_and_legacy_aliases_for_bare_labels(self):
        cfg = settings(Path('/home/operator/git/demo'))
        cfg['workspace_roots'] = ['/home/operator/git']
        cfg['workspaces'].update(home='/home/operator', legacy='/home/operator/old-repo')
        advertised = fleet.advertised_workspaces(cfg)
        self.assertEqual(advertised[0], '/home/operator/git')
        self.assertIn('/home/operator', advertised)
        self.assertIn('/home/operator/old-repo', advertised)
        # Many legacy advertisements must not evict the usable repository root.
        cfg['workspaces'].update({f'old-{n}': f'/archive/{n}' for n in range(100)})
        advertised = fleet.advertised_workspaces(cfg)
        self.assertEqual(advertised[0], '/home/operator/git')
        self.assertEqual(len(advertised), 96)

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
            self.assertEqual(posted['state'], 'uncertain')
            self.assertEqual(posted['diagnostic']['error_type'], 'TimeoutError')
            self.assertEqual(posted['diagnostic']['stage'], 'dispatch_interactive')
            self.assertEqual(posted['diagnostic']['reason'], 'launcher_command_failed')
            self.assertNotIn('MUST_NOT_LEAK', json.dumps(posted))
            self.assertTrue(set(posted.keys()).issubset({'state', 'native_id', 'launcher_task_id', 'confirmed_exit', 'diagnostic'}))
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
        for catalog in (['list', '--json'], ['list', '--all', '--json']):
            rows = fleet.run_json([sys.executable, '-c', script, *catalog])
            self.assertEqual(len(rows), 500)
            self.assertEqual(rows[-1]['id'], '499')
        for catalog in (['list', '--json'], ['list', '--all', '--json']):
            with self.assertRaises(RuntimeError):
                fleet.run_json([sys.executable, '-c', 'print("x" * (9 * 1024 * 1024))', *catalog])

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
        with tempfile.TemporaryDirectory() as tmp:
            # Hermetic native state: the work-claim bridge must scan an empty
            # sessions dir here, find nothing to sync, and POST nothing.
            cfg['aplexer_state_dir'] = tmp
            with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []}), patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}):
                results = fleet.Dispatcher(cfg, request, runner).once()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['id'], 'workclaim-sync')
            self.assertTrue(results[0]['ok'])
            self.assertEqual(results[0]['claims'], 0)
            self.assertEqual(results[0]['messages'], 0)
        self.assertEqual([c[1] for c in calls], ['/api/machines/register', '/api/host/poll', '/api/fleet/subscriptions', '/api/fleet/sessions', '/api/sessions/host-bindings', '/api/sessions/host-bindings?kind=imported', '/api/fleet/offers'])
        self.assertTrue(calls[1][2]['gate']['launch_allowed'])
        self.assertEqual(calls[0][2]['capabilities']['workspaces'], sorted(['demo', '/repo']))



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

            # Git identity settings are passed as fallback env to interactive start
            cfg_git = settings(Path(tmp))
            cfg_git['git'] = {
                'user.name': 'Alexey Grigorev',
                'user.email': 'alexey.s.grigoriev@gmail.com'
            }
            calls.clear()
            dispatcher4 = fleet.Dispatcher(cfg_git, lambda *a: {}, runner)
            result4 = dispatcher4.dispatch(offer())
            self.assertEqual(result4['state'], 'running')
            start_argv4 = calls[1][0]
            self.assertIn("--env", start_argv4)
            self.assertIn("GIT_AUTHOR_NAME=Alexey Grigorev", start_argv4)
            self.assertIn("GIT_AUTHOR_EMAIL=alexey.s.grigoriev@gmail.com", start_argv4)
            self.assertIn("GIT_COMMITTER_NAME=Alexey Grigorev", start_argv4)
            self.assertIn("GIT_COMMITTER_EMAIL=alexey.s.grigoriev@gmail.com", start_argv4)

    def test_git_identity_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'subs.json'
            valid_cfg = settings(Path(tmp))
            valid_cfg['git'] = {
                'name': 'Alexey Grigorev',
                'email': 'alexey.s.grigoriev@gmail.com',
                'push_default': 'simple',
            }
            p.write_text(json.dumps(valid_cfg))
            loaded = fleet.load_settings(p)
            self.assertIn('git', loaded)
            self.assertEqual(loaded['git']['name'], 'Alexey Grigorev')

            # Rejects non-dict git setting
            bad_cfg = settings(Path(tmp))
            bad_cfg['git'] = "not-a-dict"
            p.write_text(json.dumps(bad_cfg))
            with self.assertRaisesRegex(ValueError, 'git identity setting must be a mapping'):
                fleet.load_settings(p)

            # Rejects newlines in key or value
            bad_cfg['git'] = {'user.name\n': 'evil'}
            p.write_text(json.dumps(bad_cfg))
            with self.assertRaisesRegex(ValueError, 'Invalid git config key'):
                fleet.load_settings(p)

            bad_cfg['git'] = {'user.name': 'evil\nvalue'}
            p.write_text(json.dumps(bad_cfg))
            with self.assertRaisesRegex(ValueError, 'Invalid git config value'):
                fleet.load_settings(p)

    def test_registration_derives_qualified_role_jobs_from_trusted_config(self):
        cfg = settings(Path('/repo'))
        calls = []
        def request(method, path, body=None):
            calls.append((method, path, body))
            return {'offers': []}
        def runner(argv, **kw):
            if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            if argv[0] == 'aplexer': return []
            return {}

        role_cfg = {
            "version": 1,
            "enabled": True,
            "profiles": {
                "principal-wake": {
                    "enabled": True,
                    "launcher_provider": "antigravity",
                    "launcher_model": "gemini-3.8-flash-high",
                },
                "writer-run": {
                    "enabled": False,
                }
            }
        }

        mock_report = {
            "kind": "principal-wake",
            "supported": True,
            "reasons": [],
            "config_enabled": True
        }

        with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []}), \
             patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}), \
             patch('agent_hub_host.runtime.RuntimeManager.can_process_role_run', return_value=(True, 'role_qualified', mock_report)):
            d = fleet.Dispatcher(cfg, request, runner, role_config=role_cfg)
            d.once()

        self.assertEqual(calls[0][1], '/api/machines/register')
        self.assertEqual(calls[0][2]['capabilities']['jobs'], ['principal-wake'])

    def test_registration_rejects_mock_or_fixture_reports_and_purges_stale_jobs(self):
        cfg = settings(Path('/repo'))
        calls = []
        def request(method, path, body=None):
            calls.append((method, path, body))
            return {'offers': []}
        def runner(argv, **kw):
            if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            if argv[0] == 'aplexer': return []
            return {}

        role_cfg = {
            "version": 1,
            "enabled": True,
            "profiles": {
                "principal-wake": {"enabled": True},
            }
        }

        mock_fixture_report = {
            "kind": "principal-wake",
            "status": "MOCK_FIXTURE",
            "is_fixture_only": True,
            "operational_qualified": False,
        }

        with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {'jobs': ['stale-job']}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []}), \
             patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}), \
             patch('agent_hub_host.runtime.RuntimeManager.can_process_role_run', return_value=(True, 'mock_runner_active', mock_fixture_report)):
            d = fleet.Dispatcher(cfg, request, runner, role_config=role_cfg)
            d.once()

        self.assertEqual(calls[0][1], '/api/machines/register')
        self.assertNotIn('jobs', calls[0][2]['capabilities'])

    def test_registration_rejects_arbitrary_configured_jobs_text(self):
        cfg = settings(Path('/repo'))
        cfg['capabilities'] = ['arbitrary-needs']
        calls = []
        def request(method, path, body=None):
            calls.append((method, path, body))
            return {'offers': []}
        def runner(argv, **kw):
            if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            if argv[0] == 'aplexer': return []
            return {}

        # Pre-existing jobs in system_info capabilities must be stripped and not published
        with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {'jobs': ['fake-job-text']}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []}), \
             patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}):
            d = fleet.Dispatcher(cfg, request, runner)
            d.once()

        self.assertEqual(calls[0][1], '/api/machines/register')
        self.assertNotIn('jobs', calls[0][2]['capabilities'])

    def test_registration_purges_stale_capability_when_config_becomes_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles_path = Path(tmp) / 'roles.json'
            roles_path.write_text(json.dumps({
                "version": 1,
                "enabled": True,
                "profiles": {
                    "principal-wake": {"enabled": True},
                }
            }))
            cfg = settings(Path(tmp))
            calls = []
            def request(method, path, body=None):
                calls.append((method, path, body))
                return {'offers': []}
            def runner(argv, **kw):
                if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if argv[0] == 'aplexer': return []
                return {}

            mock_report = {"kind": "principal-wake", "supported": True, "reasons": [], "config_enabled": True}

            with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []}), \
                 patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}), \
                 patch('agent_hub_host.runtime.RuntimeManager.can_process_role_run', return_value=(True, 'role_qualified', mock_report)):
                d = fleet.Dispatcher(cfg, request, runner, role_config_path=str(roles_path))
                d.once()
                self.assertEqual(calls[0][2]['capabilities']['jobs'], ['principal-wake'])

            # Now disable the config on disk
            roles_path.write_text(json.dumps({"version": 1, "enabled": False, "profiles": {}}))
            calls.clear()

            with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {'jobs': ['stale-wake']}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []}), \
                 patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}):
                d.once()
                self.assertNotIn('jobs', calls[0][2]['capabilities'])

    def test_headless_submit_registers_exact_result_path_for_normal_and_legacy_offers_while_interactive_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp)); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': tmp, 'config_dir': str(Path(tmp) / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6.1-sol', launcher_effort='high')
            calls, requests = [], []
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow(),
                            'model_supported': True, 'model': 'gpt-6.1-sol', 'effort': 'high'}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6.1-sol', 'task_fit': 1.0, 'account_label': 'shared-codex', 'profile': None}]}
                if 'run' in argv:
                    return {'receipt': {'model': 'gpt-6.1-sol', 'model_verified': True, 'unit_name': 'headless-unit', 'exit_code': 0}}
                return {}

            d = fleet.Dispatcher(cfg, lambda *a: requests.append(a) or {}, runner)

            # 1. ACTUAL normal headless offer WITHOUT task_id (canonical session/intake case)
            normal_offer = offer(mode='headless', task_id=None, prompt='Normal intake task')
            normal_result_file = Path(tmp) / '.local' / 'hub-results' / (normal_offer['id'] + '.json')

            result_normal = d.dispatch(normal_offer)
            self.assertEqual(result_normal['state'], 'exited')
            self.assertTrue(result_normal['confirmed_exit'])
            self.assertEqual(result_normal['launcher_task_id'], f"hub-{normal_offer['id']}")

            submit_call = next(c for c in calls if 'submit' in c)
            self.assertIn('--paths', submit_call)
            registered_path = submit_call[submit_call.index('--paths') + 1]

            # Precisely correct unique absolute owned result leaf
            self.assertEqual(registered_path, str(normal_result_file))
            self.assertTrue(Path(registered_path).is_absolute())
            self.assertTrue(registered_path.endswith(f"{normal_offer['id']}.json"))
            # Must NOT be broad directory or foreign path
            self.assertNotEqual(registered_path, str(tmp))
            self.assertNotEqual(registered_path, str(Path(tmp) / '.local'))
            self.assertNotEqual(registered_path, str(normal_result_file.parent))
            self.assertTrue(Path(registered_path).is_relative_to(Path(tmp)))
            self.assertTrue(normal_result_file.parent.is_dir())
            self.assertEqual(stat.S_IMODE(normal_result_file.parent.stat().st_mode), 0o700)

            # Assert prompt instruction includes exact structured result file path
            payload_str = submit_call[submit_call.index('--payload') + 1]
            payload = json.loads(payload_str)
            self.assertIn(f"Write a final JSON object to {normal_result_file}:", payload['goal'])

            # 2. Legacy task_id-present offer route
            calls.clear()
            requests.clear()
            legacy_offer = offer(mode='headless', task_id='task-intake-999', prompt='Legacy review task',
                                 needs=[], selection={'id': 'sol', 'model': 'gpt-6.1-sol', 'effort': 'high'})
            legacy_result_file = Path(tmp) / '.local' / 'hub-results' / (legacy_offer['id'] + '.json')

            def legacy_runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow(),
                            'model_supported': True, 'model': 'gpt-6.1-sol', 'effort': 'high'}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6.1-sol', 'task_fit': 1.0, 'account_label': 'shared-codex', 'profile': None}]}
                if 'run' in argv:
                    legacy_result_file.write_text(json.dumps({'status': 'done', 'report': 'Finished legacy task', 'artifacts': []}), encoding='utf-8')
                    return {'receipt': {'model': 'gpt-6.1-sol', 'model_verified': True, 'unit_name': 'headless-unit', 'exit_code': 0}}
                return {}

            d_legacy = fleet.Dispatcher(cfg, lambda *a: requests.append(a) or {}, legacy_runner)
            result_legacy = d_legacy.dispatch(legacy_offer)
            self.assertEqual(result_legacy['state'], 'done')
            submit_call_legacy = next(c for c in calls if 'submit' in c)
            self.assertIn('--paths', submit_call_legacy)
            self.assertEqual(submit_call_legacy[submit_call_legacy.index('--paths') + 1], str(legacy_result_file))
            task_posts = [r for r in requests if r[1] == f"/api/fleet/tasks/{legacy_offer['task_id']}/result"]
            self.assertEqual(len(task_posts), 1)
            self.assertEqual(task_posts[0][2]['status'], 'done')

            # 3. Interactive route unchanged (no launcher submit, no --paths, no result file)
            calls.clear()
            requests.clear()
            interactive_offer = offer(mode='interactive', prompt='')
            def interactive_runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if argv[0] == 'aplexer' and argv[1] == 'start':
                    return {'id': 'native-1', 'workspace': str(Path(tmp).resolve()), 'tag': f"hub-{interactive_offer['id']}",
                            'engine': 'codex', 'profile': None, 'phase': 'running'}
                return {}

            d_interactive = fleet.Dispatcher(cfg, lambda *a: requests.append(a) or {}, interactive_runner)
            result_interactive = d_interactive.dispatch(interactive_offer)
            self.assertEqual(result_interactive['state'], 'running')
            self.assertFalse(any('submit' in c for c in calls))

    def test_headless_prelaunch_path_confinement_and_symlink_refusal_without_outside_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            ws = Path(tmp) / 'ws'
            ws.mkdir()

            cfg = settings(ws); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': str(ws), 'config_dir': str(ws / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6-sol', launcher_effort='high')

            calls = []
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                return {}

            d = fleet.Dispatcher(cfg, lambda *a: {}, runner)

            # Case 1: .local ancestor is a symlink pointing outside
            (ws / '.local').symlink_to(outside)
            normal_offer = offer(mode='headless', prompt='Normal task')
            with self.assertRaisesRegex(ValueError, 'Refuse symlinked ancestor directory'):
                d.dispatch(normal_offer)
            # Verify no files or directories created inside outside directory
            self.assertEqual(list(outside.iterdir()), [])
            (ws / '.local').unlink()

            # Case 2: hub-results ancestor is a symlink pointing outside
            (ws / '.local').mkdir(mode=0o700)
            (ws / '.local' / 'hub-results').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'Refuse symlinked ancestor directory'):
                d.dispatch(normal_offer)
            self.assertEqual(list(outside.iterdir()), [])
            (ws / '.local' / 'hub-results').unlink()

            # Case 3: result leaf is pre-existing symlink pointing to an outside file
            (ws / '.local' / 'hub-results').mkdir(mode=0o700)
            target_outside_file = outside / 'secret.json'
            leaf_symlink = ws / '.local' / 'hub-results' / (normal_offer['id'] + '.json')
            leaf_symlink.symlink_to(target_outside_file)
            with self.assertRaisesRegex(ValueError, 'Refuse symlinked result leaf'):
                d.dispatch(normal_offer)
            self.assertFalse(target_outside_file.exists())
            leaf_symlink.unlink()

            # Case 4: Offer identity traversal / lookalike validation
            for bad_id in [
                '../../etc/passwd',
                '../escape',
                'evil/slash',
                '.hidden',
                '',
                'a' * 65,
                '-bad-start',
                '_bad-start',
                'bad$char',
                'bad char',
            ]:
                with self.subTest(bad_id=bad_id):
                    bad_offer = offer(mode='headless', id=bad_id, prompt='Test bad id')
                    with self.assertRaises(ValueError):
                        d.dispatch(bad_offer)
            self.assertEqual(list(outside.iterdir()), [])

            # Case 5: Idempotent contract: multiple dispatches re-use directory cleanly with mode 0o700
            runner_calls = []
            def ok_runner(argv, **kw):
                runner_calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'task_fit': 1.0, 'account_label': 'shared-codex', 'profile': None}]}
                if 'run' in argv:
                    return {'receipt': {'unit_name': 'headless-unit', 'exit_code': 0}}
                return {}

            d_ok = fleet.Dispatcher(cfg, lambda *a: {}, ok_runner)
            res1 = d_ok.dispatch(offer(mode='headless', id='first-task-1', prompt='First run'))
            self.assertEqual(res1['state'], 'exited')
            self.assertEqual(stat.S_IMODE((ws / '.local' / 'hub-results').stat().st_mode), 0o700)

            res2 = d_ok.dispatch(offer(mode='headless', id='second-task-2', prompt='Second run'))
            self.assertEqual(res2['state'], 'exited')
            self.assertEqual(stat.S_IMODE((ws / '.local' / 'hub-results').stat().st_mode), 0o700)

    def test_task_assigned_empty_or_malformed_report_refuses_successful_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp)); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': tmp, 'config_dir': str(Path(tmp) / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6.1-sol', launcher_effort='high')

            assigned_offer = offer(mode='headless', task_id='task-intake-abc', prompt='Do task',
                                   needs=[], selection={'id': 'sol', 'model': 'gpt-6.1-sol', 'effort': 'high'})
            result_file = Path(tmp) / '.local' / 'hub-results' / (assigned_offer['id'] + '.json')

            for case_name, file_content, process_exit_code, expected_status, is_symlink in [
                ("empty_file", "", 0, "failed", False),
                ("malformed_json", "not valid json {", 0, "failed", False),
                ("missing_status", json.dumps({"report": "missing status field"}), 0, "failed", False),
                ("invalid_status_enum", json.dumps({"status": "unknown-status", "report": "bad status"}), 0, "failed", False),
                ("oversized_file", json.dumps({"status": "done", "report": "x" * 25000}), 0, "failed", False),
                ("process_failed_exit", json.dumps({"status": "done", "report": "claimed success"}), 1, "failed", False),
                ("missing_file", None, 0, "failed", False),
                ("symlink_refusal", json.dumps({"status": "done", "report": "via symlink"}), 0, "failed", True),
                ("genuine_success", json.dumps({"status": "done", "report": "valid report", "artifacts": [{"label": "out", "text": "log"}]}), 0, "done", False),
                ("waiting_on_human", json.dumps({"status": "waiting-on-human", "report": "need input", "artifacts": []}), 0, "waiting-on-human", False),
            ]:
                with self.subTest(case=case_name):
                    requests = []
                    def runner(argv, **kw):
                        if argv[0] == 'maintained-gate':
                            return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow(),
                                    'model_supported': True, 'model': 'gpt-6.1-sol', 'effort': 'high'}
                        if 'plan' in argv:
                            return {'eligible': [{'provider': 'codex', 'model': 'gpt-6.1-sol', 'task_fit': 1.0, 'account_label': 'shared-codex', 'profile': None}]}
                        if 'run' in argv:
                            # Workload writes the result file during execution
                            if is_symlink:
                                target = Path(tmp) / 'real_result.json'
                                target.write_text(file_content, encoding='utf-8')
                                result_file.symlink_to(target)
                            elif file_content is not None:
                                result_file.write_text(file_content, encoding='utf-8')
                            return {'receipt': {'model': 'gpt-6.1-sol', 'model_verified': True, 'unit_name': 'headless-unit', 'exit_code': process_exit_code}}
                        return {}

                    d = fleet.Dispatcher(cfg, lambda *a: requests.append(a) or {}, runner)

                    # Ensure clean slate before dispatch
                    if result_file.is_symlink() or result_file.exists():
                        result_file.unlink()

                    res = d.dispatch(assigned_offer)
                    self.assertEqual(res['state'], expected_status)
                    task_result_posts = [r for r in requests if r[1] == f"/api/fleet/tasks/{assigned_offer['task_id']}/result"]
                    self.assertEqual(len(task_result_posts), 1)
                    posted_body = task_result_posts[0][2]
                    self.assertEqual(posted_body['status'], expected_status)
                    self.assertTrue(posted_body['confirmed_exit'])
                    if expected_status == 'done':
                        self.assertEqual(posted_body['report'], 'valid report')
                        self.assertEqual(posted_body['artifacts'], [{"label": "out", "text": "log"}])
                    elif expected_status == 'failed' and case_name != "process_failed_exit":
                        self.assertIn('without a valid structured report', posted_body['report'])

    @unittest.skipUnless(os.path.isdir("/home/alexey/git/agent-quota-launcher/launcher"), "agent-quota-launcher checkout not available")
    def test_isolated_launcher_store_and_reconcile_contract_with_result_path(self):
        import sys
        # Keep the optional checkout and its top-level `tests` package out of
        # the host suite's interpreter. Only this real contract runs in the child.
        if os.environ.get("HUB_TEST_LAUNCHER_CONTRACT_CHILD") != "1":
            import subprocess
            env = dict(os.environ, HUB_TEST_LAUNCHER_CONTRACT_CHILD="1")
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "FleetTests.test_isolated_launcher_store_and_reconcile_contract_with_result_path"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            return
        launcher_repo = Path("/home/alexey/git/agent-quota-launcher")
        if not (launcher_repo / "launcher" / "store.py").is_file():
            self.skipTest("Optional Launcher checkout is unavailable")
        if str(launcher_repo) not in sys.path:
            sys.path.insert(0, str(launcher_repo))
        from launcher.store import Store
        from launcher.cli import _result_evidence, reconcile, accept
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "launcher_config"
            config_dir.mkdir(parents=True)
            db_path = str(config_dir / "state.db")
            store = Store(db_path)

            ws = Path(tmp) / "workspace"
            ws.mkdir()
            offer_id = "550e8400-e29b-41d4-a716-446655440000"
            result_file = ws / ".local" / "hub-results" / f"{offer_id}.json"
            result_file.parent.mkdir(parents=True, exist_ok=True)

            task_id = f"hub-{offer_id}"
            # Model ACTUAL normal headless Hub offer WITHOUT task_id
            payload = {
                "owner": "hub-fleet",
                "cwd": str(ws),
                "timeout": 1800,
                "goal": f"Normal intake task\nWrite a final JSON object to {result_file}: ...",
                "model_requirements": {"providers": ["antigravity"], "models": ["gemini-3.8-flash-high"]},
                "account_label": "shared-antigravity",
                "profile": "model-task",
            }

            # Case 1: Submit with NO paths (the pre-repair defect)
            defect_task_id = f"hub-defect-{offer_id}"
            store.submit_task(defect_task_id, defect_task_id, payload, paths=[])
            self.assertEqual(store.get_task_paths(defect_task_id), [])
            # Result file exists on disk
            result_file.write_text(json.dumps({"status": "done", "report": "ok"}), encoding="utf-8")
            # Evidence count is 0 because owned_paths is empty!
            self.assertEqual(_result_evidence(store, defect_task_id), [])

            # Case 2: Submit with repaired exact result path
            store.submit_task(task_id, task_id, payload, paths=[str(result_file)])
            registered_paths = store.get_task_paths(task_id)
            self.assertEqual(registered_paths, [str(result_file.resolve())])

            # Evidence exists when file is present and non-empty
            evidence = _result_evidence(store, task_id)
            self.assertEqual(evidence, [str(result_file.resolve())])

            # Evidence is empty if file is empty
            result_file.write_text("", encoding="utf-8")
            self.assertEqual(_result_evidence(store, task_id), [])

            # Restore non-empty file
            result_file.write_text(json.dumps({"status": "done", "report": "ok"}), encoding="utf-8")
            self.assertEqual(_result_evidence(store, task_id), [str(result_file.resolve())])

            # Transition task to running with a simulated native binding
            store.transition_task(task_id, "running", ("queued",), reason="started")
            native_id = "00000000-0000-4000-8000-000000000001"
            from launcher.tags import run_tag_for
            store.record_native_binding(
                task_id,
                native_id=native_id,
                tag=run_tag_for(task_id),
                workspace=str(ws),
                engine="antigravity",
                incarnation=1792323159000,
            )

            # Test reconcile transitions to completed-awaiting-review
            import contextlib
            with patch("launcher.native_lifecycle.probe_native_lifecycle", return_value=("retired-finished", "matching finished tombstone verified")):
                reconcile_args = argparse.Namespace(
                    id=task_id,
                    config_dir=config_dir,
                    reviewer="test-reconcile",
                    json=True,
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = reconcile(reconcile_args)
                self.assertEqual(rc, 0)
                reconciled_task = store.get_task(task_id)
                self.assertEqual(reconciled_task["state"], "completed-awaiting-review")

            # Test accept succeeds with --no-refill
            accept_args = argparse.Namespace(
                id=task_id,
                config_dir=config_dir,
                reviewer="root",
                refill=False,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                accept_rc = accept(accept_args)
            self.assertEqual(accept_rc, 0)
            accepted_task = store.get_task(task_id)
            self.assertEqual(accepted_task["state"], "accepted")

    def test_normal_headless_positive_retirement_reconciles_and_settles_fleet_result_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            cfg = settings(ws); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': str(ws), 'config_dir': str(ws / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6-sol', launcher_effort='high')
            native_id = "2ac89cc7-2810-44c2-bab6-34e41633b1e5"
            offer_id = "38bb1d42-d697-435e-a537-f8a93ac6cf3e"
            task_id = f"hub-{offer_id}"

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "GET" and path == "/api/fleet/offers":
                    return {"offers": []}
                if method == "POST" and path == "/api/machines/register":
                    return {}
                if method == "POST" and path == "/api/host/poll":
                    return {}
                if method == "PUT" and "/api/fleet/" in path:
                    return {}
                if method == "GET" and path == "/api/sessions/host-bindings":
                    return {"sessions": []}
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/claim":
                    return {"id": offer_id, "state": "starting"}
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/result":
                    return {"id": offer_id, "state": body.get("state")}
                return {}

            calls = []
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'task_fit': 1.0}]}
                if 'run' in argv:
                    return {'receipt': {'session_id': native_id, 'state': 'running', 'first_action_verified': True,
                                        'tag': f"task-{task_id}", 'workspace': str(ws.resolve()), 'engine': 'codex',
                                        'created_at_ms': 1792330666399}}
                if 'reconcile' in argv:
                    return {'task_id': task_id, 'native_id': native_id, 'state': 'completed-awaiting-review', 'resources_reserved': False,
                            'reconciled_action': 'completed-awaiting-review', 'result_evidence_count': 1}
                if argv[-2:] == ['list', '--json']:
                    return []
                return {}

            d = fleet.Dispatcher(cfg, request, runner, journal_dir=str(Path(tmp) / 'journal'))
            normal_offer = offer(mode='headless', id=offer_id, prompt='Test normal task')
            disp_res = d.dispatch(normal_offer)
            self.assertEqual(disp_res['state'], 'running')
            self.assertEqual(disp_res['native_id'], native_id)

            # Pre-retirement: reconcile returns no terminal settlement while workload is alive
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(False, None, "target_still_active")):
                pre_reconciled = d.reconcile_launches()
                self.assertEqual(pre_reconciled, [])
                self.assertFalse(any(path.endswith('/result') and body.get('confirmed_exit') for m, path, body in requests))

            # Post positive retirement: verify_native_retirement returns True
            tombstone = {"cause": "finished", "id": native_id, "tag": f"task-{task_id}",
                         "workspace": str(ws.resolve()), "finished_at_ms": 1792330700000}
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                reconciled = d.reconcile_launches()
                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0]['id'], offer_id)
                self.assertEqual(reconciled[0]['state'], 'exited')
                self.assertTrue(reconciled[0]['reconciled'])

            # Verify terminal result posted to own-host fleet result endpoint
            terminal_post = next(r for r in requests if r[1] == f"/api/fleet/launches/{offer_id}/result" and r[2].get('confirmed_exit'))
            self.assertEqual(terminal_post[2]['state'], 'exited')
            self.assertEqual(terminal_post[2]['native_id'], native_id)
            self.assertEqual(terminal_post[2]['launcher_task_id'], task_id)
            self.assertTrue(terminal_post[2]['confirmed_exit'])

            # Verify launcher reconcile was called
            self.assertTrue(any('reconcile' in c and task_id in c for c in calls))

            # Sub-round: second reconcile pass is idempotent, zero duplicate calls
            calls_before = len(calls)
            requests_before = len(requests)
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                second_reconciled = d.reconcile_launches()
                self.assertEqual(second_reconciled, [])
            self.assertEqual(len(calls), calls_before)
            self.assertEqual(len(requests), requests_before)

    def test_normal_headless_restart_resumes_pending_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            journal = Path(tmp) / 'journal'
            journal.mkdir()
            cfg = settings(ws); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': str(ws), 'config_dir': str(ws / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6-sol', launcher_effort='high')
            native_id = "2ac89cc7-2810-44c2-bab6-34e41633b1e5"
            offer_id = "38bb1d42-d697-435e-a537-f8a93ac6cf3e"
            task_id = f"hub-{offer_id}"

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/claim":
                    return {"id": offer_id, "state": "starting"}
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/result":
                    return {"id": offer_id, "state": body.get("state")}
                return {}

            def runner(argv, **kw):
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'task_fit': 1.0}]}
                if 'run' in argv:
                    return {'receipt': {'session_id': native_id, 'state': 'running', 'first_action_verified': True,
                                        'tag': f"task-{task_id}", 'workspace': str(ws.resolve()), 'engine': 'codex',
                                        'created_at_ms': 1792330666399}}
                if 'reconcile' in argv:
                    return {'task_id': task_id, 'native_id': native_id, 'state': 'completed-awaiting-review', 'resources_reserved': False}
                return {}

            # Dispatch with first Dispatcher instance
            d1 = fleet.Dispatcher(cfg, request, runner, journal_dir=str(journal))
            d1.dispatch(offer(mode='headless', id=offer_id, prompt='Test task'))

            # Check journal file exists on disk
            j_file = journal / 'fleet-launches' / f"{offer_id}.json"
            self.assertTrue(j_file.is_file())
            j_data = json.loads(j_file.read_text(encoding='utf-8'))
            self.assertEqual(j_data['state'], 'running')
            self.assertFalse(j_data['settled'])
            self.assertEqual(j_data['native_id'], native_id)
            self.assertEqual(j_data['incarnation'], 1792330666399)

            # Host restart: instantiate brand new Dispatcher with same journal
            d2 = fleet.Dispatcher(cfg, request, runner, journal_dir=str(journal))
            self.assertIn(offer_id, d2._active_launches)
            self.assertEqual(d2._active_launches[offer_id]['native_id'], native_id)

            # Reconcile on d2 after restart
            tombstone = {"cause": "finished", "id": native_id, "tag": f"task-{task_id}",
                         "workspace": str(ws.resolve()), "finished_at_ms": 1792330700000}
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                reconciled = d2.reconcile_launches()
                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0]['state'], 'exited')

            # Journal on disk updated to settled
            j_data_after = json.loads(j_file.read_text(encoding='utf-8'))
            self.assertTrue(j_data_after['settled'])
            self.assertEqual(j_data_after['state'], 'exited')

    def test_normal_headless_lost_response_retry_recovers_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            journal = Path(tmp) / 'journal'
            journal.mkdir()
            cfg = settings(ws); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': str(ws), 'config_dir': str(ws / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6-sol', launcher_effort='high')
            native_id = "2ac89cc7-2810-44c2-bab6-34e41633b1e5"
            offer_id = "38bb1d42-d697-435e-a537-f8a93ac6cf3e"
            task_id = f"hub-{offer_id}"

            fail_post = [True]
            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/claim":
                    return {"id": offer_id, "state": "starting"}
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/result":
                    if body.get('confirmed_exit') and fail_post[0]:
                        fail_post[0] = False
                        raise ConnectionResetError("network lost response")
                    if body.get('confirmed_exit') and not fail_post[0]:
                        # Exact committed terminal receipt is acknowledged idempotently.
                        return {"id": offer_id, "state": body["state"], "replay": True}
                    return {"id": offer_id, "state": body.get("state")}
                return {}

            run_calls = []
            def runner(argv, **kw):
                if 'run' in argv:
                    run_calls.append(argv)
                    return {'receipt': {'session_id': native_id, 'state': 'running', 'first_action_verified': True,
                                        'tag': f"task-{task_id}", 'workspace': str(ws.resolve()), 'engine': 'codex',
                                        'created_at_ms': 1792330666399}}
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'task_fit': 1.0}]}
                if 'reconcile' in argv:
                    return {'task_id': task_id, 'native_id': native_id, 'state': 'completed-awaiting-review', 'resources_reserved': False}
                return {}

            d = fleet.Dispatcher(cfg, request, runner, journal_dir=str(journal))
            d.dispatch(offer(mode='headless', id=offer_id, prompt='Test task'))
            self.assertEqual(len(run_calls), 1)

            tombstone = {"cause": "finished", "id": native_id, "tag": f"task-{task_id}",
                         "workspace": str(ws.resolve()), "finished_at_ms": 1792330700000}
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                # First attempt: connection reset
                first_attempt = d.reconcile_launches()
                self.assertEqual(first_attempt, [])
                self.assertFalse(d._active_launches[offer_id]['settled'])

                # Second attempt receives an explicit acknowledgement of the committed receipt
                second_attempt = d.reconcile_launches()
                self.assertEqual(len(second_attempt), 1)
                self.assertTrue(second_attempt[0]['reconciled'])
                self.assertTrue(d._active_launches[offer_id]['settled'])

            # No duplicate model starts!
            self.assertEqual(len(run_calls), 1)

    def test_normal_headless_unsettled_cases_preserve_reservation_and_refuse_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            cfg = settings(ws); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': str(ws), 'config_dir': str(ws / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6-sol', launcher_effort='high')
            native_id = "2ac89cc7-2810-44c2-bab6-34e41633b1e5"
            offer_id = "38bb1d42-d697-435e-a537-f8a93ac6cf3e"
            task_id = f"hub-{offer_id}"

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/claim":
                    return {"id": offer_id, "state": "starting"}
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/result":
                    return {"id": offer_id, "state": body.get("state")}
                return {}

            reconcile_calls = []
            def runner(argv, **kw):
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'task_fit': 1.0}]}
                if 'run' in argv:
                    return {'receipt': {'session_id': native_id, 'state': 'running', 'first_action_verified': True,
                                        'tag': f"task-{task_id}", 'workspace': str(ws.resolve()), 'engine': 'codex',
                                        'created_at_ms': 1792330666399}}
                if 'reconcile' in argv:
                    reconcile_calls.append(argv)
                    return {'task_id': task_id, 'native_id': native_id, 'state': 'completed-awaiting-review', 'resources_reserved': False}
                return {}

            d = fleet.Dispatcher(cfg, request, runner)
            d.dispatch(offer(mode='headless', id=offer_id, prompt='Test task'))

            # Case A: Live workload in catalog
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(False, None, "target_still_active_in_catalog: phase=running")):
                self.assertEqual(d.reconcile_launches(), [])
            self.assertEqual(reconcile_calls, [])

            # Case B: Tombstone absent / unknown
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(False, None, "retired_tombstone_absent: /path/to/tombstone.json")):
                self.assertEqual(d.reconcile_launches(), [])
            self.assertEqual(reconcile_calls, [])

            # Case C: Catalog error / offline API
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(False, None, "live_catalog_query_failed: timeout")):
                self.assertEqual(d.reconcile_launches(), [])
            self.assertEqual(reconcile_calls, [])

            # Case D: Wrong incarnation in record
            d._active_launches[offer_id]['incarnation'] = -1
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, {}, "retired_tombstone_verified")):
                self.assertEqual(d.reconcile_launches(), [])
            self.assertEqual(reconcile_calls, [])
            d._active_launches[offer_id]['incarnation'] = 1792330666399

            # Case E: Launcher reports resources still reserved
            def runner_busy(argv, **kw):
                if 'reconcile' in argv:
                    reconcile_calls.append(argv)
                    return {'task_id': task_id, 'native_id': native_id, 'state': 'running', 'resources_reserved': True}
                return runner(argv, **kw)
            d.runner = runner_busy
            tombstone = {"cause": "finished", "id": native_id, "tag": f"task-{task_id}",
                         "workspace": str(ws.resolve()), "finished_at_ms": 1792330700000}
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                self.assertEqual(d.reconcile_launches(), [])
            self.assertFalse(any(r[1].endswith('/result') and r[2].get('confirmed_exit') for r in requests))

    def test_normal_headless_failed_evidence_settles_failed_without_business_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            cfg = settings(ws); cfg['capabilities'] = []
            cfg['launcher'] = {'argv': ['python3', '-m', 'launcher'], 'root': str(ws), 'config_dir': str(ws / 'config')}
            cfg['subscriptions'][0].update(launcher_provider='codex', launcher_model='gpt-6-sol', launcher_effort='high')
            native_id = "2ac89cc7-2810-44c2-bab6-34e41633b1e5"
            offer_id = "38bb1d42-d697-435e-a537-f8a93ac6cf3e"
            task_id = f"hub-{offer_id}"

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/claim":
                    return {"id": offer_id, "state": "starting"}
                if method == "POST" and path == f"/api/fleet/launches/{offer_id}/result":
                    return {"id": offer_id, "state": body.get("state")}
                return {}

            launcher_cmds = []
            def runner(argv, **kw):
                launcher_cmds.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol', 'task_fit': 1.0}]}
                if 'run' in argv:
                    return {'receipt': {'session_id': native_id, 'state': 'running', 'first_action_verified': True,
                                        'tag': f"task-{task_id}", 'workspace': str(ws.resolve()), 'engine': 'codex',
                                        'created_at_ms': 1792330666399}}
                if 'reconcile' in argv:
                    # Native died with no evidence -> launcher transitions to failed
                    return {'task_id': task_id, 'native_id': native_id, 'state': 'failed', 'resources_reserved': False,
                            'reconciled_action': 'failed', 'result_evidence_count': 0}
                return {}

            d = fleet.Dispatcher(cfg, request, runner)
            d.dispatch(offer(mode='headless', id=offer_id, prompt='Test task'))

            tombstone = {"cause": "finished", "id": native_id, "tag": f"task-{task_id}",
                         "workspace": str(ws.resolve()), "finished_at_ms": 1792330700000}
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                reconciled = d.reconcile_launches()
                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0]['state'], 'failed')

            terminal_post = next(r for r in requests if r[1] == f"/api/fleet/launches/{offer_id}/result" and r[2].get('confirmed_exit'))
            self.assertEqual(terminal_post[2]['state'], 'failed')

            # Strictly verify launcher accept was NEVER invoked
            self.assertFalse(any('accept' in c for c in launcher_cmds))
            # Strictly verify no task result was posted to /api/fleet/tasks
            self.assertFalse(any('/api/fleet/tasks' in r[1] for r in requests))

    def test_normal_headless_journal_fails_closed_on_corrupt_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / 'journal'
            launches_dir = journal / 'fleet-launches'
            launches_dir.mkdir(parents=True)

            # Corrupt file 1: invalid json
            (launches_dir / 'corrupt1.json').write_text("{broken json", encoding='utf-8')
            # Corrupt file 2: empty file
            (launches_dir / 'empty.json').write_text("", encoding='utf-8')
            # Corrupt file 3: not a dict
            (launches_dir / 'notdict.json').write_text('["list"]', encoding='utf-8')
            # Corrupt file 4: missing required 5-field tuple keys
            (launches_dir / 'missing_keys.json').write_text('{"state": "running", "offer_id": "bad"}', encoding='utf-8')
            # Symlink pointing to valid file outside
            target = Path(tmp) / 'outside.json'
            target.write_text('{"offer_id": "outside", "state": "running"}', encoding='utf-8')
            (launches_dir / 'symlink.json').symlink_to(target)

            cfg = settings(Path(tmp))
            # Must load cleanly without raising and with zero active launches loaded
            d = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a, **kw: {}, journal_dir=str(journal))
            self.assertEqual(d._active_launches, {})


    def test_normal_headless_requires_exact_release_identity_and_terminal_ack(self):
        cases = ["valid", "accepted", "missing_release", "truthy_release", "foreign_native", "missing_native", "transition_error", "wrong_ack", "empty_ack"]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / "ws"
                ws.mkdir()
                cfg = settings(ws)
                cfg["launcher"] = {"argv": ["maintained-launcher"], "root": str(ws), "config_dir": str(ws / "config")}
                offer_id = "38bb1d42-d697-435e-a537-f8a93ac6cf3e"
                task_id = "hub-" + offer_id
                native_id = "2ac89cc7-2810-44c2-bab6-34e41633b1e5"
                output = {"task_id": task_id, "native_id": native_id, "state": "accepted" if case == "accepted" else "completed-awaiting-review", "resources_reserved": False}
                if case == "missing_release":
                    output.pop("resources_reserved")
                if case == "truthy_release":
                    output["resources_reserved"] = "false"
                if case == "foreign_native":
                    output["native_id"] = "72df443d-e9b1-4bfe-b025-3454f9f09e93"
                if case == "missing_native":
                    output.pop("native_id")
                calls = []
                def request(method, path, body=None):
                    calls.append((method, path, body))
                    if case == "transition_error":
                        raise RuntimeError("409 invalid_transition")
                    if case == "wrong_ack":
                        return {"id": "other", "state": "exited"}
                    if case == "empty_ack":
                        return {}
                    return {"id": offer_id, "state": "exited"}
                commands = []
                def runner(argv, **kw):
                    commands.append(argv)
                    return output
                d = fleet.Dispatcher(cfg, request, runner, journal_dir=str(Path(tmp) / "journal"))
                d._record_running_launch(offer_id, task_id, ws, "codex", "shared-codex", native_id, ws / "result.json", {"tag": "task-" + task_id, "created_at_ms": 1792330666399})
                self.assertIn(offer_id, d._active_launches)
                with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, {}, "retired_tombstone_verified")):
                    settled = d.reconcile_launches()
                accepted = case in {"valid", "accepted"}
                self.assertEqual(bool(settled), accepted)
                self.assertIs(d._active_launches[offer_id]["settled"], accepted)
                if case in {"missing_release", "truthy_release", "foreign_native", "missing_native"}:
                    self.assertEqual(calls, [])
                self.assertFalse(any("accept" in x or "reject" in x or "run" in x for x in commands))

    def test_normal_headless_restart_rejects_uncoupled_journal_identity(self):
        for field, value in [("task_id", "foreign-task"), ("tag", "foreign-tag"), ("mode", "interactive")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / "ws"
                ws.mkdir()
                cfg = settings(ws)
                journal = Path(tmp) / "journal"
                offer_id = "38bb1d42-d697-435e-a537-f8a93ac6cf3e"
                d = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a, **kw: {}, journal_dir=str(journal))
                d._record_running_launch(offer_id, "hub-" + offer_id, ws, "codex", "shared-codex", "2ac89cc7-2810-44c2-bab6-34e41633b1e5", ws / "result.json", {"tag": "task-hub-" + offer_id, "created_at_ms": 1792330666399})
                path = journal / "fleet-launches" / (offer_id + ".json")
                data = json.loads(path.read_text())
                data[field] = value
                path.write_text(json.dumps(data))
                restored = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a, **kw: {}, journal_dir=str(journal))
                self.assertEqual(restored._active_launches, {})
                self.assertTrue(path.exists())

    def test_imported_session_positive_retirement_settles_stopped_and_replays_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            cfg = settings(ws)
            native_id = "2d7bc739-3162-4fb9-9c39-691a1b328726"
            tag = "task-hub-role-head-recovery-01M57V53RB1XM8RZ0R4GWK2P7W-g3-e15"
            incarnation = 1792338485058
            ws_path = str(ws.resolve())

            imported_session = {
                "id": native_id,
                "engine": "antigravity",
                "state": "running",
                "session_type": "aplexer",
                "binding": {
                    "native_id": native_id,
                    "tag": tag,
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": incarnation,
                    "provenance": "observed_native",
                    "adopted_at": "2026-10-18T15:48:32.883Z",
                },
            }

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "GET" and path == "/api/sessions/host-bindings?kind=imported":
                    return {"sessions": [imported_session], "next_cursor": None}
                if method == "POST" and path == f"/api/sessions/{native_id}/retirement":
                    return {"id": native_id, "state": "stopped", "replay": False}
                return {}

            commands = []
            def runner(argv, **kw):
                commands.append(argv)
                return {}

            d = fleet.Dispatcher(cfg, request, runner)

            tombstone = {
                "cause": "finished",
                "id": native_id,
                "tag": tag,
                "workspace": ws_path,
                "finished_at_ms": 1792338699929,
            }

            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")) as mock_verify:
                reconciled = d.reconcile_imported_sessions()
                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0], {"id": native_id, "state": "stopped", "reconciled": True})

                # Verify mock_verify was called with the exact 5-field NativeTarget
                mock_verify.assert_called_once()
                target_arg = mock_verify.call_args[0][1]
                self.assertEqual(target_arg.session_id, native_id)
                self.assertEqual(target_arg.tag, tag)
                self.assertEqual(target_arg.workspace, ws_path)
                self.assertEqual(target_arg.created_at_ms, incarnation)
                self.assertEqual(target_arg.engine, "antigravity")

            # Check terminal POST payload sent to Hub
            terminal_post = next(r for r in requests if r[1] == f"/api/sessions/{native_id}/retirement")
            self.assertEqual(terminal_post[0], "POST")
            self.assertEqual(terminal_post[2]["native"]["native_id"], native_id)
            self.assertEqual(terminal_post[2]["native"]["tag"], tag)
            self.assertEqual(terminal_post[2]["native"]["workspace"], ws_path)
            self.assertEqual(terminal_post[2]["native"]["created_at_ms"], incarnation)
            self.assertEqual(terminal_post[2]["native"]["engine"], "antigravity")
            self.assertEqual(terminal_post[2]["finished_at_ms"], 1792338699929)
            self.assertEqual(terminal_post[2]["cause"], "finished")
            self.assertEqual(terminal_post[2]["state"], "stopped")
            self.assertEqual(terminal_post[2]["generation"], 1)

            # Replay idempotency: server returns replay: True on second report
            def request_replay(method, path, body=None):
                if method == "GET" and path == "/api/sessions/host-bindings?kind=imported":
                    return {"sessions": [imported_session], "next_cursor": None}
                if method == "POST" and path == f"/api/sessions/{native_id}/retirement":
                    return {"id": native_id, "state": "stopped", "replay": True}
                return {}

            d.request = request_replay
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                second = d.reconcile_imported_sessions()
                self.assertEqual(len(second), 1)
                self.assertEqual(second[0]["state"], "stopped")

            # Verify launcher reconcile / task accept was NEVER invoked
            self.assertFalse(any("accept" in x or "reconcile" in x for x in commands))

    def test_imported_session_negative_cases_preserve_custody_and_never_settle(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            cfg = settings(ws)
            native_id = "2d7bc739-3162-4fb9-9c39-691a1b328726"
            tag = "task-hub-role-head-recovery-01M57V53RB1XM8RZ0R4GWK2P7W-g3-e15"
            incarnation = 1792338485058
            ws_path = str(ws.resolve())

            base_session = {
                "id": native_id,
                "engine": "antigravity",
                "state": "running",
                "binding": {
                    "native_id": native_id,
                    "tag": tag,
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": incarnation,
                    "provenance": "observed_native",
                },
            }

            cases = [
                ("live_workload", False, None, "target_still_active_in_catalog: phase=running"),
                ("tombstone_absent", False, None, "retired_tombstone_absent: /path/to/tombstone.json"),
                ("catalog_error", False, None, "live_catalog_query_failed: timeout"),
                ("tag_reused", False, None, "tag_reused_by_active_session: session_id=other phase=running"),
            ]

            for case_name, is_ret, tomb, detail in cases:
                with self.subTest(case=case_name):
                    d = fleet.Dispatcher(cfg, lambda m, p, b=None: {"sessions": [base_session]})
                    with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(is_ret, tomb, detail)):
                        res = d.reconcile_imported_sessions()
                        self.assertEqual(res, [])

            # Tuple mismatch cases (should be skipped before verify_native_retirement)
            mismatch_cases = [
                ("wrong_incarnation", {**base_session, "binding": {**base_session["binding"], "native_created_at_ms": -5}}),
                ("relative_workspace", {**base_session, "binding": {**base_session["binding"], "workspace": "relative/path"}}),
                ("missing_tag", {**base_session, "binding": {**base_session["binding"], "tag": ""}}),
                ("foreign_provenance", {**base_session, "binding": {**base_session["binding"], "provenance": "fleet_launch"}}),
            ]

            for case_name, sess in mismatch_cases:
                with self.subTest(mismatch=case_name):
                    d = fleet.Dispatcher(cfg, lambda m, p, b=None: {"sessions": [sess]})
                    with patch("agent_hub_host.fleet.verify_native_retirement") as mock_v:
                        res = d.reconcile_imported_sessions()
                        self.assertEqual(res, [])
                        mock_v.assert_not_called()

            # Server error on terminal post preserves state and does not settle
            tombstone = {"cause": "finished", "id": native_id, "tag": tag, "workspace": ws_path, "finished_at_ms": 1792338699929}
            def request_fail(m, p, b=None):
                if m == "GET": return {"sessions": [base_session], "next_cursor": None}
                raise RuntimeError("500 internal server error")
            d = fleet.Dispatcher(cfg, request_fail)
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                self.assertEqual(d.reconcile_imported_sessions(), [])

    def test_imported_session_bootstrap_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            cfg = settings(ws)
            native_id = "021d9370-9b3d-4e5a-b59b-256334c08c75"
            tag = "batching"
            ws_path = str(ws.resolve())
            incarnation = 1792257043571

            imported_session = {
                "id": native_id,
                "engine": "argon",
                "state": "running",
                "binding": {
                    "native_id": native_id,
                    "tag": tag,
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": incarnation,
                    "provenance": "observed_native",
                },
            }

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "GET":
                    return {"sessions": [imported_session], "next_cursor": None}
                if method == "POST":
                    return {"id": native_id, "state": "stopped"}
                return {}

            # Fresh host start with empty active launches
            d = fleet.Dispatcher(cfg, request, lambda *a, **kw: {})
            self.assertEqual(d._active_launches, {})

            tombstone = {"cause": "finished", "id": native_id, "tag": tag, "workspace": ws_path, "finished_at_ms": 1792257100000}
            with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                reconciled = d.reconcile_imported_sessions()
                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0]["id"], native_id)
                self.assertEqual(reconciled[0]["state"], "stopped")

    def test_dispatcher_once_full_cycle_settles_imported_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            cfg = settings(ws)
            native_id = "2d7bc739-3162-4fb9-9c39-691a1b328726"
            tag = "task-hub-role-head-recovery-01M57V53RB1XM8RZ0R4GWK2P7W-g3-e15"
            incarnation = 1792338485058
            ws_path = str(ws.resolve())

            imported_session = {
                "id": native_id,
                "engine": "antigravity",
                "state": "running",
                "session_type": "aplexer",
                "binding": {
                    "native_id": native_id,
                    "tag": tag,
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": incarnation,
                    "provenance": "observed_native",
                    "adopted_at": "2026-10-18T15:48:32.883Z",
                },
            }

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "GET" and path == "/api/sessions/host-bindings":
                    return {"sessions": []}
                if method == "GET" and path == "/api/sessions/host-bindings?kind=imported":
                    return {"sessions": [imported_session], "next_cursor": None}
                if method == "POST" and path == f"/api/sessions/{native_id}/retirement":
                    return {"id": native_id, "state": "stopped"}
                return {"offers": []}

            def runner(argv, **kw):
                if argv[0] == "maintained-gate":
                    return {"launch_allowed": True, "profile_supported": True, "account_label": "shared-codex", "observed_at": fleet.utcnow()}
                if argv[0] == "aplexer":
                    return []
                return {}

            tombstone = {"cause": "finished", "id": native_id, "tag": tag, "workspace": ws_path, "finished_at_ms": 1792338699929}
            with patch("agent_hub_host.system.get_system_info", return_value={"capabilities": {}, "hostname": "test", "os": "test", "network": "home", "roles": []}), \
                 patch("agent_hub_host.api.get_heartbeat_payload", return_value={"type": "heartbeat"}), \
                 patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                d = fleet.Dispatcher(cfg, request, runner)
                # Hermetic native state for the appended work-claim summary.
                with tempfile.TemporaryDirectory() as statedir:
                    d.settings["aplexer_state_dir"] = statedir
                    results = d.once()
                reconciled = [r for r in results if r.get("id") != "workclaim-sync"]
                bridge = [r for r in results if r.get("id") == "workclaim-sync"]
                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0]["id"], native_id)
                self.assertEqual(reconciled[0]["state"], "stopped")
                self.assertTrue(reconciled[0]["reconciled"])
                self.assertEqual(len(bridge), 1)
                self.assertTrue(bridge[0]["ok"])

            self.assertTrue(any(r[1] == f"/api/sessions/{native_id}/retirement" for r in requests))

    def test_imported_session_correlated_ack_boundaries(self):
        """Correlated ACK boundary: report reconciled ONLY when ack.id equals exact target UUID
        AND state == 'stopped'. Replay true alone, wrong id, missing id, or wrong state is NOT ACK.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            cfg = settings(ws)
            native_id = "2d7bc739-3162-4fb9-9c39-691a1b328726"
            tag = "task-hub-role-head-recovery-01M57V53RB1XM8RZ0R4GWK2P7W-g3-e15"
            ws_path = str(ws.resolve())
            incarnation = 1792338485058

            imported_session = {
                "id": native_id,
                "engine": "antigravity",
                "state": "running",
                "binding": {
                    "native_id": native_id,
                    "tag": tag,
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": incarnation,
                    "provenance": "observed_native",
                },
            }

            tombstone = {
                "cause": "finished",
                "id": native_id,
                "tag": tag,
                "workspace": ws_path,
                "finished_at_ms": 1792338699929,
            }

            # Boundary probes:
            cases = [
                ("unrelated_replay", {"replay": True}, False),
                ("wrong_id_stopped", {"id": "00000000-0000-0000-0000-000000000000", "state": "stopped"}, False),
                ("missing_id_stopped", {"state": "stopped"}, False),
                ("correct_id_wrong_state", {"id": native_id, "state": "running"}, False),
                ("empty_dict", {}, False),
                ("positive_control_new", {"id": native_id, "state": "stopped", "replay": False}, True),
                ("positive_control_replay", {"id": native_id, "state": "stopped", "replay": True}, True),
            ]

            for case_name, ack_response, expect_reconciled in cases:
                with self.subTest(case=case_name):
                    def request(method, path, body=None):
                        if method == "GET":
                            return {"sessions": [imported_session], "next_cursor": None}
                        if method == "POST":
                            return ack_response
                        return {}

                    d = fleet.Dispatcher(cfg, request, lambda *a, **kw: {})
                    with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, tombstone, "retired_tombstone_verified")):
                        reconciled = d.reconcile_imported_sessions()
                        if expect_reconciled:
                            self.assertEqual(len(reconciled), 1)
                            self.assertEqual(reconciled[0]["id"], native_id)
                            self.assertEqual(reconciled[0]["state"], "stopped")
                        else:
                            self.assertEqual(reconciled, [])

    def test_imported_session_fair_pagination_across_cycles_and_restart(self):
        """Fair bounded pagination: host queries canonical imported GET, respects next_cursor
        even when page is empty/live-only, persists cursor to journal_dir, resumes across restart,
        and wraps around when list completes. Invalid cursor fails safely.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            jdir = Path(tmp) / "journal"
            jdir.mkdir(mode=0o700)
            cfg = settings(ws)
            cfg["journal_dir"] = str(jdir)

            native_id = "2d7bc739-3162-4fb9-9c39-691a1b328726"
            tag = "task-hub-role-head-recovery-01M57V53RB1XM8RZ0R4GWK2P7W-g3-e15"
            ws_path = str(ws.resolve())
            incarnation = 1792338485058

            live_session_1 = {
                "id": "11111111-1111-1111-1111-111111111111",
                "engine": "antigravity",
                "state": "running",
                "binding": {
                    "native_id": "11111111-1111-1111-1111-111111111111",
                    "tag": "tag-1",
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": incarnation,
                    "provenance": "observed_native",
                },
            }
            finished_session_2 = {
                "id": native_id,
                "engine": "antigravity",
                "state": "running",
                "binding": {
                    "native_id": native_id,
                    "tag": tag,
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": incarnation,
                    "provenance": "observed_native",
                },
            }

            tombstone_finished = {
                "cause": "finished",
                "id": native_id,
                "tag": tag,
                "workspace": ws_path,
                "finished_at_ms": 1792338699929,
            }

            requests = []
            def request(method, path, body=None):
                requests.append((method, path, body))
                if method == "GET":
                    if path == "/api/sessions/host-bindings?kind=imported":
                        # Page 1: all live, next_cursor points to cur_2
                        return {"sessions": [live_session_1], "next_cursor": "cur_2"}
                    if "cursor=cur_2" in path:
                        # Page 2: empty/live-filtered page, next_cursor points to cur_3
                        return {"sessions": [], "next_cursor": "cur_3"}
                    if "cursor=cur_3" in path:
                        # Page 3: finished session, next_cursor is None (end)
                        return {"sessions": [finished_session_2], "next_cursor": None}
                if method == "POST":
                    return {"id": native_id, "state": "stopped"}
                return {}

            def mock_verify(native_bin, target, state_dir=None):
                if target.session_id == native_id:
                    return (True, tombstone_finished, "verified")
                return (False, None, "still_running")

            # Cycle 1: Page 1 fetched, live only -> reconciled empty, cursor 'cur_2' saved to disk
            d1 = fleet.Dispatcher(cfg, request, lambda *a, **kw: {})
            with patch("agent_hub_host.fleet.verify_native_retirement", side_effect=mock_verify):
                res1 = d1.reconcile_imported_sessions()
                self.assertEqual(res1, [])
                self.assertEqual(d1._get_imported_cursor(), "cur_2")
                # Verify cursor persisted to journal_dir
                cursor_file = jdir / "imported-cursor.json"
                self.assertTrue(cursor_file.is_file())
                self.assertIn("cur_2", cursor_file.read_text())

            # Cycle 2: Page 2 fetched with cursor=cur_2 -> empty page, next_cursor 'cur_3' saved
            with patch("agent_hub_host.fleet.verify_native_retirement", side_effect=mock_verify):
                res2 = d1.reconcile_imported_sessions()
                self.assertEqual(res2, [])
                self.assertEqual(d1._get_imported_cursor(), "cur_3")

            # Cycle 3 (Restart): Process restarts! New Dispatcher instance loaded from cfg
            d2 = fleet.Dispatcher(cfg, request, lambda *a, **kw: {})
            self.assertEqual(d2._get_imported_cursor(), "cur_3")
            with patch("agent_hub_host.fleet.verify_native_retirement", side_effect=mock_verify):
                res3 = d2.reconcile_imported_sessions()
                self.assertEqual(len(res3), 1)
                self.assertEqual(res3[0]["id"], native_id)
                # Next cursor was None -> wrapped to None!
                self.assertIsNone(d2._get_imported_cursor())

            # Cycle 4: Wrapping around to beginning (no cursor in path)
            requests.clear()
            with patch("agent_hub_host.fleet.verify_native_retirement", side_effect=mock_verify):
                d2.reconcile_imported_sessions()
                self.assertEqual(requests[0][1], "/api/sessions/host-bindings?kind=imported")

            # Fault boundary: Corrupted cursor file fails safely without crashing
            cursor_file.write_text("invalid json {{{")
            d3 = fleet.Dispatcher(cfg, request, lambda *a, **kw: {})
            self.assertIsNone(d3._get_imported_cursor())

    def test_imported_session_incomplete_stored_binding_rejected(self):
        """Reject malformed / incomplete imported sessions without attempting retirement."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            cfg = settings(ws)
            ws_path = str(ws.resolve())

            base = {
                "id": "2d7bc739-3162-4fb9-9c39-691a1b328726",
                "engine": "antigravity",
                "state": "running",
                "binding": {
                    "native_id": "2d7bc739-3162-4fb9-9c39-691a1b328726",
                    "tag": "my-tag",
                    "workspace": ws_path,
                    "generation": 1,
                    "native_created_at_ms": 1792338485058,
                    "provenance": "observed_native",
                },
            }

            incomplete_cases = [
                ("missing_engine", {**base, "engine": "", "binding": {**base["binding"], "engine": ""}}),
                ("zero_creation", {**base, "binding": {**base["binding"], "native_created_at_ms": 0}}),
                ("negative_generation", {**base, "binding": {**base["binding"], "generation": -1}}),
                ("non_uuid_id", {**base, "id": "not-a-uuid", "binding": {**base["binding"], "native_id": "not-a-uuid"}}),
                ("non_observed_native", {**base, "binding": {**base["binding"], "provenance": "something_else"}}),
                ("missing_workspace", {**base, "binding": {**base["binding"], "workspace": ""}}),
            ]

            for name, sess in incomplete_cases:
                with self.subTest(case=name):
                    d = fleet.Dispatcher(cfg, lambda m, p, b=None: {"sessions": [sess]})
                    with patch("agent_hub_host.fleet.verify_native_retirement") as mock_v:
                        self.assertEqual(d.reconcile_imported_sessions(), [])
                        mock_v.assert_not_called()


class ImportedCursorPrivateBoundaryTests(unittest.TestCase):
    def test_refused_private_write_cannot_fall_back_to_predictable_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / 'journal'
            journal.mkdir(mode=0o700)
            original = root / 'original'
            protected = root / 'protected-fixture'
            original.write_text('{}')
            protected.write_text('sentinel')
            cursor = journal / 'imported-cursor.json'
            cursor.symlink_to(original)
            (journal / f'imported-cursor.json.{os.getpid()}.tmp').symlink_to(protected)
            dispatcher = fleet.Dispatcher({}, journal_dir=str(journal))
            dispatcher._set_imported_cursor('page2')
            self.assertEqual(protected.read_text(), 'sentinel')
            self.assertTrue(cursor.is_symlink())
            self.assertEqual(original.read_text(), '{}')

    def test_refused_symlink_read_cannot_supply_network_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / 'journal'
            journal.mkdir(mode=0o700)
            foreign = root / 'private-fixture'
            foreign.write_text(json.dumps({'cursor': 'private-value-must-not-leave'}))
            (journal / 'imported-cursor.json').symlink_to(foreign)
            paths = []
            def request(method, path, payload=None):
                paths.append(path)
                return {'sessions': [], 'next_cursor': None}
            dispatcher = fleet.Dispatcher({}, request=request, journal_dir=str(journal))
            self.assertEqual(dispatcher.reconcile_imported_sessions(), [])
            self.assertEqual(paths, ['/api/sessions/host-bindings?kind=imported'])
            self.assertIn('private-value-must-not-leave', foreign.read_text())

    def test_private_cursor_read_is_byte_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'imported-cursor.json'
            p.write_text(json.dumps({'cursor': 'x' * 8192}))
            p.chmod(0o600)
            dispatcher = fleet.Dispatcher({}, journal_dir=tmp)
            self.assertIsNone(dispatcher._get_imported_cursor())

    def test_malformed_page_cannot_reset_or_advance_existing_cursor(self):
        for reply in ({'error': 'unexpected'}, {'sessions': [], 'next_cursor': 12},
                      {'sessions': 'not-an-array', 'next_cursor': 'page3'}):
            with self.subTest(reply=reply):
                dispatcher = fleet.Dispatcher({}, request=lambda *args: reply)
                dispatcher._set_imported_cursor('page2')
                self.assertEqual(dispatcher.reconcile_imported_sessions(), [])
                self.assertEqual(dispatcher._get_imported_cursor(), 'page2')

    def test_explicit_end_cursor_persists_without_unsafe_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher = fleet.Dispatcher({}, journal_dir=tmp)
            dispatcher._set_imported_cursor('page2')
            self.assertEqual(fleet.Dispatcher({}, journal_dir=tmp)._get_imported_cursor(), 'page2')
            dispatcher._set_imported_cursor(None)
            self.assertIsNone(fleet.Dispatcher({}, journal_dir=tmp)._get_imported_cursor())
            self.assertEqual(json.loads((Path(tmp) / 'imported-cursor.json').read_text()), {'cursor': None})


if __name__ == '__main__': unittest.main()
