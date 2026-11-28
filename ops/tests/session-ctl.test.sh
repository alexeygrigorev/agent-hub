#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
# Runtime commands below see disposable HOME and fake CLI executables; the
# original file-only Git fixture remains real Git under that disposable HOME. No provider,
# systemd, auth/profile or production process is contacted by this test suite.
python3 - "$repo" <<'PY_TESTS'
import contextlib,datetime,json,os,pathlib,shutil,signal,stat,subprocess,sys,tempfile,time,unittest
os.umask(0o077)
REPO=pathlib.Path(sys.argv[1]);CTL=REPO/'ops/lib/session-ctl.sh'

@contextlib.contextmanager
def owned_fixture_parent(repo, supplied):
 # No global TMPDIR rewrite. A supplied parent is used only if already trusted.
 fds=[];created=[];run=None;run_identity=None;parent=None
 def opened(path):
  path=pathlib.Path(path)
  if not path.is_absolute():raise RuntimeError('test_tmpdir_untrusted')
  fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
  for part in path.parts[1:]:
   if part in ('.','..'):raise RuntimeError('test_tmpdir_untrusted')
   fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
   st=os.fstat(fd)
   if st.st_uid not in (0,os.getuid()) or st.st_mode&0o022:raise RuntimeError('test_tmpdir_untrusted')
  return fd
 try:
  if supplied is not None:
   opened(supplied);yield pathlib.Path(supplied);return
  fd=opened(repo)
  for name in ('.local','tmp'):
   made=False
   try:os.mkdir(name,mode=0o700,dir_fd=fd);made=True
   except FileExistsError:pass
   child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(child);st=os.fstat(child)
   if st.st_uid!=os.getuid() or st.st_mode&0o022:raise RuntimeError('test_tmpdir_untrusted')
   if made:created.append((fd,name,st.st_dev,st.st_ino))
   fd=child
  parent=fd
  run=pathlib.Path(tempfile.mkdtemp(prefix='ops-managed-run-',dir=pathlib.Path(repo)/'.local/tmp'))
  st=run.lstat();run_identity=(st.st_dev,st.st_ino)
  if st.st_uid!=os.getuid() or not stat.S_ISDIR(st.st_mode) or st.st_mode&0o777!=0o700:raise RuntimeError('test_tmpdir_untrusted')
  yield run
 finally:
  try:
   if run is not None:
    st=os.stat(run.name,dir_fd=parent,follow_symlinks=False)
    if (st.st_dev,st.st_ino)!=run_identity or st.st_uid!=os.getuid() or not stat.S_ISDIR(st.st_mode):raise RuntimeError('test_tmpdir_cleanup_unconfirmed')
    shutil.rmtree(run.name,dir_fd=parent)
   for fd,name,device,inode in reversed(created):
    st=os.stat(name,dir_fd=fd,follow_symlinks=False)
    if (st.st_dev,st.st_ino)!=(device,inode) or st.st_uid!=os.getuid() or not stat.S_ISDIR(st.st_mode):raise RuntimeError('test_tmpdir_cleanup_unconfirmed')
    os.rmdir(name,dir_fd=fd) # empty-only; never recursively remove shared parents
  finally:
   for fd in reversed(fds):os.close(fd)

