"""Real root/service-owner Git regression; executes only the installer Python block."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ('hub2/node/alert-evidence.ts', 'hub2/node/main.ts', 'hub2/src/worker.ts',
             'hub2/src/model-picker.ts', 'hub2/src/routing-pool.ts')
GIT_ENV = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}


class InstallerGitOwnerTests(unittest.TestCase):
    def setUp(self):
        self.block = (ROOT/'deploy/vm/install-hub2-node.sh').read_text().split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
        self.assertEqual(self.block.count('overrides = service_environment()'), 1)
        self.block = self.block.replace('overrides = service_environment()', 'overrides = {}')
        parent = ROOT/'.local/tmp'
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.temporary = None
        self.addCleanup(self.cleanup_fixture)  # Registered before creating any privileged fixture resources.
        self.temporary = tempfile.TemporaryDirectory(prefix='installer-git-owner-', dir=parent)
        self.base = Path(self.temporary.name)
        self.uid = os.geteuid() if os.geteuid() else ROOT.stat().st_uid
        self.gid = os.getegid() if os.geteuid() else ROOT.stat().st_gid
        self.assertNotEqual(self.uid, 0, 'root backend requires a service-owned checkout; do not relax ancestor permissions')
        self.repo = self.base/'checkout'; self.repo.mkdir(mode=0o700)
        for relative in ARTIFACTS:
            p = self.repo/relative; p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            p.write_text('synthetic authority '+relative); p.chmod(0o600)
        stage = self.repo/'scripts/stage-credential-monitor.py'
        stage.parent.mkdir(mode=0o700)
        stage.write_bytes((ROOT/'scripts/stage-credential-monitor.py').read_bytes());stage.chmod(0o700)
        for p in self.repo.rglob('*'):
            if p.is_dir():p.chmod(0o700)
        def git(*args):
            return subprocess.check_output(['/usr/bin/git','-C',str(self.repo),*args],env=GIT_ENV,
                stderr=subprocess.PIPE,text=True,timeout=10,umask=0o077).strip()
        git('init','-q');git('add','.')
        git('-c','user.name=Synthetic Installer Fixture','-c','user.email=fixture@example.invalid',
            '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','synthetic source')
        self.head = git('rev-parse','HEAD')
        self.vars = self.repo/'.dev.vars'
        self.before = 'SYNTHETIC_PRIVATE=unchanged\nHUB2_RELEASE_MODE=bootstrap\nHUB2_ALERT_EVIDENCE_DIR=\nHUB2_SCHEMA_MIGRATIONS=defer\n'
        self.vars.write_text(self.before);self.vars.chmod(0o640)
        self.evidence = self.base/'evidence';self.evidence.mkdir(mode=0o700)
        if os.geteuid()==0:
            for p in [*self.base.rglob('*'), self.base]:os.chown(p,self.uid,self.gid)

    def cleanup_fixture(self):
        if self.temporary is not None:
            self.temporary.cleanup()

    def root_command(self, args):
        if os.geteuid()==0:return args
        sudo = shutil.which('sudo')
        self.assertIsNotNone(sudo, 'required root Git-owner fixture needs sudo -n')
        return [sudo,'-n',*args]

    def invoke_block(self, root):
        command = ['/usr/bin/python3','-I','-S','-',str(self.vars),'active','apply',str(self.repo),str(self.evidence)]
        return subprocess.run(self.root_command(command) if root else command, input=self.block,
            text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=35)

    def assert_staged(self, result):
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'')
        text=self.vars.read_text();self.assertIn('SYNTHETIC_PRIVATE=unchanged\n',text)
        self.assertIn('HUB2_RELEASE_MODE=active\n',text);self.assertIn('HUB2_SCHEMA_MIGRATIONS=apply\n',text)
        entries=[line.split('=',1)[1] for line in text.splitlines() if line.startswith('HUB2_ALERT_EVIDENCE_DIR=')]
        self.assertEqual(len(entries),1)
        destination=Path(entries[0]);self.assertEqual(destination.parent,self.evidence)
        enrollment=json.loads((destination/'enrollment.json').read_text())
        self.assertEqual(enrollment['release_sha'],self.head)
        self.assertEqual(len(enrollment['artifacts']),3)
        for p in [destination,destination/'enrollment.json',destination/'journal.json']:
            info=p.stat();self.assertEqual((info.st_uid,info.st_gid),(self.uid,self.gid))
            self.assertEqual(stat.S_IMODE(info.st_mode),0o700 if p.is_dir() else 0o600)
        info=self.vars.stat();self.assertEqual((info.st_uid,info.st_gid),(self.uid,self.gid))
        self.assertEqual(stat.S_IMODE(info.st_mode),0o640)

    def test_root_git_dubious_owner_control_and_actual_installer_service_stage(self):
        # Real Git, real euid0, no SUDO_UID or safe.directory exemption. The
        # unchanged root lookup fails before parsing checkout configuration.
        code='import subprocess,sys,json; r=subprocess.run(["/usr/bin/git","-C",sys.argv[1],"rev-parse","HEAD"],env='+repr(GIT_ENV)+',capture_output=True,text=True); print(json.dumps({"rc":r.returncode,"err":r.stderr}))'
        control=subprocess.run(self.root_command(['/usr/bin/python3','-I','-S','-c',code,str(self.repo)]),
            capture_output=True,text=True,timeout=10)
        self.assertEqual(control.returncode,0,control.stderr)
        raw=json.loads(control.stdout);self.assertEqual(raw['rc'],128)
        self.assertIn('dubious ownership',raw['err'])
        self.assert_staged(self.invoke_block(root=True))

    def test_nonroot_actual_installer_service_stage_is_unchanged(self):
        command=['/usr/bin/python3','-I','-S','-',str(self.vars),'active','apply',str(self.repo),str(self.evidence)]
        if os.geteuid()==0:
            def drop():
                os.setgroups([self.gid]);os.setresgid(self.gid,self.gid,self.gid);os.setresuid(self.uid,self.uid,self.uid)
            result=subprocess.run(command,input=self.block,text=True,capture_output=True,timeout=35,preexec_fn=drop)
        else:result=self.invoke_block(root=False)
        self.assert_staged(result)


if __name__=='__main__':unittest.main()
