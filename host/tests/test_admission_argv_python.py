import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_hub_host import fleet


class HostPythonTokenTest(unittest.TestCase):
    def test_token_expands_to_running_interpreter(self):
        cfg = {'version': 1, 'workspaces': {}, 'subscriptions': [{
            'service': 'claude', 'account_label': 'hub-x',
            'admission_argv': ['{host_python}', '-c', 'print(1)'],
            'startup_argv': ['{host_python}', 'x']}]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'subscriptions.json'
            p.write_text(json.dumps(cfg))
            sub = fleet.load_settings(str(p))['subscriptions'][0]
        self.assertEqual(sub['admission_argv'][0], sys.executable)
        self.assertEqual(sub['startup_argv'][0], sys.executable)


if __name__ == '__main__':
    unittest.main()
