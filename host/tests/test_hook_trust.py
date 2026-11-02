"""Hook trust follows a regenerated hooks.json, so zcodex never asks 'hook needs review'."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import hook_trust

HOOKS = os.path.join('/home/alexey/.config/agent-hub/managed-engines/zai-central-x/home/.zcodex', 'hooks.json')
DOC = {'hooks': {
    'SessionStart': [{'hooks': [{'command': '/home/alexey/.local/bin/a state-report working || true', 'type': 'command'}]},
                     {'hooks': [{'command': '/home/alexey/.local/bin/aplexer context hook --engine codex 2>/dev/null || true # aplexer-managed-awareness-hook-v1',
                                 'timeout': 30, 'type': 'command'}]}],
    'Stop': [{'hooks': [{'command': '/home/alexey/.local/bin/a state-report idle || true', 'type': 'command'}]}],
}}


class HookTrust(unittest.TestCase):
    def test_hash_matches_the_value_zcodex_stored_when_the_hook_was_approved(self):
        # Values copied from a live zai-central home after `t` was pressed in the engine.
        got = hook_trust.entries('/h/hooks.json', DOC)
        self.assertEqual(got['/h/hooks.json:session_start:0:0'],
                         'sha256:ac5079da741baea9d7cb457e131ce58f3b563b443cd750940bfa2e54be63a0ec')
        self.assertEqual(got['/h/hooks.json:session_start:1:0'],
                         'sha256:b8d459bbdbea59e7ff5bd6064a65ced7d9ea57686bb163d1c9fb40eef8ffd4f6')
        self.assertEqual(got['/h/hooks.json:stop:0:0'],
                         'sha256:6d270f46d79288e0768c5124346fd2289e3c1b694f38041892633190beb161f8')

    def test_refresh_replaces_stale_state_keeps_other_config_and_is_idempotent(self):
        tmp = Path(tempfile.mkdtemp())
        config = tmp / 'config.toml'
        hooks = tmp / 'hooks.json'
        config.write_text('model = "glm-5.3-flash"\n\n[projects."/x"]\ntrust_level = "trusted"\n\n'
                          '[hooks.state.%s]\ntrusted_hash = "sha256:stale"\n\n'
                          '[hooks.state."/other/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:keep"\n' % __import__('json').dumps(str(hooks) + ':stop:0:0'),
                          encoding='utf-8')
        self.assertTrue(hook_trust.refresh(config, hooks, DOC))
        text = config.read_text(encoding='utf-8')
        self.assertNotIn('sha256:stale', text)
        self.assertIn('sha256:keep', text)
        self.assertIn('model = "glm-5.3-flash"', text)
        self.assertIn('[projects."/x"]', text)
        self.assertEqual(text.count('[hooks.state.'), 4)
        self.assertFalse(hook_trust.refresh(config, hooks, DOC))
        self.assertEqual(config.read_text(encoding='utf-8'), text)

    def test_creates_config_when_absent(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertTrue(hook_trust.refresh(tmp / 'new' / 'config.toml', tmp / 'hooks.json', DOC))
        self.assertIn('trusted_hash', (tmp / 'new' / 'config.toml').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
