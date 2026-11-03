import argparse
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import repos, fleet, session_control


class RepoTests(unittest.TestCase):
    def test_origin_parsing(self):
        for value in ('git@github.com:Example/site.git', 'https://github.com/Example/site.git', 'ssh://git@github.com/Example/site'):
            self.assertEqual(repos.origin_name(value), 'Example/site')
        self.assertEqual(repos.origin_name('https://secret@example.test/site'), '')

    def test_full_repository_layout_settings_are_validated_and_normalized(self):
        with tempfile.TemporaryDirectory() as tmp, patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
            root = Path(tmp).resolve()
            checkout, trees = root/'repos', root/'trees'
            checkout.mkdir()
            path = root/'subscriptions.json'
            config = {'version':1,'subscriptions':[],'workspaces':{},'workspace_roots':[str(root)],
                      'repos':{'scan_roots':[str(checkout)],'checkout_root':str(checkout),'worktree_root':str(trees),
                               'clone_on_demand':True,'clone_filter':'blob:none','clone_timeout_sec':37,'max_cached_clones':64}}
            path.write_text(json.dumps(config))
            loaded = fleet.load_settings(path)
            layout = fleet.repo_layout(loaded)
            self.assertEqual(layout['checkout_root'], checkout)
            self.assertEqual(layout['worktree_root'], trees)
            self.assertTrue(layout['clone_enabled'])
            self.assertEqual(layout['clone_timeout_sec'],37)
            self.assertIn(str(trees),loaded['workspace_roots'])
            self.assertIn(str(trees),loaded['cleanup_roots'])
            offer = {'id':'launch-test', 'workspace':str(trees/'hub-launch-test'/'demo'),
                     'repo_provision':{'mode':'clone_on_demand','name':'demo','workspace_root':str(checkout),
                                       'checkout_path':str(checkout/'demo'),'worktree_root':str(trees),'session_id':'launch-test'}}
            self.assertEqual(fleet.offered_repo_checkout(loaded, offer), checkout/'demo')
            self.assertFalse(trees.exists())
            for field,value in [('clone_on_demand',1),('clone_timeout_sec',False),('clone_filter','arbitrary'),('worktree_root','relative')]:
                changed = json.loads(json.dumps(config))
                changed['repos'][field] = value
                path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    fleet.load_settings(path)

    def test_single_clone_report_uses_only_git_metadata_and_marks_cache_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()/'demo'
            (root/'.git').mkdir(parents=True)
            def command(argv):
                self.assertEqual(argv[0],'git')
                return 'https://github.com/Example/demo.git' if argv[3]=='remote' else 'a'*40+'\n2026-10-29T00:00:00Z'
            row = repos.scan_single(root,'fixture-host',cloned_on_demand=True,runner=command)
            self.assertEqual(row['origin'],'Example/demo')
            self.assertEqual(row['checkout_status'],'present')
            self.assertTrue(row['cloned_on_demand'])
            self.assertEqual(row['last_commit']['sha'],'a'*40)

    def test_scan_configuration_is_accepted_by_the_shared_fleet_settings_loader(self):
        with tempfile.TemporaryDirectory() as tmp, patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
            root = Path(tmp).resolve()
            path = root / 'subscriptions.json'
            config = {'version':1,'subscriptions':[],'workspaces':{'demo':str(root)},'repos':{'scan_roots':[str(root)]}}
            path.write_text(json.dumps(config))
            self.assertEqual(fleet.load_settings(path)['repos']['scan_roots'], [str(root)])
            self.assertEqual(repos.scan_roots(settings_path=path), [str(root)])
            config['repos']['scan_roots'] = ['relative/path']
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, 'repository scan roots'):
                fleet.load_settings(path)

    def test_seed_preserves_cells_aliases_groups_and_duplicates(self):
        text = '''## Main / active projects
### DataTalks.Club (org `DataTalksClub`)
| Dir | Origin | Purpose / stack | Key folders | Workflows | URL |
|---|---|---|---|---|---|
| `dtc-website` | Example/website | Django (Py 3.13, uv), **verbatim** | `content/`, `e2e/` | `ci.yml` | dev.example.test, example.test |
## Other repos (less active)
| Group | Dirs (origin `org/repo` when it differs) |
|---|---|
| DTC misc | `dtc-d12b-package` (= website, old), `other` (Example/other) |
| AISL / courses | `python-course(-code)` |
| Not git / scratch | `tmp` |
## Aliases
- "DTC website", "new site" -> `dtc-website` (content/blog -> `other`); "another label" -> `other`; "web" (client) -> `other`
'''
        with tempfile.TemporaryDirectory() as tmp:
            rows = {r['name']: r for r in repos.seed_map(text, tmp, 'host')}
        self.assertEqual(rows['dtc-website']['purpose'], 'Django (Py 3.13, uv), **verbatim**')
        self.assertEqual(rows['dtc-website']['stack'], rows['dtc-website']['purpose'])
        self.assertEqual(rows['dtc-website']['key_folders'], '`content/`, `e2e/`')
        self.assertEqual(rows['dtc-website']['urls'], 'dev.example.test, example.test')
        self.assertEqual(rows['dtc-website']['aliases'], ['DTC website', 'new site'])
        self.assertEqual(rows['other']['aliases'], ['content/blog', 'another label', 'web'])
        self.assertEqual(rows['dtc-d12b-package']['canonical'], 'dtc-website')
        self.assertIn('python-course-code', rows)
        self.assertIn('python-course', rows)
        self.assertNotIn('tmp', rows)

    def runner(self, argv, cwd=None):
        if argv[0] == 'gh': return json.dumps(dict(defaultBranchRef=dict(name='main'), isArchived=False, url='https://github.com/Example/site'))
        if 'get-url' in argv: return 'git@github.com:Example/site.git'
        if 'log' in argv: return 'a' * 40 + '\n2026-10-26T00:00:00Z'
        if 'branch' in argv: return 'main'
        if 'status' in argv: return '?? new.txt'
        self.fail('Unexpected argv')

    def test_scan_skips_worktrees_symlinks_and_flags_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = root / 'slack-moderation'
            (clone / '.git').mkdir(parents=True)
            (clone / '.github/workflows').mkdir(parents=True)
            (clone / '.github/workflows/ci.yml').touch()
            wt = root / 'site-wt'
            wt.mkdir()
            (wt / '.git').write_text('gitdir: elsewhere')
            (root / 'link').symlink_to(clone, target_is_directory=True)
            result = repos.scan(root, 'host', [], self.runner)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['name'], 'slack-moderation')
            self.assertIn('NEW', result[0]['flags'])
            self.assertEqual(result[0]['workflows'], 'ci.yml')
            self.assertEqual(result[0]['last_commit']['sha'], 'a' * 40)
            self.assertFalse(set(result[0]) & set(repos.HUMAN))

    def test_move_and_missing_retain_human_fields_server_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'renamed/.git').mkdir(parents=True)
            old = dict(name='site', machine='host', dir=str(root / 'old'), origin='Example/site', org='Example', status='active', flags=[], purpose='human')
            missing = dict(old, name='missing', dir=str(root / 'missing'), origin='Other/repo')
            result = {r['name']: r for r in repos.scan(root, 'host', [old, missing], self.runner)}
            self.assertIn('MOVED', result['site']['flags'])
            self.assertEqual(result['site']['dir'], str(root / 'renamed'))
            self.assertIn('MISSING', result['missing']['flags'])
            self.assertNotIn('purpose', result['site'])
            self.assertNotIn('purpose', result['missing'])

    def test_gh_failure_retains_successful_server_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'site'
            (path / '.git').mkdir(parents=True)
            def runner(argv):
                if argv[0] == 'gh': raise ValueError('failure')
                return self.runner(argv)
            old = dict(name='site', machine='host', dir=str(path), status='active', default_branch='known', flags=[])
            result = repos.scan(tmp, 'host', [old], runner)[0]
            self.assertIn('GH_ERROR', result['flags'])
            self.assertNotIn('default_branch', result)

    def test_scan_preserves_operator_confirmed_duplicate_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old-site'
            (path / '.git').mkdir(parents=True)
            old = dict(name='old-site', machine='host', dir=str(path), origin='Example/site', org='Example',
                       status='duplicate', canonical='main-site', flags=[])
            result = repos.scan(tmp, 'host', [old], self.runner)[0]
            self.assertEqual(result['status'], 'duplicate')
            self.assertEqual(result['canonical'], 'main-site')

    def test_registry_absolute_paths_keep_existing_host_confinement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'git'
            root.mkdir()
            (root / 'site').mkdir()
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            (root / 'escape').symlink_to(outside, target_is_directory=True)
            settings = dict(workspaces={}, workspace_roots=[str(root)])
            self.assertEqual(fleet.resolve_workspace_path(settings, str(root / 'site')), root / 'site')
            for path in (str(outside), str(root / 'escape')):
                with self.assertRaises(ValueError): fleet.resolve_workspace_path(settings, path)

    def test_close_matches_are_safe_cli_diagnostic(self):
        raw = json.dumps(dict(error='unknown_workspace', matches=['dtc-website', 'https://bad.test', 'bad\ntext'])).encode()
        err = urllib.error.HTTPError('https://example.test', 400, 'bad', {}, io.BytesIO(raw))
        result = session_control.http_error(err)
        self.assertIn('close matches: dtc-website', str(result))
        self.assertNotIn('bad.test', str(result))

    def test_registered_repo_suggestion_is_safe_cli_diagnostic(self):
        raw = json.dumps(dict(error='unknown_repo', repo='AI-Shipping-Labs/ai-shipping-labs',
                              matches=['AI-Shipping-Labs/website', 'https://bad.test'])).encode()
        err = urllib.error.HTTPError('https://example.test', 400, 'bad', {}, io.BytesIO(raw))
        result = session_control.http_error(err)
        self.assertIn('unknown repository: AI-Shipping-Labs/ai-shipping-labs', str(result))
        self.assertIn('registered repositories: AI-Shipping-Labs/website', str(result))
        self.assertNotIn('bad.test', str(result))

    def test_cli_filters_and_operator_edit(self):
        parser = argparse.ArgumentParser()
        repos.add_commands(parser.add_subparsers(dest='cmd'))
        with patch.object(repos.intake, 'request', return_value={}) as request:
            repos.execute(parser.parse_args(['repos', 'list', '--org', 'Example', '--status', 'active', '--flagged']))
            self.assertEqual(request.call_args.args, ('GET', '/api/repos?org=Example&status=active&flagged=true'))

            with tempfile.TemporaryDirectory() as tmp:
                fields = Path(tmp) / 'repo.json'
                fields.write_text(json.dumps({'label': 'Old site', 'status': 'duplicate', 'canonical': 'main-site'}))
                repos.execute(parser.parse_args(['repos', 'edit', 'old-site', '--file', str(fields)]))
            self.assertEqual(request.call_args.args, ('PATCH', '/api/repos/old-site', {'label': 'Old site', 'status': 'duplicate', 'canonical': 'main-site'}))

