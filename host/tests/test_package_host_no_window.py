import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parents[1] / 'agent_hub_host'


class PackageHostTasksStartWithoutConsole(unittest.TestCase):
    """Windows package hosts register the controller and update tasks through conhost --headless (446227dc)."""

    def test_controller_and_update_tasks_use_headless_conhost(self):
        for name in ('bootstrap.py', 'package_update.py'):
            text = (HOST / name).read_text(encoding='utf-8')
            self.assertIn('conhost.exe', text, name)
            self.assertIn('--headless', text, name)
            self.assertIn('-WindowStyle Hidden', text, name)
            self.assertNotIn('-Execute "powershell.exe"', text, name)
            self.assertNotIn("-Execute 'powershell.exe'", text, name)


if __name__ == '__main__':
    unittest.main()
