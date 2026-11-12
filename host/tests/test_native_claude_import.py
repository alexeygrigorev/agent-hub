import argparse
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from unittest.mock import patch

from agent_hub_host import native_claude_import as n
from agent_hub_host import native_import_host as h
from agent_hub_host import provisioning as p
from agent_hub_host import engine_login


class NativeImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            'HOME': str(self.home), 'USERPROFILE': str(self.home),
            'XDG_CONFIG_HOME': str(self.home / '.config'), 'APPDATA': str(self.home / '.config'),
            'CLAUDE_CONFIG_DIR': str(self.home), 'HUB_URL': 'https://hub.test',
            'HUB_CONFIG_FILE': '', 'HUB_TOKEN': '', 'HUB_JOB_TOKEN': '',
            'HUB_WORKER_CONFIG_FILE': '', 'APLEXER_TAG': '', 'APLEXER_SESSION_ID': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = self.home / '.credentials.json'
        self.oauth = {'accessToken': 'synthetic-access', 'refreshToken': 'synthetic-refresh', 'expiresAt': int(time.time() * 1000) + 3600000}
        self.write({'claudeAiOauth': self.oauth, 'mcpOAuth': {'private': 'must-not-import'}})
        self.args = argparse.Namespace(provider='claude', label='hub-claude')
        self.profiles = self.home / '.config/agent-hub'
        self.profiles.mkdir(parents=True, mode=0o700)
        self.v2 = self.profiles / 'v2.json'
        self.legacy = self.profiles / 'host.json'
        self.enrollment = {'server': 'https://hub.test', 'token': 'synthetic-host-token',
                           'machine_id': 'test-host', 'roots': [str(self.home)], 'engines': ['claude']}
        self.profile(self.v2, self.enrollment)
        self.profile(self.legacy, {'base_url': 'https://retired.test', 'access_token': 'synthetic-retired-token', 'machine_id': 'retired-host'})
        self.profile(self.profiles / 'cli.json', {'base_url': 'https://hub.test',
                     'access_token': 'synthetic-operator-token', 'client_kind': 'human'})

    def profile(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def write(self, value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def test_collect_preserves_native_source_and_excludes_mcp(self):
        before = self.path.read_bytes()
        result = n.collect()
        self.assertEqual(json.loads(result[n.AUTH_PATH]), {'claudeAiOauth': self.oauth})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn('must-not-import', repr(result))

    def test_unsafe_sources_and_invalid_credentials_are_sanitized(self):
        good = self.path.read_bytes()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'unsafe or expired'):
            n.collect()
        self.path.chmod(0o600)
        self.path.rename(self.home / 'other')
        self.path.symlink_to(self.home / 'other')
        with self.assertRaises(ValueError):
            n.collect()
        self.path.unlink()
        os.link(self.home / 'other', self.path)
        with self.assertRaises(ValueError):
            n.collect()
        self.path.unlink()
        for value in ({'claudeAiOauth': dict(self.oauth, expiresAt=1)}, {'claudeAiOauth': {}}, ['synthetic-secret']):
            self.write(value)
            with self.assertRaises(ValueError) as exc:
                n.collect()
            self.assertNotIn('synthetic', str(exc.exception))
        self.path.write_bytes(good)

    def test_managed_home_is_refused(self):
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home / 'managed-engines' / 'home/.claude')}):
            with self.assertRaisesRegex(ValueError, 'Native Claude home required'):
                n.collect()

    def scoped_materialization_integration(self, resume):
        from agent_hub_host import credentials
        revision = '33333333-3333-4333-8333-333333333333'
        root = self.home / 'managed-scoped'
        dest = root / ('hub-claude-' + revision)
        home = dest / 'home'
        (home / '.claude').mkdir(parents=True, mode=0o700)
        for directory in (root, dest, home): directory.chmod(0o700)
        old = {'label':'hub-claude','provider':'claude','version':'2.1.322','revision':revision,
               'files':{n.AUTH_PATH:json.dumps({'claudeAiOauth':{'accessToken':'synthetic-old5'}})},
               'env':{},'args':[],'credential_paths':[n.AUTH_PATH],'credential_generation':5}
        incoming = dict(old, credential_generation=6, files={n.AUTH_PATH:json.dumps({'claudeAiOauth': self.oauth})})
        credentials.reconcile('hub-claude',old,home,dest,lambda *args: self.fail('no request for initial fixture'))
        (home/n.AUTH_PATH).write_text('synthetic changed old generation5')
        p.save_bundle(dest,old)
        state = [{'label':'unrelated','provider':'codex','revision':revision},
                 {'label':'hub-claude','provider':'claude','revision':revision}]
        state_bytes=json.dumps(state).encode();(root/'state.json').write_bytes(state_bytes);(root/'state.json').chmod(0o600)
        unrelated=root/'unrelated-private';unrelated.mkdir(mode=0o700);(unrelated/'keep').write_bytes(b'unchanged')
        profile=self.home/'.config/aplexer/config.toml';profile.parent.mkdir(parents=True,mode=0o700)
        original='version = 1\n[profiles.unrelated]\nengine = "shell"\ncommand = ["synthetic"]\n'
        profile.write_text(original);profile.chmod(0o600)
        calls=[];current=6 if resume else 5;acked=5
        def transport(req):
            nonlocal current,acked
            method,path=req.get_method(),urllib.parse.urlsplit(req.full_url).path
            payload=json.loads(req.data) if req.data else None
            calls.append((method,path))
            if method=='GET' and path=='/api/engines':
                return {'credentials':[{'label':'hub-claude','generation':current,'hosts':['test-host']}],
                        'sync_status':[{'machine_id':'test-host','accounts':[{'label':'hub-claude',
                            'credential_generation':current,'acked_generation':acked}]}]}
            if method=='POST' and path=='/api/engines/import-native':
                self.assertFalse(resume);self.assertEqual(payload['expected_generation'],5)
                current=6;return {'credential_generation':6}
            if method=='GET' and path=='/api/engines/sync':
                return {'engines':[incoming],'logins':[{'id':'must-not-complete'}]}
            if method=='POST' and path=='/api/engines/hub-claude':return {'ok':True}
            if method=='POST' and path=='/api/engines/sync-report':
                self.assertEqual(payload['items'][0]['credential_generation'],6);acked=6
                return {'status':'recorded'}
            self.fail('unexpected API path '+method+' '+path)
        self.args.resume_generation=6 if resume else None
        p._last_sync_report.update(body=None,at=0)
        native_before=self.path.read_bytes()
        with patch.dict(os.environ,{'AGENT_HUB_MANAGED_DIR':str(root),'APLEXER_CONFIG':str(profile)}), \
             patch.object(h.api,'safe_http_json',side_effect=transport), \
             patch.object(p,'provision_managed_home'), \
             patch.object(engine_login,'cancel_removed',side_effect=AssertionError('no global cleanup')):
            if resume:
                with patch.object(n,'collect',side_effect=AssertionError('resume must not collect')):
                    result=n.command(self.args)
            else:result=n.command(self.args)
        self.assertEqual(result['credential_generation'],6);self.assertEqual(result['acked_generation'],6)
        self.assertEqual((home/n.AUTH_PATH).read_text(),incoming['files'][n.AUTH_PATH])
        self.assertEqual(json.loads((dest/credentials.STATE_FILE).read_text())['generation'],6)
        self.assertEqual(self.path.read_bytes(),native_before)
        self.assertEqual((root/'state.json').read_bytes(),state_bytes)
        self.assertEqual((unrelated/'keep').read_bytes(),b'unchanged')
        self.assertTrue(profile.read_text().startswith(original))
        self.assertIn('[profiles.hub-hub-claude]',profile.read_text())
        self.assertFalse(any(path.endswith('/credential') for _,path in calls))
        self.assertEqual(sum(path=='/api/engines/import-native' for _,path in calls),0 if resume else 1)

    def test_initial_import_real_scoped_modules_do_not_write_back_old_local_generation(self):
        self.scoped_materialization_integration(False)

    def test_resume_real_scoped_modules_do_not_collect_or_write_back_old_local_generation(self):
        self.scoped_materialization_integration(True)

    def run_command(self, *, incoming=5, receipt=5, ack=5, central=5, current=4, lease=False, fail_upload=False, fail_sync=False, fail_verify=False):
        calls = []
        before = {'credentials': [{'label': 'hub-claude', 'generation': current, 'hosts': ['test-host']}]}
        after = {'sync_status': [{'machine_id': 'test-host', 'accounts': [{'label': 'hub-claude', 'credential_generation': central, 'acked_generation': ack}]}]}
        def transport(req):
            method, path = req.get_method(), urllib.parse.urlsplit(req.full_url).path
            payload = None if req.data is None else json.loads(req.data)
            calls.append((method, path, payload))
            self.assertEqual(urllib.parse.urlsplit(req.full_url).netloc, 'hub.test')
            host_path = path in ('/api/engines/sync', '/api/engines/hub-claude', '/api/engines/sync-report')
            self.assertEqual(req.get_header('Authorization'), 'Bearer synthetic-host-token' if host_path else 'Bearer synthetic-operator-token')
            if host_path:
                return {'engines': [{'label': 'hub-claude', 'credential_generation': incoming,'refresh_lease':lease}], 'logins': [{'id': 'do-not-complete'}]}
            if method == 'GET':
                if fail_verify and len(calls) > 1:
                    raise OSError('synthetic-private-provider-output')
                return before if len(calls) == 1 else after
            if fail_upload:
                raise ValueError('generation_conflict')
            self.assertEqual(payload['expected_generation'], 4)
            self.assertNotIn('must-not-import', repr(payload))
            return {'credential_generation': receipt}
        def reconcile(request, **kwargs):
            self.assertEqual(kwargs, {'target_label': 'hub-claude', 'expected_generation': receipt, 'lock_timeout': 30})
            if fail_sync:
                raise OSError('synthetic-private-provider-output')
            response = request('GET', '/api/engines/sync')
            self.assertEqual(response['logins'], [])
            request('POST', '/api/engines/hub-claude', {'revision': 'synthetic-revision', 'authenticated': True})
            request('POST', '/api/engines/sync-report', {'state': 'synced', 'items': []})
            return {'status': 'synced'}
        with patch.object(h.api, 'safe_http_json', side_effect=transport), patch.object(n.provisioning, 'sync', side_effect=reconcile) as sync:
            try:
                result = n.command(self.args)
            finally:
                self.calls = calls
                self.sync_called = sync.called
        return result

    def test_upload_precedes_sync_and_ack_is_verified(self):
        result = self.run_command()
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['acked_generation'], 5)
        self.assertNotIn('synthetic', repr(result))
        self.assertEqual([call[0] for call in self.calls], ['GET', 'POST', 'GET', 'POST', 'POST', 'GET'])

    def test_resume_saved_generation_never_reads_native_or_uploads(self):
        self.args.resume_generation=5
        with patch.object(n,'collect',side_effect=AssertionError('no native credential read')):
            result=self.run_command(current=5)
        self.assertEqual(result['status'],'resumed');self.assertEqual(result['acked_generation'],5)
        self.assertNotIn('/api/engines/import-native',[path for method,path,payload in self.calls])

    def test_resume_generation_mismatch_fails_before_collect_or_sync(self):
        self.args.resume_generation=5
        with patch.object(n,'collect',side_effect=AssertionError('no native read')):
            with self.assertRaisesRegex(ValueError,'does not match current'):
                self.run_command(current=6)
        self.assertFalse(self.sync_called)

    def test_scoped_sync_rejects_newer_generation_and_lease(self):
        for flags in [{'incoming':6},{'lease':True}]:
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(ValueError,'saved at generation 5'):
                    self.run_command(**flags)
                self.assertFalse(any(method=='POST' and path in ('/api/engines/hub-claude','/api/engines/sync-report') for method,path,payload in self.calls))

    def test_stale_upload_never_syncs(self):
        with self.assertRaisesRegex(ValueError, 'generation_conflict'):
            self.run_command(fail_upload=True)
        self.assertFalse(self.sync_called)

    def test_bad_receipt_never_syncs(self):
        with self.assertRaisesRegex(ValueError, 'no sync attempted'):
            self.run_command(receipt=4)
        self.assertFalse(self.sync_called)

    def test_expired_central_generation_is_not_materialized(self):
        with self.assertRaisesRegex(ValueError, 'saved at generation 5'):
            self.run_command(incoming=4)

    def test_post_upload_failures_preserve_saved_receipt_without_provider_output(self):
        for flags in ({'fail_sync': True}, {'fail_verify': True}):
            with self.assertRaisesRegex(ValueError, 'saved at generation 5') as exc:
                self.run_command(**flags)
            self.assertNotIn('synthetic-private', str(exc.exception))

    def test_missing_or_stale_ack_is_not_success(self):
        for ack, central in ((None, 5), (4, 5), (5, 6)):
            with self.assertRaisesRegex(ValueError, 'acknowledgement is pending'):
                self.run_command(ack=ack, central=central)

    def test_https_and_host_origin_required_before_collect(self):
        for base in ('http://localhost', 'https://other.test'):
            with patch.dict(os.environ, {'HUB_URL': base}), patch.object(n, 'collect') as collect:
                with self.assertRaisesRegex(ValueError, 'HTTPS'):
                    n.command(self.args)
                collect.assert_not_called()

    def assert_enrollment_refused(self):
        with patch.object(n, 'collect') as collect, patch.object(h.api, 'safe_http_json') as network:
            with self.assertRaisesRegex((ValueError, n.config.ConfigPermissionError), 'HTTPS and an enrolled host|Unsafe Hub profile permissions') as exc:
                n.command(self.args)
            self.assertNotIn('synthetic', str(exc.exception))
            collect.assert_not_called()
            network.assert_not_called()

    def test_current_enrollment_works_without_any_legacy_profile(self):
        self.legacy.unlink()
        self.assertEqual(self.run_command()['machine_id'], 'test-host')

    def test_only_absent_v2_allows_valid_legacy_host(self):
        self.v2.unlink()
        self.profile(self.legacy, {'base_url': 'https://hub.test', 'access_token': 'synthetic-host-token', 'machine_id': 'test-host'})
        self.assertEqual(self.run_command()['status'], 'imported')
        self.legacy.unlink()
        self.assert_enrollment_refused()

    def test_invalid_v2_never_falls_back_to_valid_legacy(self):
        self.profile(self.legacy, {'base_url': 'https://hub.test', 'access_token': 'synthetic-host-token', 'machine_id': 'test-host'})
        invalid = [None, [], {}, {'base_url': 'https://hub.test', 'access_token': 'synthetic-host-token', 'machine_id': 'test-host'}]
        for key, values in {
            'machine_id': [None, '', ' ', 'bad\nidentity', True],
            'token': [None, '', ' ', 'synthetic secret', 'synthetic\nsecret', 'synthetic\x7fsecret', 123],
            'server': [None, '', 'http://hub.test', 'https://other.test', 'https://hub.test:444',
                       'https://synthetic-secret@hub.test', 'https://hub.test/path', 'https://hub.test/?q=secret',
                       'https://hub.test/#secret', 'https://[invalid', ' https://hub.test'],
            'client_kind': ['human', 'agent', 'worker'],
            'worker': [{}, None], 'worker_launch_id': ['synthetic-launch', None],
        }.items():
            invalid.extend(dict(self.enrollment, **{key: value}) for value in values)
        for value in invalid:
            with self.subTest(value=value):
                self.profile(self.v2, value)
                self.assert_enrollment_refused()
        self.v2.write_text('{broken synthetic-secret')
        self.assert_enrollment_refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions and links')
    def test_unsafe_v2_files_and_parent_refused_without_fallback(self):
        self.profile(self.legacy, {'base_url': 'https://hub.test', 'access_token': 'synthetic-host-token', 'machine_id': 'test-host'})
        self.v2.chmod(0o644)
        self.assert_enrollment_refused()
        self.v2.chmod(0o600)
        self.profiles.chmod(0o755)
        self.assert_enrollment_refused()
        with self.assertRaisesRegex(ValueError, 'HTTPS and an enrolled host'):
            h.bind('https://hub.test')
        self.profiles.chmod(0o700)
        other = self.profiles / 'saved.json'
        self.v2.rename(other)
        self.v2.symlink_to(other)
        self.assert_enrollment_refused()
        self.v2.unlink()
        os.link(other, self.v2)
        self.assert_enrollment_refused()
        self.v2.unlink()
        self.v2.symlink_to(self.profiles / 'missing.json')
        self.assert_enrollment_refused()
        self.v2.unlink()
        self.v2.mkdir(mode=0o700)
        self.assert_enrollment_refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX directory links')
    def test_inaccessible_current_enrollment_never_selects_separate_legacy(self):
        separate = self.home / 'legacy-config' / 'agent-hub'
        separate.mkdir(parents=True, mode=0o700)
        self.profile(separate / 'host.json', {
            'base_url': 'https://hub.test', 'access_token': 'synthetic-legacy-token',
            'machine_id': 'legacy-host',
        })
        # Python 3.14 Path.exists() suppresses permission errors. Such an
        # inaccessible current enrollment is not proof that it is absent.
        self.profiles.chmod(0)
        try:
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(separate.parent)}):
                with self.assertRaisesRegex(ValueError, 'HTTPS and an enrolled host'):
                    h.bind('https://hub.test')
        finally:
            self.profiles.chmod(0o700)

    @unittest.skipIf(os.name == 'nt', 'POSIX directory links')
    def test_current_enrollment_never_follows_parent_symlink(self):
        relocated = self.home / 'relocated'
        self.profiles.rename(relocated)
        self.profiles.symlink_to(relocated, target_is_directory=True)
        # Operator profile is also below the symlink; bind directly to prove
        # the host reader refuses the linked parent independently.
        with self.assertRaisesRegex(ValueError, 'HTTPS and an enrolled host'):
            h.bind('https://hub.test')

    def test_explicit_override_rejects_human_worker_missing_and_relative_profiles(self):
        override = self.profiles / 'override.json'
        invalid = [dict(self.enrollment, client_kind='human'), dict(self.enrollment, client_kind='agent'),
                   dict(self.enrollment, worker={'machine_id': 'test-host'}),
                   dict(self.enrollment, worker_launch_id='synthetic-launch'),
                   {'server': 'https://hub.test', 'token': 'synthetic-human-token'},
                   {'base_url': 'https://hub.test', 'access_token': 'synthetic-human-token', 'client_kind': 'human', 'machine_id': 'test-host'}]
        for value in invalid:
            self.profile(override, value)
            with patch.dict(os.environ, {'HUB_CONFIG_FILE': str(override)}):
                self.assert_enrollment_refused()
        override.unlink()
        with patch.dict(os.environ, {'HUB_CONFIG_FILE': str(override)}):
            self.assert_enrollment_refused()
        with patch.dict(os.environ, {'HUB_CONFIG_FILE': 'relative.json'}):
            with self.assertRaisesRegex(ValueError, 'must be absolute'):
                n.command(self.args)

    def test_host_binding_ignores_inherited_worker_and_token_selectors_and_stays_pinned(self):
        worker = self.profiles / 'worker.json'
        self.profile(worker, {'server': 'https://worker.test', 'token': 'synthetic-worker-token', 'worker': {}})
        before = {path: path.read_bytes() for path in (self.v2, self.legacy, worker)}
        with patch.dict(os.environ, {'HUB2_CONFIG': str(worker), 'HUB2_TOKEN': 'synthetic-worker-token',
                                   'HUB2_SERVER': 'https://worker.test', 'HUB_TOKEN': 'synthetic-operator-token'}):
            machine, request = h.bind('https://hub.test:443/')
            self.assertEqual(machine, 'test-host')
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.profile(self.v2, dict(self.enrollment, server='https://changed.test', token='synthetic-changed-token'))
            with patch.object(h.api, 'safe_http_json', return_value={}) as transport:
                request('GET', '/api/engines/sync')
                req = transport.call_args.args[0]
                self.assertEqual(req.full_url, 'https://hub.test/api/engines/sync')
                self.assertEqual(req.get_header('Authorization'), 'Bearer synthetic-host-token')

    def test_pinned_host_requester_refuses_other_paths_before_transport(self):
        _, request = h.bind('https://hub.test')
        with patch.object(h.api, 'safe_http_json') as transport:
            for path in ('https://other.test/api/engines/sync', '//other.test', '/api/engines/../token',
                         '/api/engines/sync?token=secret', '/api/engines/sync#fragment', '/api/engines/sync\r\n'):
                with self.assertRaises(ValueError):
                    request('GET', path)
            with self.assertRaises(ValueError):
                request('POST', '/api/engines/import-native', {})
            transport.assert_not_called()

    def test_host_transport_refuses_same_and_cross_origin_redirects(self):
        _, request = h.bind('https://hub.test')
        for location in ('https://other.test/api/engines/sync', 'https://hub.test/redirected'):
            headers = Message()
            headers['Location'] = location
            response = urllib.response.addinfourl(io.BytesIO(b''), headers, 'https://hub.test/api/engines/sync', 302)
            response.msg = 'Found'
            with patch.object(urllib.request.HTTPSHandler, 'https_open', return_value=response) as transport:
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    request('GET', '/api/engines/sync')
                self.assertEqual(exc.exception.code, 302)
                transport.assert_called_once()

    def test_managed_exec_binder_is_current_only_and_narrow(self):
        with patch.object(h.api, 'safe_http_json', return_value={'status': 'recorded'}) as request:
            machine, send=h.bind_managed_claude('https://hub.test')
            self.assertEqual(machine,'test-host')
            send('GET','/api/engines/hub-claude/credential')
            send('POST','/api/engines/admission',{'label':'hub-claude','native':True})
            for method,path,body in [('GET','/api/engines/sync',None),
                    ('POST','/api/engines/hub-claude',{}),('POST','/api/engines/login',{}),
                    ('GET','/api/engines/other/credential',None),
                    ('POST','/api/engines/admission',{'label':'hub-claude'}),
                    ('POST','/api/engines/sync-report',{'state':'synced','items':[]}),
                    ('POST','/api/engines/sync-report',{'state':'synced','items':[{'label':'other','state':'installed'}]})]:
                with self.subTest(path=path,body=body),self.assertRaises(ValueError):send(method,path,body)
            self.assertEqual(request.call_count,2)
        self.v2.unlink()
        with self.assertRaises(ValueError):h.bind_managed_claude('https://hub.test')

    def test_trusted_exec_under_v2_native_tag_keeps_ordinary_worker_denial(self):
        from agent_hub_host import config, worker_auth
        tag='task-hub2-11111111-2222-4333-8444-555555555555'
        with patch.dict(os.environ, {'APLEXER_TAG':tag,'HUB2_CONFIG':''}), \
                patch.object(h.api,'safe_http_json',return_value={}) as transport:
            machine,send=h.bind_managed_claude('https://hub.test')
            self.assertEqual(os.environ['APLEXER_TAG'],tag)
            self.assertEqual(machine,'test-host')
            send('GET','/api/engines/hub-claude/credential')
            send('POST','/api/engines/hub-claude/credential',{'revision':'synthetic'})
            send('POST','/api/engines/admission',{'label':'hub-claude','native':True})
            send('POST','/api/engines/sync-report',{'state':'synced','items':[
                {'label':'hub-claude','revision':'synthetic','state':'installed','credential_generation':1}]})
            for method,path in [('GET','/api/sessions'),('GET','/api/engines/sync'),
                    ('GET','/api/engines/hub-claude/credential?other=1'),('DELETE','/api/engines/hub-claude/credential')]:
                with self.subTest(path=path),self.assertRaises(ValueError):send(method,path)
            self.assertEqual(transport.call_count,4)
            for call in transport.call_args_list:
                self.assertTrue(call.args[0].full_url.startswith('https://hub.test/api/engines/'))
                self.assertEqual(call.args[0].get_header('Authorization'),'Bearer synthetic-host-token')
            with self.assertRaises(worker_auth.WorkerContextError):config._load_from(self.v2)
            with self.assertRaises(ValueError):h.bind('https://hub.test')
            self.assertEqual(transport.call_count,4)

    def test_trusted_exec_preserves_real_profile_test_guard_before_any_read(self):
        from agent_hub_host import config
        with patch.dict(os.environ, {'APLEXER_TAG':'task-hub2-11111111-2222-4333-8444-555555555555',
                'HUB2_CONFIG':'',config.TEST_GUARD_HOME_ENV:str(self.home)}), \
                patch.object(h.private_fs,'read_bytes') as read,patch.object(h.api,'safe_http_json') as transport:
            with self.assertRaises(ValueError):h.bind_managed_claude('https://hub.test')
            read.assert_not_called()
            transport.assert_not_called()

    def test_exec_ignores_worker_selector_but_never_accepts_explicit_or_legacy_override(self):
        worker=self.home/'malicious-worker.json'
        self.profile(worker,dict(self.enrollment,server='https://wrong.test',token='synthetic-wrong-token'))
        with patch.dict(os.environ, {'APLEXER_TAG':'task-hub2-11111111-2222-4333-8444-555555555555',
                'HUB2_CONFIG':str(worker)}),patch.object(h.api,'safe_http_json',return_value={}) as transport:
            _,send=h.bind_managed_claude('https://hub.test')
            send('GET','/api/engines/hub-claude/credential')
            self.assertEqual(transport.call_args.args[0].get_header('Authorization'),'Bearer synthetic-host-token')
            with patch.dict(os.environ,{'HUB_CONFIG_FILE':str(self.v2)}):
                with self.assertRaises(ValueError):h.bind_managed_claude('https://hub.test')
            self.v2.unlink()
            with self.assertRaises(ValueError):h.bind_managed_claude('https://hub.test')
            self.assertEqual(transport.call_count,1)

    def test_exec_fixed_enrollment_still_refuses_malicious_metadata_and_unsafe_files(self):
        tag='task-hub2-11111111-2222-4333-8444-555555555555'
        with patch.dict(os.environ,{'APLEXER_TAG':tag,'HUB2_CONFIG':''}), \
                patch.object(h.api,'safe_http_json') as transport:
            for fields in [{'client_kind':'human'},{'client_kind':'worker'},{'worker':{}},
                    {'worker_launch_id':'synthetic-worker'},{'machine_id':'invalid machine'},
                    {'server':'http://hub.test'},{'server':'https://wrong.test'},
                    {'token':''},{'token':'synthetic\nsecret'}]:
                with self.subTest(fields=fields):
                    self.profile(self.v2,dict(self.enrollment,**fields))
                    with self.assertRaises(ValueError) as error:h.bind_managed_claude('https://hub.test')
                    self.assertEqual(str(error.exception),h._ENROLLMENT_ERROR)
            self.profile(self.v2,self.enrollment)
            for fault in ['mode','hardlink','symlink','oversize','deep_json','invalid_json']:
                with self.subTest(fault=fault):
                    linked=self.home/'synthetic-linked'
                    if fault=='mode':self.v2.chmod(0o640)
                    elif fault=='hardlink':os.link(self.v2,linked)
                    elif fault=='symlink':self.v2.rename(linked);self.v2.symlink_to(linked)
                    elif fault=='oversize':self.v2.write_bytes(b'x'*(1024*1024+1))
                    elif fault=='deep_json':self.v2.write_bytes(b'['*2000+b'0'+b']'*2000)
                    else:self.v2.write_bytes(b'[]')
                    with self.assertRaises(ValueError) as error:h.bind_managed_claude('https://hub.test')
                    self.assertEqual(str(error.exception),h._ENROLLMENT_ERROR)
                    if self.v2.is_symlink():self.v2.unlink()
                    linked.unlink(missing_ok=True)
                    self.profile(self.v2,self.enrollment)
            transport.assert_not_called()

    def test_cli_dispatch(self):
        parser = argparse.ArgumentParser()
        p.add_commands(parser.add_subparsers(dest='cmd'))
        args = parser.parse_args(['engines', 'import-native', '--provider', 'claude', '--label', 'hub-claude'])
        with patch.object(n, 'command', return_value={'status': 'imported'}) as command:
            self.assertEqual(p.command(args), {'status': 'imported'})
            command.assert_called_once_with(args)


if __name__ == '__main__':
    unittest.main()


class NativeImportParserTest(unittest.TestCase):
    def test_saved_generation_flag_stays_on_existing_command(self):
        parser=argparse.ArgumentParser()
        p.add_commands(parser.add_subparsers(dest='command'))
        args=parser.parse_args(['engines','import-native','--provider','claude','--label','hub-claude','--resume-generation','5'])
        self.assertEqual(args.engines_cmd,'import-native')
        self.assertEqual(args.resume_generation,5)
