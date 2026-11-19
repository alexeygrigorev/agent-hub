"""Archive caller contracts with fake selection/transport/business effects.

No Root file, actual manager, provider, service or observation is exercised.
"""
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from agent_hub_host import quota_natural_primary as primary
from agent_hub_host import quota_dispatch_entry as entry

ROOT = Path(__file__).resolve().parents[3]
ACTUAL_DEADLINE_CHANNEL = primary.DeadlineChannel


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class PrimaryTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.quota = (b'import builtins\n'
            b'def main(argv, _managed_observer=None):\n'
            b'    return builtins._natural_primary_effect(argv, _managed_observer is not None, __file__)\n')
        self.payloads = {'hub-quota-publish.py': self.quota,
            'hub_alert_client.py': (ROOT/'scripts/hub_alert_client.py').read_bytes(),
            'natural-quota-prelude.py': (ROOT/'scripts/natural-quota-prelude.py').read_bytes()}
        self.plan = {'v': 1, 'kind': 'natural_quota_roles_v1',
            'quota_path': '/owned/hub-quota-publish.py',
            'client_path': '/owned/hub_alert_client.py',
            'gate_directory': '/owned/gate',
            'quota_sha256': digest(self.quota),
            'client_sha256': digest(self.payloads['hub_alert_client.py']),
            'prelude_sha256': digest(self.payloads['natural-quota-prelude.py']),
            'roles': {'quota_publish': {'argv': []}, 'quota_monitor': {'argv':
                ['--check','--health-check','--health-config','/owned/health.json',
                 '--health-timeout','8','--alert-timeout','8']}}}
        self.plan_raw = json.dumps(self.plan).encode()
        self.sent = []
        class Channel:
            def __init__(inner): inner.reads = 0
            def send(inner, raw, end):
                self.events.append('send'); self.sent.append(raw)
            def recv(inner, end):
                inner.reads += 1
                self.events.append('context' if inner.reads == 1 else 'release')
                return b'open:'+b'a'*32 if inner.reads == 1 else b'qualified-run'
            def close(inner): self.events.append('close')
        self.channel = Channel()
        def read(path, expected, **kwargs):
            if path == '/owned/selection.json':
                self.events.append('selection'); return self.plan_raw
            self.events.append('source-open')
            self.assertEqual(path, self.plan['quota_path'])
            self.assertTrue(self.sent and json.loads(self.sent[0])['op'] == 'held')
            return self.quota
        def connect(*args):
            self.events.append('connect'); return self.channel
        def effect(argv, observed, path):
            self.events.append(('business', observed, path)); return 23
        for target, fn in [('agent_hub_host.quota_natural_primary.read_root_file', read),
                           ('agent_hub_host.quota_natural_primary.DeadlineChannel', connect),
                           ('time.monotonic', lambda: 1.01),
                           ('builtins._natural_primary_effect', effect)]:
            item = patch(target, side_effect=fn, create=target.startswith('builtins.'))
            item.start(); self.addCleanup(item.stop)
        for target in ('socket.socket', 'subprocess.Popen', 'subprocess.run', 'os.pidfd_open'):
            item = patch(target, side_effect=AssertionError('real effect forbidden'))
            item.start(); self.addCleanup(item.stop)

    def run_primary(self, started=1_000_000_000):
        return primary.run_primary('/owned/selection.json', digest(self.plan_raw),
            'quota_publish', started_ns=started, payloads=self.payloads)

    def test_watched_source_is_not_opened_before_context_and_business_is_once(self):
        self.assertEqual(self.run_primary(), 23)
        self.assertLess(self.events.index('context'), self.events.index('source-open'))
        effects = [e for e in self.events if isinstance(e, tuple)]
        self.assertEqual(effects, [('business', True, self.plan['quota_path'])])
        self.assertEqual(self.sent[1], b'quota-source-opened')

    def test_expired_original_entry_deadline_is_not_renewed_by_selection(self):
        self.assertEqual(self.run_primary(started=500_000_000), 23)
        self.assertNotIn('connect', self.events)
        self.assertEqual([e for e in self.events if isinstance(e, tuple)],
                         [('business', False, self.plan['quota_path'])])

    def test_changed_source_disables_observation_but_runs_retained_original_once(self):
        original = primary.read_root_file
        def changed(path, *a, **k):
            if path == self.plan['quota_path']: return b'replaced'
            return original(path, *a, **k)
        with patch.object(primary, 'read_root_file', side_effect=changed):
            self.assertEqual(self.run_primary(), 23)
        self.assertEqual([e for e in self.events if isinstance(e, tuple)],
                         [('business', False, self.plan['quota_path'])])

    def test_bad_observer_client_never_gates_or_repeats_original_business(self):
        self.payloads['hub_alert_client.py'] = b'changed client'
        self.assertEqual(self.run_primary(), 23)
        self.assertNotIn('connect', self.events)
        self.assertEqual([e for e in self.events if isinstance(e, tuple)],
                         [('business', False, self.plan['quota_path'])])

    def test_bad_optional_bootstrap_falls_back_before_business_only(self):
        self.payloads['natural-quota-prelude.py'] = b'changed bootstrap'
        self.assertEqual(self.run_primary(), 23)
        self.assertNotIn('connect', self.events)
        self.assertEqual([e for e in self.events if isinstance(e, tuple)],
                         [('business', False, self.plan['quota_path'])])

    def test_business_exception_is_original_and_never_retried(self):
        original = ValueError('business failure'); effects = []
        def failing(*a): effects.append(a); raise original
        with patch('builtins._natural_primary_effect', side_effect=failing):
            with self.assertRaises(ValueError) as caught: self.run_primary()
        self.assertIs(caught.exception, original)
        self.assertEqual(len(effects), 1)

    def test_unverified_business_bytes_never_execute(self):
        self.payloads['hub-quota-publish.py'] = b'changed business'
        with self.assertRaises(ValueError): self.run_primary()
        self.assertFalse(any(isinstance(e, tuple) for e in self.events))

    def test_duplicate_selection_fields_are_not_overwritten(self):
        self.plan_raw = b'{"v":1,"v":1}'
        with self.assertRaises(ValueError): self.run_primary()

    def test_missing_optional_client_key_runs_valid_original(self):
        del self.payloads['hub_alert_client.py']
        self.assertEqual(self.run_primary(),23)
        self.assertEqual([e for e in self.events if isinstance(e,tuple)],
                         [('business',False,self.plan['quota_path'])])

    def test_missing_optional_prelude_key_runs_valid_original(self):
        del self.payloads['natural-quota-prelude.py']
        self.assertEqual(self.run_primary(),23)
        self.assertEqual([e for e in self.events if isinstance(e,tuple)],
                         [('business',False,self.plan['quota_path'])])

    def _member_load_failure(self,missing):
        calls=[]
        def data(path):
            name=path.rsplit('/',1)[-1];calls.append(name)
            if name==missing:raise OSError('optional member unreadable')
            return self.payloads[name]
        loader=SimpleNamespace(archive='/owned/entry.pyz',get_data=data)
        with patch.dict(primary.__dict__,{'__loader__':loader}):
            payloads=primary.archive_payloads()
        self.assertEqual(calls[0],'hub-quota-publish.py')
        self.payloads=payloads
        self.assertEqual(self.run_primary(),23)
        self.assertEqual([e for e in self.events if isinstance(e,tuple)],
                         [('business',False,self.plan['quota_path'])])

    def test_optional_client_member_read_error_runs_valid_original(self):
        self._member_load_failure('hub_alert_client.py')

    def test_optional_prelude_member_read_error_runs_valid_original(self):
        self._member_load_failure('natural-quota-prelude.py')

    def test_connect_exhaustion_does_not_wait_again_and_business_runs_once(self):
        clock={'now':1.0};waits=[];closed=[]
        sock=SimpleNamespace(setblocking=lambda *a:None,
            connect_ex=lambda *a:clock.update(now=1.2) or primary.errno.EINPROGRESS,
            getsockopt=lambda level,opt,*a:0 if opt==primary.socket.SO_ERROR
                else primary.struct.pack('3i',101,1000,1000),
            close=lambda:closed.append(True))
        def select(read,write,error,timeout):waits.append(timeout);return [],[sock],[]
        with patch.object(primary,'DeadlineChannel',side_effect=ACTUAL_DEADLINE_CHANNEL),\
             patch('socket.socket',return_value=sock),\
             patch('os.getuid',return_value=1000),\
             patch('os.stat',return_value=SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=1000)),\
             patch('time.monotonic',side_effect=lambda:clock['now']),\
             patch.object(primary.select,'select',side_effect=select):
            self.assertEqual(self.run_primary(),23)
        self.assertEqual(waits,[])
        self.assertEqual(closed,[True])
        self.assertEqual([e for e in self.events if isinstance(e,tuple)],
                         [('business',False,self.plan['quota_path'])])

    def test_archive_entry_captures_deadline_before_payload_loading(self):
        calls = []
        def payloads(): calls.append('payload'); return self.payloads
        def run(*a, **k): calls.append(('run', k['started_ns'])); return 23
        with patch.object(entry.time, 'monotonic_ns', side_effect=lambda:calls.append('clock') or 123),\
             patch.object(primary, 'archive_payloads', side_effect=payloads),\
             patch.object(primary, 'run_primary', side_effect=run):
            self.assertEqual(entry.main(['natural-primary','/owned/selection.json','a'*64,'quota_publish']),23)
        self.assertEqual(calls,['clock','payload',('run',123)])


