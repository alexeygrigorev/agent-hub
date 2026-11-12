"""Actual-filesystem, local-Git staging regressions; no services or providers."""
import array
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import socket
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'scripts/stage-picker-diagnostic-evidence.py'
spec = importlib.util.spec_from_file_location('diagnostic_stage', HELPER)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
TOKEN = 'a' * 32


def kill_owned_pidfd(pidfd):
    # Some supported CPython builds omit the os/signal pidfd wrappers.
    libc = ctypes.CDLL(None, use_errno=True)
    send = libc.pidfd_send_signal
    send.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    send.restype = ctypes.c_int
    if send(pidfd, signal.SIGKILL, None, 0) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def owned_process_alive(pid, pidfd):
    # pidfd pins the owned child even if its numeric PID is later reused.
    if select.select([pidfd], [], [], 0)[0]:
        return False
    try:
        return Path('/proc', str(pid), 'stat').read_text().split(') ', 1)[1].split()[0] != 'Z'
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ESRCH):
            return False
        raise


class DiagnosticStaging(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tmp'; parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix='diagnostic-stage-', dir=parent))
        self.addCleanup(shutil.rmtree, self.base)
        self.repo = self.base / 'checkout'; self.repo.mkdir(mode=0o700)
        (self.repo/'hub2').mkdir(mode=0o700)
        self.parent = self.base / 'private'; self.parent.mkdir(mode=0o700)
        for relative in m.ARTIFACTS:
            file = self.repo / relative; file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            file.write_bytes(('synthetic ' + relative + '\n').encode()); file.chmod(0o600)
        self.git('init', '-q'); self.git('add', '.')
        self.git('-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'synthetic source')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(self.repo), *args],
            env={'PATH':os.defpath,'HOME':str(self.base),'GIT_CONFIG_NOSYSTEM':'1',
                 'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null'}, stderr=subprocess.DEVNULL, text=True, umask=0o077)

    def stage(self, **changes):
        return m.stage(changes.get('checkout', self.repo), changes.get('parent', self.parent),
                       changes.get('commit', self.commit), changes.get('transaction_token', TOKEN))

    def refused(self, **changes):
        with self.assertRaises((m.Refused, OSError, ValueError)):
            self.stage(**changes)

    def test_exact_pair_private_files_and_caller_safe_durable_receipt(self):
        result = self.stage()
        self.assertIs(result['staged'], True); self.assertIs(result['activated'], False)
        self.assertIs(result['historical_coverage'], False); self.assertIs(result['caller_must_require_exit0'], True)
        self.assertEqual(result['release_sha'], self.commit); self.assertEqual(result['transaction_token'], TOKEN)
        final = self.parent / TOKEN
        self.assertEqual(set(p.name for p in self.parent.iterdir()), {TOKEN, TOKEN+'.claim'})
        self.assertEqual(set(p.name for p in final.iterdir()), {*m.PRODUCERS, 'publication.json'})
        raw = (final/'publication.json').read_bytes(); receipt = json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['publication_receipt_sha256'])
        self.assertEqual(m.canonical(receipt)+b'\n', raw)
        self.assertEqual(receipt, {k:v for k,v in result.items() if k not in ('status','publication_receipt_sha256')})
        ids = []
        for producer in m.PRODUCERS:
            directory = final/producer; self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for file in directory.iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600); self.assertEqual(file.stat().st_uid, os.geteuid())
                self.assertEqual(file.stat().st_gid, os.getegid()); self.assertEqual(file.stat().st_nlink, 1)
                self.assertEqual(file.read_bytes(), m.canonical(json.loads(file.read_bytes()))+b'\n')
            e = json.loads((directory/'enrollment.json').read_bytes()); j = json.loads((directory/'journal.json').read_bytes())
            self.assertEqual(e['kind'], 'hub_picker_diagnostic_enrollment_v1'); self.assertEqual(j['kind'], 'hub_picker_diagnostic_journal_v1')
            self.assertEqual(e['producer'], producer); self.assertEqual(j['producer'], producer)
            self.assertEqual(result['producers'][producer]['enrollment_sha256'], m.sha((directory/'enrollment.json').read_bytes()))
            self.assertEqual(result['producers'][producer]['journal_sha256'], m.sha((directory/'journal.json').read_bytes()))
            self.assertEqual(e['source_id'], m.sha(m.canonical(e['artifacts'])))
            self.assertEqual(e['source_id'], j['source_id']); self.assertEqual(e['enrollment_id'], j['enrollment_id'])
            self.assertEqual([r['path'] for r in e['artifacts']], [str(self.repo/p) for p in m.ARTIFACTS])
            self.assertEqual((j['occurrence_count'],j['started_total'],j['finished_total']), (0,0,0))
            ids.append(e['enrollment_id'])
        self.assertEqual(len(set(ids)),2)

    def test_no_history_overwrite_same_token_and_same_sha_new_token_is_separate(self):
        first = self.stage(); raw = (self.parent/TOKEN/'publication.json').read_bytes()
        self.refused(); self.assertEqual((self.parent/TOKEN/'publication.json').read_bytes(), raw)
        other = self.stage(transaction_token='b'*32)
        self.assertNotEqual(first['directory'],other['directory'])
        self.assertNotEqual(first['producers']['picker_excluded']['enrollment_id'],other['producers']['picker_excluded']['enrollment_id'])
        shutil.rmtree(self.parent/TOKEN)
        self.refused()  # Durable claim forbids reuse even if an owner removed the bundle.

    def test_invalid_identity_and_wrong_head_create_no_boundary(self):
        for change in ({'commit':'f'*40}, {'commit':'HEAD'}, {'transaction_token':'../escape'},
                       {'transaction_token':'same-sha'}, {'transaction_token':'a'*31}):
            self.refused(**change)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_git_object_pin_refuses_dirty_bytes_symlink_and_hardlink(self):
        path=self.repo/m.ARTIFACTS[-1]; original=path.read_bytes()
        path.write_bytes(b'dirty'); self.refused(); path.write_bytes(original)
        victim=self.base/'victim'; victim.write_bytes(original); victim.chmod(0o600)
        path.unlink(); path.symlink_to(victim); self.refused(); path.unlink()
        os.link(victim,path); self.refused(); path.unlink()
        os.mkfifo(path,0o600); self.refused(); path.unlink(); path.write_bytes(original); path.chmod(0o600)
        self.assertFalse((self.parent/TOKEN).exists()); self.assertEqual(victim.read_bytes(),original)

    def test_source_parent_and_destination_symlinks_and_unsafe_modes_refused(self):
        alias=self.base/'alias';alias.symlink_to(self.repo,target_is_directory=True);self.refused(checkout=alias)
        alias.unlink();alias.symlink_to(self.parent,target_is_directory=True);self.refused(parent=alias)
        self.parent.chmod(0o750);self.refused();self.parent.chmod(0o700)
        self.repo.chmod(0o775);self.refused();self.repo.chmod(0o700)
        path=self.repo/m.ARTIFACTS[0];path.chmod(0o620);self.refused()
        self.assertFalse((self.parent/TOKEN).exists())

    def test_existing_target_and_claim_are_never_followed_or_overwritten(self):
        victim=self.base/'untouched';victim.write_text('unchanged')
        for name in (TOKEN,TOKEN+'.claim'):
            for kind in ('symlink','file','fifo','directory'):
                p=self.parent/name
                if kind=='symlink':p.symlink_to(victim)
                elif kind=='file':p.write_text('existing')
                elif kind=='fifo':os.mkfifo(p,0o600)
                else:p.mkdir(mode=0o700)
                self.refused()
                if kind=='directory':p.rmdir()
                else:p.unlink()
        self.assertEqual(victim.read_text(),'unchanged')

    def test_created_file_hardlink_and_cross_producer_replacement_fail_closed(self):
        real=m.publish
        for fault in ('hardlink','cross-producer'):
            token=('c' if fault=='hardlink' else 'd')*32
            def corrupt(parent,pending,final):
                root=parent.path/pending;one=root/m.PRODUCERS[0]/'journal.json';two=root/m.PRODUCERS[1]/'journal.json'
                if fault=='hardlink':two.unlink();os.link(one,two)
                else:two.write_bytes(one.read_bytes())
                return real(parent,pending,final)
            with mock.patch.object(m,'publish',side_effect=corrupt):self.refused(transaction_token=token)
            self.assertTrue((self.parent/(token+'.claim')).exists())
            self.refused(transaction_token=token)

    def test_same_bytes_new_source_inode_after_captured_read_is_refused(self):
        real=m.File.read;hit=False
        def captured(file):
            nonlocal hit
            data=real(file)
            if file.name=='routing-pool.ts' and not hit:
                hit=True;p=file.directory.path/file.name;old=p.with_suffix('.held');p.rename(old);p.write_bytes(data);p.chmod(0o600)
            return data
        with mock.patch.object(m.File,'read',captured):self.refused()
        self.assertTrue(hit);self.assertFalse((self.parent/TOKEN).exists())

    def test_captured_final_source_bytes_cannot_hide_inplace_change(self):
        real=m.File.read;calls=0
        def captured(file):
            nonlocal calls
            data=real(file)
            if file.name=='routing-pool.ts':
                calls+=1
                if calls==3:(file.directory.path/file.name).write_bytes(b'changed after captured read')
            return data
        with mock.patch.object(m.File,'read',captured):self.refused()
        self.assertEqual(calls,3)
        self.refused()  # A later call cannot bless the consumed token or wrong source.

    def test_replaced_parent_inode_and_raced_target_never_authorize(self):
        real=m.publish
        def replaced(parent,pending,final):
            self.parent.rename(self.base/'held-parent');self.parent.mkdir(mode=0o700)
            return real(parent,pending,final)
        with mock.patch.object(m,'publish',side_effect=replaced):self.refused()
        self.assertEqual(list(self.parent.iterdir()),[])
        # Restore only this test's own original directory for the second fault.
        self.parent.rmdir();(self.base/'held-parent').rename(self.parent)
        def race(parent,pending,final):
            os.mkdir(final,0o700,dir_fd=parent.fd);return real(parent,pending,final)
        with mock.patch.object(m,'publish',side_effect=race):self.refused(transaction_token='e'*32)
        self.assertEqual(list((self.parent/('e'*32)).iterdir()),[])

    def test_git_environment_replace_refs_and_checkout_config_cannot_change_provenance(self):
        # Invalid config would make an ordinary Git command fail. The helper's
        # isolated view must neither parse it nor run referenced commands.
        (self.repo/'.git/config').write_text('[include]\npath=/must-not-be-read\n[invalid\n')
        with mock.patch.dict(os.environ,{'GIT_DIR':'/wrong','GIT_WORK_TREE':'/wrong','GIT_OBJECT_DIRECTORY':'/wrong',
             'GIT_ALTERNATE_OBJECT_DIRECTORIES':'/wrong','GIT_CONFIG_PARAMETERS':"'include.path=/wrong'",'GIT_CONFIG_COUNT':'1',
             'GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':'/wrong','HOME':'/wrong','XDG_CONFIG_HOME':'/wrong'}):
            result=self.stage()
        self.assertTrue(result['staged'])

    def test_actual_replace_ref_is_ignored_and_packed_objects_remain_supported(self):
        path=self.repo/m.ARTIFACTS[-1];old=path.read_bytes();original=self.commit
        path.write_bytes(b'replacement tree');self.git('add','.')
        self.git('-c','user.name=Synthetic Fixture','-c','user.email=fixture@example.invalid',
                 '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','replacement')
        replacement=self.git('rev-parse','HEAD').strip()
        ref=self.git('symbolic-ref','HEAD').strip();self.git('update-ref',ref,original);path.write_bytes(old)
        replace=self.repo/'.git/refs/replace';replace.mkdir(mode=0o700)
        (replace/original).write_text(replacement+'\n');(replace/original).chmod(0o600)
        self.assertNotEqual(self.git('show',original+':'+m.ARTIFACTS[-1]).encode(),old)
        result=self.stage();self.assertTrue(result['staged'])
        # Normal packed refs/object databases must not require a loose-object
        # shortcut, a working-tree config or a second repository clone.
        (replace/original).unlink();self.git('pack-refs','--all');self.git('gc','--prune=now')
        self.assertTrue(self.stage(transaction_token='2'*32)['staged'])

    def test_wrong_owner_is_refused_even_when_mode_and_contents_are_valid(self):
        info=(self.repo/m.ARTIFACTS[0]).stat();fields=list(info);fields[4]=123456
        with self.assertRaises(m.Refused):m.File.check_permissions(os.stat_result(fields))
        fields=list(info);fields[5]=123456;fields[0]=stat.S_IFREG|0o600
        with self.assertRaises(m.Refused):m.File.check_permissions(os.stat_result(fields),created=True)

    def test_oversize_source_and_changed_head_at_publication_are_refused(self):
        path=self.repo/m.ARTIFACTS[-1];old=path.read_bytes()
        with path.open('r+b') as f:f.truncate(m.FILE_LIMIT+1)
        self.refused();path.write_bytes(old)
        real=m.publish;ref=self.git('symbolic-ref','HEAD').strip()
        def changed(parent,pending,final):
            value=real(parent,pending,final)
            (self.repo/'.git'/ref).write_text('f'*40+'\n')
            return value
        with mock.patch.object(m,'publish',side_effect=changed):self.refused()
        self.assertTrue((self.parent/(TOKEN+'.claim')).exists());self.refused()

    def test_claim_creation_collision_never_overwrites_a_raced_symlink(self):
        victim=self.base/'victim';victim.write_text('untouched');real=m.File.__init__
        def raced(file,directory,name,*args,**kwargs):
            if name.endswith('.claim'):(directory.path/name).symlink_to(victim)
            return real(file,directory,name,*args,**kwargs)
        with mock.patch.object(m.File,'__init__',raced):self.refused()
        self.assertEqual(victim.read_text(),'untouched');self.assertFalse((self.parent/TOKEN).exists())

    def test_alternate_object_store_or_gitdir_symlink_refused(self):
        alternate=self.repo/'.git/objects/info/alternates';alternate.write_text('/not-authorized\n');self.refused();alternate.unlink()
        metadata=self.repo/'.git';metadata.rename(self.repo/'held-git');metadata.symlink_to(self.repo/'held-git',target_is_directory=True)
        self.refused();self.assertFalse((self.parent/TOKEN).exists())

    def test_noncanonical_head_and_invalid_symbolic_ref_are_not_a_checkout_proof(self):
        head=self.repo/'.git/HEAD';original=head.read_bytes()
        for value in (b' '+self.commit.encode()+b'\n', self.commit.encode()+b'\n\n',
                      b'ref: refs/heads/not..valid\n', b'ref: refs/heads/branch.lock\n'):
            head.write_bytes(value);self.refused();head.write_bytes(original)
        self.assertFalse((self.parent/TOKEN).exists())

    def test_isolated_git_view_tampering_is_refused_before_launching_git(self):
        real=m.Git.run
        def changed(git,args,limit):
            (git.view.path/'config').write_text('[include]\npath=/must-not-be-read\n')
            return real(git,args,limit)
        with mock.patch.object(m.Git,'run',changed),mock.patch.object(m.subprocess,'Popen') as launched:
            self.refused()
        launched.assert_not_called()
        self.assertFalse((self.parent/TOKEN).exists())

    def test_foreign_bundle_and_schema_entries_are_not_published_as_valid_boundaries(self):
        real=m.publish
        for fault,token in [('producer','3'*32),('schema','4'*32)]:
            def extra(parent,pending,final):
                root=parent.path/pending
                if fault=='producer':(root/'credential_monitor').mkdir(mode=0o700)
                else:(root/m.PRODUCERS[0]/'unexpected.json').write_text('{}')
                return real(parent,pending,final)
            with mock.patch.object(m,'publish',extra):self.refused(transaction_token=token)
            self.assertTrue((self.parent/(token+'.claim')).exists());self.refused(transaction_token=token)

    def test_git_blob_object_hash_is_authenticated_not_only_worktree_digest(self):
        # Alter the loose blob bytes while retaining its expected object filename.
        import zlib
        oid=self.git('rev-parse',self.commit+':'+m.ARTIFACTS[-1]).strip()
        object_path=self.repo/'.git/objects'/oid[:2]/oid[2:]
        object_path.chmod(0o600);object_path.write_bytes(zlib.compress(b'blob 7\0forged\n'))
        self.refused();self.assertFalse((self.parent/TOKEN).exists())

    def test_file_and_publication_directory_sync_failures_consume_token_without_success(self):
        real=m.os.fsync
        for fault,token in [('file','f'*32),('publication','1'*32)]:
            def sync(fd):
                info=os.fstat(fd)
                # Ignore Gitview verification files: fault begins at the claim.
                if fault=='file' and (self.parent/(token+'.claim')).exists() and stat.S_ISREG(info.st_mode):raise OSError('synthetic fsync')
                if fault=='publication' and (self.parent/token).exists():raise OSError('synthetic fsync')
                return real(fd)
            with mock.patch.object(m.os,'fsync',side_effect=sync):self.refused(transaction_token=token)
            self.assertTrue((self.parent/(token+'.claim')).exists());self.refused(transaction_token=token)

    def test_sigkill_after_claim_prevents_same_token_reentry(self):
        code='''import importlib.util,os,signal,sys
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('m',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=m.File.__init__
def stopped(self,*args,**kw):
 original(self,*args,**kw)
 if self.name.endswith('.claim'):os.kill(os.getpid(),signal.SIGKILL)
m.File.__init__=stopped
m.stage(sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5])
'''
        result=subprocess.run([sys.executable,'-I','-S','-c',code,str(HELPER),str(self.repo),str(self.parent),self.commit,TOKEN],
                              stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,-signal.SIGKILL);self.assertEqual(result.stdout,b'')
        self.assertFalse((self.parent/TOKEN).exists());self.refused()

    def test_owned_process_disappearing_during_proc_read_is_not_alive(self):
        for number in (errno.ENOENT, errno.ESRCH):
            with self.subTest(errno=number), mock.patch.object(select, 'select', return_value=([], [], [])), \
                    mock.patch.object(Path, 'read_text', side_effect=OSError(number, 'synthetic disappearance')):
                self.assertFalse(owned_process_alive(123, 456))

    def test_owned_process_permission_and_other_errors_are_not_absence(self):
        for number in (errno.EPERM, errno.EACCES, errno.EIO):
            with self.subTest(errno=number), mock.patch.object(select, 'select', return_value=([], [], [])), \
                    mock.patch.object(Path, 'read_text', side_effect=OSError(number, 'synthetic read failure')):
                with self.assertRaises(OSError) as raised:
                    owned_process_alive(123, 456)
                self.assertEqual(raised.exception.errno, number)

    def test_exited_pidfd_prevents_inspecting_a_reused_numeric_pid(self):
        with mock.patch.object(select, 'select', return_value=([456], [], [])), \
                mock.patch.object(Path, 'read_text') as read:
            self.assertFalse(owned_process_alive(123, 456))
            read.assert_not_called()

    def test_git_child_dies_if_staging_owner_is_killed(self):
        import time
        receiver, sender = socket.socketpair()
        receiver.settimeout(10)
        owned = {'pidfd': None, 'helper': None}
        def cleanup():
            try:
                try:
                    if owned['pidfd'] is not None:
                        try:
                            kill_owned_pidfd(owned['pidfd'])
                        except ProcessLookupError:
                            pass
                        finally:
                            os.close(owned['pidfd'])
                finally:
                    if owned['helper'] is not None:
                        if owned['helper'].poll() is None:
                            owned['helper'].kill()
                        owned['helper'].communicate(timeout=10)
            finally:
                receiver.close()
                sender.close()
        self.addCleanup(cleanup)  # Registered before creating either process.
        code='''import array,ctypes,importlib.util,os,signal,socket,subprocess,sys
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('m',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=subprocess.Popen
channel=socket.socket(fileno=int(sys.argv[6]))
def child(*args,**kwargs):
 process=original([sys.executable,'-I','-S','-c','import time;time.sleep(20)'],**kwargs)
 libc=ctypes.CDLL(None,use_errno=True)
 open_pidfd=libc.pidfd_open;open_pidfd.argtypes=(ctypes.c_int,ctypes.c_uint);open_pidfd.restype=ctypes.c_int
 pidfd=open_pidfd(process.pid,0)
 if pidfd<0:
  number=ctypes.get_errno();raise OSError(number,os.strerror(number))
 try:
  channel.sendmsg([str(process.pid).encode()],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[pidfd]))])
 finally:os.close(pidfd)
 if channel.recv(1)!=b'K':raise RuntimeError('missing parent acknowledgement')
 os.kill(os.getpid(),signal.SIGKILL)
m.subprocess.Popen=child
m.stage(sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5])
'''
        owned['helper']=subprocess.Popen([sys.executable,'-I','-S','-c',code,str(HELPER),str(self.repo),str(self.parent),self.commit,TOKEN,str(sender.fileno())],
                              stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=(sender.fileno(),))
        sender.close()
        payload, ancillary, flags, _ = receiver.recvmsg(32, socket.CMSG_SPACE(array.array('i').itemsize))
        received = array.array('i')
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received.frombytes(data[:len(data) - len(data) % received.itemsize])
        # Own every received descriptor before assertions can fail.
        if received:
            owned['pidfd'] = received[0]
            for extra in received[1:]:os.close(extra)
        self.assertEqual(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC), 0)
        if not received:
            output, error = owned['helper'].communicate(timeout=10)
            self.fail('owned child pidfd was not received: ' + error.decode(errors='replace'))
        self.assertEqual(len(received), 1)
        pid = int(payload)
        os.set_inheritable(owned['pidfd'], False)
        self.assertTrue(owned_process_alive(pid, owned['pidfd']), 'owned child exited before helper kill')
        receiver.sendall(b'K')  # Helper cannot die before cleanup has the pinned child.
        owned['helper'].communicate(timeout=10)
        self.assertEqual(owned['helper'].returncode, -signal.SIGKILL)
        deadline=time.monotonic()+2
        while owned_process_alive(pid, owned['pidfd']) and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(owned_process_alive(pid, owned['pidfd']), 'owned Git child survived its killed helper')

    def test_cli_default_off_single_json_and_failed_replay_has_no_success_stdout(self):
        base=[sys.executable,'-I','-S',str(HELPER)]
        result=subprocess.run(base,stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,2);self.assertEqual(list(self.parent.iterdir()),[])
        argv=base+['stage','--checkout',str(self.repo),'--parent',str(self.parent),'--commit',self.commit,'--transaction-token',TOKEN]
        result=subprocess.run(argv,cwd='/',stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(len(result.stdout.splitlines()),1)
        self.assertTrue(json.loads(result.stdout)['staged']);self.assertFalse(json.loads(result.stdout)['activated'])
        result=subprocess.run(argv,cwd='/',stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,2);self.assertEqual(result.stdout,b'')

    def test_both_boundaries_load_in_actual_node_authority_and_cross_kind_is_refused(self):
        renamed=self.base/'checkout-unicode-雪';self.repo.rename(renamed);self.repo=renamed
        for relative in m.ARTIFACTS:
            (self.repo/relative).write_bytes((ROOT/relative).read_bytes())
        self.git('add','.');self.git('-c','user.name=Synthetic Fixture','-c','user.email=fixture@example.invalid',
            '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','actual authority source')
        self.commit=self.git('rev-parse','HEAD').strip();result=self.stage()
        code='''import {pathToFileURL} from 'node:url';
const {AlertEvidence}=await import(pathToFileURL(process.argv[1]).href);
const fs=await import('node:fs');const receipt=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const loadedArtifacts=Object.fromEntries(receipt.artifacts.map(a=>[a.path,a.sha256]));
for(const producer of ['picker_excluded','picker_quota_stale']){
 const directory=receipt.producers[producer].directory;
 const observer=await AlertEvidence.load(directory,{producer,loadedArtifacts,timeoutMs:2000});
 const proof=await observer.snapshot('1'.repeat(32));if(proof.complete!==true||proof.producer!==producer)throw new Error('missing actual proof');
 await observer.close();
 let refused=false;try{await AlertEvidence.load(directory,{producer:producer==='picker_excluded'?'picker_quota_stale':'picker_excluded',loadedArtifacts});}catch{refused=true;}
 if(!refused)throw new Error('cross-producer enrollment accepted');
}
console.log('both actual authority schemas accepted, cross-producer refused');
'''
        child=subprocess.run(['node','--experimental-transform-types','--input-type=module','-e',code,
            str(self.repo/m.ARTIFACTS[0]),str(Path(result['directory'])/'publication.json')],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr.decode());self.assertIn(b'both actual authority schemas accepted',child.stdout)


if __name__=='__main__':unittest.main()
