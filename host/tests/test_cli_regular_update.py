"""Synthetic owned-files only. AUTHORED UNRUN; never inspect the real CLI/root."""
import argparse
import errno
import importlib.util
import io
import sys
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest import mock
import zipfile
from agent_hub_host import regular_cli as r


def artifact(generation):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('agent_hub_host/_build_info.py','BUILD_COMMIT = '+repr(generation)+'\n')
    data=output.getvalue()
    return data,{'host_version':generation,'artifacts':{'hub.pyz':{'sha256':r.digest(data),'size':len(data)}}}


@unittest.skipUnless(sys.platform=='linux','Linux regular-file publication contract')
class RegularCliTests(unittest.TestCase):
    def setUp(self):
        repo=Path(__file__).resolve().parents[2]
        # Required CI's HOME may have /tmp ancestry; never weaken production
        # guards or mutate shared ancestor modes to make the fixture pass.
        for directory in (repo/'.local',repo/'.local/tmp'):
            directory.mkdir(mode=0o700,exist_ok=True)
        scratch=repo/'.local/tmp'/('regular-cli-unit-'+str(uuid.uuid4()))
        scratch.mkdir(mode=0o700)
        self.addCleanup(scratch.rmdir)
        self.temp=tempfile.TemporaryDirectory(prefix='regular-cli-owned-',dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);os.chmod(self.base,0o700)
        self.root=self.base/'state';self.target=self.base/'bin/hub'
        self.state=r.state_path(self.root,self.target)
        self.a,self.ma=artifact('a'*40);self.b,self.mb=artifact('b'*40)
    def create(self):
        return r.publish(self.a,self.ma,self.target,self.root,create=True)
    def test_absent_creation_verified_upgrade_and_original_inode_retained(self):
        self.assertEqual(self.create()['status'],'published')
        before=self.target.stat()
        result=r.publish(self.b,self.mb,self.target,self.root)
        retained=self.target.parent/result['retained']
        self.assertEqual(retained.stat().st_ino,before.st_ino)
        self.assertEqual(retained.read_bytes(),self.a)
        self.assertEqual(self.target.read_bytes(),self.b)
        self.assertTrue(stat.S_ISREG(self.target.lstat().st_mode))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode),0o755)
    def test_same_release_is_current_without_new_stage_or_inode(self):
        self.create();before=self.target.stat().st_ino
        stages=list(self.target.parent.glob('.hub-managed-*'))
        self.assertEqual(r.publish(self.a,self.ma,self.target,self.root)['status'],'current')
        self.assertEqual(self.target.stat().st_ino,before)
        self.assertEqual(list(self.target.parent.glob('.hub-managed-*')),stages)
    def test_unknown_existing_target_never_enrolled_by_publisher(self):
        self.target.parent.mkdir(mode=0o700);self.target.write_bytes(b'operator-owned')
        os.chmod(self.target,0o755);before=self.target.stat().st_ino
        self.assertEqual(r.publish(self.a,self.ma,self.target,self.root,create=True),{'status':'unmanaged'})
        self.assertEqual(self.target.stat().st_ino,before)
        self.assertFalse((self.state/r.OWNER).exists())
    def test_ownership_drift_does_not_fall_through_unmanaged(self):
        self.create();self.target.write_bytes(b'foreign')
        self.assertTrue(r.selected(self.root,self.target))
        with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root)
        self.assertEqual(self.target.read_bytes(),b'foreign')
    def test_invalid_size_hash_generation_and_duplicate_build_member_refuse(self):
        for data,m in [(self.a,{**self.ma,'host_version':'b'*40}),
                       (self.a,{'host_version':'a'*40,'artifacts':{'hub.pyz':{'sha256':'0'*64,'size':len(self.a)}}}),
                       (self.a,{'host_version':'a'*40,'artifacts':{'hub.pyz':{'sha256':r.digest(self.a),'size':True}}})]:
            with self.assertRaises(r.Refused):r.publish(data,m,self.target,self.root,create=True)
        duplicate=io.BytesIO()
        with zipfile.ZipFile(duplicate,'w') as archive:
            archive.writestr('agent_hub_host/_build_info.py',"BUILD_COMMIT = '"+'a'*40+"'\n")
            archive.writestr('agent_hub_host/_build_info.py',"BUILD_COMMIT = '"+'a'*40+"'\n")
        payload=duplicate.getvalue();manifest={'host_version':'a'*40,'artifacts':{'hub.pyz':{'sha256':r.digest(payload),'size':len(payload)}}}
        with self.assertRaises(r.Refused):r.publish(payload,manifest,self.target,self.root,create=True)
        self.assertFalse(self.target.exists())
    def test_symlink_hardlink_and_mode_drift_refuse(self):
        self.create();backup=self.base/'old';os.link(self.target,backup)
        with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root)
        backup.unlink();self.target.chmod(0o777)
        with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root)
        self.target.unlink();self.target.symlink_to(backup)
        with self.assertRaises(OSError):r.publish(self.b,self.mb,self.target,self.root)
    def test_two_cooperating_writers_contend_on_same_stable_lock(self):
        self.create();fds=r.FDs();fd=r.directory(self.root,fds)
        errors=[]
        try:
            with r.writer(fd,fds):
                def other():
                    try:r.publish(self.b,self.mb,self.target,self.root)
                    except BaseException as exc:errors.append(exc)
                thread=threading.Thread(target=other);thread.start();thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],BlockingIOError)
                self.assertEqual(self.target.read_bytes(),self.a)
        finally:fds.close()
    def test_post_exchange_foreign_target_is_retained_unknown_never_blind_rollback(self):
        self.create();actual=r.exchange
        def replace(parent,left,right,flags=2):
            result=actual(parent,left,right,flags)
            if flags==2 and right=='hub':
                foreign=self.target.parent/'foreign';foreign.write_bytes(b'foreign')
                os.replace(foreign,self.target)
            return result
        with mock.patch.object(r,'exchange',replace):
            with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root)
        self.assertEqual(self.target.read_bytes(),b'foreign')
        pending=json.loads((self.state/r.PENDING).read_text())
        self.assertEqual((self.target.parent/pending['stage']).read_bytes(),self.a)
        with self.assertRaises(r.Refused):r.rollback(self.target,self.root,pending)
    def test_partial_publication_conditional_rollback_restores_exact_original_inode(self):
        self.create();inode=self.target.stat().st_ino;actual=r.write_record
        def fail(parent,name,*args,**kw):
            if name==r.OWNER:raise OSError('synthetic durable failure')
            return actual(parent,name,*args,**kw)
        with mock.patch.object(r,'write_record',fail):
            with self.assertRaises(OSError):r.publish(self.b,self.mb,self.target,self.root)
        pending=json.loads((self.state/r.PENDING).read_text())
        self.assertEqual(r.rollback(self.target,self.root,pending),{'status':'rolled_back'})
        self.assertEqual(self.target.stat().st_ino,inode)
        self.assertEqual(self.target.read_bytes(),self.a)
    def test_before_publication_fsync_expiry_does_not_publish(self):
        self.create();clock=[0.0];actual=r.write_record
        def slow(parent,name,*args,**kw):
            result=actual(parent,name,*args,**kw)
            if name==r.PENDING:clock[0]=21
            return result
        with mock.patch.object(r,'write_record',slow):
            with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root,clock=lambda:clock[0])
        self.assertEqual(self.target.read_bytes(),self.a)
        self.assertTrue((self.state/r.PENDING).exists())
    def test_final_close_lateness_never_returns_success(self):
        self.create();clock=[0.0];actual=r.FDs.close
        def slow(fds):actual(fds);clock[0]=21
        with mock.patch.object(r.FDs,'close',slow):
            with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root,clock=lambda:clock[0])
        self.assertEqual(self.target.read_bytes(),self.b)  # Effect happened; outcome UNKNOWN, not rolled back blindly.
    def test_backward_clock_refuses_before_publication(self):
        self.create();values=iter([100,100,0])
        with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root,clock=lambda:next(values,0))
        self.assertEqual(self.target.read_bytes(),self.a)
    def test_pending_uncertainty_never_blindly_retries_and_explicit_reconcile_only_checks_owned_effect(self):
        self.create();actual=r.write_record
        def fail(parent,name,*args,**kwargs):
            if name==r.OWNER:raise OSError('synthetic late commit failure')
            return actual(parent,name,*args,**kwargs)
        with mock.patch.object(r,'write_record',fail):
            with self.assertRaises(OSError):r.publish(self.b,self.mb,self.target,self.root)
        with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root)
        self.assertEqual(r.reconcile(self.target,self.root),{'status':'reconciled_published'})
        self.assertEqual(r.publish(self.b,self.mb,self.target,self.root)['status'],'current')
    def test_close_consumes_each_fd_once_even_if_close_raises_after_consumption(self):
        fds=r.FDs();a=fds.add(os.open(self.base,os.O_RDONLY|os.O_DIRECTORY));b=fds.add(os.dup(a));real=os.close;seen=[]
        def close(fd):
            seen.append(fd);real(fd)
            if fd==b:raise OSError('synthetic consumed-close failure')
        with mock.patch.object(r.os,'close',close):
            with self.assertRaises(OSError):fds.close()
        self.assertEqual(seen,[b,a]);self.assertEqual(fds.values,[])


    def test_invalid_record_or_record_directory_never_selects_legacy_fallback(self):
        self.create();(self.state/r.OWNER).write_text('{"target":"foreign"}')
        self.assertTrue(r.selected(self.root,self.target))
        with self.assertRaises(r.Refused):r.publish(self.b,self.mb,self.target,self.root)
        other=self.base/'unrelated/hub'
        self.assertFalse(r.selected(self.root,other))
    def test_owned_manual_cmd_update_uses_actual_shared_publication(self):
        from agent_hub_host import cli
        home=self.base/'home';home.mkdir(mode=0o700)
        owned_root=home/'.local/share/agent-hub2'
        r.publish(self.a,self.ma,self.target,owned_root,create=True)
        before=self.target.stat().st_ino
        public={'built_from_commit':'b'*40,'version':'synthetic','sha256':r.digest(self.b)}
        values=[json.dumps(public).encode(),self.b,json.dumps(self.mb).encode()]
        class Response(io.BytesIO):status=200
        def urlopen(*args,**kwargs):return Response(values.pop(0))
        args=argparse.Namespace(url='https://fixture.invalid',check=False,force=False,install_dir=None)
        with mock.patch.dict(os.environ,{'HOME':str(home)}),mock.patch.object(sys,'argv',[str(self.target)]),mock.patch.object(cli,'get_build_info',return_value={'commit':'a'*40,'version':'synthetic'}),mock.patch('urllib.request.urlopen',urlopen),mock.patch.object(sys,'stdout',io.StringIO()):
            cli.cmd_update(args)
        self.assertEqual(values,[])
        self.assertEqual(self.target.read_bytes(),self.b)
        saved=json.loads((r.state_path(owned_root,self.target)/r.OWNER).read_text())
        self.assertEqual(saved['artifact']['generation'],'b'*40)
        retained=[p for p in self.target.parent.glob('.hub-managed-*') if p.stat().st_ino==before]
        self.assertEqual(len(retained),1)
    def test_background_component_absent_no_spawn_and_timeout_nonfatal(self):
        path=Path(__file__).resolve().parents[2]/'scripts/hub2-self-update.py'
        spec=importlib.util.spec_from_file_location('regular_cli_fake_updater',path)
        updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)
        with mock.patch.object(updater.subprocess,'run',side_effect=AssertionError('must not spawn')):
            self.assertEqual(updater.update_regular_operator_cli(self.root,self.base,self.ma),'unmanaged')
        self.create();release=self.base/'release';release.mkdir(mode=0o700)
        (release/'hub.pyz').write_bytes(self.b);(release/'release.json').write_text(json.dumps(self.mb))
        calls=[]
        def timeout(argv,**kwargs):
            calls.append((argv,kwargs));raise updater.subprocess.TimeoutExpired(argv,22)
        with mock.patch.object(updater.subprocess,'run',timeout),mock.patch.object(sys,'stdout',io.StringIO()):
            self.assertEqual(updater.update_regular_operator_cli(self.root,release,self.mb),'unknown')
        self.assertEqual(len(calls),1)
        self.assertIn('-I',calls[0][0]);self.assertIn('-S',calls[0][0])
        self.assertEqual(calls[0][1]['timeout'],22)
        self.assertEqual(self.target.read_bytes(),self.a)
    def test_installer_and_updater_keep_original_managed_link_paths(self):
        # Source wiring coverage complements actual publication tests; not a new
        # provider/process harness. Existing collected installer assertions stay.
        repo=Path(__file__).resolve().parents[2]
        deploy=(repo/'deploy/host-install.sh').read_text()
        self.assertIn('if [ ! -e "$HOME/.local/bin/hub" ] && [ ! -L "$HOME/.local/bin/hub" ]; then',deploy)
        self.assertIn('ln -s "$ROOT/current/hub.pyz" "$HOME/.local/bin/hub"',deploy)
        shell=(repo/'public/cli/install.sh').read_text()
        self.assertIn('regular_cli',shell)
        self.assertIn('host-release.json',shell)
        self.assertIn('mv -f "$TMP_BIN" "$INSTALL_DIR/hub"',shell)

    def test_existing_owner_null_is_invalid_and_cannot_establish_new_ownership(self):
        fds=r.FDs()
        try:r.directory(self.state,fds,create=True)
        finally:fds.close()
        owner=self.state/r.OWNER;owner.write_bytes(b'null');owner.chmod(0o600)
        inode=owner.stat().st_ino
        with self.assertRaises(r.Refused):r.publish(self.a,self.ma,self.target,self.root,create=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(owner.read_bytes(),b'null');self.assertEqual(owner.stat().st_ino,inode)
    def test_existing_pending_null_is_invalid_for_publish_reconcile_and_rollback(self):
        self.create();pending=self.state/r.PENDING;pending.write_bytes(b'null');pending.chmod(0o600)
        inode=self.target.stat().st_ino
        for call in (lambda:r.publish(self.b,self.mb,self.target,self.root),
                     lambda:r.reconcile(self.target,self.root),
                     lambda:r.rollback(self.target,self.root,None)):
            with self.assertRaises(r.Refused):call()
        self.assertEqual(self.target.stat().st_ino,inode)
        self.assertEqual(self.target.read_bytes(),self.a);self.assertEqual(pending.read_bytes(),b'null')
    def test_real_unprivileged_permission_denial_blocks_both_installer_and_manual_writer(self):
        from agent_hub_host import cli
        self.assertNotEqual(os.geteuid(),0,'DAC regression requires the ordinary unprivileged test lane')
        home=self.base/'home';home.mkdir(mode=0o700);root=home/'.local/share/agent-hub2'
        r.publish(self.a,self.ma,self.target,root,create=True)
        inode=self.target.stat().st_ino;blocked=root/'regular-cli';mode=stat.S_IMODE(blocked.stat().st_mode)
        public={'built_from_commit':'b'*40,'version':'synthetic','sha256':r.digest(self.b)}
        values=[json.dumps(public).encode(),self.b]
        class Response(io.BytesIO):status=200
        def urlopen(*args,**kwargs):return Response(values.pop(0))
        args=argparse.Namespace(url='https://fixture.invalid',check=False,force=False,install_dir=None)
        blocked.chmod(0)
        try:
            with self.assertRaises(PermissionError):r.selected(root,self.target)
            # This is the exact selection API used by the verified shell module.
            with self.assertRaises(PermissionError):r.install_decision(root,self.target)
            with mock.patch.dict(os.environ,{'HOME':str(home)}),mock.patch.object(sys,'argv',[str(self.target)]),mock.patch.object(cli,'get_build_info',return_value={'commit':'a'*40,'version':'synthetic'}),mock.patch('urllib.request.urlopen',urlopen),mock.patch.object(sys,'stdout',io.StringIO()):
                with self.assertRaises(PermissionError):cli.cmd_update(args)
            self.assertEqual(self.target.stat().st_ino,inode)
            self.assertEqual(self.target.read_bytes(),self.a)
        finally:blocked.chmod(mode)
    def test_injected_selection_eacces_and_invalid_state_never_mean_absent(self):
        self.create();actual=r.os.open
        def denied(path,*args,**kwargs):
            if path=='regular-cli':raise PermissionError(errno.EACCES,'synthetic inaccessible ancestor')
            return actual(path,*args,**kwargs)
        with mock.patch.object(r.os,'open',denied):
            with self.assertRaises(PermissionError):r.selected(self.root,self.target)
            with self.assertRaises(PermissionError):r.install_decision(self.root,self.target)
        # A real symlink at the selected state is unknown, not a missing record.
        saved=self.state.with_name('saved-state');self.state.rename(saved);self.state.symlink_to(saved,target_is_directory=True)
        with self.assertRaises(OSError):r.selected(self.root,self.target)
        with self.assertRaises(OSError):r.install_decision(self.root,self.target)
        self.assertEqual(self.target.read_bytes(),self.a)
    def test_true_absence_retains_explicit_unmanaged_manual_update(self):
        from agent_hub_host import cli
        home=self.base/'home';home.mkdir(mode=0o700);root=home/'.local/share/agent-hub2'
        self.target.parent.mkdir(mode=0o700);self.target.write_bytes(self.a);self.target.chmod(0o755)
        self.assertFalse(r.selected(root,self.target))
        self.assertEqual(r.install_decision(root,self.target),'unmanaged')
        self.assertEqual(r.install_decision(root,self.base/'absent/hub'),'new')
        public={'built_from_commit':'b'*40,'version':'synthetic','sha256':r.digest(self.b)}
        values=[json.dumps(public).encode(),self.b]
        class Response(io.BytesIO):status=200
        def urlopen(*args,**kwargs):return Response(values.pop(0))
        args=argparse.Namespace(url='https://fixture.invalid',check=False,force=False,install_dir=None)
        with mock.patch.dict(os.environ,{'HOME':str(home)}),mock.patch.object(sys,'argv',[str(self.target)]),mock.patch.object(cli,'get_build_info',return_value={'commit':'a'*40,'version':'synthetic'}),mock.patch('urllib.request.urlopen',urlopen),mock.patch.object(sys,'stdout',io.StringIO()):
            cli.cmd_update(args)
        self.assertEqual(values,[]);self.assertEqual(self.target.read_bytes(),self.b)
        self.assertFalse(r.selected(root,self.target))
    def test_shell_selection_delegates_to_verified_closed_python_decision(self):
        repo=Path(__file__).resolve().parents[2]
        shell=(repo/'public/cli/install.sh').read_text()
        branch=shell.split('OWNED_ROOT=',1)[1].split('else\n  # Atomic user-local install:',1)[0]
        self.assertIn('--select-install',branch)
        self.assertIn('|| fail "regular CLI ownership selection unavailable; no manual fallback"',branch)
        self.assertIn('owned|new|unmanaged',branch)
        self.assertNotIn('[ -e',branch);self.assertNotIn('[ -L',branch)

    def test_real_shell_installer_permission_refusal_preserves_owned_regular_inode(self):
        self.assertNotEqual(os.geteuid(),0,'DAC regression requires the ordinary unprivileged test lane')
        home=self.base/'shell-home';home.mkdir(mode=0o700)
        root=home/'.local/share/agent-hub2';target=home/'bin/hub'
        r.publish(self.a,self.ma,target,root,create=True)
        inode=target.stat().st_ino
        assets=self.base/'assets';assets.mkdir(mode=0o700)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('agent_hub_host/__init__.py','')
            archive.writestr('agent_hub_host/regular_cli.py',Path(r.__file__).read_bytes())
            archive.writestr('agent_hub_host/_build_info.py',"BUILD_COMMIT = '"+'b'*40+"'\n")
            archive.writestr('__main__.py','raise SystemExit(0)\n')
        payload=output.getvalue();checksum=r.digest(payload)
        release={'host_version':'b'*40,'artifacts':{'hub.pyz':{'sha256':checksum,'size':len(payload)}}}
        self.assertEqual(r.verified(payload,release)['generation'],'b'*40)
        self.assertEqual(r.install_decision(root,target),'owned')
        (assets/'hub.pyz').write_bytes(payload)
        (assets/'hub.pyz.sha256').write_text(checksum+'  hub.pyz\n')
        (assets/'manifest.json').write_text(json.dumps({'built_from_commit':'b'*40,'sha256':checksum}))
        (assets/'host-release.json').write_text(json.dumps(release))
        fakebin=self.base/'fake-bin';fakebin.mkdir(mode=0o700)
        (fakebin/'python3').symlink_to(sys.executable)
        fakecurl=fakebin/'curl'
        fakecurl.write_text('#!'+sys.executable+'\n'+
            'import pathlib,sys\nargs=sys.argv[1:]\n'+
            'urls=[x for x in args if x.startswith("https://fixture.invalid/cli/")]\n'+
            'assert len(urls)==1 and "-o" in args\n'+
            'name=urls[0].rsplit("/",1)[1]\n'+
            'assert name in ("hub.pyz","hub.pyz.sha256","manifest.json","host-release.json")\n'+
            'pathlib.Path(args[args.index("-o")+1]).write_bytes((pathlib.Path('+repr(str(assets))+')/name).read_bytes())\n')
        fakecurl.chmod(0o700)
        cache=home/'cache';cache.mkdir(mode=0o700)
        scratch=self.base/'shell-tmp';scratch.mkdir(mode=0o700)
        env={'HOME':str(home),'PATH':str(fakebin)+os.pathsep+os.defpath,
             'XDG_CACHE_HOME':str(cache),'TMPDIR':str(scratch),
             'HUB_BASE_URL':'https://fixture.invalid','HUB_INSTALL_DIR':str(target.parent)}
        for name in ('AGENT_HUB_HEAVY_TEST_HELD','AGENT_HUB_HEAVY_TEST_DIR'):
            if name in os.environ:env[name]=os.environ[name]
        blocked=root/'regular-cli';mode=stat.S_IMODE(blocked.stat().st_mode)
        script=Path(__file__).resolve().parents[2]/'public/cli/install.sh'
        process=None;blocked.chmod(0)
        try:
            process=subprocess.Popen(['/bin/sh',str(script)],env=env,stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,start_new_session=True)
            stdout,stderr=process.communicate(timeout=10)
            self.assertLess(len(stdout),65536);self.assertLess(len(stderr),65536)
            self.assertNotEqual(process.returncode,0)
            self.assertEqual(target.stat().st_ino,inode)
            self.assertEqual(target.read_bytes(),self.a)
        finally:
            # Only our own still-unreaped group; no foreign/global process lookup.
            try:
                if process is not None:
                    if process.poll() is None:
                        os.killpg(process.pid,signal.SIGKILL)
                    process.wait(timeout=2)
                    for pipe in (process.stdout,process.stderr):
                        if pipe is not None:pipe.close()
            finally:blocked.chmod(mode)


if __name__=='__main__':unittest.main()
