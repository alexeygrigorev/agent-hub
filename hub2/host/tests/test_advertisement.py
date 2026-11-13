import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from hub2_agent_hub_host.advertisement import Advertisement
from hub2_agent_hub_host.agent import Bridge


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
                      ['grant:browser'], ['tool:bad tag'], 'browser:orin-chrome', [None]]:
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
        bridge = Bridge(Mock(), None, [self.root], ['codex'], executable=sys.executable,
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


    def clone(self, root, name):
        path = root / name
        path.mkdir()
        (path / '.git').mkdir()
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


    def test_large_workspace_inventory_transport_allowance_is_heartbeat_only(self):
        from hub2_agent_hub_host.client import Client
        import json
        capabilities = ['workspace:project-' + str(i) + 'x' * 45 for i in range(1125)]
        body = {'labels': [], 'engines': ['codex'], 'capabilities': capabilities}
        self.assertGreater(len(json.dumps(body).encode()), 65536)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        with patch('urllib.request.build_opener', return_value=opener):
            Client('https://hub.test').request('POST', '/api/heartbeat', body)
        for method, path in [('POST', '/api/sessions'), ('PUT', '/api/heartbeat')]:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'request too large'):
                Client('https://hub.test').request(method, path, body)
        with self.assertRaisesRegex(ValueError, 'request too large'):
            Client('https://hub.test').request('POST', '/api/heartbeat', {'data': 'x' * 262144})