FAKE=r'''#!/usr/bin/env python3
import hashlib,json,os,pathlib,sys
args=sys.argv[1:];name=pathlib.Path(sys.argv[0]).name;root=pathlib.Path(os.environ['FAKE']);log=root/(name+'.jsonl')
with log.open('a') as f:f.write(json.dumps(args)+'\n')
def option(k):return args[args.index(k)+1]
def stable(prefix,project,intake,key):
 h=hashlib.sha256(json.dumps([prefix,project,intake,key],separators=(',',':')).encode()).hexdigest();return h[:8]+'-'+h[8:12]+'-5'+h[13:16]+'-a'+h[17:20]+'-'+h[20:32]
def load():return json.loads((root/'session.json').read_text())
def save(s):(root/'session.json').write_text(json.dumps(s))
if name=='a':
 if args[0]=='list':print((root/'legacy.json').read_text());sys.exit(0)
 if args[0]=='capture':print((root/'screen.txt').read_text());sys.exit(0)
 if args[0]=='send':sys.exit(0)
 sys.exit(97) # new native launch, engines, kill or forget is a failure
if name=='git':
 if args[0]=='-C':directory=pathlib.Path(args[1]);args=args[2:]
 if args[:2]==['worktree','add']:
  target=pathlib.Path(args[3]);(target/'.git/info').mkdir(parents=True,exist_ok=True);(target/'.git/info/exclude').touch()
 elif args[:2]==['rev-parse','--git-path']:print(directory/'.git/info/exclude')
 elif args[:2]==['rev-parse','--short']:print('abc1234')
 sys.exit(0)
if name in ('gh','systemctl','systemd-run'):sys.exit(96)
if name!='hub':sys.exit(98)
if args[:2]==['sessions','start']:
 if os.environ.get('FAKE_START_REJECT')=='1':sys.exit(1)
 key=option('--key');project=option('--repo');intake=option('--intake-task-id');workspace=option('--workspace')
 digest=hashlib.sha256(json.dumps({'intake_id':intake,'project_id':project},sort_keys=True).encode()).hexdigest()
 resolved=workspace+'/.local/hub-worktrees/'+digest
 if os.environ.get('FAKE_WORKSPACE_MODE')=='plain':resolved=workspace
 if os.environ.get('FAKE_WORKSPACE_MODE')=='retained':resolved='/retained/original/checkout/.local/hub-worktrees/'+digest
 s=dict(id=stable('hub2-start-session-v1',project,intake,key),operation_id=stable('hub2-start-operation-v1',project,intake,key),project_id=project,intake_id=intake,machine_id=option('--machine'),epoch=7,native_id='12345678-1234-4123-a123-123456789012',managed=True,state='running',requested_workspace=workspace,workspace=resolved)
 save(s)
 if os.environ.get('FAKE_START_LOST')=='1':sys.exit(124)
 print(json.dumps({'session':s}));sys.exit(0)
if args[:2]==['sessions','show']:
 if os.environ.get('FAKE_SHOW_FAIL')=='1':sys.exit(124)
 s=load();case=os.environ.get('FAKE_FOREIGN','')
 if case=='intake':s['intake_id']='ffffffff-1234-4123-a123-123456789012'
 if case=='native':s['native_id']='ffffffff-1234-4123-a123-123456789012'
 if case=='machine':s['machine_id']='foreign-machine'
 if case=='epoch':s['epoch']+=1
 if case=='operation':s['operation_id']='ffffffff-1234-4123-a123-123456789012'
 if case=='requested':s['requested_workspace']='/foreign'
 if case=='resolved':s['workspace']='/foreign/.local/hub-worktrees/'+'b'*64
 if case=='mode':s['managed']=False
 if case=='project':s['project_id']='foreign/project'
 if case=='project-missing':s.pop('project_id')
 if case=='digest':s['workspace']=s['requested_workspace']+'/.local/hub-worktrees/'+'b'*64
 print(json.dumps({'session':s}));sys.exit(0)
if args[:2]==['sessions','send']:
 body=sys.stdin.read();key=option('--key');effects=root/'effects.json';v=json.loads(effects.read_text()) if effects.exists() else {};v.setdefault(key,body);effects.write_text(json.dumps(v))
 if os.environ.get('FAKE_SEND_FAIL')=='1':sys.exit(124)
 print(json.dumps({'command_wait':{'state':'acked','ack':{'outcome':'delivered'}}}));sys.exit(0)
if args[:2]==['sessions','capture']:
 print(json.dumps({'command_wait':{'state':'acked','ack':{'outcome':'delivered','capture':(root/'screen.txt').read_text()}}}));sys.exit(0)
if args[:2]==['sessions','stop']:
 if os.environ.get('FAKE_STOP_FAIL')=='1':sys.exit(1)
 s=load();s['state']='stopped';s['stop_confirmed']=True;save(s);print(json.dumps({'stop_wait':s}));sys.exit(0)
sys.exit(99)
'''
class ManagedOps(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='ops-managed-',dir=FIXTURE_PARENT);self.addCleanup(self.temp.cleanup)
  self.home=pathlib.Path(self.temp.name);self.home.chmod(0o700);self.bin=self.home/'bin';self.bin.mkdir(mode=0o700);self.fake=self.home/'fake';self.fake.mkdir(mode=0o700)
  (self.fake/'legacy.json').write_text('[]');(self.fake/'screen.txt').write_text('normal ready prompt')
  for name in ('a','hub','git','gh','systemctl','systemd-run'):
   p=self.bin/name;p.write_text(FAKE);p.chmod(0o700)
  for name,source in [('oncall-send',REPO/'ops/lib/send.sh'),('orch-send',REPO/'ops/lib/send.sh'),('session-ctl.sh',CTL)]:
   # Installed wrappers are executable copies; tracked send.sh is100644.
   target=self.bin/name;target.write_bytes(source.read_bytes());target.chmod(0o700)
  skills=self.home/'skills';(skills/'.git').mkdir(parents=True);(skills/'.agents/skills').mkdir(parents=True)
  self.env={'PATH':str(self.bin)+':/usr/bin:/bin','HOME':str(self.home),'TMPDIR':str(self.home),'FAKE':str(self.fake),'BASE_REPO':str(self.home/'base'),'SKILLS_REPO':str(skills),'SKILLS_DIR':str(skills/'.agents/skills'),'OPS_LOCK_DIR':str(self.home),'OPS_TIMERS':'0','OPS_HUB_REPORT':'0','OPS_HUB_MACHINE':'fixture-machine','MIN_RESTART_GAP':'0','PYTHONDONTWRITEBYTECODE':'1','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_ALLOW_PROTOCOL':'file','GIT_TERMINAL_PROMPT':'0'}
 def invoke(self,argv,*,success=True,extra=None):
  child=None
  try:
   child=subprocess.Popen(argv,env={**self.env,**(extra or {})},stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
   stdout,stderr=child.communicate(timeout=12)
   if success:self.assertEqual(child.returncode,0,(stdout+stderr).decode())
   else:self.assertNotEqual(child.returncode,0,(stdout+stderr).decode())
   return (stdout+stderr).decode()
  finally:
   if child is not None and child.poll() is None:
    os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
 def ctl(self,command='start',role='oncall',**kwargs):return self.invoke(['bash',str(CTL),role,command],**kwargs)
 def calls(self,name='hub'):
  p=self.fake/(name+'.jsonl');return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
 def count(self,kind):return sum(a[:2]==['sessions',kind] for a in self.calls())
 def state(self,role='oncall'):return self.home/'tmp/agent-reports'/role/'state'
 def record(self,role='oncall'):return json.loads((self.state(role)/'managed/current.json').read_text())
 def assert_no_native_mutations(self):
  self.assertFalse(any(a[0] in ('start','send','kill','forget','engines') for a in self.calls('a')))
 def test_model_less_interactive_binding_and_useful_brief(self):
  self.ctl();r=self.record();self.assertTrue(r['brief_sent']);self.assertEqual(r['binding']['machine'],'fixture-machine');self.assertNotEqual(r['binding']['resolved'],r['requested'])
  start=next(a for a in self.calls() if a[:2]==['sessions','start'])
  for banned in ('--engine','--model','--profile','--force','--reason','--with-goal','--headless','--intake-worktree'):self.assertNotIn(banned,start)
  self.assertIn('--interactive',start);self.assertIn('--intake-task-id',start)
  bodies=json.loads((self.fake/'effects.json').read_text());self.assertIn('Run the oncall check now:',next(iter(bodies.values())));self.assertNotIn('/loop',next(iter(bodies.values())))
  self.assertEqual((self.state()/'managed/current.json').stat().st_mode&0o777,0o600);self.assert_no_native_mutations()
 def test_lost_start_response_reconciles_original_id_without_duplicate(self):
  self.ctl(extra={'FAKE_START_LOST':'1'});r=self.record();self.ctl();self.assertEqual(self.count('start'),1);self.assertEqual(self.record()['key'],r['key']);self.assert_no_native_mutations()
 def test_unknown_start_and_reconcile_only_never_resubmit(self):
  self.ctl(success=False,extra={'FAKE_SHOW_FAIL':'1'});key=self.record()['key'];self.assertEqual(self.count('start'),1)
  self.ctl('reconcile-only',success=False,extra={'FAKE_SHOW_FAIL':'1'});self.ctl('reconcile-only');self.assertEqual(self.count('start'),1);self.assertEqual(self.count('send'),0)
  self.ctl();self.assertEqual(self.count('start'),1);self.assertEqual(self.record()['key'],key);self.assertEqual(self.count('send'),1)
 def test_watchdog_resumes_original_brief_after_binding_loss_without_new_start(self):
  self.ctl(success=False,extra={'FAKE_SHOW_FAIL':'1'});self.assertFalse(self.record()['brief_sent'])
  self.ctl('check');self.assertEqual(self.count('start'),1);self.assertEqual(self.count('send'),1);self.assertTrue(self.record()['brief_sent'])
 def test_lost_brief_ack_reuses_exact_send_key_and_effect(self):
  self.ctl(success=False,extra={'FAKE_SEND_FAIL':'1'});self.ctl();self.assertEqual(self.count('start'),1)
  sends=[a for a in self.calls() if a[:2]==['sessions','send']];self.assertEqual(len(sends),2);self.assertEqual(sends[0],sends[1]);self.assertEqual(len(json.loads((self.fake/'effects.json').read_text())),1)
 def test_all_foreign_binding_fields_hold_without_send_stop_or_new_start(self):
  self.ctl();initial=(self.count('start'),self.count('send'),self.count('stop'))
  for field in ('intake','native','machine','epoch','operation','requested','resolved','mode','project','project-missing','digest'):
   with self.subTest(field=field):self.ctl('reconcile-only',success=False,extra={'FAKE_FOREIGN':field})
  self.assertEqual((self.count('start'),self.count('send'),self.count('stop')),initial)
 def test_foreign_prebinding_does_not_claim_or_deliver(self):
  self.ctl(success=False,extra={'FAKE_FOREIGN':'intake'});self.assertEqual(self.count('send'),0);self.assertIsNone(self.record()['binding']);self.assert_no_native_mutations()
 def test_unknown_stop_never_starts_replacement(self):
  self.ctl();old=self.record();self.ctl('restart',success=False,extra={'FAKE_STOP_FAIL':'1'});self.assertEqual(self.count('start'),1);self.assertFalse(self.record()['retired']);self.assertEqual(self.record()['key'],old['key'])
 def test_qualified_stop_then_one_new_generation_archives_old(self):
  self.ctl();old=self.record();self.ctl('restart');new=self.record();self.assertEqual(self.count('start'),2);self.assertEqual(self.count('stop'),1);self.assertNotEqual(old['key'],new['key'])
  history=self.state()/'managed'/(old['key']+'.retired.json');self.assertTrue(json.loads(history.read_text())['retired']);self.assert_no_native_mutations()
 def test_natural_exit_never_authorizes_restart_as_confirmed_stop(self):
  self.ctl();p=self.fake/'session.json';s=json.loads(p.read_text());s.update(state='failed',stop_confirmed=False);p.write_text(json.dumps(s))
  self.ctl('restart',success=False,extra={'FAKE_STOP_FAIL':'1'});self.assertEqual(self.count('start'),1);self.assertFalse(self.record()['retired'])
 def test_inbox_saved_and_delivered_via_exact_managed_hub_id(self):
  self.ctl();self.invoke([str(self.bin/'oncall-send'),'useful inbox item']);self.assertEqual(self.count('send'),2);self.assertEqual(len(list((self.home/'tmp/agent-reports/oncall/inbox').glob('*.md'))),1);self.assert_no_native_mutations()
 def test_inbox_foreign_binding_retains_file_without_native_fallback(self):
  self.ctl();self.invoke([str(self.bin/'oncall-send'),'durable pending inbox'],success=False,extra={'FAKE_FOREIGN':'native'});self.assertEqual(self.count('send'),1);self.assertEqual(len(list((self.home/'tmp/agent-reports/oncall/inbox').glob('*.md'))),1);self.assert_no_native_mutations()
 def test_managed_malformed_or_symlink_state_never_native_fallback(self):
  self.ctl();p=self.state()/'managed/current.json';p.write_text('{}');self.invoke([str(self.bin/'oncall-send'),'still safe'],success=False);self.assert_no_native_mutations()
 def test_recent_inbox_healthy_and_stale_inbox_requires_qualified_recovery(self):
  self.ctl();f=self.home/'tmp/agent-reports/oncall/inbox/item.md';f.write_text('pending');self.ctl('check');self.assertEqual(self.count('start'),1)
  old=time.time()-1300;os.utime(f,(old,old));(self.state()/'last_start').write_text(str(int(old)))
  self.ctl('check');self.assertEqual(self.count('stop'),1);self.assertEqual(self.count('start'),2);self.assert_no_native_mutations()
 def test_permission_menu_never_receives_automatic_keystrokes(self):
  (self.fake/'screen.txt').write_text('Do you trust this folder? Yes, proceed');self.ctl();self.assert_no_native_mutations();self.assertFalse(any('--hex' in x for a in self.calls() for x in a))
 def test_managed_usage_limit_both_roles_stops_and_replaces_through_hub(self):
  for role in ('oncall','orchestrator'):
   self.ctl(role=role);(self.fake/'screen.txt').write_text('You have hit your weekly limit')
   before=self.count('start');self.ctl('check',role=role);self.assertEqual(self.count('start'),before+1)
   (self.fake/'screen.txt').write_text('normal ready prompt')
  self.assert_no_native_mutations()
 def test_orchestrator_proof_and_tick_deadlines_retained(self):
  self.ctl(role='orchestrator');state=self.state('orchestrator');state.joinpath('last_start').write_text(str(int(time.time())-2000))
  self.ctl('check',role='orchestrator');self.assertTrue((state/'proof_due').exists());due,path,engine=(state/'proof_due').read_text().split();self.assertEqual(engine,'hub')
  ticks=self.home/'tmp/agent-reports/orchestrator-ticks.jsonl';ticks.write_text(json.dumps({'ts':datetime.datetime.now(datetime.timezone.utc).isoformat()})+'\n')
  self.ctl('check',role='orchestrator');self.assertFalse((state/'proof_due').exists())
 def test_live_unmanaged_role_read_only_until_owner_handoff(self):
  ws=self.home/'git/agent-hub-wt-oncall-run/ops/oncall';ws.mkdir(parents=True)
  (self.fake/'legacy.json').write_text(json.dumps([{'id':'legacy-oncall','workspace':str(ws),'tag':'oncall-engineer','phase':'running','worker_alive':True}]))
  self.ctl();self.assertEqual(self.count('start'),0)
  self.ctl('restart',success=False);self.assertEqual(self.count('stop'),0);self.assert_no_native_mutations()
 def test_local_engine_overrides_cannot_pin_new_managed_route(self):
  self.ctl(extra={'ENGINE':'claude','ENGINE_SELECTED':'1','ONCALL_ENGINE':'opencode'});start=next(a for a in self.calls() if a[:2]==['sessions','start']);self.assertNotIn('--engine',start);self.assert_no_native_mutations()
 def test_default_machine_is_explicit_not_fleet_guess(self):
  env=self.env.pop('OPS_HUB_MACHINE');self.addCleanup(lambda:self.env.update(OPS_HUB_MACHINE=env));self.ctl();self.assertEqual(self.record()['target_machine'],'hetzner-aplexer')
 def test_skills_sync_retains_actual_owned_file_git_fast_forward(self):
  # Same file-only Git behavior exercised by the original suite, retained
  # without any remote network or provider access.
  realgit='/usr/bin/git';origin=self.home/'origin.git';base=self.home/'base';push=self.home/'pusher';rundir=self.home/'git/agent-hub-wt-oncall-run'
  def git(*args):
   return self.invoke([realgit,*map(str,args)]).strip()
  git('init','-q','--bare','-b','main',origin);git('clone','-q',origin,base)
  git('-C',base,'-c','user.name=t','-c','user.email=t@example.invalid','commit','-q','--allow-empty','-m','one')
  git('-C',base,'push','-q','origin','HEAD:main');git('-C',base,'fetch','-q','origin');git('-C',base,'worktree','add','-q','--detach',rundir,'origin/main')
  old=git('-C',rundir,'rev-parse','HEAD');git('clone','-q',origin,push)
  git('-C',push,'-c','user.name=t','-c','user.email=t@example.invalid','commit','-q','--allow-empty','-m','two')
  git('-C',push,'push','-q','origin','HEAD:main');new=git('-C',push,'rev-parse','HEAD')
  output=self.ctl('skills-sync',extra={'PATH':'/usr/bin:'+str(self.bin)+':/bin'})
  self.assertNotEqual(old,new);self.assertEqual(git('-C',rundir,'rev-parse','HEAD'),new);self.assertIn('run checkout updated',output);self.assertIn('skills synced',output)
 def test_legacy_nested_inbox_delivery_is_explicit_until_adopted(self):
  ws=self.home/'git/agent-hub-wt-oncall-run/ops/oncall/.local/hub-worktrees/legacy'
  (self.fake/'legacy.json').write_text(json.dumps([{'id':'legacy-oncall','workspace':str(ws),'tag':'oncall-engineer','phase':'running','worker_alive':True}]))
  output=self.invoke([str(self.bin/'oncall-send'),'nested inbox item'])
  self.assertIn('DELIVERY=delivered',output);self.assertEqual(self.count('start'),0)
  sends=[a for a in self.calls('a') if a[0]=='send'];self.assertEqual(len(sends),1);self.assertEqual(sends[0][1],'legacy-oncall')
 def test_hub_rejection_does_not_enter_local_quota_or_native_fallback(self):
  self.ctl(success=False,extra={'FAKE_START_REJECT':'1'});self.assertEqual(self.count('start'),1)
  self.ctl('reconcile-only',success=False);self.assertEqual(self.count('start'),1);self.assert_no_native_mutations()
 def test_first_binding_requires_exact_public_project(self):
  for field in ('project-missing','project'):
   with self.subTest(field=field):
    self.ctl(success=False,extra={'FAKE_FOREIGN':field})
    self.assertIsNone(self.record()['binding']);self.assertEqual(self.count('send'),0)
  self.assertEqual(self.count('start'),1);self.assert_no_native_mutations()
 def test_first_binding_rejects_wrong_actual_intake_digest(self):
  self.ctl(success=False,extra={'FAKE_FOREIGN':'digest'})
  self.assertIsNone(self.record()['binding']);self.assertEqual(self.count('send'),0);self.assert_no_native_mutations()
 def test_unredirected_requested_workspace_is_valid(self):
  self.ctl(extra={'FAKE_WORKSPACE_MODE':'plain'});r=self.record()
  self.assertEqual(r['binding']['resolved'],r['requested']);self.assertTrue(r['brief_sent'])
 def test_authenticated_retained_prefix_with_actual_intake_digest_is_valid(self):
  self.ctl(extra={'FAKE_WORKSPACE_MODE':'retained'});r=self.record()
  digest=__import__('hashlib').sha256(json.dumps({'intake_id':r['intake'],'project_id':r['project']},sort_keys=True).encode()).hexdigest()
  self.assertEqual(r['binding']['resolved'],'/retained/original/checkout/.local/hub-worktrees/'+digest)
  self.assertTrue(r['brief_sent']);self.assertEqual(self.count('start'),1)
 def test_watchdog_terminal_requires_successful_owned_stop_before_one_replacement(self):
  self.ctl();old=self.record();p=self.fake/'session.json';s=json.loads(p.read_text());s['state']='failed';p.write_text(json.dumps(s))
  self.ctl('check');self.assertEqual(self.count('stop'),1);self.assertEqual(self.count('start'),2)
  self.assertNotEqual(old['key'],self.record()['key'])
  history=self.state()/'managed'/(old['key']+'.retired.json');self.assertTrue(json.loads(history.read_text())['retired'])
  calls=[a[:2] for a in self.calls() if a[:2] in (['sessions','start'],['sessions','stop'])]
  self.assertEqual(calls,[['sessions','start'],['sessions','stop'],['sessions','start']]);self.assert_no_native_mutations()
 def test_watchdog_terminal_unknown_stop_retains_original_custody(self):
  self.ctl();old=self.record();p=self.fake/'session.json';s=json.loads(p.read_text());s['state']='stopped';s['stop_confirmed']=False;p.write_text(json.dumps(s))
  self.ctl('check',success=False,extra={'FAKE_STOP_FAIL':'1'})
  self.assertEqual(self.count('stop'),1);self.assertEqual(self.count('start'),1);self.assertEqual(self.record()['key'],old['key']);self.assertFalse(self.record()['retired'])
 def test_watchdog_unknown_transport_foreign_and_brief_ack_never_replace(self):
  self.ctl()
  for extra in ({'FAKE_SHOW_FAIL':'1'},{'FAKE_FOREIGN':'project'},{'FAKE_FOREIGN':'digest'}):
   with self.subTest(extra=extra):self.ctl('check',success=False,extra=extra)
  self.assertEqual(self.count('start'),1);self.assertEqual(self.count('stop'),0)
  record=self.record();record['brief_sent']=False;(self.state()/'managed/current.json').write_text(json.dumps(record))
  self.ctl('check',success=False,extra={'FAKE_SEND_FAIL':'1'})
  self.assertEqual(self.count('start'),1);self.assertEqual(self.count('stop'),0);self.assertFalse(self.record()['brief_sent']);self.assert_no_native_mutations()
 def test_missing_tmpdir_uses_owned_repo_run_parent_and_cleans_only_new_tree(self):
  repo=self.home/'synthetic-repo';repo.mkdir(mode=0o700)
  with owned_fixture_parent(repo,None) as parent:
   self.assertEqual(parent.parent,repo/'.local/tmp');self.assertTrue(parent.name.startswith('ops-managed-run-'))
   self.assertEqual(parent.stat().st_mode&0o777,0o700);(parent/'owned').write_text('fixture')
   saved=parent
  self.assertFalse(saved.exists());self.assertTrue(repo.is_dir());self.assertFalse((repo/'.local').exists())
 def test_supplied_tmpdir_is_preserved_without_mode_or_environment_change(self):
  parent=self.home/'provided';parent.mkdir(mode=0o700);keep=parent/'keep';keep.write_text('preserve')
  before=parent.stat();environment=dict(os.environ)
  with owned_fixture_parent(self.home/'not-created',str(parent)) as observed:self.assertEqual(observed,parent)
  after=parent.stat();self.assertEqual((before.st_dev,before.st_ino,before.st_mode),(after.st_dev,after.st_ino,after.st_mode))
  self.assertEqual(keep.read_text(),'preserve');self.assertEqual(dict(os.environ),environment)
 def test_untrusted_tmpdir_or_fallback_component_refuses_without_chmod(self):
  unsafe=self.home/'unsafe';unsafe.mkdir(mode=0o775);unsafe.chmod(0o775)
  with self.assertRaises(RuntimeError):
   with owned_fixture_parent(self.home,str(unsafe)):self.fail('untrusted parent accepted')
  self.assertEqual(unsafe.stat().st_mode&0o777,0o775)
  repo=self.home/'symlink-repo';repo.mkdir(mode=0o700);(repo/'.local').symlink_to(unsafe,target_is_directory=True)
  with self.assertRaises(OSError):
   with owned_fixture_parent(repo,None):self.fail('symlink accepted')
  self.assertEqual(list(unsafe.iterdir()),[]);self.assertEqual(unsafe.stat().st_mode&0o777,0o775)
 def test_hourly_inbox_instruction_retained(self):
  self.assertIn('Never put DataOps/dapier access in `NEEDS_ALEXEY.md`',(REPO/'ops/oncall/hourly-check.md').read_text())
if __name__=='__main__':
 with owned_fixture_parent(REPO,os.environ.get('TMPDIR')) as FIXTURE_PARENT:
  unittest.main(argv=['session-ctl.test.sh'],verbosity=2)
PY_TESTS
