"""Capture --screen coverage: CLI payload, receiver mapping, executor dispatch.

No live sessions are touched. Native effects go to a stubbed
``native_sessions.capture_output`` (existing adapter contract), asserting the
exact mode flag reaches the adapter and the bounded UTF-8 return holds.
"""
import argparse
import unittest
from unittest.mock import patch

from agent_hub_host import session_control
from agent_hub_host import session_executor as se
from agent_hub_host import session_receiver as rc
from agent_hub_host.native_sessions import NativeTarget, SessionControlError


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root


def target():
    return NativeTarget(session_id='11111111-1111-4111-8111-111111111111',
                        tag='hub-launch-1', workspace='/srv/hub/demo',
                        created_at_ms=1792223000000).validated()


def custody():
    return se.CallerCustody(actor='machine:hetzner', machine_id='hetzner',
                            credential_family='host-hetzner').validated()


def offer(command_id, kind, payload, mode=None):
    base = {
        'command_id': command_id,
        'correlation': 'cmd:%s:gen:1' % command_id,
        'kind': kind,
        'generation': 1,
        'binding': {'native_id': target().session_id, 'tag': target().tag,
                    'workspace': target().workspace, 'generation': 1,
                    'native_created_at_ms': 1792223000000},
        'payload': payload,
    }
    if mode is not None:
        base['mode'] = mode
    return base


class TestCaptureScreenCli(unittest.TestCase):
    def parse(self, *args):
        return parser().parse_args(['sessions', *args])

    def test_screen_encodes_exact_payload(self):
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('capture', 'sid', '--no-wait', '--max-bytes', '1024',
                                               '--screen', '--key', 'k'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sid/capture'))
        self.assertEqual(transport.call_args.args[2],
                         {'key': 'k', 'max_bytes': 1024, 'mode': 'screen'})

    def test_default_tail_wire_unchanged(self):
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('capture', 'sid', '--no-wait', '--max-bytes', '1024',
                                               '--key', 'k'))
        self.assertEqual(transport.call_args.args[2], {'key': 'k', 'max_bytes': 1024})


class TestCaptureScreenReceiver(unittest.TestCase):
    def test_screen_mode_passes_through(self):
        cmd = rc.offer_to_command(offer('aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa',
                                        'capture', {'max_bytes': 512, 'mode': 'screen'},
                                        mode='screen'))
        self.assertEqual(cmd['mode'], 'screen')
        self.assertEqual(cmd['limit_bytes'], 512)

    def test_tail_default_for_legacy_rows(self):
        cmd = rc.offer_to_command(offer('bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb',
                                        'capture', {'max_bytes': 512}))
        self.assertEqual(cmd['mode'], 'tail')

    def test_bad_mode_refused_before_claim(self):
        with self.assertRaises(SessionControlError):
            rc.offer_to_command(offer('cccccccc-cccc-4ccc-cccc-cccccccccccc',
                                      'capture', {'max_bytes': 512, 'mode': 'fullscreen'}))


class TestCaptureScreenExecutor(unittest.TestCase):
    def test_uses_existing_adapter_screen_flag_and_bounded_return(self):
        from types import SimpleNamespace
        seen = {}

        def fake_capture(executable, tgt, limit_bytes=32 * 1024, mode='tail'):
            seen['mode'] = mode
            seen['limit'] = limit_bytes
            seen['args'] = (executable, tgt.session_id)
            text = 'S' * (limit_bytes + 100)
            raw = text.encode('utf-8')
            truncated = len(raw) > limit_bytes
            out = raw[:limit_bytes].decode('utf-8', 'replace')
            return SimpleNamespace(session_id=tgt.session_id, tag=tgt.tag, mode=mode,
                                   text=out, truncated=truncated,
                                   byte_length=len(out.encode('utf-8')))

        with patch('agent_hub_host.session_executor._capture', side_effect=fake_capture):
            out = se.capture_session_output('aplexer', target(), custody=custody(),
                                            limit_bytes=64, mode='screen')
        self.assertEqual(seen['mode'], 'screen')
        self.assertEqual(seen['limit'], 64)
        self.assertEqual(out['mode'], 'screen')
        self.assertTrue(out['truncated'])
        self.assertLessEqual(len(out['text'].encode('utf-8')), 64)

    def test_bad_mode_refused_with_no_effect(self):
        with patch('agent_hub_host.session_executor._capture') as cap:
            with self.assertRaises(SessionControlError):
                se.capture_session_output('aplexer', target(), custody=custody(),
                                          limit_bytes=64, mode='fullscreen')
        cap.assert_not_called()


if __name__ == '__main__':
    unittest.main()
