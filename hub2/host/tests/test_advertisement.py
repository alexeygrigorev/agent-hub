import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock

from agent_hub_host.advertisement import Advertisement
from agent_hub_host.agent import Bridge


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
                      {'home': str(self.root.parent)}, {'home': 'relative'}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Advertisement([self.root], [], value)

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
        with self.assertRaises(OSError):
            observed.resolve_alias('home')


    def test_alias_must_be_directory_and_capacity_is_bounded(self):
        file = self.root / 'file'
        file.write_text('synthetic')
        with self.assertRaises(ValueError):
            Advertisement([self.root], [], {'home': str(file)})
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

    def test_resource_observations_are_never_silently_truncated(self):
        bridge = self.bridge(capabilities=['tool:item' + str(i) for i in range(31)])
        with self.assertRaisesRegex(ValueError, 'advertisement_capacity_exceeded'):
            bridge.heartbeat([], include_native_sessions=False)
        bridge.client.heartbeat.assert_not_called()