class MultiHostRepoTests(unittest.TestCase):
    runner = RepoTests.runner
    def test_configured_roots_and_explicit_override_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / 'a', root / 'b'
            a.mkdir(); b.mkdir()
            settings = root / 'subscriptions.json'
            settings.write_text(json.dumps(dict(workspace_roots=[str(a), str(b)])))
            self.assertEqual(repos.scan_roots(settings_path=settings), [str(a), str(b)])
            self.assertEqual(repos.scan_roots([str(b)], settings), [str(b)])
            settings.write_text(json.dumps(dict(workspace_roots=[str(a)], repos=dict(scan_roots=[str(b)]))))
            self.assertEqual(repos.scan_roots(settings_path=settings), [str(b)])
            settings.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'no_configured_repo_roots'):
                repos.scan_roots(settings_path=settings)

    def test_second_host_scans_only_own_state_and_reports_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'site/.git').mkdir(parents=True)
            existing = [dict(name='site', origin='Example/site', status='active', flags=[], checkouts=[
                dict(host_id='first', path='/different/root/site', status='present', flags=[])])]
            rows = repos.scan(root, 'second', existing, self.runner)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['machine'], 'second')
            self.assertEqual(rows[0]['dir'], str(root / 'site'))
            self.assertEqual(rows[0]['origin_url'], 'Example/site')
            self.assertEqual(rows[0]['default_branch'], 'main')
            self.assertEqual(rows[0]['checkout_status'], 'present')

    def test_multi_root_does_not_mark_other_root_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / 'a', root / 'b'
            (a / 'one/.git').mkdir(parents=True); (b / 'two/.git').mkdir(parents=True)
            existing = [dict(name='one', machine='host', dir=str(a / 'one'), status='active', flags=[]),
                        dict(name='two', machine='host', dir=str(b / 'two'), status='active', flags=[])]
            rows = repos.scan_all([a, b], 'host', existing, self.runner)
            self.assertEqual({r['name'] for r in rows}, {'one', 'two'})
            self.assertTrue(all(r['checkout_status'] == 'present' and 'MISSING' not in r['flags'] for r in rows))

    def test_refresh_roots_fail_before_http_and_repeatable_cli_flags(self):
        parser = argparse.ArgumentParser()
        repos.add_commands(parser.add_subparsers(dest='cmd'))
        args = parser.parse_args(['repos', 'refresh', '--machine', 'host', '--root', '/a', '--root', '/b'])
        self.assertEqual(args.root, ['/a', '/b'])
        with patch.object(repos, 'scan_roots', side_effect=ValueError('no_configured_repo_roots')), patch.object(repos.intake, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'no_configured_repo_roots'): repos.execute(args)
            request.assert_not_called()
