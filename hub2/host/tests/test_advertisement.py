import os
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from agent_hub_host.advertisement import Advertisement, INVENTORY_OVERFLOW_TAG
from agent_hub_host.agent import Bridge
from agent_hub_host.repository_identity import (REPOSITORY_IDENTITY_CAPABILITY, _read_regular,
                                                 normalize_git_origin, qualified_workspace,
                                                 read_git_origin)


class AdvertisementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'actual-user-directory'
        self.home.mkdir()

    def test_explicit_observations_and_alias(self):
        observed = Advertisement([self.root], ['browser:orin-chrome', 'tool:agent-browser'], {'Home': str(self.home)})
        self.assertEqual(observed.resolve_alias('HOME'), self.home)
        self.assertEqual(observed.tags(), ['browser:orin-chrome', 'tool:agent-browser', 'workspace:home'])
        self.assertEqual(Advertisement([self.root]).tags(), [])

    def test_rejects_escalation_tags_and_invalid_shapes(self):
        for value in [['headless'], ['credentials:local:interactive:codex:codex'], ['workspace:home'],
                      ['grant:browser'], ['tool:bad tag'], ['repo-identity:v1'], 'browser:orin-chrome', [None]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Advertisement([self.root], value)
        for value in [{'home': str(self.home), 'HOME': str(self.home)}, {'../home': str(self.home)},
                      {'home': 'relative'}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Advertisement([self.root], [], value)
        # Outside-allowlist identity is retained but never advertised or launched.
        escaped = Advertisement([self.root], [], {'home': str(self.root.parent)})
        self.assertEqual(escaped.tags(), [])
        with self.assertRaises(ValueError):
            escaped.resolve_alias('home')

    def test_deleted_or_retargeted_alias_is_never_advertised_or_launched(self):
        link = self.root / 'alias'
        try:
            link.symlink_to(self.home, target_is_directory=True)
        except OSError:
            self.skipTest('symlink unavailable')
        observed = Advertisement([self.root], [], {'home': str(link)})
        link.unlink()
        link.symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(observed.tags(), [])
        with self.assertRaises(ValueError):
            observed.resolve_alias('home')
        link.unlink()
        self.assertEqual(observed.tags(), [])
        with self.assertRaises(ValueError):
            observed.resolve_alias('home')


    def test_missing_alias_directory_keeps_bridge_alive_and_refuses_only_that_alias(self):
        missing = self.root / 'deleted-alias'
        bridge = self.bridge(workspaces={'home': str(missing)}, capabilities=['browser:orin-chrome'])
        (self.root / 'home').mkdir()  # basename candidate must not win while the alias is enrolled
        self.assertEqual(bridge.advertisement.workspaces, {'home': str(missing)})
        self.assertEqual(bridge.advertisement.tags(), ['browser:orin-chrome'])
        with self.assertRaisesRegex(ValueError, 'workspace_unavailable'):
            bridge.resolve_workspace('home')
        other = self.root / 'other'
        other.mkdir()
        self.assertEqual(bridge.resolve_workspace('other'), other)
        bridge.heartbeat(['desktop'], include_native_sessions=False)
        tags = bridge.client.heartbeat.call_args.args[2]
        self.assertIn('browser:orin-chrome', tags)
        self.assertNotIn('workspace:home', tags)

    def test_alias_must_be_directory_and_capacity_is_bounded(self):
        file = self.root / 'file'
        file.write_text('synthetic')
        observed = Advertisement([self.root], [], {'home': str(file)})
        self.assertEqual(observed.tags(), [])
        with self.assertRaises(ValueError):
            observed.resolve_alias('home')
        with self.assertRaises(ValueError):
            Advertisement([self.root], ['tool:item' + str(i) for i in range(32)], {'home': str(self.home)})

    def bridge(self, **kwargs):
        roots = kwargs.pop('roots', [self.root])
        bridge = Bridge(Mock(), None, roots, ['codex'], executable=sys.executable,
                        launcher_config={'private_provider_homes': True}, **kwargs)
        bridge.launcher.capabilities = lambda: {'codex': {'headless', 'interactive', 'effort'}}
        return bridge

    def test_heartbeat_and_launch_use_the_same_enrolled_alias(self):
        bridge = self.bridge(capabilities=['browser:orin-chrome', 'tool:agent-browser'],
                             workspaces={'home': str(self.home)})
        self.assertEqual(bridge.resolve_workspace('HOME'), self.home)
        bridge.heartbeat(['desktop'], include_native_sessions=False)
        tags = bridge.client.heartbeat.call_args.args[2]
        self.assertTrue({'browser:orin-chrome', 'tool:agent-browser', 'workspace:home', 'effort:codex'} <= set(tags))
        self.assertIn(REPOSITORY_IDENTITY_CAPABILITY, tags)
        self.home.rmdir()
        bridge.heartbeat([], include_native_sessions=False)
        self.assertNotIn('workspace:home', bridge.client.heartbeat.call_args.args[2])
        with self.assertRaises((OSError, ValueError)):
            bridge.resolve_workspace('home')

    def test_alias_precedes_basename_and_no_implicit_home_is_invented(self):
        (self.root / 'home').mkdir()
        bridge = self.bridge(workspaces={'home': str(self.home)})
        self.assertEqual(bridge.resolve_workspace('home'), self.home)
        legacy = self.bridge()
        self.assertEqual(legacy.resolve_workspace('home'), self.root / 'home')
        legacy.heartbeat([], include_native_sessions=False)
        self.assertNotIn('workspace:home', legacy.client.heartbeat.call_args.args[2])

    def test_capability_overflow_still_connects_and_preserves_accepted_tags(self):
        bridge = self.bridge(capabilities=['tool:item' + str(i) for i in range(31)])
        bridge.heartbeat(['desktop'], include_native_sessions=False)
        bridge.client.heartbeat.assert_called()
        tags = bridge.client.heartbeat.call_args.args[2]
        self.assertEqual(len(tags), 32)
        self.assertTrue({'tool:item' + str(i) for i in range(31)} <= set(tags))
        self.assertIn('headless', tags)
        self.assertNotIn(REPOSITORY_IDENTITY_CAPABILITY, tags)

    def test_full_nonworkspace_budget_disables_repo_identity_without_losing_bare_labels(self):
        self.clone_with_origin(self.root, 'legacy-name', 'https://github.com/org/legacy-name.git')
        bridge = self.bridge(capabilities=['tool:item' + str(i) for i in range(31)])
        bridge.heartbeat([], include_native_sessions=False)
        tags = bridge.client.heartbeat.call_args.args[2]
        self.assertIn('workspace:legacy-name', tags)
        self.assertIn(INVENTORY_OVERFLOW_TAG, tags)
        self.assertNotIn('workspace:' + qualified_workspace('https://github.com/org/legacy-name'), tags)
        self.assertNotIn(REPOSITORY_IDENTITY_CAPABILITY, tags)


    def clone(self, root, name):
        path = root / name
        path.mkdir()
        (path / '.git').mkdir()
        return path

    def clone_with_origin(self, root, name, origin):
        path = self.clone(root, name)
        (path / '.git' / 'config').write_text('[remote "origin"]\n\turl = "' + origin + '"\n', encoding='utf-8')
        return path

    def test_clone_created_after_enrollment_is_advertised_and_resolved(self):
        bridge = self.bridge()
        repo = self.clone(self.root, 'telegram-writing-assistant')
        bridge.heartbeat([], include_native_sessions=False)
        self.assertIn('workspace:telegram-writing-assistant', bridge.client.heartbeat.call_args.args[2])
        self.assertEqual(bridge.resolve_workspace('TELEGRAM-WRITING-ASSISTANT'), repo)
        (repo / '.git').rmdir()
        self.assertNotIn('workspace:telegram-writing-assistant', bridge.advertisement.tags())

    def test_git_worktree_file_and_checkout_root_are_discovered(self):
        repo = self.root / 'project'
        repo.mkdir()
        (repo / '.git').write_text('gitdir: elsewhere')
        observed = Advertisement([repo])
        self.assertEqual(observed.tags(), ['workspace:project'])
        self.assertEqual(observed.resolve_alias('PROJECT'), repo)

    def test_ambiguous_clone_labels_and_escaped_symlinks_are_not_advertised(self):
        first, second = self.root / 'first', self.root / 'second'
        first.mkdir(); second.mkdir()
        self.clone(first, 'project'); self.clone(second, 'project')
        observed = Advertisement([first, second])
        self.assertNotIn('workspace:project', observed.tags())
        self.assertIsNone(observed.resolve_alias('project'))
        outside = self.clone(self.root, 'outside')
        (first / 'escape').symlink_to(outside, target_is_directory=True)
        self.assertNotIn('workspace:escape', observed.tags())

    def test_missing_explicit_alias_masks_discovered_clone(self):
        self.clone(self.root, 'project')
        observed = Advertisement([self.root], workspaces={'project': str(self.root / 'missing')})
        self.assertEqual(observed.tags(), [])
        with self.assertRaisesRegex(ValueError, 'workspace_unavailable'):
            observed.resolve_alias('project')

    def test_many_clones_preserve_engine_capabilities_and_all_workspace_labels(self):
        bridge = self.bridge()
        for index in range(80):
            self.clone(self.root, 'project-' + str(index))
        bridge.heartbeat([], include_native_sessions=False)
        tags = bridge.client.heartbeat.call_args.args[2]
        self.assertTrue({'headless', 'interactive:codex', 'effort:codex'} <= set(tags))
        self.assertEqual(len([tag for tag in tags if tag.startswith('workspace:')]), 80)

    def test_many_checkouts_with_one_origin_consume_one_qualified_tag(self):
        bridge = self.bridge()
        for index in range(80):
            self.clone_with_origin(self.root, 'replica-' + str(index),
                                   'https://github.com/org/shared-origin.git')
        bridge.heartbeat([], include_native_sessions=False)
        tags = bridge.client.heartbeat.call_args.args[2]
        self.assertEqual(len([tag for tag in tags if tag.startswith('workspace:')]), 81)
        self.assertIn('workspace:' + qualified_workspace('https://github.com/org/shared-origin'), tags)
        self.assertIn(REPOSITORY_IDENTITY_CAPABILITY, tags)

    def test_repository_identity_matches_registry_https_and_checkout_ssh_vector(self):
        registry = 'https://github.com/AI-Shipping-Labs/Website.git'
        checkout = 'git@github.com:ai-shipping-labs/website.git'
        self.assertEqual(normalize_git_origin(registry), 'github.com/ai-shipping-labs/website')
        self.assertEqual(normalize_git_origin(checkout), normalize_git_origin(registry))
        self.assertEqual(qualified_workspace(registry), 'repo-948832775e82e294b99d190121d47ac86ec4986811a36b2c505e3a6f802')
        self.assertEqual(qualified_workspace('https://user:secret@github.com/owner/repo.git'),
                         qualified_workspace('ssh://git@github.com:22/OWNER/REPO'))
        self.assertIsNone(normalize_git_origin('https://github.com/owner/repo?token=secret'))

    def test_same_basename_different_origins_resolve_distinct_repositories(self):
        first_root, second_root = self.root / 'owner-a', self.root / 'owner-b'
        first_root.mkdir(); second_root.mkdir()
        first = self.clone_with_origin(first_root, 'project', 'https://github.com/owner-a/project.git')
        second = self.clone_with_origin(second_root, 'project', 'git@github.com:owner-b/project.git')
        bridge = self.bridge(roots=[first_root, second_root])
        first_token = qualified_workspace('https://github.com/owner-a/project')
        second_token = qualified_workspace('https://github.com/owner-b/project')
        tags = bridge.advertisement.tags()
        self.assertNotIn('workspace:project', tags)
        self.assertIn('workspace:' + first_token, tags)
        self.assertIn('workspace:' + second_token, tags)
        self.assertEqual(bridge.resolve_workspace(first_token), first)
        self.assertEqual(bridge.resolve_workspace(second_token), second)

    def test_wrong_origin_content_is_skipped_and_renamed_correct_clone_is_selected(self):
        shipping = self.root / 'AI-Shipping-Labs'
        shipping.mkdir()
        wrong = self.clone_with_origin(shipping, 'content', 'https://github.com/AI-Shipping-Labs/content.git')
        renamed = self.clone_with_origin(shipping, 'workshops-content-copy',
                                         'git@github.com:AI-Shipping-Labs/workshops-content.git')
        token = qualified_workspace('https://github.com/AI-Shipping-Labs/workshops-content')
        bridge = self.bridge(roots=[shipping])
        self.assertEqual(bridge.resolve_workspace(token), renamed)
        self.assertNotEqual(bridge.resolve_workspace(token), wrong)
        self.assertIn('workspace:' + token, bridge.advertisement.tags())

    def test_hidden_clone_names_advertise_only_origin_identity(self):
        agents = self.clone_with_origin(self.root, '.agents', 'https://github.com/alexeygrigorev/agents.git')
        codex = self.clone_with_origin(self.root, '.codex', 'https://github.com/alexeygrigorev/codex.git')
        bridge = self.bridge()
        tags = bridge.advertisement.tags()
        self.assertIn('workspace:' + qualified_workspace('https://github.com/alexeygrigorev/agents'), tags)
        self.assertIn('workspace:' + qualified_workspace('https://github.com/alexeygrigorev/codex'), tags)
        self.assertNotIn('workspace:.agents', tags)
        self.assertNotIn('workspace:.codex', tags)
        self.assertEqual(bridge.resolve_workspace(qualified_workspace('https://github.com/alexeygrigorev/agents')), agents)
        self.assertEqual(bridge.resolve_workspace(qualified_workspace('https://github.com/alexeygrigorev/codex')), codex)

    def test_multiple_same_origin_clones_choose_canonical_basename_then_stable_path(self):
        canonical = self.clone_with_origin(self.root, 'canonical-project', 'https://github.com/org/canonical-project.git')
        self.clone_with_origin(self.root, 'z-renamed', 'git@github.com:org/canonical-project.git')
        self.clone_with_origin(self.root, 'a-renamed', 'https://github.com/org/canonical-project')
        bridge = self.bridge()
        token = qualified_workspace('https://github.com/org/canonical-project')
        self.assertEqual(bridge.resolve_workspace(token), canonical)
        canonical_config = canonical / '.git' / 'config'
        canonical_config.unlink()
        self.assertEqual(bridge.resolve_workspace(token), self.root / 'a-renamed')

    def test_worktree_commondir_and_ssh_origin_match_registry_https(self):
        main = self.root / 'main-checkout'
        common = main / '.git'
        worktree_admin = common / 'worktrees' / 'task'
        worktree_admin.mkdir(parents=True)
        main.mkdir(exist_ok=True)
        (common / 'config').write_text('[remote "origin"]\n\turl = https://github.com/Org/Project.git\n', encoding='utf-8')
        (worktree_admin / 'commondir').write_text('../..\n', encoding='utf-8')
        worktree = self.root / 'project'
        worktree.mkdir()
        (worktree / '.git').write_text('gitdir: ../main-checkout/.git/worktrees/task\n', encoding='utf-8')
        bridge = self.bridge()
        token = qualified_workspace('https://github.com/org/project')
        self.assertIn('workspace:' + token, bridge.advertisement.tags())
        self.assertEqual(bridge.resolve_workspace(token), worktree)

    def test_worktree_commondir_pointer_cannot_escape_enrolled_roots(self):
        worktree = self.root / 'linked'
        gitdir = self.root / 'common.git' / 'worktrees' / 'linked'
        worktree.mkdir()
        (gitdir / 'info').mkdir(parents=True)
        outside_temp = tempfile.TemporaryDirectory(dir=self.root.parent)
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        (outside / 'config').write_text(
            '[remote "origin"]\n\turl = https://github.com/org/escaped.git\n', encoding='utf-8')
        (gitdir / 'commondir').write_text(str(outside) + '\n', encoding='utf-8')
        (worktree / '.git').write_text('gitdir: ../common.git/worktrees/linked\n', encoding='utf-8')
        bridge = self.bridge()
        token = qualified_workspace('https://github.com/org/escaped')
        self.assertNotIn('workspace:' + token, bridge.advertisement.tags())
        with self.assertRaisesRegex(ValueError, 'workspace_identity_unavailable'):
            bridge.resolve_workspace(token)

    def test_stale_origin_symlink_escape_and_reserved_token_never_fall_back(self):
        url = 'https://github.com/org/expected.git'
        repo = self.clone_with_origin(self.root, 'expected', url)
        token = qualified_workspace(url)
        bridge = self.bridge()
        self.assertEqual(bridge.resolve_workspace(token), repo)
        (repo / '.git' / 'config').write_text('[remote "origin"]\n\turl = https://github.com/other/expected.git\n', encoding='utf-8')
        self.assertNotIn('workspace:' + token, bridge.advertisement.tags())
        with self.assertRaisesRegex(ValueError, 'workspace_identity_unavailable'):
            bridge.resolve_workspace(token)

        outside_temp = tempfile.TemporaryDirectory(dir=self.root.parent)
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        outside_config = outside / 'config'
        outside_config.write_text('[remote "origin"]\n\turl = ' + url + '\n', encoding='utf-8')
        (repo / '.git').rename(repo / '.git.safe')
        try:
            (repo / '.git').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaisesRegex(ValueError, 'workspace_identity_unavailable'):
            bridge.resolve_workspace(token)

        reserved = self.root / token
        reserved.mkdir()
        with self.assertRaisesRegex(ValueError, 'workspace_identity_unavailable'):
            bridge.resolve_workspace(token)
        reserved.rmdir()

        impostor = self.clone_with_origin(self.root, token, 'https://github.com/org/not-expected.git')
        alias_bridge = self.bridge(workspaces={token: str(impostor)})
        self.assertIn('workspace:' + token, alias_bridge.advertisement.tags())
        with self.assertRaisesRegex(ValueError, 'workspace_identity_unavailable'):
            alias_bridge.resolve_workspace(token)

    def test_identity_observation_is_bounded_and_does_not_invoke_git(self):
        repo = self.clone_with_origin(self.root, 'bounded', 'https://github.com/org/bounded.git')
        observed = Advertisement([self.root])
        with patch('subprocess.run', side_effect=AssertionError('Git subprocess is forbidden')):
            self.assertIn('workspace:' + qualified_workspace('https://github.com/org/bounded'), observed.tags())
        with patch('agent_hub_host.advertisement.MAX_ROOT_ENTRIES', 0):
            tags = observed.tags()
        self.assertIn('workspace:inventory-overflow', tags)
        self.assertNotIn('workspace:bounded', tags)
        self.assertNotIn('workspace:' + qualified_workspace('https://github.com/org/bounded'), tags)

    def test_git_config_read_limit_and_includes_fail_closed_even_with_direct_origin(self):
        repo = self.clone_with_origin(self.root, 'limited', 'https://github.com/org/limited.git')
        with patch('agent_hub_host.repository_identity.MAX_GIT_METADATA_BYTES', 16):
            tags = Advertisement([self.root]).tags()
        self.assertIn('workspace:limited', tags)  # the legacy label remains observable
        self.assertNotIn('workspace:' + qualified_workspace('https://github.com/org/limited'), tags)

        included = tempfile.TemporaryDirectory(dir=self.root.parent)
        self.addCleanup(included.cleanup)
        included_path = Path(included.name) / 'config'
        included_path.write_text('[remote "origin"]\n\turl = https://github.com/org/included.git\n', encoding='utf-8')
        direct = 'https://github.com/org/limited.git'
        (repo / '.git' / 'config').write_text(
            '[remote "origin"]\n\turl = ' + direct + '\n[include]\n\tpath = ' + str(included_path) + '\n',
            encoding='utf-8')
        self.assertIsNone(Advertisement([self.root])._observe()[1].get(
            qualified_workspace(direct)))
        self.assertIsNone(Advertisement([self.root])._observe()[1].get(
            qualified_workspace('https://github.com/org/included')))

    def test_inactive_worktree_config_cannot_fabricate_origin_and_empty_override_fails_closed(self):
        worktree = self.root / 'linked'
        gitdir = self.root / 'common.git' / 'worktrees' / 'linked'
        common = self.root / 'common.git'
        worktree.mkdir()
        (gitdir / 'info').mkdir(parents=True)
        (common / 'config').write_text('[remote "origin"]\n\turl = https://github.com/org/common.git\n', encoding='utf-8')
        (gitdir / 'commondir').write_text('../..\n', encoding='utf-8')
        (gitdir / 'config.worktree').write_text(
            '[remote "origin"]\n\turl = https://github.com/org/inactive-spoof.git\n', encoding='utf-8')
        (worktree / '.git').write_text('gitdir: ../common.git/worktrees/linked\n', encoding='utf-8')
        observed = Advertisement([self.root])
        self.assertIn(qualified_workspace('https://github.com/org/common'), observed._observe()[1])
        self.assertNotIn(qualified_workspace('https://github.com/org/inactive-spoof'), observed._observe()[1])

        (common / 'config').write_text(
            '[extensions]\n\tworktreeConfig = true\n[remote "origin"]\n'
            '\turl = https://github.com/org/common.git\n', encoding='utf-8')
        (gitdir / 'config.worktree').write_text(
            '[remote "origin"]\n\turl = ""\n\turl = https://github.com/org/worktree.git\n', encoding='utf-8')
        origins = observed._observe()[1]
        self.assertNotIn(qualified_workspace('https://github.com/org/worktree'), origins)
        self.assertNotIn(qualified_workspace('https://github.com/org/common'), origins)
        self.assertIsNone(read_git_origin(worktree, [self.root]))

        # When common config has no origin, a single enabled config.worktree origin is accepted.
        (common / 'config').write_text('[extensions]\n\tworktreeConfig = true\n', encoding='utf-8')
        (gitdir / 'config.worktree').write_text(
            '[remote "origin"]\n\turl = https://github.com/org/worktree.git\n', encoding='utf-8')
        self.assertIn(qualified_workspace('https://github.com/org/worktree'), observed._observe()[1])

    def test_empty_and_multiple_origin_urls_fail_closed_against_real_git_oracle(self):
        git_env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}

        def git_remote_origin(repo_path):
            return subprocess.run(
                ['git', '-C', str(repo_path), 'remote', 'get-url', 'origin'],
                check=True, capture_output=True, text=True, env=git_env,
            ).stdout.strip()

        url_a = 'https://github.com/org/alpha.git'
        url_b = 'https://github.com/org/beta.git'
        url_c = 'https://github.com/org/gamma.git'

        # Git versions differ on whether an empty URL resets earlier values.
        # Either observed choice remains unsupported by the bounded reader.
        # (a) Same config: URL A, empty URL, URL B.
        same_cfg = self.root / 'same-cfg'
        subprocess.run(['git', 'init', str(same_cfg)], check=True, capture_output=True, env=git_env)
        (same_cfg / '.git' / 'config').write_text(
            f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {url_a}\n\turl = ""\n\turl = {url_b}\n',
            encoding='utf-8',
        )
        self.assertIn(git_remote_origin(same_cfg), (url_a, url_b))
        self.assertIsNone(read_git_origin(same_cfg, [self.root]))

        # (b) Common origin A + enabled linked config.worktree with empty URL then B.
        primary = self.root / 'primary'
        subprocess.run(['git', 'init', str(primary)], check=True, capture_output=True, env=git_env)
        subprocess.run(
            ['git', '-C', str(primary), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
             'commit', '--allow-empty', '-m', 'init'],
            check=True, capture_output=True, env=git_env,
        )
        linked = self.root / 'linked-wt'
        subprocess.run(['git', '-C', str(primary), 'worktree', 'add', str(linked)], check=True, capture_output=True, env=git_env)
        subprocess.run(['git', '-C', str(primary), 'config', 'extensions.worktreeConfig', 'true'], check=True, capture_output=True, env=git_env)
        subprocess.run(['git', '-C', str(primary), 'remote', 'add', 'origin', url_a], check=True, capture_output=True, env=git_env)
        linked_gitdir = Path((linked / '.git').read_text(encoding='utf-8').split('gitdir:', 1)[1].strip())
        (linked_gitdir / 'config.worktree').write_text(
            f'[remote "origin"]\n\turl = ""\n\turl = {url_b}\n',
            encoding='utf-8',
        )
        self.assertIn(git_remote_origin(linked), (url_a, url_b))
        self.assertIsNone(read_git_origin(linked, [self.root]))

        # (c) Primary checkout enabled config.worktree is handled consistently (never skipped or wrong-guessed).
        (primary / '.git' / 'config.worktree').write_text(
            f'[remote "origin"]\n\turl = ""\n\turl = {url_c}\n',
            encoding='utf-8',
        )
        self.assertIn(git_remote_origin(primary), (url_a, url_c))
        self.assertIsNone(read_git_origin(primary, [self.root]))

        (primary / '.git' / 'config.worktree').write_text(
            f'[remote "origin"]\n\turl = {url_c}\n',
            encoding='utf-8',
        )
        self.assertIn(git_remote_origin(primary), (url_a, url_c))
        self.assertIsNone(read_git_origin(primary, [self.root]))

        subprocess.run(['git', '-C', str(primary), 'remote', 'remove', 'origin'], check=True, capture_output=True, env=git_env)
        self.assertEqual(git_remote_origin(primary), url_c)
        self.assertEqual(read_git_origin(primary, [self.root]), normalize_git_origin(url_c))

        # Disabled worktreeConfig on primary checkout ignores config.worktree.
        (primary / '.git' / 'config.worktree').unlink()
        subprocess.run(['git', '-C', str(primary), 'remote', 'add', 'origin', url_a], check=True, capture_output=True, env=git_env)
        subprocess.run(['git', '-C', str(primary), 'config', '--unset', 'extensions.worktreeConfig'], check=True, capture_output=True, env=git_env)
        (primary / '.git' / 'config.worktree').write_text(
            f'[remote "origin"]\n\turl = {url_c}\n',
            encoding='utf-8',
        )
        self.assertEqual(git_remote_origin(primary), url_a)
        self.assertEqual(read_git_origin(primary, [self.root]), normalize_git_origin(url_a))

    def test_regular_metadata_open_does_not_block_if_file_is_replaced_by_fifo(self):
        metadata = self.root / 'metadata'
        metadata.write_text('small')
        real_open = os.open
        flags_seen = []

        def replace_with_fifo(path, flags):
            if Path(path) == metadata:
                metadata.unlink()
                os.mkfifo(metadata)
                flags_seen.append(flags)
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(path, flags)

        with patch('agent_hub_host.repository_identity.os.open', side_effect=replace_with_fifo):
            self.assertIsNone(_read_regular(metadata))
        self.assertEqual(len(flags_seen), 1)

    def test_qualified_workspace_overflow_is_explicit_not_silently_truncated(self):
        self.clone_with_origin(self.root, 'one', 'https://github.com/org/one.git')
        self.clone_with_origin(self.root, 'two', 'https://github.com/org/two.git')
        bridge = self.bridge()
        with patch('agent_hub_host.advertisement.MAX_WORKSPACE_TAGS', 3):
            tags = bridge.advertisement.tags()
        self.assertIn('workspace:inventory-overflow', tags)
        self.assertIn('workspace:one', tags)
        self.assertIn('workspace:two', tags)
        self.assertNotIn('workspace:' + qualified_workspace('https://github.com/org/one'), tags)
        self.assertNotIn('workspace:' + qualified_workspace('https://github.com/org/two'), tags)

    def test_digest_collision_is_not_advertised_or_resolved_as_an_identity(self):
        first = self.clone_with_origin(self.root, 'first', 'https://github.com/owner-a/project.git')
        self.clone_with_origin(self.root, 'second', 'https://github.com/owner-b/project.git')
        bridge = self.bridge()
        with patch('agent_hub_host.advertisement.workspace_token_from_origin', return_value='repo-' + 'a' * 59):
            tags = bridge.advertisement.tags()
            self.assertNotIn('workspace:' + 'repo-' + 'a' * 59, tags)
            with self.assertRaisesRegex(ValueError, 'workspace_identity_unavailable'):
                bridge.resolve_workspace('repo-' + 'a' * 59)


    def test_large_workspace_inventory_transport_allowance_is_heartbeat_only(self):
        from agent_hub_host.client import Client
        import json
        capabilities = ['workspace:project-' + str(i) + 'x' * 45 for i in range(1125)]
        body = {'labels': [], 'engines': ['codex'], 'capabilities': capabilities}
        self.assertGreater(len(json.dumps(body).encode()), 65536)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        with patch('urllib.request.build_opener', return_value=opener):
            Client('https://hub.test').request('POST', '/api/heartbeat', body)
        qualified = ['workspace:repo-' + format(i, '059x') for i in range(2048)]
        nonworkspace = ['headless', REPOSITORY_IDENTITY_CAPABILITY, *['tool:item' + str(i) for i in range(30)]]
        native_sessions = [{'session_id': 'session-' + str(i).zfill(3),
                            'native_id': 'native-' + str(i).zfill(3), 'phase': 'running',
                            'last_activity_at': 1794543623525, 'exit_code': None,
                            'containment_empty': True} for i in range(100)]
        qualified_body = {'labels': [], 'engines': ['codex'], 'capabilities': [*nonworkspace, *qualified],
                          'native_sessions': native_sessions}
        self.assertLess(len(json.dumps(qualified_body).encode()), 262144)
        with patch('urllib.request.build_opener', return_value=opener):
            Client('https://hub.test').request('POST', '/api/heartbeat', qualified_body)
        for method, path in [('POST', '/api/sessions'), ('PUT', '/api/heartbeat')]:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'request too large'):
                Client('https://hub.test').request(method, path, body)
        with self.assertRaisesRegex(ValueError, 'request too large'):
            Client('https://hub.test').request('POST', '/api/heartbeat', {'data': 'x' * 262144})
