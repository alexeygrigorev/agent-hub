"""Interactive Claude folder-trust dialog handling (DESKTOP-FP4OP26, intake 2699b8d7).

Claude Code defaults its trust dialog to "No, exit" for the user's home
directory and never persists home trust. The first Hub-delivered Enter then
ended the interactive `home` session with exit code 1.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host import native_launch as nl

NATIVE_ID = 'd6cf9872-8597-48a5-be64-990cdef4db31'


def trust_screen(path, selected):
    no = ('\u276f ' if selected == 'no' else '  ') + '1. No, exit'
    yes = ('\u276f ' if selected == 'yes' else '  ') + '2. Yes, I trust this folder'
    return ('\u2500' * 80 + '\n Accessing workspace:\n\n ' + path + '\n\n'
            ' Quick safety check: Is this a project you created or one you trust?\n\n'
            " Claude Code'll be able to read, edit, and execute files here.\n\n"
            ' ' + no + '\n ' + yes + '\n\n Enter to confirm \u00b7 Esc to cancel\n')


READY = '\u276f \n  ? for shortcuts\n'


class FakePane:
    """capture returns the current screen; send applies arrow/Enter keys."""

    def __init__(self, path, selected='no', *, send_code=0, stuck=False):
        self.path, self.selected, self.send_code, self.stuck = path, selected, send_code, stuck
        self.dismissed = selected is None
        self.calls = []

    def screen(self):
        return READY if self.dismissed else trust_screen(self.path, self.selected)

    def __call__(self, argv, **options):
        self.calls.append(argv[1:])
        if argv[1] == 'capture':
            return subprocess.CompletedProcess(argv, 0, self.screen(), '')
        if argv[1] == 'send':
            key = argv[-1]
            if not self.stuck:
                if key == '1b5b42':
                    self.selected = 'yes'
                elif key == '0d' and self.selected == 'yes':
                    self.dismissed = True
            return subprocess.CompletedProcess(argv, self.send_code, '', '')
        raise AssertionError(argv)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ClaudeWorkspaceTrustDetectorTests(unittest.TestCase):
    def test_home_dialog_defaults_to_no_and_names_the_selected_option(self):
        home = 'C:\\Users\\User'
        self.assertEqual(nl.claude_workspace_trust_prompt(trust_screen(home, 'no'), home), 'no')
        self.assertEqual(nl.claude_workspace_trust_prompt(trust_screen(home, 'yes'), home), 'yes')

    def test_other_folder_or_ordinary_screen_never_matches(self):
        home = 'C:\\Users\\User'
        self.assertIsNone(nl.claude_workspace_trust_prompt(trust_screen('C:\\Users\\Other', 'no'), home))
        self.assertIsNone(nl.claude_workspace_trust_prompt(READY, home))
        self.assertIsNone(nl.claude_workspace_trust_prompt('', home))
        self.assertIsNone(nl.claude_workspace_trust_prompt(trust_screen(home, 'no'), ''))
        self.assertIsNone(nl.claude_workspace_trust_prompt(None, home))

    def test_wrapped_long_path_and_ansi_still_match(self):
        path = '/home/someone/work/' + 'x' * 90
        wrapped = trust_screen(path[:60] + '\n ' + path[60:], 'no').replace('Accessing', '\x1b[1mAccessing\x1b[0m')
        self.assertEqual(nl.claude_workspace_trust_prompt(wrapped, path), 'no')


class ClaudeWorkspaceTrustLauncherTests(unittest.TestCase):
    def adapter(self, pane):
        return nl.NativeLauncher({'routes': {'claude': nl.LOCAL_ROUTES['claude']}},
                                 aplexer='synthetic-aplexer', runner=pane)

    def test_default_no_moves_to_yes_then_confirms(self):
        home = 'C:\\Users\\User'
        pane, clock = FakePane(home, 'no'), Clock()
        with self.assertLogs('agent_hub_host.native_launch', level='WARNING'):
            self.assertTrue(self.adapter(pane).accept_claude_workspace_trust(
                NATIVE_ID, home, sleep=clock.sleep, clock=clock))
        sends = [call[-1] for call in pane.calls if call[0] == 'send']
        self.assertEqual(sends, ['1b5b42', '0d'])
        self.assertTrue(pane.dismissed)
        self.assertTrue(all(call[1] == NATIVE_ID for call in pane.calls))

    def test_trusted_or_ready_pane_is_left_alone(self):
        pane, clock = FakePane('/w', None), Clock()
        self.assertFalse(self.adapter(pane).accept_claude_workspace_trust(NATIVE_ID, '/w', sleep=clock.sleep, clock=clock))
        self.assertEqual([call[0] for call in pane.calls], ['capture'])

    def test_unanswerable_dialog_refuses_loudly(self):
        for pane in (FakePane('/w', 'no', send_code=1), FakePane('/w', 'no', stuck=True)):
            with self.subTest(send_code=pane.send_code, stuck=pane.stuck):
                clock = Clock()
                with self.assertRaisesRegex(RuntimeError, '^claude_workspace_trust_unverified$'):
                    self.adapter(pane).accept_claude_workspace_trust(NATIVE_ID, '/w', sleep=clock.sleep, clock=clock)

    def test_slow_start_waits_only_until_the_bounded_deadline(self):
        clock = Clock()

        def blank(argv, **options):
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.assertFalse(self.adapter(blank).accept_claude_workspace_trust(
            NATIVE_ID, '/w', wait_seconds=3, sleep=clock.sleep, clock=clock))
        self.assertGreaterEqual(clock.now, 3)


class ClaudeWorkspaceTrustLaunchTests(unittest.TestCase):
    def test_interactive_claude_launch_answers_trust_before_returning_receipt(self):
        with tempfile.TemporaryDirectory() as workspace:
            pane = FakePane(workspace, 'no')
            order = []

            def runner(argv, **options):
                if argv[1] in ('capture', 'send'):
                    order.append(argv[1])
                    return pane(argv, **options)
                order.append('start')
                child = argv[argv.index('--') + 1:]
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'id': NATIVE_ID, 'tag': 'task-claude-trust-test', 'workspace': workspace, 'command': child}), '')

            adapter = nl.NativeLauncher({'private_provider_homes': True, 'routes': {'claude': nl.LOCAL_ROUTES['claude']}},
                                        aplexer='synthetic-aplexer', runner=runner)
            specification = {'workspace': workspace, 'engine': 'claude', 'model': 'sonnet', 'prompt': 'hostname',
                             'interactive': True, 'timeout_seconds': 90, 'operation_id': 'claude-trust-op', 'epoch': 1}
            with mock.patch.object(adapter, 'record_evidence'), \
                    self.assertLogs('agent_hub_host.native_launch', level='WARNING'):
                receipt = adapter.launch('claude-trust-test', specification)
            self.assertEqual(receipt['session_id'], NATIVE_ID)
            self.assertTrue(receipt['interactive_verified'])
            self.assertEqual(order[0], 'start')
            self.assertIn('send', order)
            self.assertTrue(pane.dismissed)


if __name__ == '__main__':
    unittest.main()
