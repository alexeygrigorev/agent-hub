"""Effective optional-observer selection; real updater, fake systemd, no services."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from test import test_schema_install_configuration as configuration_fixture

ROOT = Path(__file__).resolve().parents[1]
KEY = 'HUB2_ALERT_EVIDENCE_DIR'
PROPS = {'Environment':'','EnvironmentFiles':'','PassEnvironment':'','UnsetEnvironment':'','NeedDaemonReload':'no'}


class ObserverSelectionTests(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'.local/tmp';parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='observer-selection-',dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.base.chmod(0o700)
        self.repo,self.head=configuration_fixture.SchemaInstallConfigurationTests._create_synthetic_checkout(self,self.base)
        self.vars=self.repo/'.dev.vars';self.evidence=self.base/'evidence'
        source=(ROOT/'deploy/vm/install-hub2-node.sh').read_text()
        self.block=source.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]

    def invoke(self, text, *, props=None, manager='', fail=False, ambient=None, race=False, show=None, fail_stage=None, timeout_stage=None):
        raw=text.encode() if isinstance(text,str) else text
        self.vars.write_bytes(raw);self.vars.chmod(0o640)
        properties=dict(PROPS,**(props or {}))
        fixture={'properties':properties,'manager':manager,'fail':fail,'race':race,'artifact':str(self.repo/configuration_fixture.ARTIFACTS[0]),'show':show,'fail_stage':fail_stage,'timeout_stage':timeout_stage}
        calls=self.base/'probe-calls.jsonl'
        prefix='''import subprocess,json
_fixture=FIXTURE
_real_run=subprocess.run
def _fake_run(argv,*args,**kwargs):
    if argv and argv[0]=='/usr/bin/systemctl':
        assert kwargs['env']=={'PATH':__import__('os').defpath,'LC_ALL':'C'}
        with open(CALLS,'a') as trace:trace.write(json.dumps(argv)+'\\n')
        if argv[1]==_fixture['timeout_stage']:raise subprocess.TimeoutExpired(argv,5)
        if _fixture['fail'] or argv[1]==_fixture['fail_stage']:return subprocess.CompletedProcess(argv,1,stdout=b'',stderr=b'')
        if argv[1]=='daemon-reload':out=''
        elif argv[1]=='show':out=_fixture['show'] if _fixture['show'] is not None else '\\n'.join(k+'='+v for k,v in _fixture['properties'].items())+'\\n'
        elif argv[1]=='show-environment':out=_fixture['manager']
        else:raise AssertionError('unexpected service command')
        return subprocess.CompletedProcess(argv,0,stdout=out.encode(),stderr=b'')
    if _fixture['race'] and any(str(x).endswith('/scripts/stage-credential-monitor.py') for x in argv):
        p=__import__('pathlib').Path(_fixture['artifact']);p.write_bytes(p.read_bytes()+b'synthetic hash race')
    return _real_run(argv,*args,**kwargs)
subprocess.run=_fake_run
'''.replace('FIXTURE',repr(fixture)).replace('CALLS',repr(str(calls)))
        env=dict(os.environ,**(ambient or {}))
        done=subprocess.run([sys.executable,'-I','-S','-',str(self.vars),'active','apply',str(self.repo),str(self.evidence)],
                            input=prefix+self.block,text=True,capture_output=True,env=env,timeout=35)
        self.assertNotIn('SYNTHETIC_PRIVATE',done.stdout+done.stderr)
        return done

    def assert_unavailable_without_mutation(self, text, **kwargs):
        # Deliberately unavailable evidence must never repair these untrusted files or refuse primary service.
        for rel in configuration_fixture.ARTIFACTS:(self.repo/rel).chmod(0o664)
        target=self.base/'untouched';target.mkdir(mode=0o755)
        self.evidence.symlink_to(target,target_is_directory=True)
        before=(target.stat().st_mode,self.evidence.lstat().st_ino)
        done=self.invoke(text,**kwargs)
        self.assertEqual(done.returncode,0,done.stderr)
        self.assertEqual((target.stat().st_mode,self.evidence.lstat().st_ino),before)
        self.assertEqual(list(target.iterdir()),[])
        result=self.vars.read_bytes()
        source=text.encode() if isinstance(text,str) else text
        expected=source.replace(b'HUB2_RELEASE_MODE=bootstrap',b'HUB2_RELEASE_MODE=active').replace(b'HUB2_SCHEMA_MIGRATIONS=defer',b'HUB2_SCHEMA_MIGRATIONS=apply')
        self.assertEqual(result,expected)
        self.assertEqual(stat.S_IMODE(self.vars.stat().st_mode),0o640)
        self.assertNotIn('zero',done.stdout.lower());self.assertNotIn('healthy',done.stdout.lower())

    def test_unavailable_file_variants_preserve_all_other_bytes_and_untrusted_artifacts(self):
        core=b'# retained\r\nSYNTHETIC_PRIVATE=never-print\r\nHUB2_RELEASE_MODE=bootstrap\r\nHUB2_SCHEMA_MIGRATIONS=defer\r\n'
        for tail in [b'',b'HUB2_ALERT_EVIDENCE_DIR=',b'HUB2_ALERT_EVIDENCE_DIR=""',b"HUB2_ALERT_EVIDENCE_DIR='   '\nHUB2_ALERT_EVIDENCE_DIR=''",b'HUB2_ALERT_EVIDENCE_DIR=/old\n  HUB2_ALERT_EVIDENCE_DIR = ""']:
            with self.subTest(tail=tail):
                self.assert_unavailable_without_mutation(core+tail)
                self.evidence.unlink()
                (self.base/'untouched').rmdir()

    def test_service_empty_override_wins_file_on_and_caller_environment_is_ignored(self):
        text='HUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\nHUB2_ALERT_EVIDENCE_DIR=/old\n'
        self.assert_unavailable_without_mutation(text,props={'Environment':'HUB2_ALERT_EVIDENCE_DIR='},ambient={KEY:'/caller-not-authority'})

    def test_unset_file_ignores_nonempty_caller_environment(self):
        self.assert_prepared(self.invoke('HUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\n',ambient={KEY:'/caller-not-authority'}))

    def assert_prepared(self,done):
        self.assertEqual(done.returncode,0,done.stderr);self.assertNotIn('unavailable',done.stderr)
        keys={'HUB2_ALERT_EVIDENCE_DIR','HUB2_PICKER_EXCLUDED_EVIDENCE_DIR','HUB2_PICKER_QUOTA_STALE_EVIDENCE_DIR'}
        selected=dict(line.split('=',1) for line in self.vars.read_text().splitlines() if '=' in line and line.split('=',1)[0] in keys)
        self.assertEqual(set(selected),keys)
        self.assertEqual(len([p for p in self.evidence.iterdir() if p.is_dir()]),3)
        for key,value in selected.items():
            directory=Path(value);self.assertEqual(directory.parent,self.evidence)
            entry=json.loads((directory/'enrollment.json').read_text())
            self.assertEqual(entry['release_sha'],self.head)
            self.assertEqual(len(entry['artifacts']),3 if key==KEY else 5)
            self.assertTrue((directory/'journal.json').is_file())

    @staticmethod
    def primary_config(text):
        expected=text.replace('HUB2_RELEASE_MODE=bootstrap','HUB2_RELEASE_MODE=active').replace('HUB2_SCHEMA_MIGRATIONS=defer','HUB2_SCHEMA_MIGRATIONS=apply')
        if not any(line.startswith('HUB2_SCHEMA_MIGRATIONS=') for line in text.splitlines()):
            expected='HUB2_SCHEMA_MIGRATIONS=apply\n'+expected
        return expected

    def assert_unknown(self,done,text):
        self.assertEqual(done.returncode,0,done.stderr)
        self.assertIn('no new coverage authorized',done.stderr)
        self.assertEqual(self.vars.read_text(),self.primary_config(text))
        if self.evidence.is_dir():
            self.assertEqual([p for p in self.evidence.iterdir() if p.is_dir()],[])

    def assert_role_held(self,done,text):
        self.assertEqual(done.returncode,0,done.stderr)
        self.assertIn('node_observer_held:credential_monitor:',done.stderr)
        selected=dict(line.split('=',1) for line in self.vars.read_text().splitlines() if '=' in line)
        # Every original credential assignment survives; the two independent
        # diagnostic authorities are materialized by the actual preparer child.
        for line in self.primary_config(text).splitlines():
            self.assertIn(line,self.vars.read_text().splitlines())
        self.assertEqual(len([p for p in self.evidence.iterdir() if p.is_dir()]),2)
        for key in ('HUB2_PICKER_EXCLUDED_EVIDENCE_DIR','HUB2_PICKER_QUOTA_STALE_EVIDENCE_DIR'):
            self.assertTrue((Path(selected[key])/'journal.json').is_file())

    def test_invalid_configured_paths_never_heal_into_fresh_zero(self):
        for value in ['0',"'   '",'"   "','"a\u2028b"','/old\nHUB2_ALERT_EVIDENCE_DIR=" "']:
            with self.subTest(value=value):
                text='SYNTHETIC_PRIVATE=never-print\nHUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\n'+KEY+'='+value+'\n'
                done=self.invoke(text);self.assert_role_held(done,text)

    def test_enabled_0664_authority_is_still_refused_without_changing_configuration(self):
        (self.repo/configuration_fixture.ARTIFACTS[0]).chmod(0o664)
        text='HUB2_RELEASE_MODE=bootstrap\n'
        done=self.invoke(text)
        self.assertEqual(done.returncode,0)
        self.assertIn('no new coverage authorized',done.stderr)
        self.assertEqual(self.vars.read_text(),self.primary_config(text))

    def test_enabled_symlink_hardlink_and_hash_race_remain_refused(self):
        artifact=self.repo/configuration_fixture.ARTIFACTS[0]
        original=artifact.read_bytes()
        for fault in ['symlink','hardlink','hash_race']:
            with self.subTest(fault=fault):
                linked=self.base/'linked-artifact'
                if fault=='symlink':
                    linked.write_bytes(original);artifact.unlink();artifact.symlink_to(linked)
                elif fault=='hardlink':os.link(artifact,linked)
                text='HUB2_RELEASE_MODE=bootstrap\n'
                done=self.invoke(text,race=fault=='hash_race')
                self.assertEqual(done.returncode,0)
                self.assertIn('no new coverage authorized',done.stderr)
                self.assertEqual(self.vars.read_text(),self.primary_config(text))
                if fault=='symlink':artifact.unlink();artifact.write_bytes(original)
                elif fault=='hash_race':artifact.write_bytes(original)
                linked.unlink(missing_ok=True);artifact.chmod(0o600)

    def test_known_environment_override_holds_only_credential_producer(self):
        for value in ('/fixed','   ',''):
            with self.subTest(value=value):
                text='HUB2_RELEASE_MODE=bootstrap\n'+KEY+'=""\n'
                done=self.invoke(text,props={'Environment':'"'+KEY+'='+value+'"'})
                self.assert_role_held(done,text)
                self.assertIn(':service_override;',done.stderr)

    def test_nonempty_or_ambiguous_service_overrides_fail_before_staging(self):
        cases=[{'PassEnvironment':KEY},{'UnsetEnvironment':KEY},{'UnsetEnvironment':KEY+'=/old'},
               {'NeedDaemonReload':'yes'}]
        for props in cases:
            with self.subTest(props=props):
                text='HUB2_RELEASE_MODE=bootstrap\n'+KEY+'=""\n'
                done=self.invoke(text,props=props)
                self.assertEqual(done.returncode,0)
                self.assertIn('no new coverage authorized',done.stderr)
                self.assertEqual(self.vars.read_text(),self.primary_config(text))
                self.assertFalse(self.evidence.exists())
        done=self.invoke('HUB2_RELEASE_MODE=bootstrap\n',manager=KEY+'=/manager\n')
        self.assertEqual(done.returncode,0);self.assertFalse(self.evidence.exists())

    def test_unrelated_service_environment_files_and_names_are_not_a_gate(self):
        file=self.base/'service.env';file.write_text('UNRELATED=one\nAUTH_FAKE=not-a-credential\n')
        props={'Environment':'UNRELATED=1','EnvironmentFiles':str(file)+' (ignore_errors=no)',
               'PassEnvironment':'UNRELATED','UnsetEnvironment':'UNRELATED=one'}
        self.assert_prepared(self.invoke('HUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\n',props=props,manager='UNRELATED=2\n'))

    def test_target_environment_file_is_explicitly_unsupported_not_silently_off(self):
        file=self.base/'service.env';file.write_text(KEY+'=""\n')
        text='HUB2_RELEASE_MODE=bootstrap\n'+KEY+'=/on\n'
        done=self.invoke(text,props={'EnvironmentFiles':str(file)+' (ignore_errors=no)'})
        self.assertEqual(done.returncode,0)
        self.assertEqual(self.vars.read_text(),self.primary_config(text));self.assertFalse(self.evidence.exists())
        file.write_text('HUB2_ALERT_' + chr(92) + '\nEVIDENCE_DIR=/hidden\n')
        done=self.invoke(text,props={'EnvironmentFiles':str(file)+' (ignore_errors=no)'})
        self.assertEqual(done.returncode,0)
        self.assertIn('no new coverage authorized',done.stderr)
        self.assertEqual(self.vars.read_text(),self.primary_config(text));self.assertFalse(self.evidence.exists())

    def test_actual_systemd255_omitted_empty_array_allows_default_active_staging(self):
        # Observed formatter shape: rc0, four scalar lines, no EnvironmentFiles.
        show='Environment=NODE_ENV=production TMPDIR=/synthetic/tmp HOME=/synthetic/home\nPassEnvironment=\nUnsetEnvironment=\nNeedDaemonReload=no\n'
        self.assert_prepared(self.invoke('HUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\n',show=show))

    def test_omitted_empty_array_stages_all_default_authorities(self):
        show='Environment=NODE_ENV=production\nPassEnvironment=\nUnsetEnvironment=\nNeedDaemonReload=no\n'
        done=self.invoke('HUB2_RELEASE_MODE=bootstrap\n',show=show)
        self.assertEqual(done.returncode,0,done.stderr)
        selected=[line for line in self.vars.read_text().splitlines() if line.startswith(KEY+'=')]
        self.assertEqual(len(selected),1)
        directory=Path(selected[0].split('=',1)[1])
        self.assertEqual(directory.parent,self.evidence)
        self.assertTrue((directory/'enrollment.json').is_file())
        self.assertTrue((directory/'journal.json').is_file())

    def test_repeated_file_rows_validate_every_file_including_earlier_target(self):
        first=self.base/'first.env';second=self.base/'second.env'
        first.write_text(KEY+'=/earlier-must-not-be-lost\n');second.write_text('UNRELATED=one\n')
        scalar='Environment=\nPassEnvironment=\nUnsetEnvironment=\nNeedDaemonReload=no\n'
        show=scalar+f'EnvironmentFiles={first} (ignore_errors=no)\nEnvironmentFiles={second} (ignore_errors=no)\n'
        text='HUB2_RELEASE_MODE=bootstrap\n'+KEY+'=/on\n'
        done=self.invoke(text,show=show)
        self.assertEqual(done.returncode,0)
        self.assertIn('no new coverage authorized',done.stderr)
        self.assertEqual(self.vars.read_text(),self.primary_config(text));self.assertFalse(self.evidence.exists())
        first.write_text('UNRELATED=two\n')
        self.assert_prepared(self.invoke('HUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\n',show=show))

    def test_only_environment_files_may_be_absent_or_repeated(self):
        scalar={'Environment':'','PassEnvironment':'','UnsetEnvironment':'','NeedDaemonReload':'no'}
        cases=[]
        for key in scalar:
            cases.append(''.join(k+'='+v+'\n' for k,v in scalar.items() if k!=key))
            cases.append(''.join(k+'='+v+'\n' for k,v in scalar.items())+key+'='+scalar[key]+'\n')
        standard=''.join(k+'='+v+'\n' for k,v in scalar.items())
        cases.extend([standard+'Unexpected=\n',standard+'EnvironmentFiles=[]\n',
                      standard+'EnvironmentFiles=\nEnvironmentFiles=/synthetic (ignore_errors=no)\n',
                      standard+'EnvironmentFiles=malformed\nEnvironmentFiles=\n'])
        text='HUB2_RELEASE_MODE=bootstrap\n'+KEY+'=/on\n'
        for show in cases:
            with self.subTest(shape=show):
                done=self.invoke(text,show=show)
                self.assertEqual(done.returncode,0)
                self.assertIn('no new coverage authorized',done.stderr)
                self.assertEqual(self.vars.read_text(),self.primary_config(text));self.assertFalse(self.evidence.exists())

    def test_each_metadata_command_failure_and_timeout_is_still_unknown(self):
        text='HUB2_RELEASE_MODE=bootstrap\n'+KEY+'=/on\n'
        for stage in ['daemon-reload','show','show-environment']:
            for option in ['fail_stage','timeout_stage']:
                with self.subTest(stage=stage,kind=option):
                    done=self.invoke(text,**{option:stage})
                    self.assertEqual(done.returncode,0)
                    self.assertIn('no new coverage authorized',done.stderr)
                    self.assertEqual(self.vars.read_text(),self.primary_config(text));self.assertFalse(self.evidence.exists())

    def test_metadata_failure_is_unknown_and_cannot_disable_selected_observer(self):
        text='HUB2_RELEASE_MODE=bootstrap\n'+KEY+'=/on\n'
        done=self.invoke(text,fail=True)
        self.assertEqual(done.returncode,0);self.assertEqual(self.vars.read_text(),self.primary_config(text))
        self.assertFalse(self.evidence.exists())


if __name__=='__main__':unittest.main()
