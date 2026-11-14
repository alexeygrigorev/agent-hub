"""Actual Node checkpoint I/O -> strict Python stager; no provider/service calls."""
import hashlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/stage-credential-monitor.py'
spec = importlib.util.spec_from_file_location('checkpoint_stager', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

NODE = r"""
import {readFile} from 'node:fs/promises';import {pathToFileURL} from 'node:url';
const state=process.argv[2],entry=JSON.parse(await readFile(state+'/enrollment.json','utf8'));
const source=entry.artifacts.find(x=>x.path.endsWith('/hub2/node/alert-evidence.ts')).path;
const {AlertEvidence}=await import(pathToFileURL(source).href);
const observer=await AlertEvidence.load(state,{producer:entry.producer,
 loadedArtifacts:Object.fromEntries(entry.artifacts.map(x=>[x.path,x.sha256])),timeoutMs:2000});
try {const batch=process.argv[3]==='coalesced'?10:1;
 for(let n=0;n<500;n+=batch){const ids=Array.from({length:batch},()=>observer.begin());if(ids.some(x=>x===null))throw Error('unexpected admission loss');
 await Promise.all(ids.map((id,j)=>observer.finish(id,(n+j)%11===0?'alert':'observation')));}
 console.log(JSON.stringify(await observer.snapshot('1'.repeat(32))));}finally{await observer.close();}
"""


class CheckpointReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / '.local/tmp';scratch.mkdir(parents=True,exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='checkpoint-reader-owned-',dir=scratch)
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name);cls.repo=cls.base/'source';cls.repo.mkdir(mode=0o700)
        cls.env={'PATH':os.environ.get('PATH',os.defpath),'HOME':str(cls.base/'home'),
                 'TMPDIR':str(scratch),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null',
                 'GIT_TERMINAL_PROMPT':'0'}
        (cls.base/'home').mkdir(mode=0o700)
        for rel in m.DIAGNOSTIC_ARTIFACTS:
            dst=cls.repo/rel;dst.parent.mkdir(parents=True,exist_ok=True)
            if rel=='hub2/node/alert-evidence.ts':shutil.copyfile(ROOT/rel,dst)
            else:dst.write_text('// synthetic owned '+rel+'\n')
            dst.chmod(0o600)
        subprocess.run(['/usr/bin/git','init','-q',str(cls.repo)],env=cls.env,check=True)
        subprocess.run(['/usr/bin/git','-C',str(cls.repo),'add','.'],env=cls.env,check=True)
        cls.commit=cls.commit_source(cls.repo)
        cls.authorities=cls.base/'authorities';cls.authorities.mkdir(mode=0o700)
        prepared=m.prepare_active(cls.repo,cls.authorities,cls.commit,{p:'' for p in m.PRODUCERS})
        cls.names={p:Path(prepared['pointers'][key]).name for p,key in m.KEYS.items()}
        cls.producer='picker_excluded';state=cls.authorities/cls.names[cls.producer]
        generator=cls.base/'generate.mjs';generator.write_text(NODE)
        generated=subprocess.run(['node','--experimental-transform-types',str(generator),str(state)],
            env=cls.env,capture_output=True,text=True,timeout=90,check=True)
        cls.node_proof=json.loads(generated.stdout)
        assert cls.node_proof['complete'] is True and cls.node_proof['started_total']==500
        assert cls.node_proof['occurrence_count']==46
        coalesced=cls.authorities/cls.names['picker_quota_stale']
        generated=subprocess.run(['node','--experimental-transform-types',str(generator),str(coalesced),'coalesced'],
            env=cls.env,capture_output=True,text=True,timeout=60,check=True)
        cls.node_coalesced_proof=json.loads(generated.stdout)
        assert cls.node_coalesced_proof['complete'] is True and cls.node_coalesced_proof['started_total']==500
        assert len(list(coalesced.glob('snapshot-*.json')))<256 and not list(coalesced.glob('checkpoint-*.json'))

    @classmethod
    def commit_source(cls, repo):
        subprocess.run(['/usr/bin/git','-C',str(repo),'-c','user.name=Offline Fixture',
            '-c','user.email=fixture@example.invalid','-c','core.hooksPath=/dev/null',
            '-c','commit.gpgsign=false','commit','-qm','synthetic owned source'],env=cls.env,check=True)
        return subprocess.check_output(['/usr/bin/git','-C',str(repo),'rev-parse','HEAD'],env=cls.env,text=True).strip()

    def setUp(self):
        self.case=Path(tempfile.mkdtemp(prefix='case-',dir=self.base));self.addCleanup(shutil.rmtree,self.case)
        self.root=self.case/'authorities';shutil.copytree(self.authorities,self.root)
        self.current={p:str(self.root/name) for p,name in self.names.items()}
        self.path=Path(self.current[self.producer])

    def prepare(self):
        return m.prepare_active(self.repo,self.root,self.commit,self.current)

    def slots(self):
        return sorted(self.path.glob('checkpoint-*.json'),key=lambda p:json.loads(p.read_text())['sequence'])

    def store(self,path,value):
        path.write_bytes(m.canonical(value)+b'\n');path.chmod(0o600)

    def assert_role_held(self):
        before={str(p):p.read_bytes() for p in self.path.iterdir()}
        result=self.prepare()
        self.assertEqual(result['held'],{self.producer:'same_tuple_unqualified'})
        self.assertEqual(len(result['pointers']),2)
        self.assertNotIn(m.KEYS[self.producer],result['pointers'])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.path.iterdir()})

    def test_real_node_500_checkpoint_reinstall_reuses_exact_history_and_epoch(self):
        before={str(p):p.read_bytes() for p in self.path.iterdir()}
        check=m.validate_boundary(self.path,self.producer)
        self.assertIs(check['capacity_reached'],False)
        result=self.prepare();self.assertEqual(result['held'],{})
        self.assertEqual(result['pointers'][m.KEYS[self.producer]],str(self.path))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.path.iterdir()})
        self.assertEqual(len(list(self.path.iterdir())),260)
        latest=json.loads(self.slots()[-1].read_text())
        self.assertEqual(latest['journal']['started_total'],500)
        self.assertEqual(latest['journal']['occurrence_count'],self.node_proof['occurrence_count'])
        self.assertEqual(latest['controller_id'],self.node_proof['controller_id'])

    def test_real_coalesced500_prefix_before_rollover_reuses_verified_selected_source(self):
        path=Path(self.current['picker_quota_stale'])
        self.assertFalse(list(path.glob('checkpoint-*.json')))
        self.assertLess(len(list(path.glob('snapshot-*.json'))),256)
        before={p.name:p.read_bytes() for p in path.iterdir()}
        result=self.prepare();self.assertEqual(result['held'],{})
        self.assertEqual(result['pointers'][m.KEYS['picker_quota_stale']],str(path))
        self.assertEqual(before,{p.name:p.read_bytes() for p in path.iterdir()})
        self.assertEqual(self.node_coalesced_proof['started_total'],500)

    def test_wrong_anchor_link_source_or_regression_holds_only_the_producer(self):
        latest=self.slots()[-1];original=latest.read_bytes()
        for fault in ['anchor','previous','source','count_regression','active_overflow','bool_count','float_sequence','foreign_boot','wrong_parity']:
            with self.subTest(fault=fault):
                record=json.loads(original)
                if fault=='anchor':record['prefix_sha256']='f'*64
                elif fault=='previous':record['previous_sha256']='f'*64
                elif fault=='source':record['journal']['source_id']='f'*64
                elif fault=='count_regression':record['journal'].update(started_total=499,finished_total=499)
                elif fault=='active_overflow':record['journal'].update(started_total=700,finished_total=500)
                elif fault=='bool_count':record['journal']['finished_total']=True
                elif fault=='float_sequence':record['sequence']=float(record['sequence'])
                elif fault=='foreign_boot':record['controller_id']='f'*32
                else:record['sequence']+=1
                self.store(latest,record);self.assert_role_held();latest.write_bytes(original)

    def test_missing_prefix_slot_or_partial_unknown_file_never_heals(self):
        for target in [self.path/next(p.name for p in self.path.glob('snapshot-*-256.json')),self.slots()[0]]:
            data=target.read_bytes();target.unlink();self.assert_role_held();target.write_bytes(data);target.chmod(0o600)
        for name in ['.checkpoint-unknown.tmp','checkpoint-future.json','unknown.json']:
            path=self.path/name;path.write_text('{}\n');path.chmod(0o600)
            self.assert_role_held();path.unlink()

    def test_malformed_truncated_duplicate_and_private_metadata_are_refused(self):
        slot=self.slots()[-1];original=slot.read_bytes()
        for raw in [b'{',original[:-4],original.replace(b'"sequence":',b'"sequence":1,"sequence":',1)]:
            slot.write_bytes(raw);self.assert_role_held();slot.write_bytes(original)
        slot.chmod(0o644);self.assert_role_held();slot.chmod(0o600)
        alias=self.case/'alias';os.link(slot,alias);self.assert_role_held();alias.unlink()
        slot.unlink();slot.symlink_to(self.slots()[0])
        with self.assertRaises(OSError) as caught:m.validate_boundary(self.path,self.producer)
        self.assertEqual(caught.exception.errno,errno.ELOOP)
        self.assert_role_held()

    def test_single_first_checkpoint_is_valid_only_at_sequence257_with_complete_prefix(self):
        # Rebuild the genuine prefix's first rollup shape; no live claim is made.
        slots=self.slots()
        for slot in slots:slot.unlink()
        prefix=self.path/next(p.name for p in self.path.glob('snapshot-*-256.json'))
        previous=json.loads(prefix.read_text());boot=previous['controller_id']
        record={'kind':'hub_picker_diagnostic_checkpoint_v1','controller_id':boot,'sequence':257,
            'prefix_sha256':hashlib.sha256(m.canonical(previous)).hexdigest(),
            'previous_sha256':hashlib.sha256(m.canonical(previous)).hexdigest(),
            'journal':dict(previous['journal'],started_total=129,finished_total=129)}
        target=self.path/f'checkpoint-{boot}-1.json';self.store(target,record)
        self.assertIs(m.validate_boundary(self.path,self.producer)['capacity_reached'],False)
        record['sequence']=259;self.store(target,record);self.assert_role_held()

    def test_old_snapshot_only_and_legacy_capacity_semantics_remain(self):
        path=Path(self.current['credential_monitor'])
        self.assertIs(m.validate_boundary(path,'credential_monitor')['capacity_reached'],False)
        entry=json.loads((path/'journal.json').read_text())
        record={'kind':'hub_credential_monitor_snapshot_v1','controller_id':'a'*32,'sequence':1,
            'previous_sha256':hashlib.sha256(m.canonical(entry)).hexdigest(),
            'journal':dict(entry,started_total=128,finished_total=128)}
        self.store(path/('snapshot-'+'a'*32+'-1.json'),record)
        self.assertIs(m.validate_boundary(path,'credential_monitor')['capacity_reached'],True)

    def test_z_genuine_new_selected_source_advances_without_touching_rolled_history(self):
        before={str(p):p.read_bytes() for p in self.path.iterdir()}
        repo=self.case/'next-source';shutil.copytree(self.repo,repo)
        file=repo/'hub2/src/worker.ts';file.write_text(file.read_text()+'// next genuine source\n')
        subprocess.run(['/usr/bin/git','-C',str(repo),'add','.'],env=self.env,check=True)
        commit=self.commit_source(repo)
        result=m.prepare_active(repo,self.root,commit,self.current)
        self.assertEqual(result['held'],{})
        self.assertTrue(all(result['pointers'][key]!=self.current[p] for p,key in m.KEYS.items()))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.path.iterdir()})


if __name__=='__main__':
    # A wrapper timeout must unwind subprocess.run, which kills/waits its owned
    # Node fixture before class cleanup. Importing this test changes no signals.
    import signal
    def terminate(_signal, _frame):
        raise SystemExit(143)
    signal.signal(signal.SIGTERM,terminate)
    unittest.main(verbosity=2)
