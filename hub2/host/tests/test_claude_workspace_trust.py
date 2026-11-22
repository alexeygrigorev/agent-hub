"""Interactive Claude folder-trust dialog handling (DESKTOP-FP4OP26, intake 2699b8d7).

Claude Code preselects "No, exit" in its trust dialog for the user's home
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
HOME = 'C:\\Users\\User'

# Verbatim (ANSI stripped) raw capture of session adc0165d on DESKTOP-FP4OP26,
# 2026-11-22 16:13Z: no digit labels, "No, exit" first and preselected.
LIVE_DESKTOP_CAPTURE = (
    '\n' + '\u2500' * 80 + '\n'
    ' Accessing workspace:\n\n C:\\Users\\User\n\n'
    ' Quick safety check: Is this a project you created or one you trust? (Like your\n'
    ' own code, a well-known open source project, or work from your team). If not,\n'
    " take a moment to review what's in this folder first.\n\n"
    " Claude Code'll be able to read, edit, and execute files here.\n\n"
    ' Security guide\n\n'
    ' \u276f No, exit\n'
    '  Yes, I trust this folder\n\n'
    ' Enter to confirm \u00b7 Esc to cancel\n')


def trust_screen(path, selected):
    screen = LIVE_DESKTOP_CAPTURE.replace(' C:\\Users\\User\n', ' ' + path + '\n')
    if selected == 'yes':
        screen = screen.replace(' \u276f No, exit\n  Yes,', '  No, exit\n \u276f Yes,')
    elif selected is None:
        screen = screen.replace('\u276f', ' ')
    return screen


READY = '\u276f \n  ? for shortcuts\n'


class FakePane:
    """capture returns the rendered screen; send applies Down/Enter.

    lag: each key becomes visible only after one more capture (stale frame).
    dies_on_enter: Enter exits the pane; captures then fail like aplexer does.
    """

    def __init__(self, path=HOME, selected='no', *, send_code=0, stuck=False, lag=False, dies_on_enter=False):
        self.path, self.selected, self.send_code, self.stuck = path, selected, send_code, stuck
        self.lag, self.dies_on_enter = lag, dies_on_enter
        self.dismissed, self.dead, self.pending = selected is None, False, None
        self.calls = []

    def screen(self):
        return READY if self.dismissed else trust_screen(self.path, self.selected)

    def apply(self, key):
        if self.stuck:
            return
        if key == '1b5b42':
            self.selected = 'yes' if self.selected == 'no' else 'no'
        elif key == '0d':
            if self.dies_on_enter or self.selected == 'no':
                self.dead = True
            else:
                self.dismissed = True

    def __call__(self, argv, **options):
        self.calls.append(argv[1:])
        if argv[1] == 'capture':
            if self.dead:
                return subprocess.CompletedProcess(argv, 1, '', 'a: no matching session')
            text = self.screen()
            if self.pending is not None:
                self.apply(self.pending)
                self.pending = None
            return subprocess.CompletedProcess(argv, 0, text, '')
        if argv[1] == 'send':
            if self.lag:
                self.pending = argv[-1]
            else:
                self.apply(argv[-1])
            return subprocess.CompletedProcess(argv, self.send_code, '', '')
        raise AssertionError(argv)

    def sends(self):
        return [call[-1] for call in self.calls if call[0] == 'send']


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ClaudeWorkspaceTrustDetectorTests(unittest.TestCase):
    def test_live_desktop_capture_is_the_home_dialog_on_no(self):
        self.assertEqual(nl.claude_workspace_trust_prompt(LIVE_DESKTOP_CAPTURE, HOME), 'no')
        self.assertEqual(nl.claude_workspace_trust_prompt(trust_screen(HOME, 'yes'), HOME), 'yes')
        self.assertFalse(nl.claude_ready_screen(LIVE_DESKTOP_CAPTURE))
        self.assertTrue(nl.claude_ready_screen(READY))

    def test_path_must_be_equal_not_a_prefix_or_alias(self):
        for shown in ('C:\\Users\\User2', 'C:\\Users', '~', 'C:/Users/User', 'D:\\Users\\User'):
            with self.subTest(shown=shown):
                self.assertEqual(nl.claude_workspace_trust_prompt(trust_screen(shown, 'no'), HOME), 'unverified')

    def test_markers_without_verifiable_cursor_or_options_are_unverified(self):
        self.assertEqual(nl.claude_workspace_trust_prompt(trust_screen(HOME, None), HOME), 'unverified')
        self.assertEqual(nl.claude_workspace_trust_prompt(LIVE_DESKTOP_CAPTURE.replace('No, exit', 'Cancel'), HOME),
                         'unverified')
        self.assertEqual(nl.claude_workspace_trust_prompt(LIVE_DESKTOP_CAPTURE, ''), 'unverified')
        both = LIVE_DESKTOP_CAPTURE.replace('  Yes, I trust', ' \u276f Yes, I trust')
        self.assertEqual(nl.claude_workspace_trust_prompt(both, HOME), 'unverified')

    def test_ordinary_screens_are_not_dialogs(self):
        for text in (READY, '', None, 'PS C:\\Users\\User> '):
            with self.subTest(text=text):
                self.assertIsNone(nl.claude_workspace_trust_prompt(text, HOME))

    def test_wrapped_long_path_and_ansi_still_match(self):
        path = '/home/someone/work/' + 'x' * 90
        wrapped = trust_screen(path[:60] + '\n ' + path[60:], 'no').replace('Accessing', '\x1b[1mAccessing\x1b[0m')
        self.assertEqual(nl.claude_workspace_trust_prompt(wrapped, path), 'no')


class ClaudeWorkspaceTrustLauncherTests(unittest.TestCase):
    def accept(self, pane, workspace=HOME, **kwargs):
        clock = Clock()
        adapter = nl.NativeLauncher({'routes': {'claude': nl.LOCAL_ROUTES['claude']}},
                                    aplexer='synthetic-aplexer', runner=pane)
        return adapter.accept_claude_workspace_trust(NATIVE_ID, workspace, sleep=clock.sleep, clock=clock, **kwargs)

    def test_default_no_moves_once_to_yes_then_confirms_into_a_ready_pane(self):
        pane = FakePane()
        with self.assertLogs('agent_hub_host.native_launch', level='WARNING'):
            self.assertTrue(self.accept(pane))
        self.assertEqual(pane.sends(), ['1b5b42', '0d'])
        self.assertTrue(pane.dismissed and not pane.dead)
        self.assertTrue(all(call[1] == NATIVE_ID for call in pane.calls))

    def test_lagging_frames_never_send_a_second_down(self):
        pane = FakePane(lag=True)
        with self.assertLogs('agent_hub_host.native_launch', level='WARNING'):
            self.assertTrue(self.accept(pane))
        self.assertEqual(pane.sends(), ['1b5b42', '0d'])
        self.assertTrue(pane.dismissed and not pane.dead)

    def test_preselected_yes_is_confirmed_without_moving(self):
        pane = FakePane(selected='yes')
        with self.assertLogs('agent_hub_host.native_launch', level='WARNING'):
            self.assertTrue(self.accept(pane))
        self.assertEqual(pane.sends(), ['0d'])

    def test_trusted_or_ready_pane_is_left_alone(self):
        pane = FakePane(selected=None)
        self.assertFalse(self.accept(pane))
        self.assertEqual(pane.sends(), [])

    def test_failures_refuse_loudly_instead_of_reporting_success(self):
        cases = {
            'send fails': FakePane(send_code=1),
            'dialog ignores keys': FakePane(stuck=True),
            'pane dies on enter': FakePane(dies_on_enter=True),
            'foreign path': FakePane(path='C:\\Users\\User2'),
            'path shown as ~': FakePane(path='~'),
        }
        for label, pane in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(RuntimeError, '^claude_workspace_trust_unverified$'):
                    self.accept(pane)
                self.assertNotIn('0d', pane.sends()[:-1] if label == 'pane dies on enter' else [])
        self.assertEqual(cases['foreign path'].sends(), [])

    def test_capture_or_send_timeout_is_typed(self):
        for verb in ('capture', 'send'):
            with self.subTest(verb=verb):
                pane = FakePane()

                def runner(argv, _pane=pane, **options):
                    if argv[1] == verb:
                        raise subprocess.TimeoutExpired(argv, 5)
                    return _pane(argv, **options)
                with self.assertRaisesRegex(RuntimeError, '^claude_workspace_trust_unverified$'):
                    self.accept(runner)

    def test_nothing_recognisable_proceeds_after_the_bounded_wait_with_a_log(self):
        def blank(argv, **options):
            return subprocess.CompletedProcess(argv, 0, '', '')
        clock = Clock()
        adapter = nl.NativeLauncher({'routes': {'claude': nl.LOCAL_ROUTES['claude']}},
                                    aplexer='synthetic-aplexer', runner=blank)
        with self.assertLogs('agent_hub_host.native_launch', level='WARNING'):
            self.assertFalse(adapter.accept_claude_workspace_trust(NATIVE_ID, HOME, wait_seconds=3,
                                                                   sleep=clock.sleep, clock=clock))
        self.assertGreaterEqual(clock.now, 3)


class ClaudeWorkspaceTrustLaunchTests(unittest.TestCase):
    def launch(self, *, with_goal=False):
        with tempfile.TemporaryDirectory() as workspace:
            pane = FakePane(path=workspace)
            order = []

            def runner(argv, **options):
                if argv[1] in ('capture', 'send'):
                    order.append(argv[1])
                    return pane(argv, **options)
                order.append('start')
                child = argv[argv.index('--') + 1:]
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'id': NATIVE_ID, 'tag': 'task-claude-trust-test', 'workspace': workspace,
                    'engine': 'claude', 'command': child}), '')

            adapter = nl.NativeLauncher({'private_provider_homes': True, 'routes': {'claude': nl.LOCAL_ROUTES['claude']}},
                                        aplexer='synthetic-aplexer', runner=runner)
            specification = {'workspace': workspace, 'engine': 'claude', 'model': 'sonnet', 'prompt': 'hostname',
                             'interactive': True, 'with_goal': with_goal, 'timeout_seconds': 90,
                             'operation_id': 'claude-trust-op', 'epoch': 1}

            def register_goal(*args, **kwargs):
                order.append('goal')
                return {'registered': True}
            with mock.patch.object(adapter, 'record_evidence'), \
                    mock.patch.object(adapter, 'register_goal', side_effect=register_goal), \
                    self.assertLogs('agent_hub_host.native_launch', level='WARNING'):
                receipt = adapter.launch('claude-trust-test', specification)
            return receipt, order, pane

    def test_interactive_claude_launch_answers_trust_before_returning_receipt(self):
        receipt, order, pane = self.launch()
        self.assertEqual(receipt['session_id'], NATIVE_ID)
        self.assertTrue(receipt['interactive_verified'])
        self.assertEqual(order[0], 'start')
        self.assertEqual(pane.sends(), ['1b5b42', '0d'])
        self.assertTrue(pane.dismissed)

    def test_goal_is_registered_only_after_trust_is_answered(self):
        receipt, order, pane = self.launch(with_goal=True)
        self.assertIn('goal', order)
        self.assertGreater(order.index('goal'), max(i for i, item in enumerate(order) if item == 'send'))
        self.assertEqual(receipt['goal_registration'], {'registered': True})


if __name__ == '__main__':
    unittest.main()
