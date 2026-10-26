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

    def test_cli_filters_and_operator_edit(self):
        parser = argparse.ArgumentParser()
        repos.add_commands(parser.add_subparsers(dest='cmd'))
        with patch.object(repos.intake, 'request', return_value={}) as request:
            repos.execute(parser.parse_args(['repos', 'list', '--org', 'Example', '--status', 'active', '--flagged']))
            self.assertEqual(request.call_args.args, ('GET', '/api/repos?org=Example&status=active&flagged=true'))
