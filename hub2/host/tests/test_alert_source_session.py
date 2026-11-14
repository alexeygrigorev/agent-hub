"""Real observer subprocess/Unix transport; fake manager and no business effects."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_hub_host import alert_broker as broker

ROOT = Path(__file__).resolve().parents[3]
CLIENT = ROOT / 'scripts/hub_alert_client.py'


class Manager:
    def __init__(self, callback): self.wire = self
    def start(self): pass
    def receive(self, *args): return False
    def handle(self, *args): raise AssertionError('no fabricated manager events')
    def barrier(self): pass
    def close(self): pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ss-', dir=os.environ.get('RUNNER_TEMP') or None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'b'; self.directory.mkdir(mode=0o700)
        pins = {r: broker.ram.SourcePin(1, 2, 'a' * 64) for r in broker.ram.ROLES}
        self.broker = broker.Broker(self.directory, pins, collector_factory=Manager)
        self.broker.start(); self.addCleanup(self.broker.close)
        end = time.monotonic() + 1
        while self.broker.collector is None and time.monotonic() < end: time.sleep(.001)
        self.assertIsNotNone(self.broker.collector)

    def start(self, role='backup_pull', parent=None, extra=(), output=subprocess.PIPE, inherited=None, input=subprocess.PIPE):
        args = [sys.executable, '-I', '-S', str(CLIENT), 'source-session',
                '--socket', str(self.broker.path), '--role', role]
        if parent is not None: args += ['--parent-source-claim', parent]
        args += list(extra)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('HUB_ALERT_', 'APLEXER_'))}
        env.update(inherited or {})
        process = subprocess.Popen(args, stdin=input, stdout=output,
                                   stderr=subprocess.PIPE, env=env)
        self.addCleanup(self.reap, process)
        return process

    def reap(self, process):
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream: stream.close()

    def line(self, process, timeout=2):
        raw = b''; end = time.monotonic() + timeout
        while b'\n' not in raw:
            self.assertTrue(select.select([process.stdout], [], [], max(0, end-time.monotonic()))[0])
            chunk = os.read(process.stdout.fileno(), 1)
            self.assertTrue(chunk); raw += chunk
            self.assertLessEqual(len(raw), 512)
        return json.loads(raw)

    def event(self, process, value):
        process.stdin.write(json.dumps(value).encode() + b'\n'); process.stdin.flush()
        return self.line(process)

    def test_real_one_pid_negative_ack_sequence_terminal_no_authority(self):
        process = self.start(); ready = self.line(process)
        self.assertEqual(ready['state'], 'ready_unqualified'); self.assertIs(ready['admitted'], False)
        self.assertRegex(ready['source_claim_id'], r'^[0-9a-f]{32}$')
        self.assertEqual(self.broker.unqualified_frames, [])
        for seq, value in enumerate([{'phase':'begin'},
                {'phase':'decision','decision':'alert','condition':'backup_failed','recipient_class':'dev'},
                {'phase':'terminal'}]):
            answer = self.event(process, value)
            self.assertEqual(answer, {'kind':'hub_alert_source_session_ack_v1','seq':seq,
                'local_ok':True,'broker_ok':False,'observer_ok':False,'admitted':False})
        self.assertEqual(process.wait(timeout=2), 0)
        rows = self.broker.unqualified_frames
        self.assertEqual([r['peer_process']['pid'] for r in rows], [process.pid]*3)
        self.assertEqual(len({r['peer_process']['birth_ticks'] for r in rows}), 1)
        self.assertEqual([r['frame']['seq'] for r in rows], [0,1,2])
        self.assertTrue(all(r['qualification']=='none' for r in rows))
        self.assertEqual(self.broker.ram.active, {})
        self.assertTrue(all(v['started']==v['finished']==v['occurrences']==0 for v in self.broker.ram.counts.values()))

    def test_nested_notifier_gets_distinct_claim_and_pid(self):
        first = self.start(); parent = self.line(first)['source_claim_id']
        child = self.start('backup_notifier', parent); ready = self.line(child)
        self.assertNotEqual(ready['source_claim_id'], parent); self.assertEqual(ready['parent_source_claim'], parent)
        self.event(first, {'phase':'begin'}); self.event(child, {'phase':'begin'})
        self.event(child, {'phase':'terminal'}); self.event(first, {'phase':'terminal'})
        self.assertEqual(child.wait(timeout=2), 0); self.assertEqual(first.wait(timeout=2), 0)
        rows = self.broker.unqualified_frames
        self.assertEqual({r['peer_process']['pid'] for r in rows}, {first.pid, child.pid})
        self.assertEqual(rows[1]['frame']['parent_id'], parent)

    def test_malformed_duplicate_unknown_or_identity_inputs_fail_without_frame(self):
        samples = [b'{bad}\n', b'{"phase":"begin","phase":"terminal"}\n',
            b'{"phase":"begin","seq":0}\n', b'{"phase":"begin","flags":{"test":1}}\n',
            b'{"phase":"decision","decision":"alert","condition":"backup_failed"}\n',
            b'{"phase":"begin","condition":"TOKEN=private"}\n', b'\xff\n']
        for raw in samples:
            with self.subTest(raw=raw):
                process = self.start(); self.line(process)
                process.stdin.write(raw); process.stdin.flush(); error = self.line(process)
                self.assertEqual(error, {'kind':'source_session_error_v1','reason':'invalid_event','admitted':False})
                self.assertEqual(process.wait(timeout=2), 2)
        self.assertEqual(self.broker.unqualified_frames, [])

    def test_eof_does_not_synthesize_terminal(self):
        process = self.start(); self.line(process); self.event(process, {'phase':'begin'})
        process.stdin.close(); error = self.line(process)
        self.assertEqual(error['reason'], 'eof'); self.assertEqual(process.wait(timeout=2), 2)
        self.assertEqual([r['frame']['phase'] for r in self.broker.unqualified_frames], ['begin'])

    def test_byte_and_event_limits_no_implicit_terminal(self):
        process = self.start(); self.line(process)
        process.stdin.write(b'X'*1025); process.stdin.flush()
        self.assertEqual(self.line(process)['reason'], 'input_limit'); self.assertEqual(process.wait(timeout=2), 2)
        process = self.start(); self.line(process); self.event(process, {'phase':'begin'})
        for _ in range(7): self.event(process, {'phase':'decision','decision':'observation','condition':'backup_begin'})
        self.assertEqual(self.line(process)['reason'], 'event_limit'); self.assertEqual(process.wait(timeout=2), 2)
        self.assertEqual(len(self.broker.unqualified_frames), 8)

    def test_lifetime_without_input_is_finite(self):
        process = self.start(extra=['--lifetime-seconds','1']); self.line(process)
        self.assertEqual(process.wait(timeout=2), 2); self.assertEqual(self.broker.unqualified_frames, [])

    def test_unread_output_and_full_pipe_are_bounded_no_broker_call(self):
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        try:
            while True: os.write(write_fd, b'X'*4096)
        except BlockingIOError: pass
        try:
            process = self.start(output=write_fd)
            self.assertEqual(process.wait(timeout=2), 2)
            self.assertEqual(self.broker.unqualified_frames, [])
        finally: os.close(write_fd); os.close(read_fd)

    def test_invalid_cli_never_echoes_tokens_or_paths(self):
        for extra in [['--role','quota_monitor'], ['--parent-source-claim','a'*32],
                      ['--lifetime-seconds','NaN'], ['--unknown','TOKEN=secret']]:
            process = self.start(extra=extra); error = self.line(process)
            self.assertEqual(error, {'kind':'source_session_error_v1','reason':'invalid_arguments','admitted':False})
            self.assertEqual(process.wait(timeout=2), 2); self.assertEqual(process.stderr.read(), b'')

    def test_old_quota_factory_abi_and_sticky_client_unchanged(self):
        spec = importlib.util.spec_from_file_location('session_candidate_client', CLIENT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        child = module.positive_only_context('quota_monitor', socket_path=str(self.broker.path), env={})
        self.assertFalse(child.emit('begin')); self.assertTrue(child.dirty)
        self.assertFalse(child.emit('terminal')); self.assertTrue(child.dirty)
        self.assertEqual([r['frame']['role'] for r in self.broker.unqualified_frames], ['quota_monitor']*2)
        self.assertFalse(hasattr(module, 'Controller')); self.assertFalse(hasattr(module, 'dispatch'))

    def test_total_bytes_and_rebegin_refuse_without_replay(self):
        process = self.start(); self.line(process)
        self.event(process, {'phase':'begin'})
        answer = self.event(process, {'phase':'begin'})
        self.assertEqual(answer['reason'], 'invalid_event'); self.assertEqual(process.wait(timeout=2), 2)
        self.assertEqual(len(self.broker.unqualified_frames), 1)
        process = self.start(); self.line(process)
        for index in range(5):
            phase = 'begin' if index == 0 else 'decision'
            value = {'phase':phase}
            if index: value.update(decision='observation', condition='backup_begin')
            raw = json.dumps(value).encode().ljust(900, b' ') + b'\n'
            process.stdin.write(raw); process.stdin.flush(); answer = self.line(process)
            if index < 4: self.assertIs(answer['local_ok'], True)
        self.assertEqual(answer['reason'], 'input_limit'); self.assertEqual(process.wait(timeout=2), 2)
        self.assertEqual(len(self.broker.unqualified_frames), 5)

    def test_regular_output_refused_before_any_write_or_broker_call(self):
        with tempfile.TemporaryFile() as output:
            process = self.start(output=output)
            self.assertEqual(process.wait(timeout=2), 2)
            output.seek(0); self.assertEqual(output.read(), b'')
        self.assertEqual(self.broker.unqualified_frames, [])

    def test_interrupt_exits_without_healing_or_synthetic_terminal(self):
        process = self.start(); self.line(process); self.event(process, {'phase':'begin'})
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=2), 130)
        self.assertEqual(process.stderr.read(), b'')
        self.assertEqual([r['frame']['phase'] for r in self.broker.unqualified_frames], ['begin'])

    def test_inherited_context_refused_not_adopted(self):
        process = self.start(inherited={'HUB_ALERT_INVOCATION_ID':'a'*32})
        self.assertEqual(self.line(process), {'kind':'source_session_error_v1',
            'reason':'inherited_context','admitted':False})
        self.assertEqual(process.wait(timeout=2), 2)
        self.assertEqual(self.broker.unqualified_frames, [])

    def test_oversized_packet_input_refused_before_ready_or_processing(self):
        for kind in (socket.SOCK_DGRAM, socket.SOCK_SEQPACKET):
            with self.subTest(socket_type=kind):
                # Separate pair ownership is explicit; no received packet can
                # be mistaken for a bounded prefix of a stream.
                left, right = socket.socketpair(socket.AF_UNIX, kind)
                try:
                    left.send(b'{"phase":"begin"}\n' + b'X' * 5000)
                    process = self.start(input=right)
                    self.assertEqual(self.line(process), {'kind':'source_session_error_v1',
                        'reason':'transport_unavailable','admitted':False})
                    self.assertEqual(process.wait(timeout=2), 2)
                    self.assertEqual(self.broker.unqualified_frames, [])
                finally: left.close(); right.close()

    def test_packet_output_refused_without_ready_bytes(self):
        for kind in (socket.SOCK_DGRAM, socket.SOCK_SEQPACKET):
            with self.subTest(socket_type=kind):
                left, right = socket.socketpair(socket.AF_UNIX, kind)
                try:
                    process = self.start(output=right)
                    self.assertEqual(process.wait(timeout=2), 2)
                    left.setblocking(False)
                    with self.assertRaises(BlockingIOError): left.recv(1024)
                    self.assertEqual(self.broker.unqualified_frames, [])
                finally: left.close(); right.close()

    def test_real_stream_sockets_preserve_normal_source_session(self):
        reader, input_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        writer, output_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            process = self.start(input=input_end, output=output_end)
            def line():
                raw = b''; end = time.monotonic() + 2
                while b'\n' not in raw:
                    self.assertTrue(select.select([writer], [], [], max(0,end-time.monotonic()))[0])
                    chunk = writer.recv(1); self.assertTrue(chunk); raw += chunk
                    self.assertLessEqual(len(raw),512)
                return json.loads(raw)
            self.assertEqual(line()['state'], 'ready_unqualified')
            for seq, phase in enumerate(('begin','terminal')):
                reader.sendall(json.dumps({'phase':phase}).encode()+b'\n')
                answer = line(); self.assertEqual(answer['seq'],seq)
                self.assertIs(answer['local_ok'],True); self.assertIs(answer['broker_ok'],False)
            self.assertEqual(process.wait(timeout=2),0)
            self.assertEqual([r['frame']['phase'] for r in self.broker.unqualified_frames],['begin','terminal'])
        finally:
            for endpoint in (reader,input_end,writer,output_end): endpoint.close()


if __name__ == '__main__': unittest.main()