class RootFileTests(unittest.TestCase):
    def test_exact_full_bytes_and_root_ancestry_are_required(self):
        raw = b'root-owned selection'; closed = []
        directory = SimpleNamespace(st_mode=stat.S_IFDIR|0o755, st_uid=0)
        leaf = SimpleNamespace(st_mode=stat.S_IFREG|0o444, st_uid=0, st_nlink=1,
            st_dev=1, st_ino=2, st_size=len(raw), st_mtime_ns=3, st_ctime_ns=4)
        with patch.object(primary.os,'open',side_effect=[10,11,12]),\
             patch.object(primary.os,'fstat',side_effect=lambda fd:leaf if fd==12 else directory),\
             patch.object(primary.os,'stat',return_value=leaf),\
             patch.object(primary.os,'pread',return_value=raw),\
             patch.object(primary.os,'close',side_effect=closed.append):
            self.assertEqual(primary.read_root_file('/owned/selection.json',digest(raw)),raw)
        self.assertEqual(closed,[10,12,11])

    def test_user_writable_parent_refuses_before_opening_selected_file(self):
        bad = SimpleNamespace(st_mode=stat.S_IFDIR|0o775,st_uid=0)
        closed = []
        with patch.object(primary.os,'open',return_value=10) as opened,\
             patch.object(primary.os,'fstat',return_value=bad),\
             patch.object(primary.os,'close',side_effect=closed.append):
            with self.assertRaises(ValueError):primary.read_root_file('/owned/selection.json','a'*64)
        self.assertEqual(opened.call_count,1)
        self.assertEqual(closed,[10])


