"""Cold checkout effects use synthetic temporary repositories only."""
import multiprocessing
import sys
import signal
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, ANY

from agent_hub_host import fleet


@unittest.skipUnless(fleet.repo_provisioning_supported(), 'POSIX repository effects require flock/statvfs')
class RepoProvisionTests(unittest.TestCase):
    def setUp(self):
        fleet._GH_AUTH_CACHE = None

    def test_clone_timeout_kills_descendants_before_returning_typed_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp)/'descendant.pid'
            parent = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)"
            processes = []
            native_popen = subprocess.Popen
            def spawn(*args, **kwargs):
                process = native_popen(*args, **kwargs)
                processes.append(process)
                return process
            try:
                with patch.object(fleet.subprocess, 'Popen', side_effect=spawn):
                    with self.assertRaisesRegex(ValueError, '^repo_clone_failed: clone command timed out$'):
                        fleet._run_repo_clone_command([sys.executable, '-c', parent, str(marker)], timeout=2)
                self.assertTrue(marker.exists(), 'child must start before the timeout')
                self.assertIsNotNone(processes[0].returncode)
                child = int(marker.read_text())
                proc = Path('/proc')/str(child)/'stat'
                deadline = time.monotonic() + 2
                while proc.exists() and time.monotonic() < deadline:
                    try:
                        if proc.read_text().split()[2] == 'Z':
                            break
                    except FileNotFoundError:
                        break
                    time.sleep(.01)
                if proc.exists():
                    self.assertEqual(proc.read_text().split()[2], 'Z', 'descendant must have exited')
            finally:
                for process in processes:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)

    def test_clone_command_suppresses_output_and_maps_spawn_failure(self):
        process = SimpleNamespace(wait=lambda **kwargs: 0)
        with patch.object(fleet.subprocess, 'Popen', return_value=process) as spawn:
            self.assertEqual(fleet._run_repo_clone_command(['gh', 'repo', 'clone'], timeout=1).returncode, 0)
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.args[0], ['gh', 'repo', 'clone'])
            kwargs = spawn.call_args.kwargs
            self.assertIs(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['start_new_session'])
            # Clone output goes to a bounded temp sink, never an inherited
            # pipe (intake fae60b8a); only a scrubbed line reaches the journal.
            self.assertIsNot(kwargs['stdout'], subprocess.DEVNULL)
            self.assertIs(kwargs['stderr'], kwargs['stdout'])
        with patch.object(fleet.subprocess, 'Popen', side_effect=OSError('private command error')):
            with self.assertRaisesRegex(ValueError, '^repo_clone_failed: clone command failed$'):
                fleet._run_repo_clone_command(['gh'], timeout=1)

    def test_layout_uses_first_writable_explicit_root_and_honors_disabled_cloning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first, second = root/'first', root/'second'
            first.mkdir()
            second.mkdir()
            settings = {'workspace_roots':[str(first),str(second)]}
            with patch.object(fleet.os, 'access', side_effect=lambda p, mode: Path(p) == second):
                self.assertEqual(fleet.repo_layout(settings)['checkout_root'], second)
            self.assertFalse(fleet.repo_layout({'workspaces':{'legacy':str(second)}})['clone_enabled'])
            settings['repos'] = {'checkout_root':str(first),'clone_on_demand':False}
            with patch.object(fleet, 'gh_auth_ok') as auth:
                with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                    fleet.guarded_repo_clone(settings, 'demo', 'org/demo', str(first/'demo'), disk_floor_gb=15)
                auth.assert_not_called()

    def test_clone_checks_worktree_mount_and_inodes_before_native_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            checkout, trees = root/'repos', root/'trees'
            checkout.mkdir()
            trees.mkdir()
            settings = {'workspace_roots':[str(checkout)], 'cleanup_roots':[str(trees)],
                        'repos':{'checkout_root':str(checkout),'worktree_root':str(trees)}}
            for free, inodes, expected in [(80,900,'host_disk_warn_no_clone'), (20,900,'host_disk_critical'),
                                           (900,80,'host_disk_warn_no_clone'), (900,40,'host_disk_critical')]:
                def disk(path):
                    bad = Path(path) == trees
                    f = free if bad else 900
                    return SimpleNamespace(f_blocks=1000,f_bfree=f,f_bavail=f,f_frsize=1024**3,
                                           f_files=1000,f_favail=inodes if bad else 900)
                with self.subTest(free=free,inodes=inodes), patch.object(fleet.os,'statvfs',side_effect=disk), patch.object(
                        fleet,'gh_auth_ok') as auth:
                    with self.assertRaisesRegex(ValueError, expected):
                        fleet.guarded_repo_clone(settings,'demo','org/demo',str(checkout/'demo'),disk_floor_gb=15)
                    auth.assert_not_called()

    def test_runtime_cleanup_preserves_other_receipts_and_refuses_symlink_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            results = root/'.local'/'hub-results'
            results.mkdir(parents=True)
            report = results/'offer-one.json'
            identity = root/'.local'/'first-action-hub-offer-one.json'
            report.write_text('ingested')
            identity.write_text('identity')
            sibling = results/'offer-two.json'
            sibling.write_text('preserve')
            fleet._remove_repo_runtime_files(root, 'offer-one')
            self.assertFalse(report.exists())
            self.assertFalse(identity.exists())
            self.assertEqual(sibling.read_text(), 'preserve')
            outside = root/'outside'
            outside.mkdir()
            (outside/'offer-one.json').write_text('preserve outside')
            sibling.unlink()
            results.rmdir()
            results.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                fleet._remove_repo_runtime_files(root, 'offer-one')
            self.assertEqual((outside/'offer-one.json').read_text(), 'preserve outside')

    def test_qualified_terminal_cleanup_removes_runtime_files_only_after_intake_release(self):
        from agent_hub_host import private_fs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            trees = root/'trees'
            tree = trees/'hub-offer-one'/'demo'
            results = tree/'.local'/'hub-results'
            results.mkdir(parents=True)
            report = results/'offer-one.json'
            identity = tree/'.local'/'first-action-hub-offer-one.json'
            report.write_text('ingested')
            identity.write_text('identity')
            (tree/'source').write_text('preserve source')
            journal = root/'journal'
            private_fs.atomic_write(journal/'repo-worktrees'/'offer-one.json', json.dumps({
                'workspace':str(tree), 'branch':'hub/offer-one', 'session_id':'offer-one',
                'default_branch':'develop'}).encode(), 8192)
            dispatcher = fleet.Dispatcher.__new__(fleet.Dispatcher)
            dispatcher.settings = {'cleanup_roots':[str(trees)], 'repo_worktree_root':str(trees),
                                   'journal_dir':str(journal), 'machine_id':'fixture-host'}
            snapshot = {'sessions':[{'native_id':'fixture-native', 'workspace':str(tree),
                                     'state':'exited', 'linked_intake_open':True}]}
            dispatcher.request = lambda *args: snapshot
            with patch('agent_hub_host.worktree_guard.is_locked', return_value=False), patch(
                    'agent_hub_host.native_sessions.list_sessions', return_value=[]), patch(
                    'agent_hub_host.session_cleanup.remove_owned_worktree') as remove:
                dispatcher._maybe_cleanup_worktree(tree, 'fixture-native')
                remove.assert_not_called()
                self.assertTrue(report.exists())
                snapshot['sessions'][0]['linked_intake_open'] = False
                def checked_remove(**kwargs):
                    self.assertFalse(report.exists())
                    self.assertFalse(identity.exists())
                    self.assertEqual(kwargs['default_branch'], 'develop')
                remove.side_effect = checked_remove
                dispatcher._maybe_cleanup_worktree(tree, 'fixture-native')
                remove.assert_called_once()
            self.assertEqual((tree/'source').read_text(), 'preserve source')

    def test_auth_cache_expires_and_failure_is_cached_without_output(self):
        with patch.object(fleet.time, 'monotonic', side_effect=[0, 0, 299, 301, 301]), patch.object(
                fleet.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0), OSError()]) as run:
            self.assertTrue(fleet.gh_auth_ok())
            self.assertTrue(fleet.gh_auth_ok())
            self.assertFalse(fleet.gh_auth_ok())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ['gh', 'auth', 'status', '--hostname', 'github.com'])
        self.assertEqual(run.call_args_list[0].kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(run.call_args_list[0].kwargs['stderr'], subprocess.DEVNULL)

    def test_disk_warn_critical_and_floor_refuse_before_gh(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fleet, 'gh_auth_ok') as auth:
            root = Path(tmp).resolve()
            for free, reason in [(14, 'host_disk_warn_no_clone'), (5, 'host_disk_critical'), (80, 'host_disk_warn_no_clone')]:
                with self.subTest(free=free), patch.object(fleet.os, 'statvfs', return_value=SimpleNamespace(
                        f_blocks=100, f_bfree=free, f_bavail=free, f_frsize=1024**3)):
                    with self.assertRaisesRegex(ValueError, reason):
                        fleet.guarded_repo_clone({'workspace_roots': [str(root)]}, 'demo', 'org/demo', str(root/'demo'), disk_floor_gb=90)
            auth.assert_not_called()

    def test_concurrent_processes_clone_once_under_flock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            calls = root/'calls'
            def clone(argv, **kwargs):
                if argv[0] == 'git':
                    return SimpleNamespace(returncode=0, stdout='https://github.com/org/demo.git\n')
                with calls.open('a') as f:
                    f.write('clone\n')
                time.sleep(.15)
                (Path(argv[4])/'.git').mkdir(parents=True)
                return SimpleNamespace(returncode=0)
            def child():
                with patch.object(fleet, 'gh_auth_ok', return_value=True), patch.object(fleet.subprocess, 'run', side_effect=clone), patch.object(fleet, '_run_repo_clone_command', side_effect=clone), patch.object(
                        fleet.os, 'statvfs', return_value=SimpleNamespace(f_blocks=1000, f_bfree=900, f_bavail=900, f_frsize=1024**3)):
                    fleet.guarded_repo_clone({'workspace_roots': [str(root)]}, 'demo', 'org/demo', str(root/'demo'), disk_floor_gb=15)
            ctx = multiprocessing.get_context('fork')
            children = [ctx.Process(target=child) for _ in range(2)]
            try:
                for p in children:
                    p.start()
                for p in children:
                    p.join(5)
                    self.assertEqual(p.exitcode, 0)
                self.assertEqual(calls.read_text(), 'clone\n')
                self.assertTrue((root/'demo'/'.git').is_dir())
            finally:
                for p in children:
                    if p.is_alive():
                        p.terminate()
                    if p.pid:
                        p.join()

    def test_worktree_uses_fresh_origin_branch_and_replays_without_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, checkout, trees = root/'source', root/'checkout', root/'trees'
            def git(*args):
                return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-b', 'main', source)
            git('-C', source, 'config', 'user.name', 'Unit Test')
            git('-C', source, 'config', 'user.email', 'unit@example.test')
            (source/'tracked').write_text('base')
            git('-C', source, 'add', 'tracked')
            git('-C', source, 'commit', '-m', 'base')
            git('clone', source, checkout)
            # Advance remote after clone: worktree must fetch this new HEAD.
            (source/'tracked').write_text('fresh')
            git('-C', source, 'commit', '-am', 'advance')
            settings = {'workspaces': {}, 'workspace_roots':[str(root)],
                        'cleanup_roots':[str(trees)], 'repo_worktree_root':str(trees), 'journal_dir':str(root/'journal')}
            target = fleet.create_repo_worktree(settings, checkout, 'session-one', 'main', repo_name='logical-demo')
            self.assertEqual(target, trees/'hub-session-one'/'logical-demo')
            self.assertEqual((target/'tracked').read_text(), 'fresh')
            self.assertEqual(git('-C', target, 'branch', '--show-current'), 'hub/session-one')
            (target/'.local').mkdir()
            (target/'.local'/'result.json').write_text('private evidence')
            self.assertEqual(git('-C', target, 'status', '--porcelain'), '')
            (target/'tracked').write_text('preserve my work')
            self.assertEqual(fleet.create_repo_worktree(settings, checkout, 'session-one', 'main', repo_name='logical-demo'), target)
            self.assertEqual((target/'tracked').read_text(), 'preserve my work')
            self.assertIn('tracked', git('-C', target, 'status', '--porcelain'))
            self.assertEqual((checkout/'.git/info/exclude').read_text().splitlines().count('.local/'), 1)
            self.assertEqual(git('-C', checkout, 'branch', '--show-current'), 'main')
            outside = root/'unrelated'
            outside.write_text('keep this')
            exclude = checkout/'.git/info/exclude'
            exclude.unlink()
            exclude.symlink_to(outside)
            with self.assertRaises(OSError):
                fleet.create_repo_worktree(settings, checkout, 'session-two', 'main')
            self.assertEqual(outside.read_text(), 'keep this')
            self.assertFalse((trees/'hub-session-two').exists())

    def test_cleanup_fresh_proof_uses_configured_default_branch(self):
        from agent_hub_host.session_cleanup import verify_merged_fresh
        from agent_hub_host.native_sessions import SessionControlError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, checkout = root/'source', root/'checkout'
            def git(*args):
                return subprocess.run(['git', *map(str,args)], check=True, capture_output=True, text=True)
            git('init', '-b', 'develop', source)
            git('-C', source, 'config', 'user.name', 'Unit Test')
            git('-C', source, 'config', 'user.email', 'unit@example.test')
            git('-C', source, 'commit', '--allow-empty', '-m', 'base')
            git('clone', source, checkout)
            def runner(argv, cwd, timeout):
                return subprocess.run(argv, cwd=cwd, timeout=timeout, capture_output=True, text=True)
            expected = git('-C', source, 'rev-parse', 'HEAD').stdout.strip()
            self.assertEqual(verify_merged_fresh('git', str(checkout), runner=runner, default_branch='develop'), expected)
            with self.assertRaisesRegex(SessionControlError, 'unknown'):
                verify_merged_fresh('git', str(checkout), runner=runner)
            git('-C', checkout, 'config', 'user.name', 'Unit Test')
            git('-C', checkout, 'config', 'user.email', 'unit@example.test')
            git('-C', checkout, 'commit', '--allow-empty', '-m', 'unmerged')
            with self.assertRaisesRegex(SessionControlError, 'unmerged'):
                verify_merged_fresh('git', str(checkout), runner=runner, default_branch='develop')

    def test_dispatch_provisions_only_a_validated_cold_offer_before_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            dispatcher = fleet.Dispatcher.__new__(fleet.Dispatcher)
            dispatcher.settings = {'workspace_roots':[str(root)], 'workspaces':{},
                'subscriptions':[{'service':'codex','engine':'codex','account_label':'test-codex',
                                  'profile':None,'admission_argv':['trusted-admit']}]}
            requests = []
            dispatcher.request = lambda *args: requests.append(args) or {'updated':1}
            offer = {'id':'launch-test','machine_id':'fixture-host','mode':'headless','engine':'codex','account':'test-codex','profile':None,
                     'workspace':str(root/'demo'), 'resource_management':{'disk_floor_gb':15},
                     'repo_provision':{'mode':'clone_on_demand','name':'demo','origin_url':'org/demo','workspace_root':str(root)}}
            def publish(*args, **kwargs):
                kwargs['on_created'](root/'demo')
                return root/'demo'
            with patch('agent_hub_host.host_load.check_live_load'), patch('agent_hub_host.repos.scan_single', return_value={'name':'demo','machine':'fixture-host'}) as scanned, patch.object(fleet, 'guarded_repo_clone',side_effect=publish) as clone, patch.object(
                    dispatcher, '_dispatch_admission', side_effect=RuntimeError('admission sentinel')):
                with self.assertRaisesRegex(RuntimeError, 'admission sentinel'):
                    dispatcher.dispatch(offer)
                clone.assert_called_once_with(dispatcher.settings, 'demo', 'org/demo', str(root/'demo'), disk_floor_gb=15, on_created=ANY)
                scanned.assert_called_once_with(root/'demo', 'fixture-host', default_branch='main', cloned_on_demand=True)
                self.assertEqual(requests, [('POST', '/api/repos/refresh', {'repos':[{'name':'demo','machine':'fixture-host'}]})])
                clone.reset_mock()
                with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                    dispatcher.dispatch({**offer, 'workspace':str(root/'unapproved')})
                clone.assert_not_called()

    def test_cold_offer_requires_exact_host_root_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            settings = {'workspace_roots':[str(root)]}
            offer = {'workspace':str(root/'demo'), 'repo_provision':{
                'mode':'clone_on_demand', 'name':'demo', 'workspace_root':str(root), 'origin_url':'org/demo'}}
            self.assertEqual(fleet.offered_repo_checkout(settings, offer), root/'demo')
            for workspace in [str(root/'other'), str(root.parent/'demo'), 'demo']:
                with self.subTest(workspace=workspace), self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                    fleet.offered_repo_checkout(settings, {**offer, 'workspace':workspace})
            with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                fleet.offered_repo_checkout({'workspace_roots':[]}, offer)

    def test_symlink_target_refused_without_cli(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fleet.subprocess, 'run') as run:
            root = Path(tmp).resolve()
            (root/'demo').symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                fleet.guarded_repo_clone({'workspace_roots': [str(root)]}, 'demo', 'org/demo', str(root/'demo'), disk_floor_gb=15)
            run.assert_not_called()

    def test_typed_clone_timeout_removes_only_its_staging_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root/'demo'
            staging = Path(str(target) + '.tmp.' + str(os.getpid()))
            sibling = root/'other-session.tmp'
            sibling.mkdir()
            def timeout(argv, **kwargs):
                staging.mkdir()
                (staging/'partial').write_text('partial')
                raise ValueError('repo_clone_failed')
            with patch.object(fleet, 'gh_auth_ok', return_value=True), patch.object(
                    fleet, '_run_repo_clone_command', side_effect=timeout), patch.object(fleet.os, 'statvfs', return_value=SimpleNamespace(
                    f_blocks=1000, f_bfree=900, f_bavail=900, f_frsize=1024**3)):
                with self.assertRaisesRegex(ValueError, '^repo_clone_failed$'):
                    fleet.guarded_repo_clone({'workspace_roots':[str(root)]}, 'demo', 'org/demo', str(target), disk_floor_gb=15)
            self.assertFalse(staging.exists())
            self.assertFalse(target.exists())
            self.assertTrue(sibling.is_dir())

    def test_failed_clone_cleans_only_new_staging_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root/'demo'
            staging = Path(str(target) + '.tmp.' + str(os.getpid()))
            def failed(argv, **kwargs):
                staging.mkdir()
                (staging/'partial').write_text('partial')
                return SimpleNamespace(returncode=1)
            with patch.object(fleet, 'gh_auth_ok', return_value=True), patch.object(
                    fleet, '_run_repo_clone_command', side_effect=failed), patch.object(fleet.os, 'statvfs', return_value=SimpleNamespace(
                    f_blocks=1000, f_bfree=900, f_bavail=900, f_frsize=1024**3)):
                with self.assertRaisesRegex(ValueError, 'repo_clone_failed'):
                    fleet.guarded_repo_clone({'workspace_roots':[str(root)]}, 'demo', 'org/demo', str(target), disk_floor_gb=15)
                self.assertFalse(staging.exists())
                staging.mkdir()
                (staging/'keep').write_text('pre-existing')
                with self.assertRaisesRegex(ValueError, 'repo_clone_staging_exists'):
                    fleet.guarded_repo_clone({'workspace_roots':[str(root)]}, 'demo', 'org/demo', str(target), disk_floor_gb=15)
                self.assertEqual((staging/'keep').read_text(), 'pre-existing')

    def test_existing_checkout_with_different_origin_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root/'demo'/'.git').mkdir(parents=True)
            with patch.object(fleet.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='https://github.com/other/demo.git')):
                with self.assertRaisesRegex(ValueError, 'repo_origin_mismatch'):
                    fleet.guarded_repo_clone({'workspace_roots':[str(root)]}, 'demo', 'org/demo', str(root/'demo'), disk_floor_gb=15)
            self.assertTrue((root/'demo'/'.git').is_dir())

    def test_provision_refusals_keep_explicit_reason_with_missing_workspace(self):
        from agent_hub_host.dispatch_diagnostics import classify
        for reason in ['repo_checkout_missing', 'repo_origin_missing', 'host_gh_auth_unavailable',
                       'host_disk_warn_no_clone', 'host_disk_critical', 'repo_clone_failed']:
            with self.subTest(reason=reason):
                self.assertEqual(classify(ValueError(reason), workspace_available=False)['reason'], reason)

        self.assertEqual(classify(ValueError('repo_origin_mismatch'), workspace_available=False)['reason'], 'dispatch_cwd_invalid')


