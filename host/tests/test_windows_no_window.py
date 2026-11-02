"""Host controller children never open a console window (intake 446227dc)."""
import os
import subprocess
import sys
import unittest

from agent_hub_host import windows_no_window as w


class FlagTests(unittest.TestCase):
    def test_adds_create_no_window(self):
        self.assertEqual(w.hidden_flags(0), w.CREATE_NO_WINDOW)
        self.assertTrue(w.hidden_flags(0x200) & w.CREATE_NO_WINDOW)

    def test_new_console_is_replaced(self):
        flags = w.hidden_flags(w.CREATE_NEW_CONSOLE)
        self.assertFalse(flags & w.CREATE_NEW_CONSOLE)
        self.assertTrue(flags & w.CREATE_NO_WINDOW)

    def test_detached_stays_detached(self):
        self.assertEqual(w.hidden_flags(w.DETACHED_PROCESS), w.DETACHED_PROCESS)

    def test_non_windows_is_untouched(self):
        original = subprocess.Popen.__init__
        self.assertFalse(w.install('linux'))
        self.assertIs(subprocess.Popen.__init__, original)


@unittest.skipUnless(os.name == 'nt', 'Windows-only Popen behaviour')
class WindowsPopenTests(unittest.TestCase):
    def setUp(self):
        self.original = subprocess.Popen.__init__
        self.addCleanup(setattr, subprocess.Popen, '__init__', self.original)

    def test_popen_gets_hidden_flags_and_startupinfo(self):
        seen = {}

        def spy(self_, *args, **kwargs):
            seen.update(kwargs)

        subprocess.Popen.__init__ = spy
        self.assertTrue(w.install())
        self.assertTrue(w.install())  # idempotent: no double wrap
        subprocess.Popen(['x'], creationflags=w.CREATE_NEW_CONSOLE)
        self.assertEqual(seen['creationflags'], w.CREATE_NO_WINDOW)
        self.assertTrue(seen['startupinfo'].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(seen['startupinfo'].wShowWindow, subprocess.SW_HIDE)

    def test_real_child_has_no_console_window(self):
        w.install()
        out = subprocess.run([sys.executable, '-c',
                              'import ctypes;print(ctypes.windll.kernel32.GetConsoleWindow())'],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), '0')


if __name__ == '__main__':
    unittest.main()
