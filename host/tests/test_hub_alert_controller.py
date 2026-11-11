"""Credential-monitor ingress authority: owned loopback/fake credentials/mail only.

The Node child runs the actual Worker and optional journal with synthetic routing
bindings. No real providers, Hub origin, service, enrollment or native session.
"""
import copy
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('ingress_evidence', ROOT/'scripts/hub_alert_evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
TOKEN = 'synthetic-credential-monitor-operator-token-000000000'


class IngressTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(prefix='ingress-host-', dir=parent))
        self.children = []
        self.addCleanup(shutil.rmtree, self.temp)
        self.addCleanup(self.stop_children)
        self.installed = self.temp/'installed'
        self.installed.mkdir(mode=0o700)
        for name in ('hub-model-picker-monitor.sh', 'hub_alert_evidence.py'):
            shutil.copyfile(ROOT/'scripts'/name, self.installed/name)
            (self.installed/name).chmod(0o600)
        artifacts = [{'path':str(self.installed/name), 'sha256':hashlib.sha256((self.installed/name).read_bytes()).hexdigest()}
                     for name in ('hub-model-picker-monitor.sh','hub_alert_evidence.py')]
        self.entry = {'mode':'external', 'artifacts':artifacts, 'source_id':evidence.digest(artifacts)}
        self.credential = self.temp/'cli.json'

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=3)
            child.stdout.close(); child.stderr.close()

    def synthetic_env(self):
        env={key:value for key,value in os.environ.items() if not key.startswith(('HUB_','HUB2_','APLEXER_','DTC_','XDG_'))
             and not any(word in key.upper() for word in ('TOKEN','SECRET','PASSWORD','CREDENTIAL','API_KEY'))}
        env.update(HOME=str(self.temp),TMPDIR=str(self.temp))
        return env

    def start_node(self, fail=False, port=0, producer='credential_monitor'):
        directory = self.temp/'node'
        directory.mkdir(exist_ok=True)
        child = subprocess.Popen(['node','--experimental-transform-types',str(ROOT/'hub2/tests/alert-evidence.test.ts'),
            '--fixture-server',str(directory),'fail' if fail else 'good',str(port),producer],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.synthetic_env())
        self.children.append(child)
        with selectors.DefaultSelector() as selection:
            selection.register(child.stdout, selectors.EVENT_READ)
            if not selection.select(timeout=8):
                child.kill();child.wait(timeout=3);self.fail('synthetic Node authority did not start')
            line = child.stdout.readline()
        self.assertTrue(line, 'synthetic Node authority exited')
        info = json.loads(line)
        base = 'http://127.0.0.1:' + str(info['port'])
        self.credential.write_text(json.dumps({'base_url':base,'access_token':TOKEN}))
        self.credential.chmod(0o600)
        self.entry['remote'] = {'kind':'credential_monitor_post_v1' if producer=='credential_monitor' else 'picker_diagnostic_v1','base_url':base,'credential_path':str(self.credential),
                                'source_id':info['source_id'],'release_sha':info['release_sha']}
        return child, info

    def get(self, path):
        url=self.entry['remote']['base_url']+path
        with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+TOKEN}),timeout=3) as response:
            return json.load(response)

    def monitor_env(self):
        commands=self.temp/'bin'
        commands.mkdir(exist_ok=True,mode=0o700)
        python=commands/'python3'
        # Stub only the independent existing picker-control delivery helper.
        python.write_text('#!/bin/sh\ncase "$1" in */hub-picker-alerts.py) exit 0;; esac\nexec '+sys.executable+' "$@"\n')
        python.chmod(0o700)
        mail=commands/'mail'
        mail.write_text('#!/bin/sh\nprintf "attempted\\n" >> "$MAIL_SINK"\nexit "${MAIL_STATUS:-0}"\n')
        mail.chmod(0o700)
        return dict(self.synthetic_env(),PATH=str(commands)+':'+os.environ['PATH'],HUB_BASE_URL=self.entry['remote']['base_url'],
            HUB_CLI_JSON=str(self.credential),HUB_PICKER_MONITOR_STATE=str(self.temp/'last'),HUB_PICKER_ALERT_CMD=str(mail),
            MAIL_SINK=str(self.temp/'mail-sink'),HUB_ALERT_CONTROLLER_SOCKET=str(self.temp/'missing-old-observer.sock'))

    def monitor(self, env):
        return subprocess.run(['bash',str(self.installed/'hub-model-picker-monitor.sh')],env=env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)

    def aggregate_fixture(self):
        # Other sources here are explicitly synthetic test witnesses, not live
        # enrollments or a claim that this slice implements seven producers.
        state=self.temp/'complete-fixture'
        proofs=self.temp/'synthetic-other-proofs';proofs.mkdir(mode=0o700)
        producers={}
        for name in evidence.REQUIRED_PRODUCERS:
            if name=='credential_monitor':
                producers[name]=copy.deepcopy(self.entry)
                continue
            entry={key:copy.deepcopy(self.entry[key]) for key in ('mode','artifacts','source_id')}
            entry['witness_path']=str(proofs/(name+'.json'))
            with evidence.PrivateDirectory(proofs) as directory:
                directory.publish(name+'.json',{'kind':'hub_controller_coverage_v1','producer':name,'source_id':entry['source_id'],
                    'epoch':'synthetic-epoch','controller_id':'synthetic-controller','limitations':[],'complete':True,
                    'started_total':0,'finished_total':0,'baseline_count':0,'baseline_invocations':0,
                    'occurrence_count':0,'observed_at_ms':int(time.time()*1000)},new=True)
            producers[name]=entry
        evidence.install_enrollment(state,{'kind':'hub_alert_enrollment_v1','recipient':evidence.RECIPIENT,'producers':producers})
        return state

    def test_diagnostic_remote_fetches_actual_node_decision_counters_and_refuses_dead_source(self):
        for producer in ('picker_excluded', 'picker_quota_stale'):
            with self.subTest(producer=producer):
                child, info = self.start_node(producer=producer)
                before = evidence.remote_witness(producer, self.entry)
                self.assertEqual(before['occurrence_count'], 0)
                if producer == 'picker_quota_stale':
                    self.get('/__fixture/stale')
                request = urllib.request.Request(self.entry['remote']['base_url'] + '/api/model-picker/pick',
                    data=b'{}', method='POST', headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
                with urllib.request.urlopen(request, timeout=3) as response:
                    body = json.load(response)
                self.assertTrue(body['excluded'] if producer == 'picker_excluded' else body['warnings'])
                self.get('/__fixture/idle')  # Test-only explicit durability barrier, no timing guess.
                after = evidence.remote_witness(producer, self.entry)
                self.assertEqual(after['producer'], producer)
                self.assertEqual(after['epoch'], before['epoch'])
                self.assertEqual(after['occurrence_count'], 1)
                self.assertEqual(after['started_total'], after['finished_total'])
                bad = copy.deepcopy(self.entry)
                bad['remote']['kind'] = 'credential_monitor_post_v1'
                with self.assertRaises(evidence.EvidenceUnavailable):
                    evidence.remote_witness(producer, bad)
                child.kill(); child.wait(timeout=3)
                with self.assertRaises(evidence.EvidenceUnavailable):
                    evidence.remote_witness(producer, self.entry)
                # Preserve this exact old generation rather than resetting it
                # to fresh zero when the next synthetic server starts.
                shutil.rmtree(self.temp/'node')

    def test_aggregate_actually_fetches_live_remote_and_never_uses_cached_zero(self):
        child,_=self.start_node()
        state=self.aggregate_fixture()
        before=evidence.aggregate(state)
        self.assertEqual(before['kind'],'dev_alert_occurrences_v1');self.assertEqual(before['count'],0)
        self.assertEqual(self.monitor(self.monitor_env()).returncode,0)
        after=evidence.aggregate(state)
        self.assertEqual(after.get('kind'),'dev_alert_occurrences_v1',after)
        self.assertEqual(after['generation'],before['generation']);self.assertEqual(after['count'],1)
        self.assertGreaterEqual(after['observed_at_ms'],before['observed_at_ms'])
        child.kill();child.wait(timeout=3)
        self.assertEqual(evidence.aggregate(state)['reason'],'coverage_unknown')
        self.assertFalse((state/'credential_monitor.json').exists())

    def test_actual_monitor_same_post_dedup_smtp_failure_and_healthy_counter(self):
        self.start_node()
        env=self.monitor_env()
        self.assertEqual(self.monitor(env).returncode,0)
        self.assertEqual(self.monitor(env).returncode,0)
        self.assertEqual(len((self.temp/'mail-sink').read_text().splitlines()),1)
        (self.temp/'last').unlink();env['MAIL_STATUS']='7'
        self.assertEqual(self.monitor(env).returncode,7)
        self.get('/__fixture/healthy')
        self.assertEqual(self.monitor(env).returncode,0)
        proof=evidence.witness('credential_monitor',self.entry,now_ms=int(time.time()*1000))
        self.assertTrue(proof['complete']);self.assertEqual(proof['started_total'],4)
        self.assertEqual(proof['occurrence_count'],3)
        self.assertEqual(proof['source_id'],self.entry['source_id'])
        self.assertNotIn(TOKEN,json.dumps(proof))
        self.assertFalse((self.installed/'hub_alert_controller.py').exists())

    def test_write_failure_primary_mail_then_crash_new_boot_rejects_old_interval(self):
        first,info=self.start_node(fail=True)
        before=evidence.witness('credential_monitor',self.entry,now_ms=int(time.time()*1000))
        state=self.aggregate_fixture()
        baseline=evidence.aggregate(state)
        env=self.monitor_env()
        self.assertEqual(self.monitor(env).returncode,0)
        self.assertTrue((self.temp/'mail-sink').exists())
        with self.assertRaises(evidence.EvidenceUnavailable):
            evidence.witness('credential_monitor',self.entry,now_ms=int(time.time()*1000))
        first.kill();first.wait(timeout=3)
        # Same address/source/release/journal: only the genuine process birth differs.
        self.start_node(port=info['port'])
        after=evidence.witness('credential_monitor',self.entry,now_ms=int(time.time()*1000))
        self.assertNotEqual(before['epoch'],after['epoch'])
        self.assertNotEqual(before['controller_id'],after['controller_id'])
        spec=importlib.util.spec_from_file_location('ingress_acceptance',ROOT/'scripts/hub_acceptance_10.py')
        acceptance=importlib.util.module_from_spec(spec);spec.loader.exec_module(acceptance)
        self.assertEqual(acceptance.alert_changes(baseline,evidence.aggregate(state)),['alert watermark generation changed'])

    def test_live_aggregate_missing_other_producers_is_unknown_without_local_fallback(self):
        child,_=self.start_node()
        state=self.temp/'aggregate'
        state.mkdir(mode=0o700)
        with evidence.PrivateDirectory(state) as directory:
            fd=os.open('ledger.lock',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600,dir_fd=directory.fds[-1]);os.close(fd)
            directory.publish('enrollment.json',{'kind':'hub_alert_enrollment_v1','recipient':evidence.RECIPIENT,
                'producers':{'credential_monitor':self.entry}},new=True)
        self.assertEqual(evidence.aggregate(state)['reason'],'coverage_unknown')
        child.kill();child.wait(timeout=3)
        with self.assertRaises(evidence.EvidenceUnavailable):
            evidence.witness('credential_monitor',self.entry,now_ms=int(time.time()*1000))
        self.assertFalse((state/'credential_monitor.json').exists())

    def test_remote_identity_origin_nonce_permissions_and_unsafe_redirect_refused(self):
        self.start_node()
        entry=copy.deepcopy(self.entry)
        for key,value in (('source_id','f'*64),('release_sha','f'*40)):
            bad=copy.deepcopy(entry);bad['remote'][key]=value
            with self.subTest(key=key),self.assertRaises(evidence.EvidenceUnavailable):
                evidence.witness('credential_monitor',bad,now_ms=int(time.time()*1000))
        credential=json.loads(self.credential.read_text());credential['base_url']='https://unrelated.invalid'
        self.credential.write_text(json.dumps(credential))
        with self.assertRaises(evidence.EvidenceUnavailable):evidence.remote_witness('credential_monitor',entry)
        self.credential.write_text(json.dumps({'base_url':entry['remote']['base_url'],'access_token':TOKEN}))
        self.credential.chmod(0o644)
        with self.assertRaises(evidence.EvidenceUnavailable):evidence.remote_witness('credential_monitor',entry)
        self.credential.chmod(0o600)
        for origin in ('http://example.invalid','https://user:password@example.invalid','https://example.invalid/path','https://example.invalid/?query=1'):
            bad=copy.deepcopy(entry);bad['remote']['base_url']=origin
            with self.subTest(origin=origin),self.assertRaises(evidence.EvidenceUnavailable):evidence.remote_spec('credential_monitor',bad)
        raw=evidence.canonical({'kind':'hub_controller_coverage_v1','nonce':'wrong'})
        with mock.patch.object(evidence.subprocess,'run',return_value=mock.Mock(returncode=0,stdout=raw)):
            with self.assertRaises(evidence.EvidenceUnavailable):evidence.remote_witness('credential_monitor',entry)

    def test_real_redirect_oversize_and_old_server_never_create_evidence(self):
        requests=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path,self.headers.get('Authorization')))
                mode=self.server.mode
                if mode=='redirect':self.send_response(302);self.send_header('Location','/secret-target');self.end_headers()
                elif mode=='old':self.send_response(404);self.end_headers()
                elif mode=='slow':
                    self.send_response(200);self.end_headers()
                    try:
                        for _ in range(30):self.wfile.write(b' ');self.wfile.flush();time.sleep(.2)
                    except OSError:pass
                else:self.send_response(200);self.end_headers();self.wfile.write(b'x'*(evidence.LIMIT+1))
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base='http://127.0.0.1:'+str(server.server_port)
            self.credential.write_text(json.dumps({'base_url':base,'access_token':TOKEN}));self.credential.chmod(0o600)
            self.entry['remote']={'kind':'credential_monitor_post_v1','base_url':base,'credential_path':str(self.credential),'source_id':'a'*64,'release_sha':'b'*40}
            for mode in ('redirect','old','oversize','slow'):
                server.mode=mode
                with self.subTest(mode=mode),self.assertRaises(evidence.EvidenceUnavailable):evidence.remote_witness('credential_monitor',self.entry)
            self.assertEqual(len(requests),4)
            self.assertTrue(all(path.startswith('/api/model-picker/credential-monitor-evidence?nonce=') for path,_ in requests))
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
