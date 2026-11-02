import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub_host import bootstrap


class ConfigureDefaultEngineUtf8(unittest.TestCase):
    def test_non_ascii_claude_settings_do_not_depend_on_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.claude').mkdir()
            (home / '.claude/settings.json').write_bytes(json.dumps({'note': 'caf' + chr(0xe9) + chr(0x201d)}, ensure_ascii=False).encode('utf-8'))
            env = {'APPDATA': str(home / 'appdata'), 'XDG_CONFIG_HOME': str(home / 'cfg')}
            real_read = Path.read_text

            def cp1252_default(self, encoding=None, errors=None):
                return real_read(self, encoding=encoding or 'cp1252', errors=errors)

            with mock.patch.dict(os.environ, env), mock.patch.object(Path, 'home', return_value=home), \
                    mock.patch.object(Path, 'read_text', cp1252_default):
                bootstrap.configure_default_engine()
            data = json.loads((home / '.claude/settings.json').read_text(encoding='utf-8'))
            self.assertEqual(data['model'], 'opus')


if __name__ == '__main__':
    unittest.main()
