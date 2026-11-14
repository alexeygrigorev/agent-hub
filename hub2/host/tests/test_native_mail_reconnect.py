"""Host supplied-frame progress ONLY; not MachineDO oldest-pending fairness.

No providers/native effects/network: actual Bridge receiver + journal with a
controlled transport, constant fake native target and fsynced synthetic effect.
"""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from agent_hub_host.agent import Journal
from agent_hub_host import agent
from native_mail_restart_fixture import FIRST, LATER, SESSION, NATIVE, TAG, mail

FIXTURE = Path(__file__).with_name('native_mail_restart_fixture.py')


class NativeMailReconnectTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='mr-', dir=os.environ.get('RUNNER_TEMP') or None))
        self.children, self.files = [], []
        self.addCleanup(self.cleanup)  # Before any child/file/pool allocation.
        for name in ('effects.jsonl', 'api.jsonl', 'status.jsonl'):
            with (self.root / name).open('w') as output:
                output.flush(); os.fsync(output.fileno())
        self.trace = []

    def cleanup(self):
        failures = []
        for holder in reversed(self.children):
            process = holder.get('process')
            if process is None: continue
            try:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
                if process.poll() is None: raise AssertionError('owned child not reaped')
            except BaseException as error: failures.append(type(error).__name__)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try: stream.close()
                        except OSError: failures.append('pipe_close')
        for stream in self.files:
            try: stream.close()
            except OSError: failures.append('log_close')
        if failures:
            raise AssertionError('owned_fixture_not_clean; retained ' + str(self.root) + ' ' + repr(failures))
        shutil.rmtree(self.root)

    def child(self, mode):
        holder = {}; self.children.append(holder)  # Guard installed BEFORE Popen.
        error = (self.root / (mode + '.stderr')).open('wb', buffering=0)
        self.files.append(error)
        env = {key: os.environ[key] for key in ('PYTHONUSERBASE', 'AGENT_HUB_HEAVY_TEST_HELD') if key in os.environ}
        home = self.root / 'home'; home.mkdir(mode=0o700, exist_ok=True)
        env.update(HOME=str(home), TMPDIR=str(self.root), PATH=os.defpath, PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=os.pathsep.join([str(Path(agent.__file__).parent.parent), str(FIXTURE.parent)]))
        process = subprocess.Popen([sys.executable, '-u', str(FIXTURE), str(self.root), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error, env=env, bufsize=0)
        holder['process'] = process
        return process

    def wait(self, process, predicate, timeout=5):
        end = time.monotonic() + timeout
        while True:
            raw = b''
            while not raw.endswith(b'\n'):
                left = end - time.monotonic()
                self.assertGreater(left, 0, 'own receiver protocol timeout')
                self.assertTrue(select.select([process.stdout], [], [], left)[0], 'own receiver protocol timeout')
                part = os.read(process.stdout.fileno(), 1)
                self.assertTrue(part, 'own receiver ended unexpectedly'); raw += part
                self.assertLessEqual(len(raw), 8192)
            event = json.loads(raw); self.trace.append(event)
            self.assertLessEqual(len(self.trace), 64)
            if predicate(event): return event

    def send(self, process, event):
        raw = json.dumps(event).encode() + b'\n'
        self.assertLess(len(raw), 4096)
        self.assertEqual(os.write(process.stdin.fileno(), raw), len(raw))

    def rows(self):
        journal = Journal(self.root / 'journal.sqlite')
        try:
            rows = {row['id']: dict(row) for row in journal.db.execute('SELECT * FROM native_mail')}
            outbox = [dict(row) for row in journal.db.execute('SELECT * FROM outbox')]
            return rows, outbox
        finally: journal.close_thread()

    def records(self, name):
        return [json.loads(line) for line in (self.root / name).read_text().splitlines()]

    def uncertainty(self, process, connection):
        self.send(process, {'type':'mailbox.deliver','message':mail(FIRST)})
        event = self.wait(process, lambda e:e.get('kind')=='sent' and e.get('connection')==connection
                          and e.get('frame',{}).get('message_id')==FIRST)
        error = event['frame']
        self.assertEqual(error['type'], 'host.error')
        self.assertEqual(error['reason'], 'native_mail_outcome_uncertain')
        self.assertEqual(error['session_id'], SESSION)
        self.assertEqual(error['operation_id'], mail(FIRST)['operation_id'])

    def test_crash_restart_replay_then_reconnect_and_later_supplied_frame_progress(self):
        crash = self.child('crash')
        self.assertEqual(crash.wait(timeout=5), 73)
        rows, outbox = self.rows(); first = rows[FIRST]
        self.assertEqual(first['state'], 'executing'); self.assertIsNone(first['receipt'])
        self.assertEqual(outbox, [])
        self.assertEqual(self.records('effects.jsonl'), [{'id':FIRST,'receipt':None}])
        receive = self.child('receive')
        connected = self.wait(receive, lambda e:e.get('kind')=='connected' and e.get('connection')==1)
        self.assertEqual(connected['pid'], receive.pid)
        self.assertNotEqual(receive.pid, crash.pid)
        self.uncertainty(receive, 1)
        self.assertEqual(self.rows()[0][FIRST], first)
        self.assertEqual(len(self.records('effects.jsonl')), 1)
        self.send(receive, {'fixture':'disconnect'})
        reconnected = self.wait(receive, lambda e:e.get('kind')=='connected' and e.get('connection')==2)
        self.assertEqual(reconnected['pid'], receive.pid)
        self.assertEqual(reconnected['thread'], connected['thread'])
        self.uncertainty(receive, 2)
        self.assertEqual(self.rows()[0][FIRST], first)
        self.assertEqual(len(self.records('effects.jsonl')), 1)
        # Later input is NOT precollected and is supplied only after the actual
        # new-connection uncertainty error above. Server queue behavior is absent.
        self.send(receive, {'type':'mailbox.deliver','message':mail(LATER)})
        posted = self.wait(receive, lambda e:e.get('kind')=='receipt_posted' and e.get('id')==LATER)
        self.send(receive, {'fixture':'stop'})
        self.assertEqual(receive.wait(timeout=5), 0)
        rows, outbox = self.rows()
        self.assertEqual(rows[FIRST], first); self.assertEqual(rows[LATER]['state'], 'receipted')
        later = json.loads(rows[LATER]['receipt'])
        self.assertEqual(later['native_receipt'], posted['native_receipt'])
        self.assertEqual(str(uuid.UUID(later['native_receipt']['message_id'])), later['native_receipt']['message_id'])
        self.assertEqual(later['native_receipt']['session_id'], SESSION)
        self.assertEqual(outbox, [])
        effects = self.records('effects.jsonl')
        self.assertEqual([row['id'] for row in effects], [FIRST, LATER])
        self.assertEqual(effects[1]['receipt'], later['native_receipt']['message_id'])
        self.assertEqual(self.records('api.jsonl'), [{'id':FIRST,'phase':'claim'},
            {'id':LATER,'phase':'claim'}, {'id':LATER,'phase':'receipt'}])
        status = self.records('status.jsonl')
        self.assertEqual(len(status), 2)
        self.assertTrue(all(row=={'id':NATIVE,'tag':TAG,'workspace':str(self.root),
                                 'phase':'running','created_at_ms':1000} for row in status))


if __name__ == '__main__': unittest.main()
