"""Offline tests for the Hydrogen styled-screen native proof in scripts/hub_acceptance_10.py.

Pure recognizer/acquirer/adapter cases use wholly synthetic SVG fixtures (host/tests/fixtures/acceptance/hydrogen).
Runner.run_bounded and the pinned helper run real local child processes; end-to-end cases drive the driver against
the fake `hub`/`a`/`git` binaries. No Hub, provider, native session or real key send is ever reached."""
import dataclasses
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIX = HERE / 'fixtures' / 'acceptance'
HFIX = FIX / 'hydrogen'
_spec = importlib.util.spec_from_file_location('acc10_hydrogen_base', HERE / 'test_cli_hub_acceptance_10.py')
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
acc = base.acc
frame = acc.hydrogen_frame
acquire = acc.hydrogen_acquire
native = acc.hydrogen_native
helper = acc.hydrogen_helper

HYD = acquire.MODEL
RUN = '11111111-1111-4111-8111-111111111111'
WORK = {
    'schema': 'phase7_native_work_v2', 'run_id': RUN, 'sample': 1,
    'parent_intake_id': '22222222-2222-4222-8222-222222222222',
    'child_intake_id': '33333333-3333-4333-8333-333333333333',
    'session_id': '44444444-4444-4444-8444-444444444444',
    'native_id': '55555555-5555-4555-8555-555555555555',
    'project': 'alexeygrigorev/agent-hub', 'machine_id': 'hetzner-aplexer',
    'workspace': '/home/synthetic/work/owned-probe', 'head': 'a' * 40,
    'tracked_status_sha256': 'b' * 64, 'tracked_status_entries': 0, 'calculation': 5050,
}
CHALLENGE = b'x' * 32
SCRIPT = b'# synthetic pinned helper bytes; never executed\n'
REL = frame.h7_rel_path(RUN, 1)
CMD = 'python3 ' + REL
DIGEST = hashlib.sha256(b'HUB_PHASE7_NATIVE_WORK_V2\0' + CHALLENGE + b'\0' + frame.canonical(WORK)).hexdigest()
ARTIFACT = frame.canonical(dict(WORK, proof_digest=DIGEST)) + b'\n'
EXPANDED = (HFIX / 'expanded.svg').read_bytes()
COLLAPSED = (HFIX / 'collapsed.svg').read_bytes()


THOUGHT = '<text x="14" y="156" fill="#5c6370" textLength="144">▾ Thought for 2s</text>'


def header_row(args, marker='●'):
    return ('<text x="14" y="156" fill="#98c379" textLength="9">%s</text>'
            '<text x="23" y="156" fill="#d4d4d4" textLength="9"> </text>'
            '<text x="32" y="156" fill="#e5c07b" textLength="36" font-weight="bold">Bash</text>'
            '<text x="68" y="156" fill="#abb2bf" textLength="%d">%s</text>') % (marker, 9 * len(args), args)


def recognize(svg, **change):
    args = dict(work=WORK, script_relpath=REL, script_sha256=hashlib.sha256(SCRIPT).hexdigest(),
                pinned_script=SCRIPT, challenge=CHALLENGE, canonical_artifact=ARTIFACT,
                expected_command=CMD, home='/home/synthetic')
    args.update(change)
    return frame.recognize(svg, **args)


