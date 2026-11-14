"""Real private stager/installer dataflow; synthetic source and systemd metadata only."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/stage-credential-monitor.py'
spec = importlib.util.spec_from_file_location('node_observer_stager_fixture', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class NodeAutoactivationTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.local/tmp'; scratch.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix='node-observer-offline-', dir=scratch))
        self.addCleanup(shutil.rmtree, self.base)
        self.repo = self.base/'source'; self.repo.mkdir(mode=0o700)
        self.root = self.base/'evidence'; self.root.mkdir(mode=0o700)
        self.home = self.base/'home'; self.home.mkdir(mode=0o700)
        self.env = {'PATH': os.defpath, 'HOME': str(self.home), 'GIT_CONFIG_NOSYSTEM':'1',
                    'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_TERMINAL_PROMPT':'0'}
        for name in m.DIAGNOSTIC_ARTIFACTS:
            p = self.repo/name; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('synthetic owned source '+name); p.chmod(0o600)
        (self.repo/'scripts').mkdir()
        shutil.copyfile(SCRIPT, self.repo/'scripts/stage-credential-monitor.py')
        self.git('init','-q'); self.git('add','.')
        self.git('-c','user.name=Offline Fixture','-c','user.email=fixture@example.invalid',
                 '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','synthetic fixture')
        self.commit = self.git('rev-parse','HEAD').strip()
        self.current = {producer:'' for producer in m.PRODUCERS}
        self.config = self.base/'.dev.vars'
        self.config.write_bytes(b'UNRELATED_SYNTHETIC=fake-only\r\nHUB2_RELEASE_MODE=bootstrap\r\n')
        self.config.chmod(0o600)

    def git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-C',str(self.repo),*args],env=self.env,
                                       stderr=subprocess.DEVNULL,text=True)

    def prepare(self):
        return m.prepare_active(self.repo,self.root,self.commit,self.current)

    def cli(self,*args):
        return subprocess.run([sys.executable,'-I','-S','-B',str(SCRIPT),'--checkout',str(self.repo),
                               '--expected-commit',self.commit,*args],env=self.env,
                              capture_output=True,text=True,timeout=15)

    def installer(self,mode='active',override='',after_prepare=None):
        source=(ROOT/'deploy/vm/install-hub2-node.sh').read_text()
        block=source.split("<<'PY'",1)[1].split('\nPY\n',1)[0]
        actual_run=subprocess.run; calls=[]
        def run(argv,**kwargs):
            calls.append(list(map(str,argv)))
            if argv[0]=='/usr/bin/systemctl':
                if argv[1]=='show':
                    text='Environment='+override+'\nEnvironmentFiles=\nPassEnvironment=\nUnsetEnvironment=\nNeedDaemonReload=no\n'
                elif argv[1] in ('daemon-reload','show-environment'): text=''
                else: self.fail('unexpected systemctl operation')
                return subprocess.CompletedProcess(argv,0,text.encode())
            # Exercise the actual stager executable (not a success sentinel).
            result=actual_run(argv,**kwargs)
            if '--prepare-active-root' in argv and after_prepare: after_prepare()
            return result
        err=io.StringIO()
        with mock.patch.object(sys,'argv',['installer',str(self.config),mode,'apply',str(self.repo),str(self.root)]), \
                mock.patch('subprocess.run',side_effect=run), contextlib.redirect_stderr(err):
            exec(compile(block,'actual-installer-config-block','exec'),{'__name__':'__main__'})
        return calls,err.getvalue()

    def pointers(self):
        values={}
        for line in self.config.read_text().splitlines():
            if '=' in line:
                key,value=line.split('=',1)
                if key in m.KEYS.values():values[key]=value
        return values

    def test_actual_cli_all_three_contracts_and_private_source_pins(self):
        p=self.cli('--prepare-active-root',str(self.root))
        self.assertEqual(p.returncode,0,p.stderr)
        value=json.loads(p.stdout)
        self.assertEqual(set(value['pointers']),set(m.KEYS.values()))
        self.assertIs(value['activated'],False);self.assertIs(value['historical_coverage'],False)
        for producer,key in m.KEYS.items():
            path=Path(value['pointers'][key]);entry=json.loads((path/'enrollment.json').read_text())
            self.assertEqual(len(entry['artifacts']),3 if producer=='credential_monitor' else 5)
            self.assertEqual(entry['source_id'],hashlib.sha256(m.canonical(entry['artifacts'])).hexdigest())
            self.assertEqual(entry['release_sha'],self.commit)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o700)
            for file in path.iterdir():self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o600)

    def test_diagnostic_cli_mode_has_exact_five_pins_and_typed_receipt(self):
        args=['--producer','picker_excluded','--destination',str(self.root/'direct-diagnostic')]
        for label,name in zip(('observer','main','worker','picker','pool'),m.DIAGNOSTIC_ARTIFACTS):
            args += ['--'+label+'-sha256',hashlib.sha256((self.repo/name).read_bytes()).hexdigest()]
        result=self.cli(*args)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['kind'],'picker_diagnostic_staged_boundary_v1')
        self.assertEqual(value['producer'],'picker_excluded');self.assertIs(value['activated'],False)
        self.assertEqual(self.cli(*args).returncode,2,'direct diagnostic staging never overwrites/replays')

    def test_actual_installer_absent_pointers_active_and_retry_reuses_all_bytes(self):
        calls,error=self.installer();self.assertEqual(error,'')
        self.assertTrue(any('--prepare-active-root' in c for c in calls))
        pointers=self.pointers();self.assertEqual(set(pointers),set(m.KEYS.values()))
        before={str(p):p.read_bytes() for path in pointers.values() for p in Path(path).iterdir()}
        self.installer();self.assertEqual(self.pointers(),pointers)
        self.assertEqual(before,{str(p):p.read_bytes() for path in pointers.values() for p in Path(path).iterdir()})
        self.assertIn(b'UNRELATED_SYNTHETIC=fake-only\r\n',self.config.read_bytes())

    def test_bootstrap_never_prepares_authorities(self):
        calls,_=self.installer('bootstrap')
        self.assertFalse(any('--prepare-active-root' in c for c in calls))
        self.assertEqual(self.pointers(),{});self.assertEqual(list(self.root.iterdir()),[])

    def test_service_override_is_unknown_not_clobbered_or_business_refusal(self):
        calls,error=self.installer(override='HUB2_PICKER_EXCLUDED_EVIDENCE_DIR=/synthetic/override')
        self.assertIn('no new coverage authorized',error)
        self.assertTrue(any('--blocked-producer' in c for c in calls))
        self.assertEqual(set(self.pointers()),set(m.KEYS.values())-{m.KEYS['picker_excluded']})
        self.assertIn('node_observer_held:picker_excluded:service_override',error)
        self.assertIn('HUB2_RELEASE_MODE=active',self.config.read_text())

    def test_corrupt_current_boundary_never_mints_replacement_zero(self):
        prepared=self.prepare();key=m.KEYS['picker_excluded'];path=Path(prepared['pointers'][key])
        before={str(p) for p in self.root.iterdir()}
        (path/'journal.json').write_text('{}\n')
        self.current['picker_excluded']=str(path)
        result=self.prepare()
        self.assertEqual(result['held'],{'picker_excluded':'same_tuple_unqualified'})
        self.assertNotIn(key,result['pointers'])
        self.assertEqual(len(result['pointers']),2)
        self.assertEqual(before,{str(p) for p in self.root.iterdir()})
        self.assertEqual((path/'journal.json').read_text(),'{}\n')

    def test_lost_publish_and_repeated_parent_fsync_failure_retry_repair(self):
        real=m.os.fsync;armed=[True]
        def fail(fd):
            if armed[0] and any(p.is_dir() and p.name.startswith('credential_monitor-') for p in self.root.iterdir()) \
                    and os.readlink('/proc/self/fd/'+str(fd))==str(self.root):
                raise OSError('synthetic durability failure')
            return real(fd)
        with mock.patch.object(m.os,'fsync',side_effect=fail):
            with self.assertRaises(OSError):self.prepare()
            dirs=[p for p in self.root.iterdir() if p.is_dir()]
            self.assertEqual(len(dirs),1)
            enrollment=(dirs[0]/'enrollment.json').read_bytes()
            with self.assertRaises(OSError):self.prepare()
        repaired=self.prepare()
        self.assertEqual((dirs[0]/'enrollment.json').read_bytes(),enrollment)
        self.assertEqual(len(repaired['pointers']),3)

    def test_new_real_commit_retains_old_boundaries_and_explicit_new_source(self):
        first=self.prepare();self.current={producer:first['pointers'][key] for producer,key in m.KEYS.items()}
        old={str(p):p.read_bytes() for d in first['pointers'].values() for p in Path(d).iterdir()}
        (self.repo/'hub2/src/worker.ts').write_text('synthetic changed reviewed worker')
        self.git('add','.');self.git('-c','user.name=Offline Fixture','-c','user.email=fixture@example.invalid',
             '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','changed synthetic source')
        self.commit=self.git('rev-parse','HEAD').strip();second=self.prepare()
        self.assertTrue(all(first['pointers'][key]!=second['pointers'][key] for key in m.KEYS.values()))
        self.assertTrue(all(Path(p).read_bytes()==content for p,content in old.items()))
        self.assertIs(second['activated'],False)

    def test_symlink_ancestor_and_foreign_target_without_intent_refused(self):
        link=self.base/'alias';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(OSError):m.prepare_active(self.repo,link,self.commit,self.current)
        first=self.prepare();intent=Path(first['intents']['picker_excluded']);intent.unlink()  # Unowned publication.
        before={str(p) for p in self.root.iterdir()}
        result=self.prepare()
        self.assertEqual(result['held'],{'picker_excluded':'target_ownership_unverified'})
        self.assertEqual(len(result['pointers']),2)
        self.assertEqual(before,{str(p) for p in self.root.iterdir()})

    def test_exhausted_record_is_held_not_restaged(self):
        first=self.prepare();path=Path(first['pointers'][m.KEYS['picker_excluded']]);e=json.loads((path/'enrollment.json').read_text())
        j=json.loads((path/'journal.json').read_text());j.update(started_total=128,finished_total=128)
        initial=json.loads((path/'journal.json').read_text())
        record={'kind':'hub_picker_diagnostic_snapshot_v1','controller_id':'a'*32,'sequence':1,
                'previous_sha256':hashlib.sha256(m.canonical(initial)).hexdigest(),'journal':j}
        (path/('snapshot-'+'a'*32+'-1.json')).write_bytes(m.canonical(record)+b'\n')
        (path/('snapshot-'+'a'*32+'-1.json')).chmod(0o600)
        self.current['picker_excluded']=str(path)
        checked=m.validate_boundary(path,'picker_excluded')
        self.assertIs(checked['capacity_reached'],True)
        result=self.prepare()
        self.assertEqual(result['held'],{'picker_excluded':'same_tuple_capacity_reached'})
        self.assertEqual(len(result['pointers']),2)
        self.assertEqual(json.loads((path/'journal.json').read_text()),initial)
        record['journal'].update(started_total=129,finished_total=129)
        (path/('snapshot-'+'a'*32+'-1.json')).write_bytes(m.canonical(record)+b'\n')
        (path/('snapshot-'+'a'*32+'-1.json')).chmod(0o600)
        with self.assertRaises(m.Refused):m.validate_boundary(path,'picker_excluded')

    def test_dirty_installed_authority_cannot_borrow_clean_head_provenance(self):
        (self.repo/'hub2/node/main.ts').write_text('unreviewed fixture change')
        result=self.prepare()
        self.assertEqual(result['pointers'],{})
        self.assertEqual(set(result['held']),set(m.PRODUCERS))
        self.assertEqual([p for p in self.root.iterdir() if p.name!='.prepare.lock'],[])

    def change_source(self):
        target=self.repo/'hub2/src/worker.ts'
        target.write_text(target.read_text()+' changed selected source')
        self.git('add','.')
        self.git('-c','user.name=Offline Fixture','-c','user.email=fixture@example.invalid',
                 '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','real source change')
        self.commit=self.git('rev-parse','HEAD').strip()

    def test_new_selected_source_advances_despite_held_old_picker_without_modifying_history(self):
        for fault in ('corrupt_journal','missing_journal','capacity','unknown_snapshot'):
            with self.subTest(fault=fault):
                first=self.prepare()
                self.current={producer:first['pointers'][key] for producer,key in m.KEYS.items()}
                path=Path(self.current['picker_excluded'])
                if fault=='corrupt_journal':(path/'journal.json').write_text('{}\n')
                elif fault=='missing_journal':(path/'journal.json').unlink()
                elif fault=='unknown_snapshot':(path/'future-checkpoint.json').write_text('{}\n')
                else:
                    initial=json.loads((path/'journal.json').read_text());j=dict(initial,started_total=128,finished_total=128)
                    record={'kind':'hub_picker_diagnostic_snapshot_v1','controller_id':'b'*32,'sequence':1,
                            'previous_sha256':hashlib.sha256(m.canonical(initial)).hexdigest(),'journal':j}
                    (path/('snapshot-'+'b'*32+'-1.json')).write_bytes(m.canonical(record)+b'\n')
                    (path/('snapshot-'+'b'*32+'-1.json')).chmod(0o600)
                held=self.prepare();self.assertNotIn(m.KEYS['picker_excluded'],held['pointers'])
                self.assertEqual(held['held']['picker_excluded'], 'same_tuple_capacity_reached' if fault=='capacity' else 'same_tuple_unqualified')
                before={str(p):p.read_bytes() for d in self.current.values() for p in Path(d).iterdir()}
                self.change_source();second=self.prepare()
                self.assertEqual(second['held'],{});self.assertEqual(len(second['pointers']),3)
                self.assertTrue(all(second['pointers'][key]!=first['pointers'][key] for key in m.KEYS.values()))
                self.assertTrue(all(Path(p).read_bytes()==data for p,data in before.items()))
                self.current={producer:second['pointers'][key] for producer,key in m.KEYS.items()}

    def test_missing_foreign_or_corrupt_enrollment_holds_only_its_producer(self):
        first=self.prepare()
        for fault in ('missing','foreign','corrupt'):
            with self.subTest(fault=fault):
                self.current={producer:first['pointers'][key] for producer,key in m.KEYS.items()}
                if fault=='missing':self.current['picker_excluded']=str(self.base/'missing')
                elif fault=='foreign':self.current['picker_excluded']=first['pointers'][m.KEYS['credential_monitor']]
                else:(Path(self.current['picker_excluded'])/'enrollment.json').write_text('{}\n')
                self.change_source();second=self.prepare()
                self.assertEqual(second['held'],{'picker_excluded':'current_identity_unverified'})
                self.assertEqual(len(second['pointers']),2)
                self.assertNotEqual(second['pointers'][m.KEYS['credential_monitor']],first['pointers'][m.KEYS['credential_monitor']])

    def test_same_tuple_legacy_path_reused_without_parallel_zero(self):
        first=self.prepare();key=m.KEYS['credential_monitor'];old=Path(first['pointers'][key])
        legacy=self.base/'legacy-boundary';old.rename(legacy)
        self.current['credential_monitor']=str(legacy)
        before={p.name:p.read_bytes() for p in legacy.iterdir()}
        result=self.prepare()
        self.assertEqual(result['pointers'][key],str(legacy));self.assertFalse(old.exists())
        self.assertEqual(before,{p.name:p.read_bytes() for p in legacy.iterdir()})

    def test_installer_partial_hold_advances_other_producers_and_preserves_old_pointer(self):
        self.installer();first=self.pointers();path=Path(first[m.KEYS['picker_excluded']])
        (path/'journal.json').write_text('{}\n')
        _,error=self.installer()
        self.assertIn('same_tuple_unqualified',error);self.assertEqual(self.pointers(),first)
        self.change_source();_,error=self.installer()
        self.assertEqual(error,'');second=self.pointers()
        self.assertTrue(all(first[key]!=second[key] for key in m.KEYS.values()))
        self.assertEqual((path/'journal.json').read_text(),'{}\n')

    def test_new_commit_advances_directly_from_genuine_128_picker_history(self):
        first=self.prepare();self.current={producer:first['pointers'][key] for producer,key in m.KEYS.items()}
        path=Path(self.current['picker_excluded']);initial=json.loads((path/'journal.json').read_text())
        record={'kind':'hub_picker_diagnostic_snapshot_v1','controller_id':'c'*32,'sequence':1,
                'previous_sha256':hashlib.sha256(m.canonical(initial)).hexdigest(),
                'journal':dict(initial,started_total=128,finished_total=128)}
        snapshot=path/('snapshot-'+'c'*32+'-1.json');snapshot.write_bytes(m.canonical(record)+b'\n');snapshot.chmod(0o600)
        before={str(p):p.read_bytes() for d in first['pointers'].values() for p in Path(d).iterdir()}
        self.change_source();second=self.prepare()
        self.assertEqual(set(second['pointers']),set(m.KEYS.values()))
        self.assertNotEqual(second['pointers'][m.KEYS['credential_monitor']],first['pointers'][m.KEYS['credential_monitor']])
        self.assertTrue(all(Path(p).read_bytes()==data for p,data in before.items()))

    def test_new_commit_with_corrupt_picker_advances_credential_directly(self):
        first=self.prepare();self.current={producer:first['pointers'][key] for producer,key in m.KEYS.items()}
        journal=Path(self.current['picker_excluded'])/'journal.json';journal.write_text('{}\n')
        self.change_source();second=self.prepare()
        self.assertEqual(set(second['pointers']),set(m.KEYS.values()))
        self.assertNotEqual(second['pointers'][m.KEYS['credential_monitor']],first['pointers'][m.KEYS['credential_monitor']])
        self.assertEqual(journal.read_text(),'{}\n')

    def test_diagnostic_source_fault_does_not_block_credential(self):
        (self.repo/'hub2/src/model-picker.ts').write_text('unreviewed diagnostic source')
        result=self.prepare()
        self.assertEqual(set(result['pointers']),{m.KEYS['credential_monitor']})
        self.assertEqual(result['held'],{'picker_excluded':'selected_source_unverified',
                                         'picker_quota_stale':'selected_source_unverified'})

    def test_installer_reuses_trusted_legacy_current_path(self):
        self.installer();first=self.pointers();key=m.KEYS['credential_monitor']
        legacy=self.base/'legacy-private';Path(first[key]).rename(legacy)
        self.config.write_text(self.config.read_text().replace(first[key],str(legacy)))
        _,error=self.installer()
        self.assertEqual(error,'');self.assertEqual(self.pointers()[key],str(legacy))
        self.assertFalse(Path(first[key]).exists())

    def test_installer_concurrent_config_write_not_overwritten(self):
        new=b'UNRELATED_SYNTHETIC=concurrent-owner\n'
        with self.assertRaisesRegex(ValueError,'release_configuration_changed'):
            self.installer(after_prepare=lambda:self.config.write_bytes(new))
        self.assertEqual(self.config.read_bytes(),new)
        self.assertEqual(len([p for p in self.root.iterdir() if p.is_dir()]),3)


if __name__=='__main__':unittest.main(verbosity=2)
