"""CLI coverage for hub sessions wake; never executes prompts."""
import argparse
import unittest
from unittest.mock import patch
from agent_hub_host import session_control


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root


class TestSessionWakeCli(unittest.TestCase):
    def parse(self, *args):
        return parser().parse_args(['sessions', *args])

    def test_wake_prompt_inline_posts_key_at_prompt(self):
        with patch.object(session_control, 'request', return_value={'wake_id': 'w1'}) as transport:
            result = session_control.execute(self.parse('wake', 'sess-1', '--at', 'now', '--prompt', 'resume now', '--key', 'wake-once'))
        self.assertEqual(result['wake_id'], 'w1')
        self.assertEqual(result['client_key'], 'wake-once')
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sess-1/wake'))
        body = transport.call_args.args[2]
        self.assertEqual(body['at'], 'now')
        self.assertEqual(body['prompt'], 'resume now')

    def test_wake_body_file_and_stdin(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile('w', delete=False) as tmp:
            tmp.write('watcher summary')
            path = tmp.name
        try:
            with patch.object(session_control, 'request', return_value={'wake_id': 'w2'}) as transport:
                session_control.execute(self.parse('wake', 'sess-1', '--at', '2026-10-27T12:05:00+02:00', '--body-file', path, '--key', 'wake-file'))
            self.assertEqual(transport.call_args.args[2]['prompt'], 'watcher summary')
        finally:
            os.unlink(path)

    def test_wake_requires_prompt_or_body_file_and_at(self):
        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(SystemExit):
                self.parse('wake', 'sess-1', '--at', 'now')
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('wake', '../x', '--at', 'now', '--prompt', 'hi'))
            transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