class FrameRecognizerTests(unittest.TestCase):
    def test_synthetic_expanded_frame_matches_and_binds_artifact(self):
        result = recognize(EXPANDED)
        self.assertEqual(result['status'], 'CANDIDATE_EXPANDED_FRAME_MATCH')
        self.assertEqual(result['proof_digest'], DIGEST)
        self.assertEqual(REL, '.local/h7_%s_01.py' % hashlib.sha256(frame.canonical([RUN, 1])).hexdigest()[:12])

    def test_unrelated_complete_native_header_does_not_block_exact_target(self):
        svg = EXPANDED.decode().replace(THOUGHT, header_row('(whoami)')).encode()
        self.assertEqual(recognize(svg)['proof_digest'], DIGEST)
        svg = EXPANDED.decode().replace(THOUGHT, header_row('(whoami)', marker='○')).encode()
        self.assertEqual(recognize(svg)['proof_digest'], DIGEST)

    def test_negative_frames_and_bindings_refuse(self):
        text = EXPANDED.decode()
        mutations = {
            'collapsed': COLLAPSED,
            'failed_exit': text.replace('code 0.', 'code 1.').encode(),
            'running_not_completed': text.replace('Completed At: 2026-11-13T16:57:10+01:00', 'Running...').encode(),
            'reversed_times': text.replace('16:57:10+01:00', '16:57:08+01:00').encode(),
            'offset_hour_24': text.replace('16:57:10+01:00', '16:57:10+24:00').encode(),
            'truncated_digest': text.replace(DIGEST, DIGEST[:40]).encode(),
            'wrong_style': text.replace('fill="#e5c07b" textLength="36" font-weight="bold">Bash',
                                        'fill="#d4d4d4" textLength="36" font-weight="bold">Bash').encode(),
            'footer_repeats_command': text.replace('  Format observation complete; awaiting Root STOP.',
                                                   '  I ran ' + CMD + ' as asked.').encode(),
            'footer_disclaimer': text.replace('  Format observation complete; awaiting Root STOP.',
                                              '  This is a quoted example only.').encode(),
            'second_native_header': text.replace(
                '<text x="14" y="156" fill="#5c6370" textLength="144">▾ Thought for 2s</text>',
                '<text x="14" y="156" fill="#98c379" textLength="9">●</text>').encode(),
            'hidden_nested_group': text.replace('</g></svg>', '<g><text x="14" y="12" fill="#d4d4d4" textLength="9">x</text></g></g></svg>').encode(),
            'doctype': b'<!DOCTYPE svg>' + EXPANDED,
            'other_sample_probe_header': text.replace(THOUGHT, header_row('(python3 .local/h7_000000000000_02.py)')).encode(),
            'truncated_target_header': text.replace(THOUGHT, header_row('(python3 .local/h7_c265b4…)')).encode(),
            'unterminated_header': text.replace(THOUGHT, header_row('(whoami')).encode(),
        }
        for name, svg in mutations.items():
            with self.subTest(name=name), self.assertRaises(frame.Refused):
                recognize(svg)
        for name, change in {
            'title_home': {'home': '/home/other'},
            'artifact': {'canonical_artifact': ARTIFACT.replace(b'5050', b'5051')},
            'challenge': {'challenge': b'y' * 32},
            'helper_hash': {'script_sha256': '0' * 64},
            'path_derivation': {'script_relpath': '.local/h7_000000000000_01.py',
                                'expected_command': 'python3 .local/h7_000000000000_01.py'},
            'float_sample': {'work': dict(WORK, sample=1.0)},
        }.items():
            with self.subTest(binding=name), self.assertRaises((frame.Refused, ValueError)):
                recognize(EXPANDED, **change)


    def test_completed_copy_preparation_and_unrelated_python_are_context_not_probe(self):
        commands = [
            'cp /r/helper.py ' + REL,
            'python3 -c"from shutil import*;copy(\'/h\',\'' + REL + '\')"',
            'python3 -c"import shutil;shutil.copy(\'h\',\'' + REL + '\')"',
            'python3 /r/unrelated.py',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertLessEqual(68 + 9 * (len(command) + 2), 748)
                svg = EXPANDED.decode().replace(THOUGHT, header_row('(' + command + ')')).encode()
                self.assertEqual(recognize(svg)['proof_digest'], DIGEST)

    def test_probe_invocation_or_unknown_preparation_is_never_ignored(self):
        commands = [
            CMD,
            CMD.replace('h7_', 'h\\7_'),
            'python3 -c\"open(\'x.py\')\"',
            'python3 .local/h7_000000000000_02.py',
            'python3 -c"exec(\'' + REL + '\')"',
            'cp /h .local/h7_000000000000_02.py',
            'cp /h ' + REL + '…',
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(frame.Refused):
                recognize(EXPANDED.decode().replace(THOUGHT, header_row('(' + command + ')')).encode())
        command = 'cp /h ' + REL
        for row in [header_row('(' + command + ')', marker='○'),
                    header_row('(' + command + ')').replace('#98c379', '#e06c75')]:
            with self.subTest(row=row), self.assertRaises(frame.Refused):
                recognize(EXPANDED.decode().replace(THOUGHT, row).encode())
        for command in [
            'cp "$(python3 ' + REL + ')" ' + REL,
            'cp /h ' + REL + '; ' + CMD,
            'python3 -c"from shutil import*;copy(\'/h\',\'' + REL + '\');exec(\'x\')"',
            'python3 -c"from os import*;copy(\'/h\',\'' + REL + '\')"',
            'python3 -c"from shutil import*;copy(source,\'' + REL + '\')"',
        ]:
            with self.subTest(preparation=command):
                self.assertFalse(frame._probe_preparation(command, REL))


    def test_inline_python_and_shell_patterns_are_not_literal_script_context(self):
        escaped = REL.replace('h7_', 'h7\\x5f')
        other = escaped.replace('_01.py', '_02.py')
        commands = [
            'python3 -c"exec(open(\'' + escaped + '\').read())#x.py"',
            'python3 -c"exec(open(\'' + other + '\').read())#x.py"',
            'python3 .local/*.py',
            'python3 .local/?.py',
            'python3 .local/[a-z]*.py',
            'python3 .local/{h,x}7_000000000000_01.py',
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(frame.Refused):
                recognize(EXPANDED.decode().replace(THOUGHT, header_row('(' + command + ')')).encode())


    def test_completed_literal_metadata_mode_and_path_echo_are_context_not_execution(self):
        commands = [
            'chmod 600 ' + REL, 'chmod 0600 ' + REL,
            'sha256sum ' + REL, 'ls -l ' + REL, 'stat ' + REL,
            'stat -c "%a %U %n" ' + REL, 'stat --format "%a:%u:%g" ' + REL,
            'echo ' + REL,
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertLessEqual(68 + 9 * (len(command) + 2), 748)
                svg = EXPANDED.decode().replace(THOUGHT, header_row('(' + command + ')')).encode()
                self.assertEqual(recognize(svg)['proof_digest'], DIGEST)
        # Context is never positive proof: removing the canonical result still
        # refuses even when every metadata header is completed and valid.
        command = 'sha256sum ' + REL
        svg = EXPANDED.decode().replace(THOUGHT, header_row('(' + command + ')'))
        with self.assertRaises(frame.Refused):
            recognize(svg.replace('Command exited with code 0.', 'Command exited with code 1.').encode())

    def test_metadata_expansion_wrappers_quoted_execution_and_noncompleted_refuse(self):
        commands = [
            'echo "' + CMD + '"',
            'echo "$(cat ' + REL + ')"',
            'chmod 600 ' + REL + ';:', 'ls -l ' + REL + '|cat',
            'stat ' + REL + ' >/x', 'env chmod 600 ' + REL,
            'sh -c "stat ' + REL + '"',
            'sha256sum .local/*.py', 'ls -l .local/{a,b}.py',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertLessEqual(68 + 9 * (len(command) + 2), 748)
                with self.assertRaises(frame.Refused):
                    recognize(EXPANDED.decode().replace(THOUGHT, header_row('(' + command + ')')).encode())
        for command in ['chmod 600 ' + REL, 'sha256sum ' + REL, 'echo ' + REL]:
            for marker, color in [('○', '#98c379'), ('●', '#e06c75')]:
                row = header_row('(' + command + ')', marker=marker).replace('#98c379', color)
                with self.subTest(command=command, marker=marker, color=color), self.assertRaises(frame.Refused):
                    recognize(EXPANDED.decode().replace(THOUGHT, row).encode())
        for command in ['python3 -c"exec(open(\'x\').read())#x.py"',
                        'chmod 600 $(cat x)', 'stat -c "%a;id" ' + REL,
                        'ls -l ' + REL + ' extra', 'echo AC10_WORK_V2', 'printf ' + REL,
                        'chmod 600\n' + REL, 'stat\t' + REL]:
            with self.subTest(helper=command):
                self.assertFalse(frame._probe_file_context(command, REL))


class CollapsedEligibilityTests(unittest.TestCase):
    def test_observed_idle_aggregate_for_this_sample_only(self):
        self.assertTrue(acquire.observed_collapsed(COLLAPSED, CMD))
        text = COLLAPSED.decode()
        for count in range(2, 10):
            svg = text.replace(' 3 commands ', ' %d commands ' % count).replace('(+2 · ', '(+%d · ' % (count - 1))
            self.assertTrue(acquire.observed_collapsed(svg.encode(), CMD), count)
        for name, svg in {
            'single_command': text.replace(' 3 commands ', ' 1 commands ').replace('(+2 · ', '(+0 · '),
            'count_mismatch': text.replace('(+2 · ', '(+4 · '),
            'other_sample_prefix': text.replace(CMD[:20], 'python3 .local/h7_ff'),
            'draft_composer': text.replace('fill="#5c6370" textLength="603">Auto mode',
                                           'fill="#d4d4d4" textLength="603">Auto mode'),
            'no_caption': text.replace('Format observation complete; awaiting Root STOP.',
                                       'Working on it, one moment please..............'),
            'expand_hint_missing': text.replace('(ctrl+o to expand)', '(ctrl+o to xxxxxx)'),
            'expanded_view': EXPANDED.decode(),
        }.items():
            with self.subTest(name=name):
                self.assertFalse(acquire.observed_collapsed(svg.encode(), CMD))
        # Eligibility binds only the clipped 20-character prefix (final proof needs the FULL command).
        self.assertFalse(acquire.observed_collapsed(COLLAPSED, 'python3 .local/h7_ff65b449cc22_01.py'))


IDENTITY = acquire.Identity(
    hub_session=WORK['session_id'], intake=WORK['child_intake_id'], machine='hetzner-aplexer', epoch=7,
    operation='66666666-6666-4666-8666-666666666666:0', request_digest='c' * 64, native=WORK['native_id'],
    birth_ms=1794585409000, tag='task-hub2-' + WORK['session_id'], workspace=WORK['workspace'],
    provider='antigravity', engine='antigravity', model=HYD, version=acquire.PROVIDER_VERSION,
    executable_sha256=acquire.PROVIDER_EXECUTABLE_SHA256)


class ScriptedRunner:
    """Fake bounded runner returning the exact source response shapes; counts sends."""

    def __init__(self):
        self.calls = []

    def run_bounded(self, tool, args, *, deadline, timeout, max_stdout, max_stderr):
        self.calls.append(list(args))
        i = IDENTITY
        if args[0] == 'status':
            return 0, json.dumps({'id': i.native, 'tag': i.tag, 'workspace': i.workspace, 'created_at_ms': i.birth_ms,
                                  'engine': 'antigravity', 'phase': 'running', 'state': 'running',
                                  'worker_alive': True, 'worker_reachable': True}).encode(), b''
        if args[0] == 'send':
            return 0, json.dumps({'id': i.native, 'bytes': 1, 'status': 'pty_written', 'enter_written': False,
                                  'consumed': None}).encode(), b''
        return 0, COLLAPSED, b''


class AdapterN1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='h-adapter-', dir=base.TMP))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fence = self.tmp / 'fence'
        self.fence.mkdir(mode=0o700)
        self.runner = ScriptedRunner()
        self.intent = acquire.Intent(IDENTITY, CMD, True, time.monotonic() + 60)
        hub_row = {'id': IDENTITY.hub_session, 'intake_id': IDENTITY.intake, 'project_id': 'alexeygrigorev/agent-hub',
                   'machine_id': 'hetzner-aplexer', 'epoch': 7, 'operation_id': IDENTITY.operation,
                   'native_id': IDENTITY.native, 'engine': 'antigravity', 'model': HYD,
                   'workspace': IDENTITY.workspace, 'state': 'running'}
        self.adapter = native.Adapter(
            intent=self.intent, runner=self.runner, read_hub=lambda sid, **kw: dict(hub_row),
            artifact_reader=lambda intent, **kw: 'd' * 64, verify_public_pins=lambda identity, **kw: True,
            known_human=lambda **kw: False, clock=time.monotonic, fence_directory=str(self.fence))

    def sends(self):
        return sum(1 for call in self.runner.calls if call[0] == 'send')

    def test_equal_but_malformed_callback_intent_never_remints_a_fence_or_second_key(self):
        deadline = time.monotonic() + 30
        before = acquire.Snapshot(IDENTITY, COLLAPSED, True)
        self.assertTrue(self.adapter.record_attempt(self.intent, before, b'\x0f', deadline=deadline, timeout=10))
        self.assertTrue(self.adapter.send(IDENTITY, b'\x0f', deadline=deadline, timeout=10))
        for bad in (dataclasses.replace(IDENTITY, epoch=7.0), dataclasses.replace(IDENTITY, birth_ms=True)):
            malformed = acquire.Intent(bad, CMD, True, self.intent.deadline)
            if type(bad.epoch) is float:
                self.assertEqual(malformed, self.intent)  # dataclass equality alone cannot tell 7.0 from 7
            with self.subTest(identity=bad), self.assertRaises(native.Refused):
                self.adapter.record_attempt(malformed, before, b'\x0f', deadline=deadline, timeout=10)
            with self.assertRaises(native.Refused):
                self.adapter.validate_artifact(malformed, deadline=deadline, timeout=10)
            with self.assertRaises(native.Refused):
                self.adapter.send(bad, b'\x0f', deadline=deadline, timeout=10)
        # Same validated attempt again (new adapter instance, same durable fence): refused, no new key.
        self.assertFalse(self.adapter.record_attempt(self.intent, before, b'\x0f', deadline=deadline, timeout=10))
        self.assertEqual(len(list(self.fence.iterdir())), 1)
        self.assertEqual(self.sends(), 1)
        receipt = json.loads(next(self.fence.iterdir()).read_text())
        self.assertEqual(receipt['identity']['epoch'], 7)
        self.assertEqual(oct(next(self.fence.iterdir()).stat().st_mode & 0o777), '0o600')

    def test_acquire_sends_exactly_one_byte_then_recognizes_the_post_key_frame(self):
        frames = iter([COLLAPSED, EXPANDED])
        original = self.runner.run_bounded

        def run_bounded(tool, args, **kw):
            if args[0] == 'capture':
                self.runner.calls.append(list(args))
                return 0, next(frames, EXPANDED), b''
            return original(tool, args, **kw)
        self.runner.run_bounded = run_bounded
        state = acquire.acquire(self.intent, read_identity=self.adapter.read_identity, capture=self.adapter.capture,
                                validate_artifact=self.adapter.validate_artifact,
                                recognize_expanded=lambda snap, intent, **kw: snap.svg == EXPANDED,
                                record_attempt=self.adapter.record_attempt, send=self.adapter.send,
                                clock=time.monotonic)
        self.assertEqual(state['status'], 'CANDIDATE_FRAME_RECOGNIZED', state)
        self.assertEqual(self.sends(), 1)
        self.assertIn(['send', IDENTITY.native, '--hex', '0f', '--json'], self.runner.calls)
        again = acquire.acquire(self.intent, read_identity=self.adapter.read_identity, capture=lambda **kw: acquire.Snapshot(IDENTITY, COLLAPSED, True),
                                validate_artifact=self.adapter.validate_artifact,
                                recognize_expanded=lambda snap, intent, **kw: False,
                                record_attempt=self.adapter.record_attempt, send=self.adapter.send,
                                clock=time.monotonic)
        self.assertEqual((again['status'], again['reason']), ('UNKNOWN', 'attempt_fence_not_confirmed'))
        self.assertEqual(self.sends(), 1)


class BoundedCollectorTests(unittest.TestCase):
    def run_py(self, code, timeout=5.0, out_cap=1000, err_cap=1000):
        r = acc.Runner(a=sys.executable)
        start = time.monotonic()
        result = r.run_bounded('a', ['-c', code], deadline=start + timeout, timeout=timeout,
                               max_stdout=out_cap, max_stderr=err_cap)
        return result, time.monotonic() - start

    def test_stream_caps_timeout_and_inherited_pipe_are_bounded(self):
        (rc, out, err), _ = self.run_py('import sys; sys.stdout.write("ok"); sys.stderr.write("e")')
        self.assertEqual((rc, out, err), (0, b'ok', b'e'))
        (rc, out, _), took = self.run_py('import sys,time\nwhile True: sys.stdout.write("x"*65536); sys.stdout.flush()')
        self.assertEqual((rc, out), (125, b''))
        self.assertLess(took, 3)
        (rc, _, _), _ = self.run_py('import sys,time\nsys.stderr.write("y"*5000); sys.stderr.flush(); time.sleep(10)')
        self.assertEqual(rc, 125)
        (rc, _, _), took = self.run_py('import time; time.sleep(30)', timeout=0.5)
        self.assertEqual(rc, 124)
        self.assertLess(took, 2.5)
        code = ('import subprocess,sys\nsubprocess.Popen([sys.executable,"-c","import time; time.sleep(5)"],'
                'stdout=sys.stdout, stderr=sys.stderr)\nprint("parent done", flush=True)')
        (rc, _, _), took = self.run_py(code, timeout=4)
        self.assertEqual(rc, 126)
        self.assertLess(took, 3)
        self.assertEqual(acc.Runner().run_bounded('git', ['status'], deadline=1, timeout=1, max_stdout=1, max_stderr=1)[0], 127)


    def test_absolute_deadline_caps_timeout_and_expired_deadline_never_spawns(self):
        marker = Path(tempfile.mkdtemp(dir=os.environ.get('TMPDIR'))) / 'spawned'
        r = acc.Runner(a=sys.executable)
        now = time.monotonic()
        code = 'open(%r, "w").close()' % str(marker)
        rc, out, err = r.run_bounded('a', ['-c', code], deadline=now - 1, timeout=1, max_stdout=10, max_stderr=10)
        self.assertEqual((rc, out), (124, b''))
        time.sleep(0.2)
        self.assertFalse(marker.exists(), 'an expired absolute deadline must refuse before any child exists')
        start = time.monotonic()
        rc, _, _ = r.run_bounded('a', ['-c', 'import time; time.sleep(30)'], deadline=start + 0.4, timeout=30,
                                 max_stdout=10, max_stderr=10)
        self.assertEqual(rc, 124)
        self.assertLess(time.monotonic() - start, 2.5)

    def test_interrupt_during_wait_still_kills_reaps_and_closes_owned_child(self):
        r = acc.Runner(a=sys.executable)
        seen = {}
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            seen['proc'] = proc
            real_wait = proc.wait
            calls = {'n': 0}

            def wait(timeout=None):
                calls['n'] += 1
                if calls['n'] == 1:
                    raise KeyboardInterrupt
                return real_wait(timeout)
            proc.wait = wait
            return proc
        with mock.patch.object(acc.subprocess, 'Popen', popen), self.assertRaises(KeyboardInterrupt):
            r.run_bounded('a', ['-c', 'import time; time.sleep(30)'], deadline=time.monotonic() + 0.3,
                          timeout=0.3, max_stdout=10, max_stderr=10)
        proc = seen['proc']
        self.assertIsNotNone(proc.returncode, 'owned child must be reaped')
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)


class PinnedHelperTests(unittest.TestCase):
    def test_rendered_helper_writes_the_artifact_the_driver_verifies(self):
        if not shutil.which('git'):
            self.skipTest('git unavailable')
        tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='h-helper-', dir=base.TMP)))
        self.addCleanup(shutil.rmtree, tmp, True)
        ws, out = tmp / 'ws', tmp / 'out'
        ws.mkdir()
        out.mkdir(mode=0o700)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        git = ['git', '-C', str(ws), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
        subprocess.run(git[:3] + ['init', '-q'], check=True, env=env)
        (ws / 'tracked.txt').write_text('t\n')
        subprocess.run(git + ['add', 'tracked.txt'], check=True, env=env)
        subprocess.run(git + ['commit', '-q', '-m', 'init'], check=True, env=env)
        (ws / 'tracked.txt').write_text('changed\n')
        sid, nid, child = WORK['session_id'], WORK['native_id'], WORK['child_intake_id']
        whoami = tmp / 'aplexer'
        whoami.write_text('#!%s\nimport json\nprint(json.dumps({"id": %r, "tag": %r, "workspace": %r, '
                          '"env": {"PRIVATE": "never-copied"}, "command": ["x"]}))\n'
                          % (sys.executable, nid, 'task-hub2-' + sid, str(ws)))
        whoami.chmod(0o700)
        challenge = out / 'challenge-1.bin'
        challenge.write_bytes(os.urandom(32))
        challenge.chmod(0o400)
        body = helper.render_helper({'aplexer_binary': str(whoami), 'challenge_path': str(challenge),
                                     'child_intake_id': child, 'machine_id': acc.HOST,
                                     'parent_intake_id': WORK['parent_intake_id'], 'project': acc.PROJECT,
                                     'run_id': RUN, 'sample': 1})
        self.assertEqual(body, helper.render_helper(json.loads(json.dumps({
            'aplexer_binary': str(whoami), 'challenge_path': str(challenge), 'child_intake_id': child,
            'machine_id': acc.HOST, 'parent_intake_id': WORK['parent_intake_id'], 'project': acc.PROJECT,
            'run_id': RUN, 'sample': 1}))))
        (ws / '.local').mkdir(mode=0o700)
        (ws / REL).write_bytes(body)
        done = subprocess.run([sys.executable, REL], cwd=ws, capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        line = done.stdout.strip()
        self.assertRegex(line, r'^AC10_WORK_V2 %s 1 [0-9a-f]{64} [0-9a-f]{40} 5050$' % RUN)
        self.assertNotIn('never-copied', (ws / acc.work_rel_path(RUN, 1)).read_text())
        with mock.patch.dict(os.environ, env):
            info = acc.verify_work_artifact(acc.Runner(), str(ws), RUN, 1, WORK['parent_intake_id'], child, sid, nid,
                                            challenge.read_bytes(), time.monotonic, time.monotonic() + 60,
                                            pinned_probe=body)
        self.assertEqual(line.split()[3], info['proof_digest'])
        self.assertEqual(info['tracked_status_entries'], 1)
        with self.assertRaises(ValueError):
            acc.verify_work_artifact(acc.Runner(), str(ws), RUN, 1, WORK['parent_intake_id'], child, sid, nid,
                                     challenge.read_bytes(), time.monotonic, time.monotonic() + 60,
                                     pinned_probe=body + b'#')
        # A second run never overwrites the existing artifact (O_EXCL); the driver never pre-creates it.
        again = subprocess.run([sys.executable, REL], cwd=ws, capture_output=True, text=True, timeout=60, env=env)
        self.assertNotEqual(again.returncode, 0)
        for bad in ({'sample': 11}, {'challenge_path': 'relative'}, {'run_id': 'not-a-uuid'},
                    {'aplexer_binary': "/x'y"}, {'extra': 1}):
            binding = {'aplexer_binary': '/a', 'challenge_path': '/c', 'child_intake_id': child,
                       'machine_id': acc.HOST, 'parent_intake_id': child, 'project': acc.PROJECT,
                       'run_id': RUN, 'sample': 1}
            binding.update(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper.render_helper(binding)


HYD_POOL_SOURCE = [x for x in base.NATIVE_POOL if x['engine'] == 'claude'] + [
    {'policy_id': 'antigravity-hydrogen-medium', 'engine': 'antigravity', 'model': HYD, 'effort': 'medium',
     'weight': 25}]
HYD_POOL = [dict(x, share=x['weight'] / sum(y['weight'] for y in HYD_POOL_SOURCE)) for x in HYD_POOL_SOURCE]
MIXED = [base.SOL, HYD, base.GLM, base.HAIKU, HYD, base.SOL, base.GLM, HYD, base.HAIKU, base.SOL]


class HydrogenEndToEndTests(base.Harness):
    def setUp(self):
        super().setUp()
        # The native client renders `~` from HOME; keep the synthetic child workspaces below it.
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        provider = self.tmp / 'agy-synthetic'
        provider.write_bytes(b'synthetic provider executable placeholder; never run\n')
        provider_sha = hashlib.sha256(provider.read_bytes()).hexdigest()
        for target, name, value in ((acquire, 'PROVIDER_EXECUTABLE_SHA256', provider_sha),
                                    (acc, 'CAPTURE_BUDGET_S', 40)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        client = os.path.realpath(shutil.which('a'))
        self.transport = {
            'engine': 'antigravity', 'model': HYD, 'machine_id': acc.HOST, 'mode': 'interactive',
            'format': acc.NATIVE_CAPTURE_FORMATS['antigravity'], 'proof_class': acc.NATIVE_PROOF_CLASSES['antigravity'],
            'capture_command': acc.HYDROGEN_CAPTURE_COMMAND, 'artifact_schema': acc.WORK_SCHEMA,
            'host_release_sha': base.COMMIT, 'provider_version': acquire.PROVIDER_VERSION,
            'provider_executable_path': str(provider), 'provider_executable_sha256': provider_sha,
            'client_executable_path': client,
            'client_executable_sha256': hashlib.sha256(Path(client).read_bytes()).hexdigest(),
            'profile_set_sha256': hashlib.sha256(b'nonsecret-hydrogen-interactive-argv').hexdigest(),
            'reference': 'synthetic://native-capture/hydrogen',
            'sha256': hashlib.sha256(b'offline-hydrogen-receipt').hexdigest(),
        }

    def bundle(self, **transport_change):
        path = Path(base.native_evidence())
        ev = json.loads(path.read_text())
        if transport_change.pop('omit', False) is False:
            ev['operator_validation']['capture_transports']['antigravity'] = dict(self.transport, **transport_change)
        path.write_text(json.dumps(ev))
        return str(path)

    def run_h(self, models=None, bundle=None, **cfg):
        cfg.setdefault('pool', HYD_POOL)
        out_dir = self.tmp / ('out-' + str(time.time_ns()))
        rc, out = self.go(models=models or [HYD], evidence_bundle=bundle or self.bundle(), output_dir=out_dir, **cfg)
        report = json.loads(out[out.index('\n{\n') + 1:out.rindex('\n}') + 2]) if '\n{\n' in out else None
        return rc, out, report, out_dir

    def sends(self):
        return self.state()['log'].count('native:send-0f')

    def test_mixed_formal_run_passes_with_one_fenced_key_per_hydrogen_draw(self):
        rc, out, report, out_dir = self.run_h(models=MIXED)
        self.assertEqual(rc, 0, out)
        self.assertIn('ACCEPTANCE PASS', out)
        self.assertEqual(self.sends(), 3)
        self.assertEqual(len(list((out_dir / 'hydrogen-fence').iterdir())), 3)
        self.assertAllStopped()
        manifest = json.loads(next(out_dir.glob('manifest-*.json')).read_text())
        hydrogen = [s for s in report['starts'] if s['model'] == HYD]
        self.assertEqual(len(hydrogen), 3)
        for rec in hydrogen:
            proof = rec['native_proof']
            self.assertEqual((proof['status'], proof['key_bytes_sent'], proof['native_action_verified']),
                             ('VERIFIED', 1, True))
            self.assertTrue(Path(rec['capture_path']).name.endswith('.svg'))
            sample = manifest['samples'][rec['n'] - 1]
            pinned = Path(sample['helper_path']).read_bytes()
            self.assertEqual(sample['helper_sha256'], hashlib.sha256(pinned).hexdigest())
            self.assertEqual(rec['probe_sha256'], sample['helper_sha256'])
            self.assertEqual(Path(rec['workspace'], acc.probe_rel_path(report['run_id'], rec['n'])).read_bytes(), pinned)
        for rec in report['starts']:
            self.assertEqual(rec['cleanup_status'], 'CONFIRMED')
        for path in [*out_dir.rglob('*')]:
            if path.is_file():
                self.assertNotIn(b'must-not-leak', path.read_bytes(), path)
        self.assertNotIn('must-not-leak', out)

    def test_already_expanded_frame_needs_zero_keys(self):
        rc, out, report, _ = self.run_h(h_view='expanded', fail_start_at=1)
        first = report['starts'][0]
        self.assertEqual(first['native_proof']['status'], 'VERIFIED', out)
        self.assertEqual(self.sends(), 0)
        self.assertAllStopped()

    def test_lost_send_ack_consumes_the_single_attempt_without_replay(self):
        rc, out, report, out_dir = self.run_h(h_lost_ack=True, fail_start_at=1)
        self.assertEqual(report['starts'][0]['native_proof']['status'], 'VERIFIED', out)
        self.assertEqual(self.sends(), 1)

    def test_post_key_failures_are_final_with_exactly_one_key_and_cleanup(self):
        for knob in ({'h_after_send': 'none'}, {'h_fake_footer': True}, {'h_exit_code': 1}):
            with self.subTest(knob=knob):
                rc, out, report, out_dir = self.run_h(**knob)
                self.assertEqual(rc, 1, out)
                self.assertIn('Hydrogen native display acquisition did not prove completed work', out)
                self.assertEqual(self.sends(), 1)
                self.assertEqual(len(list((out_dir / 'hydrogen-fence').iterdir())), 1)
                self.assertIs(report['starts'][0]['capture_verified'], False)
                self.assertAllStopped()

    def test_unsupported_view_or_identity_never_sends_a_key(self):
        # Busy/draft views are re-observed (no gesture) until the proof window ends; identity, status and
        # capture-cap refusals and a tampered helper are final at once. None of them may send a key.
        cases = [({'h_view': 'busy'}, None),
                 ({'h_view': 'draft'}, None),
                 ({'h_status_unreachable': True}, 'callback_failed'),
                 ({'h_birth_changes': True}, 'callback_failed'),
                 ({'h_capture_huge': True}, 'callback_failed'),
                 ({'probe_tampered': True}, 'not_started')]
        with mock.patch.object(acc, 'CAPTURE_BUDGET_S', 4):
            for knob, reason in cases:
                with self.subTest(knob=knob):
                    rc, out, report, out_dir = self.run_h(**knob)
                    self.assertEqual(rc, 1, out)
                    self.assertEqual(self.sends(), 0)
                    self.assertEqual(list((out_dir / 'hydrogen-fence').iterdir()), [])
                    proof = report['starts'][0]['native_proof']
                    self.assertIs(proof['key_attempted'], False)
                    if reason is None:
                        self.assertGreaterEqual(proof['not_ready_views'], 1)
                    else:
                        self.assertEqual((proof['reason'], proof['not_ready_views']), (reason, 0))
                    self.assertAllStopped()

    def test_natural_exit_after_hydrogen_proof_before_live_stop_fails(self):
        rc, out, report, _ = self.run_h(natural_exit_after_proof=True)
        self.assertEqual(rc, 1, out)
        self.assertEqual(report['starts'][0]['native_proof']['status'], 'VERIFIED')
        self.assertIn('natural exit after proof before live STOP', out)
        self.assertAllStopped()

    def test_missing_or_mismatched_hydrogen_attestation_and_other_antigravity_models(self):
        rc, out, _, _ = self.run_h(bundle=self.bundle(omit=True))
        self.assertIn('no verified Hydrogen styled-screen transport', out)
        self.assertEqual(self.sends(), 0)
        rc, out, _, _ = self.run_h(bundle=self.bundle(client_executable_path=str(self.tmp / 'agy-synthetic'),
                                                      client_executable_sha256=self.transport['provider_executable_sha256']))
        self.assertIn('Hydrogen identity/pin preflight failed', out)
        self.assertEqual(self.sends(), 0)
        for change in ({'provider_version': '1.5.8'}, {'model': base.MUSE}, {'capture_command': acc.NATIVE_CAPTURE_COMMAND},
                       {'provider_executable_sha256': '0' * 64}, {'unexpected': 'x'}):
            with self.subTest(change=change):
                self.assertFalse(acc.native_transport_ok('antigravity', dict(self.transport, **change), base.COMMIT))
        self.assertTrue(acc.native_transport_ok('antigravity', self.transport, base.COMMIT))
        # Another antigravity model keeps the honest UNAVAILABLE gate even with a valid Hydrogen record.
        rc, out, report, _ = self.run_h(models=[base.MUSE], pool=base.NATIVE_POOL)
        self.assertIn('native proof UNAVAILABLE for engine antigravity: UNKNOWN_NATIVE_FRAMING', out)
        self.assertEqual(self.sends(), 0)


if __name__ == '__main__':
    unittest.main()