class DeadlineTests(unittest.TestCase):
    def test_connect_elapsed_time_reduces_remaining_wait(self):
        clock={'now':1.0};waits=[]
        sock=SimpleNamespace(setblocking=lambda *a:None,
            connect_ex=lambda *a:clock.update(now=1.1) or primary.errno.EINPROGRESS,
            getsockopt=lambda level,opt,*a:0 if opt==primary.socket.SO_ERROR
                else primary.struct.pack('3i',101,1000,1000),close=lambda:None)
        def select(read,write,error,timeout):waits.append(timeout);return [],[sock],[]
        with patch('socket.socket',return_value=sock),\
             patch('os.getuid',return_value=1000),\
             patch('os.stat',return_value=SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=1000)),\
             patch('time.monotonic',side_effect=lambda:clock['now']),\
             patch.object(primary.select,'select',side_effect=select):
            channel=ACTUAL_DEADLINE_CHANNEL('/owned/gate',1.15)
            channel.close()
        self.assertEqual(len(waits),1)
        self.assertAlmostEqual(waits[0],0.05)


class ArchiveTests(unittest.TestCase):
    def test_builder_embeds_original_payloads_in_deterministic_archive(self):
        builder=runpy.run_path(str(ROOT/'scripts/build-quota-dispatcher.py'))
        with tempfile.TemporaryDirectory() as temp:
            first,second=Path(temp)/'one.pyz',Path(temp)/'two.pyz'
            builder['build_archive'](first);builder['build_archive'](second)
            self.assertEqual(first.read_bytes(),second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                manifest=json.loads(archive.read('MANIFEST.json'))
                for name in primary.PAYLOADS:
                    data=archive.read('natural/'+name)
                    self.assertEqual(data,(ROOT/'scripts'/name).read_bytes())
                    self.assertEqual(digest(data),manifest['sources']['natural/'+name])

    def test_isolated_archive_loader_reads_original_payloads(self):
        builder=runpy.run_path(str(ROOT/'scripts/build-quota-dispatcher.py'))
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'entry.pyz'
            builder['build_archive'](archive)
            code=('import sys,json,hashlib;sys.path.insert(0,sys.argv[1]);'
                  'from agent_hub_host.quota_natural_primary import archive_payloads;'
                  'print(json.dumps({k:hashlib.sha256(v).hexdigest() '
                  'for k,v in archive_payloads().items()},sort_keys=True))')
            result=subprocess.run([sys.executable,'-I','-S','-c',code,str(archive)],
                capture_output=True,text=True,timeout=10,
                env={'PATH':os.defpath,'TMPDIR':temp,'PYTHONDONTWRITEBYTECODE':'1'})
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),
                {name:digest((ROOT/'scripts'/name).read_bytes()) for name in primary.PAYLOADS})


if __name__ == '__main__':
    unittest.main()