class RepoCapabilityTests(unittest.TestCase):
    def test_windows_host_advertises_existing_checkouts_without_posix_effects(self):
        # Construct paths before simulating Windows on a POSIX test runner.
        checkout = Path('/configured/repo')
        settings = {'workspace_roots':['/configured'], 'cleanup_roots':['/configured/trees'],
                    'repos':{'checkout_root':'/configured','worktree_root':'/configured/trees'}}
        with patch.object(fleet.os, 'name', 'nt'), patch.object(fleet.subprocess, 'Popen') as spawn:
            self.assertEqual(fleet.repo_capabilities(settings), {
                'repo_checkout_root':None,'repo_clone_enabled':False,'repo_worktree_root':None})
            for callback in [lambda: fleet.guarded_repo_clone(settings,'repo','org/repo',str(checkout),disk_floor_gb=15),
                             lambda: fleet.create_repo_worktree(settings,checkout,'offer-one','main')]:
                with self.assertRaisesRegex(ValueError,'repo_checkout_missing'):
                    callback()
            spawn.assert_not_called()

    def test_missing_disk_or_lock_api_never_advertises_provisioning(self):
        with patch.object(fleet.os,'statvfs',None,create=True):
            self.assertFalse(fleet.repo_capabilities({})['repo_clone_enabled'])
            self.assertIsNone(fleet.repo_capabilities({})['repo_worktree_root'])
        with patch.dict(sys.modules, {'fcntl':None}):
            self.assertFalse(fleet.repo_provisioning_supported())

    def test_cleanup_roots_do_not_opt_in_an_intake_linked_checkout(self):
        from agent_hub_host import intake_worktree
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            checkout = root/'git'/'demo'; checkout.mkdir(parents=True)
            cleanup = root/'owned-session-worktrees'
            settings={'workspace_roots':[str(checkout.parent)],'cleanup_roots':[str(cleanup)]}
            self.assertIsNone(fleet.repo_capabilities(settings)['repo_worktree_root'])
            self.assertNotIn(str(cleanup),fleet._workspace_roots(settings))
            offer={'id':'offer-intake','intake_task_id':'intake-one','workspace':str(checkout),
                   'repo_provision':{'mode':'existing','name':'demo','checkout_path':str(checkout)}}
            self.assertEqual(fleet.offered_repo_checkout(settings,offer),checkout)
            intake_tree=root/'git'/'demo-wt-intake'
            witness={'path':str(intake_tree),'prompt_line':'Hub intake worktree'}
            with patch.object(intake_worktree,'prepare_launch_worktree',return_value=witness) as prepare:
                target, note, actual=intake_worktree.launch_cwd(lambda *args:None,offer,checkout,checkout)
            self.assertEqual(target,intake_tree)
            self.assertIs(actual,witness)
            self.assertIn('Hub intake worktree',note)
            prepare.assert_called_once_with(ANY,'offer-intake',checkout,make_tmp=False,session_id=None)
            self.assertFalse(cleanup.exists())
