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
            namespace={'__name__':'__main__'}
            exec(compile(block,'actual-installer-config-block','exec'),namespace)
            self.installer_functions=namespace
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

    def test_installer_hardens_real_git_0664_only_after_raw_blob_verification(self):
        originals={name:(self.repo/name).read_bytes() for name in m.DIAGNOSTIC_ARTIFACTS}
        for name in originals:(self.repo/name).unlink()
        subprocess.run(['/usr/bin/git','-C',str(self.repo),'checkout','--force','HEAD','--',*m.DIAGNOSTIC_ARTIFACTS],
                       env=self.env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,umask=0o002)
        self.assertEqual(m.head(self.repo),self.commit)
        for name,data in originals.items():
            self.assertEqual(stat.S_IMODE((self.repo/name).stat().st_mode),0o664)
            self.assertEqual((self.repo/name).read_bytes(),data)
            self.assertEqual(m.committed_artifact_hash(self.repo,self.commit,name),hashlib.sha256(data).hexdigest())
        held=self.prepare()
        self.assertEqual(held['pointers'],{})
        self.assertEqual(held['source_failures'],dict.fromkeys(m.PRODUCERS,'source_file_unsafe_mode'))
        owners={name:((self.repo/name).stat().st_uid,(self.repo/name).stat().st_gid) for name in originals}
        _,error=self.installer()
        self.assertEqual(error,'');self.assertEqual(len(self.pointers()),3)
        self.assertNotIn(str(self.repo),error)
        for name,data in originals.items():
            p=self.repo/name
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o644)
            self.assertEqual((p.stat().st_uid,p.stat().st_gid),owners[name])
            self.assertEqual(p.read_bytes(),data)
        self.assertEqual(len(self.prepare()['pointers']),3)

    def test_symlink_checkout_head_succeeds_but_source_directory_is_unavailable(self):
        alias=self.base/'checkout-alias';alias.symlink_to(self.repo,target_is_directory=True)
        self.assertEqual(m.head(alias),self.commit)
        result=m.prepare_active(alias,self.root,self.commit,self.current)
        self.assertEqual(result['pointers'],{})
        self.assertEqual(result['source_failures'],dict.fromkeys(m.PRODUCERS,'source_directory_unavailable'))
        self.assertTrue(alias.is_symlink(),'diagnostic must NOT canonicalize or replace checkout aliases')

    def test_exact_source_failure_enums_preserve_all_strict_file_predicates(self):
        p=self.repo/m.ARTIFACTS[0];raw=p.read_bytes()
        for fault,expected in (('hash','source_hash_mismatch'),('hardlink','source_file_linked'),
                               ('fifo','source_file_not_regular'),('symlink','source_file_unavailable')):
            with self.subTest(fault=fault):
                extra=self.base/'owned-extra'
                if fault=='hash':p.write_bytes(raw+b'changed private fixture')
                elif fault=='hardlink':os.link(p,extra)
                elif fault=='fifo':p.unlink();os.mkfifo(p,0o600)
                else:extra.write_bytes(raw);p.unlink();p.symlink_to(extra)
                result=self.prepare()
                self.assertEqual(result['source_failures'],dict.fromkeys(m.PRODUCERS,expected))
                self.assertEqual(result['pointers'],{})
                if fault in ('fifo','symlink'):p.unlink();p.write_bytes(raw)
                elif fault=='hash':p.write_bytes(raw)
                if extra.exists():extra.unlink()
                p.chmod(0o600)
        with mock.patch.object(m,'committed_artifact_hash',side_effect=m.Refused('unsafe raw exception must not project')):
            self.assertEqual(self.prepare()['source_failures'],dict.fromkeys(m.PRODUCERS,'source_git_unverified'))
        self.assertEqual(len(self.prepare()['pointers']),3)

    def test_installer_rejects_unrecognized_diagnostic_before_printing_or_publishing(self):
        actual=subprocess.run
        secret='FORBIDDEN_PRIVATE_EXCEPTION_SOURCE'
        def forged(argv,**kwargs):
            result=actual(argv,**kwargs)
            if '--prepare-active-root' in argv:
                value=json.loads(result.stdout);value['source_failures']={'credential_monitor':secret}
                return subprocess.CompletedProcess(argv,0,json.dumps(value).encode())
            return result
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            if argv[0]=='/usr/bin/systemctl':
                text='Environment=\nEnvironmentFiles=\nPassEnvironment=\nUnsetEnvironment=\nNeedDaemonReload=no\n' if argv[1]=='show' else ''
                return subprocess.CompletedProcess(argv,0,text.encode())
            return forged(argv,**kwargs)
        block=(ROOT/'deploy/vm/install-hub2-node.sh').read_text().split("<<'PY'",1)[1].split('\nPY\n',1)[0]
        error=io.StringIO()
        with mock.patch.object(sys,'argv',['installer',str(self.config),'active','apply',str(self.repo),str(self.root)]),mock.patch('subprocess.run',side_effect=run),contextlib.redirect_stderr(error):
            exec(compile(block,'actual-installer-diagnostic-block','exec'),{'__name__':'__main__'})
        self.assertNotIn(secret,error.getvalue());self.assertEqual(self.pointers(),{})
        self.assertIn('node_observer_preparation_unavailable',error.getvalue())

    def test_hardening_preserves_existing_enrollment_history_and_exec_read_bits(self):
        self.installer();pointers=self.pointers()
        history={str(p):p.read_bytes() for path in pointers.values() for p in Path(path).iterdir()}
        target=self.repo/m.ARTIFACTS[0];target.chmod(0o775)
        self.installer();self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o755)
        self.assertEqual(self.pointers(),pointers)
        self.assertTrue(all(Path(p).read_bytes()==data for p,data in history.items()))

    def test_unsafe_hash_or_linked_sources_never_chmod_foreign_inode(self):
        target=self.repo/m.ARTIFACTS[0];raw=target.read_bytes()
        for fault in ('hash','hardlink','symlink'):
            with self.subTest(fault=fault):
                extra=self.base/'outside-fixture'
                target.chmod(0o664)
                if fault=='hash':target.write_bytes(raw+b'unreviewed')
                elif fault=='hardlink':os.link(target,extra)
                else:
                    target.unlink();extra.write_bytes(raw);extra.chmod(0o664);target.symlink_to(extra)
                before={str(p):(p.stat().st_mode,p.read_bytes()) for p in (extra,) if p.exists()}
                _,error=self.installer()
                self.assertEqual(self.pointers(),{});self.assertIn('no new coverage authorized',error)
                if fault!='symlink':self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o664)
                for path,(mode,data) in before.items():self.assertEqual((Path(path).stat().st_mode,Path(path).read_bytes()),(mode,data))
                if fault=='symlink':target.unlink();target.write_bytes(raw)
                elif fault=='hash':target.write_bytes(raw)
                if extra.exists():extra.unlink()
                target.chmod(0o600)

    def test_root_owned_metadata_is_not_waived_and_foreign_owner_never_chmod(self):
        # Simulated fstat/stat ownership ONLY. No privileged chown or sudo.
        self.installer('bootstrap');ns=self.installer_functions
        target=self.repo/m.ARTIFACTS[0];real_fstat=os.fstat;real_stat=os.stat
        def changed(info,uid):
            from types import SimpleNamespace
            fields=('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
            data={name:getattr(info,name) for name in fields};data['st_uid']=uid;return SimpleNamespace(**data)
        for uid in (0,987654):
            with self.subTest(uid=uid):
                target.chmod(0o664)
                def fstat(fd):
                    info=real_fstat(fd)
                    return changed(info,uid) if os.readlink('/proc/self/fd/'+str(fd))==str(target) else info
                def stat_path(path,**kwargs):
                    info=real_stat(path,**kwargs)
                    if str(path)==target.name and 'dir_fd' in kwargs and os.readlink('/proc/self/fd/'+str(kwargs['dir_fd']))==str(target.parent):return changed(info,uid)
                    return info
                with mock.patch('os.fstat',side_effect=fstat),mock.patch('os.stat',side_effect=stat_path):
                    handles,failures=ns['harden_node_sources'](self.repo,self.commit,os.geteuid(),self.env,{})
                    try:
                        if uid==0:self.assertEqual(failures,{})
                        else:self.assertEqual(failures,dict.fromkeys(m.PRODUCERS,'source_file_foreign_owner'))
                    finally:ns['source_close'](handles)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o644 if uid==0 else 0o664)

    def test_git_replace_objects_cannot_authorize_chmod_of_uncommitted_bytes(self):
        relative=m.ARTIFACTS[0];target=self.repo/relative;original=target.read_bytes()
        original_blob=self.git('rev-parse',self.commit+':'+relative).strip()
        changed=original+b'not committed selected source'
        made=subprocess.run(['/usr/bin/git','-C',str(self.repo),'hash-object','-w','--stdin'],
            env=self.env,input=changed,capture_output=True,check=True,timeout=5)
        self.git('replace',original_blob,made.stdout.decode().strip())
        target.write_bytes(changed);target.chmod(0o664)
        self.assertEqual(m.committed_artifact_hash(self.repo,self.commit,relative),hashlib.sha256(original).hexdigest())
        _,error=self.installer()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o664,'even later stager refusal cannot justify chmod of replacement-object bytes')
        self.assertEqual(target.read_bytes(),changed);self.assertEqual(self.pointers(),{})
        self.assertIn('source_hash_mismatch',error)

    def test_identity_race_before_fchmod_never_hardens_replacement_or_publishes(self):
        target=self.repo/m.ARTIFACTS[0];raw=target.read_bytes();target.chmod(0o664)
        original=os.fchmod;armed=[True]
        old=self.base/'renamed-original'
        def race(fd,mode):
            if armed[0] and os.readlink('/proc/self/fd/'+str(fd))==str(target):
                armed[0]=False;target.rename(old);target.write_bytes(raw);target.chmod(0o644)
            return original(fd,mode)
        with mock.patch('os.fchmod',side_effect=race):_,error=self.installer()
        self.assertFalse(armed[0]);self.assertEqual(self.pointers(),{})
        self.assertIn('source_file_changed',error)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o644);self.assertEqual(target.read_bytes(),raw)

    def test_post_child_source_mode_change_and_durability_failure_withhold_publication(self):
        target=self.repo/m.ARTIFACTS[0];target.chmod(0o664)
        _,error=self.installer(after_prepare=lambda:target.chmod(0o664))
        self.assertEqual(self.pointers(),{});self.assertIn('source_file_changed',error)
        real=os.fsync
        def fail(fd):
            if os.readlink('/proc/self/fd/'+str(fd))==str(target):raise OSError('synthetic inode durability failure')
            return real(fd)
        with mock.patch('os.fsync',side_effect=fail):_,error=self.installer()
        self.assertEqual(self.pointers(),{});self.assertIn('no new coverage authorized',error)
        self.installer();self.assertEqual(len(self.pointers()),3)

    def test_source_close_failure_withholds_authority_but_not_business_configuration(self):
        target=self.repo/m.ARTIFACTS[0];real=os.close;armed=[True]
        def close(fd):
            match=armed[0] and os.readlink('/proc/self/fd/'+str(fd))==str(target)
            real(fd)
            if match:armed[0]=False;raise OSError('synthetic consumed close failure')
        with mock.patch('os.close',side_effect=close):_,error=self.installer()
        self.assertFalse(armed[0]);self.assertEqual(self.pointers(),{})
        self.assertIn('no new coverage authorized',error)
        self.assertIn('HUB2_RELEASE_MODE=active',self.config.read_text())

    def test_diagnostic_source_close_failure_isolated_from_credential(self):
        target=self.repo/'hub2/src/model-picker.ts';real=os.close;armed=[True]
        def close(fd):
            match=armed[0] and os.readlink('/proc/self/fd/'+str(fd))==str(target)
            real(fd)
            if match:armed[0]=False;raise OSError('synthetic consumed close failure')
        with mock.patch('os.close',side_effect=close):_,error=self.installer()
        self.assertFalse(armed[0]);self.assertEqual(set(self.pointers()),{m.KEYS['credential_monitor']})
        self.assertIn('source_file_unavailable',error)

    def test_diagnostic_only_bad_source_does_not_block_credential_hardening(self):
        diag=self.repo/'hub2/src/model-picker.ts';diag.write_bytes(diag.read_bytes()+b'foreign bytes');diag.chmod(0o664)
        common=self.repo/m.ARTIFACTS[0];common.chmod(0o664)
        _,error=self.installer()
        self.assertEqual(set(self.pointers()),{m.KEYS['credential_monitor']})
        self.assertEqual(stat.S_IMODE(common.stat().st_mode),0o644)
        self.assertEqual(stat.S_IMODE(diag.stat().st_mode),0o664)
        self.assertIn('source_hash_mismatch',error)

    def test_installer_concurrent_config_write_not_overwritten(self):
        new=b'UNRELATED_SYNTHETIC=concurrent-owner\n'
        with self.assertRaisesRegex(ValueError,'release_configuration_changed'):
            self.installer(after_prepare=lambda:self.config.write_bytes(new))
        self.assertEqual(self.config.read_bytes(),new)
        self.assertEqual(len([p for p in self.root.iterdir() if p.is_dir()]),3)


if __name__=='__main__':unittest.main(verbosity=2)
