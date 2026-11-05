"""Native launch adapter and provider home tests (Hub2 Resource Manager redesign)."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host import native_launch as nl
from agent_hub_host import provider_home as ph


class AdapterArgvTest(unittest.TestCase):
    def test_zai_route_pins_harness_flags_and_model(self):
        argv = nl.adapter_argv('zai', 'goal-text', 'glm-5.3-flash')
        self.assertEqual(argv[0], 'zcodex')
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', argv)
        self.assertIn('--model', argv)
        self.assertEqual(argv[argv.index('--model') + 1], 'glm-5.3-flash')
        self.assertIn('--disable', argv)
        self.assertEqual(argv[-1], 'goal-text')

    def test_go_route_pins_approved_subscription_model(self):
        argv = nl.adapter_argv('go', 'goal-text', None)
        self.assertEqual(argv[0], 'opencode')
        self.assertIn(nl.OPENCODE_MODEL, argv)
        with self.assertRaises(ValueError):
            nl.adapter_argv('go', 'goal-text', 'other/model')

    def test_glm_model_never_leaves_zai_route(self):
        with self.assertRaises(ValueError):
            nl.adapter_argv('codex', 'goal', 'glm-5.3-flash')
        with self.assertRaises(ValueError):
            nl.adapter_argv('go', 'goal', 'glm-5.3-flash')

    def test_claude_headless_shape(self):
        argv = nl.adapter_argv('claude', 'goal-text', 'sonnet')
        self.assertEqual(argv[0], 'claude')
        self.assertIn('--dangerously-skip-permissions', argv)
        self.assertIn('-p', argv)


class EffortAndInteractiveTest(unittest.TestCase):
    def test_headless_effort_codex_maps_max_to_xhigh(self):
        base = nl.adapter_argv('zai', 'goal', 'glm-5.3-flash')
        argv = nl.headless_effort_argv('zai', base, 'max')
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        self.assertEqual(argv[-1], 'goal')

    def test_headless_effort_claude(self):
        base = nl.adapter_argv('claude', 'goal', 'sonnet')
        argv = nl.headless_effort_argv('claude', base, 'high')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')

    def test_interactive_removes_exec_and_keeps_model(self):
        base = nl.adapter_argv('zai', 'goal', 'glm-5.3-flash')
        argv = nl.interactive_argv('zai', base, 'goal', model='glm-5.3-flash', effort='medium')
        self.assertNotIn('exec', argv)
        self.assertNotIn('--json', argv)
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertEqual(argv[-1], 'goal')

    def test_goal_removes_prompt_from_argv(self):
        base = nl.adapter_argv('zai', 'goal', 'glm-5.3-flash')
        argv = nl.interactive_argv('zai', base, 'goal', model='glm-5.3-flash', with_goal=True)
        self.assertNotIn('goal', argv)

    def test_goal_requires_goal_capable_provider(self):
        base = nl.adapter_argv('go', 'goal', nl.OPENCODE_MODEL)
        with self.assertRaises(ValueError):
            nl.interactive_argv('go', base, 'goal', model=nl.OPENCODE_MODEL, with_goal=True)


class FirstActionTest(unittest.TestCase):
    def start_record(self, tmp):
        return {'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                'workspace': str(tmp), 'command': ['zcodex', 'exec', '--model', 'glm-5.3-flash'],
                'phase': 'starting', 'schema_version': 1, 'created_at_ms': int(time.time() * 1000) - 1000}

    def whoami(self, tmp, **overrides):
        record = self.start_record(tmp)
        whoami = {'id': record['id'], 'tag': record['tag'], 'workspace': record['workspace'],
                  'command': record['command'], 'phase': 'working', 'schema_version': 2,
                  'worker_pid': os.getpid() + 1, 'updated_at_ms': record['created_at_ms'] + 500,
                  'boot_id': 'x'}
        whoami.update(overrides)
        return record, whoami

    def test_genuine_whoami_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, whoami = self.whoami(tmp)
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(whoami))
            os.utime(path, (time.time(), time.time()))
            self.assertTrue(nl.validate_first_action(str(path), record, time.time() - 1))

    def test_start_record_clone_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, _ = self.whoami(tmp)
            clone = dict(record, phase='working', worker_pid=os.getpid() + 1, extra='x')
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(clone))
            os.utime(path, (time.time(), time.time()))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))

    def test_identity_and_phase_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, whoami = self.whoami(tmp)
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(whoami | {'id': 'other-id'}))
            os.utime(path, (time.time(), time.time()))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))
            path.write_text(json.dumps(whoami | {'phase': 'exited'}))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))

    def test_missing_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, whoami = self.whoami(tmp)
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps({k: v for k, v in whoami.items() if k != 'worker_pid'}))
            os.utime(path, (time.time(), time.time()))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))


class TombstoneTest(unittest.TestCase):
    def tombstone(self, tmp, native_id='D6CF9872-8597-48A5-BE64-990CDEF4DB31', **overrides):
        base = {'id': native_id, 'tag': 'task-t1', 'workspace': str(tmp), 'cause': 'finished',
                'exit': {'code': 0, 'signal': None}, 'containment_empty': True,
                'deadline_ms': 1000, 'deadline_exceeded': False}
        base.update(overrides)
        directory = Path(tmp) / '.local' / 'state' / 'aplexer' / 'retired-sessions' / native_id.lower()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'tombstone.json'
        path.write_text(json.dumps(base))
        return str(Path(tmp) / '.local' / 'state' / 'aplexer'), base

    def test_read_verify_and_native_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, document = self.tombstone(tmp)
            read = nl.read_tombstone(document['id'], state_dir=state_dir)
            self.assertEqual(read['id'].lower(), document['id'].lower())
            valid, _ = nl.verify_tombstone(read, document['id'], 'task-t1', tmp)
            self.assertTrue(valid)
            adapter = nl.NativeLauncher({'routes': {}, 'aplexer_state_dir': state_dir}, runner=lambda *a, **k: None)
            result = adapter.retired(document['id'], {'tag': 'task-t1', 'workspace': tmp})
            self.assertIsNotNone(result)
            self.assertEqual(result['exit']['code'], 0)
            self.assertTrue(result['containment_empty'])
            self.assertEqual(result['retirement'], 'verified_finished')

    def test_identity_and_containment_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, document = self.tombstone(tmp)
            adapter = nl.NativeLauncher({'routes': {}, 'aplexer_state_dir': state_dir}, runner=lambda *a, **k: None)
            self.assertIsNone(adapter.retired(document['id'], {'tag': 'other', 'workspace': tmp}))
            self.tombstone(tmp, containment_empty=False)
            self.assertIsNone(adapter.retired(document['id'], {'tag': 'task-t1', 'workspace': tmp}))

    def test_oversize_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, document = self.tombstone(tmp)
            path = Path(state_dir) / 'retired-sessions' / document['id'].lower() / 'tombstone.json'
            path.write_text(json.dumps({'id': document['id'], 'pad': 'x' * 200000}))
            self.assertIsNone(nl.read_tombstone(document['id'], state_dir=state_dir))
            link = Path(state_dir) / 'retired-sessions' / 'link-id' / 'tombstone.json'
            link.parent.mkdir(parents=True)
            link.symlink_to(path)
            self.assertIsNone(nl.read_tombstone('link-id', state_dir=state_dir))
            self.assertIsNone(nl.read_tombstone('not-a-uuid', state_dir=state_dir))


class CapabilityTest(unittest.TestCase):
    def test_capabilities_follow_provider_and_binary_presence(self):
        config = {'routes': {'codex': {'provider': 'zai'}, 'opencode': {'provider': 'go'},
                             'claude': {'provider': 'claude'}, 'shell': {'provider': 'shell'}}}
        with mock.patch.object(nl.shutil, 'which', side_effect=lambda b: '/bin/' + b if b in ('zcodex', 'opencode') else None):
            adapter = nl.NativeLauncher(config)
            caps = adapter.capabilities()
        self.assertEqual(caps['codex'], {'headless', 'interactive', 'goal', 'effort'})
        self.assertEqual(caps['opencode'], {'headless', 'interactive'})
        self.assertNotIn('claude', caps)
        self.assertNotIn('shell', caps)


class PhysicalResourceTest(unittest.TestCase):
    def test_refusal_when_below_floor(self):
        with mock.patch.object(nl, 'available_memory_mb', return_value=1000):
            ready, reason = nl.physical_resources_ready(6000, 1500)
            self.assertFalse(ready)
            self.assertEqual(reason, 'host_physical_resources_insufficient')

    def test_unknown_memory_fails_closed(self):
        with mock.patch.object(nl, 'available_memory_mb', return_value=None):
            ready, reason = nl.physical_resources_ready(6000, 1500)
            self.assertFalse(ready)
            self.assertEqual(reason, 'host_physical_resources_unknown')

    def test_available_memory_reads_host_platform(self):
        value = nl.available_memory_mb()
        self.assertIsInstance(value, int)
        self.assertGreater(value, 0)


class ProviderHomeTest(unittest.TestCase):
    zai_bundle = {'config': {'schemaVersion': 1}, 'zai_token': 'test-token-value-no-spaces',
                  'zai_base_url': 'https://api.z.ai/api/anthropic'}

    def test_materialize_creates_private_home_and_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(self.zai_bundle)}
            manifest = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            data = ph.verify(manifest)
            self.assertEqual(data['account_id'], 'zai-central')
            self.assertEqual(data['generation'], 3)
            self.assertEqual(data['model'], 'glm-5.3-flash')
            home = Path(manifest).parent / 'home'
            harness = (home / '.zcodex' / 'config.toml').read_text()
            self.assertIn('wire_api="zcode"', harness)
            self.assertIn('glm-5.3-flash', harness)
            if os.name != 'nt':
                # Windows ACL privacy is enforced through the icacls check in
                # make_private; the coarse stat mode does not map to ACL bits.
                mode = stat.S_IMODE((home / '.zcodex' / 'config.toml').stat().st_mode)
                self.assertEqual(mode & 0o077, 0)
            env = ph.native_environment(manifest)
            self.assertEqual(env['CODEX_HOME'], str(home / '.zcodex'))
            self.assertEqual(env['ZCODE_HOME'], str(home / '.zcode'))

    def test_materialize_is_idempotent_per_generation_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(self.zai_bundle)}
            first = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            second = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            self.assertEqual(first, second)
            bumped = ph.materialize(str(root), 'zai-central', 'zai', 4, files, 'glm-5.3-flash')
            self.assertNotEqual(Path(first).parent, Path(bumped).parent)

    def test_non_zai_bundle_requires_valid_expiry(self):
        bundle = {'tokens': {'access_token': 'header.' + __import__('base64').urlsafe_b64encode(
            json.dumps({'exp': int(time.time() / 1000) + 3600}).encode()).decode().rstrip('=') + '.sig',
            'refresh_token': 'r'}, 'last_refresh': '2020-01-01T00:00:00Z'}
        with tempfile.TemporaryDirectory() as tmp:
            files = {ph.PATHS['codex']: json.dumps(bundle)}
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'acc', 'codex', 1, files, 'gpt-6.1-sol', timeout_seconds=7200)

    def test_bundle_schema_and_account_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {ph.PATHS['zai']: json.dumps(self.zai_bundle)}
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'other-account', 'zai', 1, files, 'glm-5.3-flash')
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'zai-central', 'zai', 1, {'wrong': '{}'}, 'glm-5.3-flash')
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'zai-central', 'zai', 0, files, 'glm-5.3-flash')
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'zai-central', 'shell', 1, files, 'glm-5.3-flash')


class ProviderAdmissionTest(unittest.TestCase):
    def test_provider_admission_validates_live_generation_and_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(ProviderHomeTest.zai_bundle)}
            manifest = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            now = time.time() * 1000
            result = {'session_id': 's1', 'operation_id': 'op1', 'epoch': 2, 'account_id': 'zai-central',
                      'launch_allowed': True, 'provider': 'zai', 'credential_generation': 3,
                      'model': 'glm-5.3-flash', 'runtime_deadline': int(now + 600000),
                      'quota': {'source': 'provider:zai:generation:3', 'captured_at': int(now) - 1000,
                                'valid_until': int(now) + 60000, 'remaining': 88.0}}
            client = mock.Mock()
            client.request.return_value = result
            chosen, data = ph.provider_admission(manifest, client, 's1', 'op1', 2)
            self.assertEqual(chosen, {'provider': 'zai', 'model': 'glm-5.3-flash'})
            self.assertEqual(data['runtime_deadline'], result['runtime_deadline'])
            client.request.assert_called_once_with('POST', '/api/sessions/s1/admission', mock.ANY)
            body = client.request.call_args[0][2]
            self.assertEqual(body, {'operation_id': 'op1', 'epoch': 2, 'provider_check': True})

    def test_provider_admission_refuses_stale_quota_or_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(ProviderHomeTest.zai_bundle)}
            manifest = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            now = time.time() * 1000
            base = {'session_id': 's1', 'operation_id': 'op1', 'epoch': 2, 'account_id': 'zai-central',
                    'launch_allowed': True, 'provider': 'zai', 'credential_generation': 3,
                    'model': 'glm-5.3-flash', 'runtime_deadline': int(now + 600000),
                    'quota': {'source': 'provider:zai:generation:3', 'captured_at': int(now) - 1000,
                              'valid_until': int(now) + 60000, 'remaining': 88.0}}
            client = mock.Mock()
            client.request.return_value = base | {'credential_generation': 2}
            with self.assertRaises(ValueError):
                ph.provider_admission(manifest, client, 's1', 'op1', 2)
            stale = base | {'quota': base['quota'] | {'captured_at': int(now) - 400000}}
            client.request.return_value = stale
            with self.assertRaises(ValueError):
                ph.provider_admission(manifest, client, 's1', 'op1', 2)
            # The codex floor is provider-scoped; a zai account below it still admits.
            below = base | {'quota': base['quota'] | {'remaining': 10.0}}
            client.request.return_value = below
            chosen, _ = ph.provider_admission(manifest, client, 's1', 'op1', 2)
            self.assertEqual(chosen['model'], 'glm-5.3-flash')
            binding = base | {'epoch': 9}
            client.request.return_value = binding
            with self.assertRaises(ValueError):
                ph.provider_admission(manifest, client, 's1', 'op1', 2)


class LaunchReceiptTest(unittest.TestCase):
    def test_launch_builds_aplexer_argv_and_rejects_unready_resources(self):
        config = {'routes': {'codex': {'provider': 'zai'}}, 'min_free_mem_mb': 6000, 'memory_mb': 1500}
        adapter = nl.NativeLauncher(config)
        spec = {'workspace': '/nonexistent-ws', 'engine': 'codex', 'model': 'glm-5.3-flash',
                'prompt': 'p', 'interactive': False, 'timeout_seconds': 600}
        with mock.patch.object(nl, 'physical_resources_ready', return_value=(False, 'host_physical_resources_insufficient')):
            with self.assertRaises(RuntimeError):
                adapter.launch('t1', spec)

    def test_launch_refuses_unknown_route(self):
        adapter = nl.NativeLauncher({'routes': {}})
        with self.assertRaises(RuntimeError):
            adapter.launch('t1', {'engine': 'codex', 'workspace': '/', 'model': 'm', 'prompt': 'p',
                                  'interactive': False, 'timeout_seconds': 600})

    def test_wait_first_action_success_without_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                      'workspace': tmp, 'command': ['x'], 'phase': 'starting', 'schema_version': 1,
                      'created_at_ms': int(time.time() * 1000) - 1000}
            whoami = {'id': record['id'], 'tag': record['tag'], 'workspace': tmp, 'command': ['x'],
                      'phase': 'working', 'schema_version': 2, 'worker_pid': os.getpid() + 1,
                      'updated_at_ms': record['created_at_ms'] + 500}
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(whoami))
            os.utime(path, (time.time(), time.time()))
            adapter = nl.NativeLauncher({'routes': {}})
            receipt = adapter.wait_first_action(str(path), record, time.time() - 1,
                                                {'workspace': tmp, 'timeout_seconds': 5},
                                                poll=lambda selector: {'phase': 'running'}, sleep=lambda s: None)
            self.assertEqual(receipt['session_id'], record['id'])
            self.assertTrue(receipt['first_action_verified'])

    def test_wait_first_action_dead_native_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                      'workspace': tmp, 'command': ['x'], 'phase': 'starting', 'schema_version': 1,
                      'created_at_ms': int(time.time() * 1000) - 1000}
            adapter = nl.NativeLauncher({'routes': {}})
            with self.assertRaises(RuntimeError):
                adapter.wait_first_action(str(Path(tmp) / 'absent.json'), record, time.time() - 1,
                                          {'workspace': tmp, 'timeout_seconds': 1},
                                          poll=lambda selector: {'phase': 'dead'}, sleep=lambda s: None)


if __name__ == '__main__':
    unittest.main()
