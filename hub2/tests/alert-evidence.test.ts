import { stripTypeScriptTypes } from 'node:module';
import { types as utilTypes } from 'node:util';
import { constants as artifactConstants } from 'node:fs';
import { isAbsolute as artifactIsAbsolute } from 'node:path';
import { test } from 'node:test';
import { open as openFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { chmodSync, copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { registerTypeScriptResolveHook, installCompatGlobals } from '../node/compat.ts';
registerTypeScriptResolveHook(); installCompatGlobals();
const ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const SELF = fileURLToPath(import.meta.url);
const TOKEN = 'synthetic-credential-monitor-operator-token-000000000';
const NONCE = '1'.repeat(32);
const RELEASE = 'a'.repeat(40);
const sha = (data: string | Uint8Array) => createHash('sha256').update(data).digest('hex');
function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
function privateJson(path: string, value: any) { writeFileSync(path, canonical(value) + '\n', { mode: 0o600 }); chmodSync(path, 0o600); }
// Only semantic fixtures omit fsync latency. The store still owns exclusive
// record creation, immutable names and verification of every committed record.
async function semanticWriter(value:any,handle:any){writeFileSync(handle.fd,canonical(value)+'\n');}
function snapshotFiles(state:string){return readdirSync(state).filter(name=>/^snapshot-[0-9a-f]{32}-[0-9]+\.json$/.test(name))
  .sort((a,b)=>Number(a.split('-').at(-1)!.split('.')[0])-Number(b.split('-').at(-1)!.split('.')[0])).map(name=>join(state,name));}

function syntheticEnvironment(home: string): NodeJS.ProcessEnv {
  const env = {...process.env,HOME:home,TMPDIR:join(ROOT,'.local/tmp')};
  for(const key of Object.keys(env)) if(/^(?:HUB_|HUB2_|APLEXER_|DTC_|XDG_)/.test(key) || /TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY/.test(key)) delete env[key];
  return env;
}
function temp() {
  const parent = join(ROOT, '.local/tmp'); mkdirSync(parent, {recursive:true});
  return mkdtempSync(join(parent, 'ingress-'));
}
async function fixture(directory: string, options: any = {}) {
  const producer = options.producer ?? 'credential_monitor', diagnostic = producer !== 'credential_monitor';
  const artifactNames = ['node/alert-evidence.ts','node/main.ts','src/worker.ts', ...(diagnostic ? ['src/model-picker.ts','src/routing-pool.ts'] : [])];
  const installed = join(directory, 'installed'), state = join(directory, 'state');
  if (!existsSync(installed)) {
    mkdirSync(join(installed, 'node'), {recursive:true, mode:0o700});
    cpSync(join(ROOT, 'hub2/src'), join(installed, 'src'), {recursive:true});
    for (const name of ['alert-evidence.ts','main.ts']) copyFileSync(join(ROOT,'hub2/node',name),join(installed,'node',name));
    for (const name of artifactNames) chmodSync(join(installed,name),0o600);
  }
  const paths = artifactNames.map(name=>join(installed,name));
  const artifacts = paths.map(path=>({path,sha256:sha(readFileSync(path))}));
  const source_id = sha(canonical(artifacts));
  if (!existsSync(state)) {
    mkdirSync(state,{mode:0o700});
    privateJson(join(state,'enrollment.json'),{kind:diagnostic?'hub_picker_diagnostic_enrollment_v1':'hub_credential_monitor_enrollment_v1',producer,recipient:'dev@dtcdev.click',
      enrollment_id:'2'.repeat(32),source_id,release_sha:RELEASE,artifacts});
    privateJson(join(state,'journal.json'),{kind:diagnostic?'hub_picker_diagnostic_journal_v1':'hub_credential_monitor_journal_v1',...(diagnostic?{producer}:{}),source_id,enrollment_id:'2'.repeat(32),
      occurrence_count:0,started_total:0,finished_total:0});
  }
  const loadedArtifacts = Object.fromEntries(artifacts.map(a=>[a.path,a.sha256]));
  const {AlertEvidence} = await import(pathToFileURL(join(installed,'node/alert-evidence.ts')).href);
  const observer = await AlertEvidence.load(state,{loadedArtifacts,...options});
  const {default:worker} = await import(pathToFileURL(join(installed,'src/worker.ts')).href);
  const policy=JSON.parse(readFileSync(join(ROOT,'config/model-policy.json'),'utf8'));
  const candidates=policy.candidates.filter((row:any)=>['codex-sol-medium','claude-sonnet-medium'].includes(row.id));
  assert.equal(candidates.length,2);
  const model:{unhealthy:boolean;excludeOne:boolean;stale:boolean;failRouting:boolean;beforeRead?:()=>Promise<void>;actor:any}={unhealthy:true,excludeOne:diagnostic,stale:false,failRouting:false,actor:{id:'synthetic-observer',kind:'operator',scopes:['operator:admin']}};
  const namespace=(kind:string)=>({idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(input:any,init?:any)=>{
    const req=input instanceof Request?input:new Request(input,init),url=new URL(req.url);
    if(kind==='control'){
      if(url.pathname==='/_verify')return Response.json({actor:model.actor});
      if(url.pathname==='/_routing-policy'){if(model.beforeRead)await model.beforeRead();if(model.failRouting)throw new Error('synthetic routing failure');return Response.json({version:17,candidates});}
      if(url.pathname==='/api/directory')return Response.json({items:[{id:'h1'}],next:null});
    }
    if(kind==='accounts')return Response.json({account:{enabled:true},credential_usable:!model.unhealthy||model.excludeOne&&id==='codex',credential_status:model.unhealthy&&!(model.excludeOne&&id==='codex')?'missing':'ready',
      quota:{remaining:60,captured_at:Date.now()-1000,valid_until:Date.now()+(model.stale?-1:900000)},fresh:!model.stale,hosts:['h1']});
    if(kind==='machines')return Response.json({machine_id:'h1',connected:true,os:'linux',engines:['codex','claude'],capabilities:['headless:codex','headless:claude','effort:codex','effort:claude']});
    throw new Error('unexpected synthetic route');
  }})});
  const env:any={CONTROL:namespace('control'),ACCOUNTS:namespace('accounts'),MACHINES:namespace('machines'),...(diagnostic?{PICKER_DIAGNOSTIC_EVIDENCE:{[producer]:observer}}:{CREDENTIAL_MONITOR_EVIDENCE:observer})};
  const post=(marked=true)=>worker.fetch(new Request('http://127.0.0.1/api/model-picker/pick',{method:'POST',
    headers:{Authorization:'Bearer '+TOKEN,'Content-Type':'application/json',...(marked?{'X-Hub-Alert-Producer':'credential_monitor'}:{})},body:'{}'}),env);
  const sample=(nonce=NONCE,method='GET')=>worker.fetch(new Request('http://127.0.0.1/api/model-picker/'+(diagnostic?'diagnostic-evidence?producer='+producer+'&nonce=':'credential-monitor-evidence?nonce=')+nonce,
    {method,headers:{Authorization:'Bearer '+TOKEN}}),env);
  return {observer,worker,env,post,sample,state,installed,loadedArtifacts,source_id,model};
}
async function serverMode(directory:string,fail:boolean,port=0,producer='credential_monitor') {
  // These HTTP/aggregate fixtures test transport and decision semantics, not
  // host fsync latency. Dedicated default-writer tests exercise real durability.
  const f=await fixture(directory,fail?{producer,writer:async()=>{throw new Error('synthetic write fault');}}:
    {producer,timeoutMs:2000,writer:semanticWriter});
  const server=createServer(async(req,res)=>{
    if(req.url==='/__fixture/idle'){await (f.observer as any).queue;res.writeHead(200);res.end('{}');return;}
    if(req.url==='/__fixture/stale'){f.model.stale=true;f.model.unhealthy=false;res.writeHead(200);res.end('{}');return;}
    if(req.url==='/__fixture/healthy'){f.model.unhealthy=false;res.writeHead(200);res.end('{}');return;}
    const chunks:Buffer[]=[]; for await(const chunk of req)chunks.push(Buffer.from(chunk));
    const result=await f.worker.fetch(new Request('http://127.0.0.1'+req.url,{method:req.method,
      headers:req.headers as any,...(req.method==='POST'?{body:Buffer.concat(chunks)}:{})}),f.env);
    res.writeHead(result.status,Object.fromEntries(result.headers));res.end(Buffer.from(await result.arrayBuffer()));
  });
  server.listen(port,'127.0.0.1',()=>console.log(JSON.stringify({port:(server.address() as any).port,source_id:f.source_id,release_sha:RELEASE,producer})));
  process.on('SIGTERM',()=>server.close(()=>process.exit(0)));
}
async function startServerChild(directory:string,fail=false) {
  const child=spawn(process.execPath,['--experimental-transform-types',SELF,'--fixture-server',directory,fail?'fail':'good'],{stdio:['ignore','pipe','pipe'],env:syntheticEnvironment(directory)});
  let log='',errors='';child.stderr!.on('data',b=>errors+=b);
  const info=await new Promise<any>((resolveInfo,reject)=>{
    const timer=setTimeout(()=>{child.kill('SIGKILL');reject(new Error('fixture startup timed out '+errors));},6000);
    child.stdout!.on('data',b=>{log+=b;const line=log.split('\n')[0];try{const value=JSON.parse(line);clearTimeout(timer);resolveInfo(value);}catch{}});
    child.once('exit',()=>{clearTimeout(timer);reject(new Error('fixture exited '+errors));});
  });
  return {child,info,base:'http://127.0.0.1:'+info.port};
}
async function stop(child:ReturnType<typeof spawn>, signal:any='SIGTERM') {
  if(child.exitCode!==null || child.signalCode!==null)return;
  await new Promise<void>(resolveDone=>{child.once('exit',()=>resolveDone());child.kill(signal);});
}
// Exercise the actual private verifier/bounded method, with ONLY in-memory IO.
// No new production export, server, DB, socket or timer sleep is needed.
function artifactHarness(options:any={}) {
  const source=readFileSync(join(ROOT,'hub2/node/alert-evidence.ts'),'utf8');
  const start=source.indexOf('async function verifyArtifacts('),end=source.indexOf('\nconst FAILURE_REASONS',start);
  assert.ok(start>=0&&end>start);
  const helpers=source.slice(source.indexOf('export function canonical('),source.indexOf('// Exhaustive type-checked'))
    .replace('export function canonical(','function canonical(');
  const progress=source.slice(source.indexOf('// Closed per-slot progress'),source.indexOf('const ownDeadlines'));
  assert.ok(progress.includes('function firstPersistFailure('));
  const persistence=source.slice(source.indexOf('const ownDeadlines'),source.indexOf('class PrivateStore'))
    .replace('export function classifyPersistFailure(','function classifyPersistFailure(');
  const projections=source.slice(source.indexOf('const PERSIST_STAGES'),source.indexOf('// Closed per-slot progress'))
    .replace('export function persistDiagnosticFields(','function persistDiagnosticFields(');
  const failures=source.slice(source.indexOf('const FAILURE_REASONS'),source.indexOf('/** One live Node incarnation.'));
  const methods=source.slice(source.indexOf('  invalidate(reason:'),source.indexOf('  async close() { this.invalidate'))
    .replaceAll('private async ','async ').replaceAll('private persist(','persist(');
  const names=Array.from({length:options.artifactCount??5},(_,i)=>'/synthetic/artifact-'+i);
  const bytes=new Map(names.map((name,i)=>[name,Buffer.from('immutable source '+i)]));
  const artifacts=names.map(path=>({path,sha256:sha(bytes.get(path)!)}));
  const loaded=Object.fromEntries(artifacts.map(a=>[a.path,a.sha256]));
  const entry:any={producer:options.producer??'picker_excluded',artifacts,source_id:sha(canonical(artifacts))};
  const waiting:Array<()=>void>=[],opened:string[]=[],closed:string[]=[];
  let active=0,maximum=0,paused=false;
  const pause=async(operation:string,path:string)=>{
    if(!paused&&options.pause?.operation===operation&&path===names[options.pause.slot]){
      paused=true;await new Promise<void>(resolve=>waiting.push(resolve));
    }
  };
  const open=async(path:string,flags:number)=>{
    assert.equal(flags,artifactConstants.O_RDONLY|artifactConstants.O_NOFOLLOW|artifactConstants.O_NONBLOCK);
    opened.push(path);await pause('open',path);
    if(options.openError)throw options.openError;
    const data=bytes.get(path)!;assert.ok(data);active++;maximum=Math.max(maximum,active);
    return {
      async stat(){
        await pause('stat',path);
        if(options.statError&&path===names[0])throw options.statError;
        return {isFile:()=>true,uid:process.geteuid!(),mode:0o100600,nlink:1,size:data.length,...options.metadata};
      },
      async read(buffer:Buffer){
        await pause('read',path);
        if(options.delayed&&(!options.delayOnlySecond||path===names[1]))await new Promise<void>(resolve=>waiting.push(resolve));
        buffer.set(data);return {bytesRead:data.length};
      },
      async close(){await pause('close',path);closed.push(path);active--;if(options.closeError&&path===names[options.closeSlot??1])throw options.closeError;},
    };
  };
  const injectedHash=(algorithm:string)=>{
    const result=createHash(algorithm);
    return {update(data:any){
      if(options.hashError&&Buffer.isBuffer(data)&&data.equals(bytes.get(names[options.hashSlot??0])!))throw options.hashError;
      if(options.proofError&&typeof data==='string'&&data===canonical(artifacts))throw options.proofError;
      result.update(data);return result;
    }};
  };
  const body=stripTypeScriptTypes(source.slice(source.indexOf('const LIMIT ='),source.indexOf('type Producer ='))+helpers+projections+progress+persistence+source.slice(start,end)+failures+
    '\nclass Methods{'+methods+'};')+'\nreturn {verifyArtifacts,methods:Methods.prototype,freezeArtifactProgress,artifactDiagnosticFields,classifyPersistFailure,persistTimerLateness,persistStep,persistAge:typeof persistAge==="function"?persistAge:undefined,persistBudget:typeof persistBudget==="function"?persistBudget:undefined};';
  const actual=new Function('open','constants','isAbsolute','requiredArtifacts','diagnosticArtifacts','createHash','utilTypes','performance',body)
    (open,artifactConstants,artifactIsAbsolute,names.slice(0,3),names,injectedHash,utilTypes,
      {now:()=>options.clock?options.clock():0});
  const timeout=Number(source.match(/const TIMEOUT_MS = (\d+);/)![1]);
  const journal={kind:'hub_picker_diagnostic_journal_v1',producer:'picker_excluded',source_id:entry.source_id,
    enrollment_id:'2'.repeat(32),occurrence_count:0,started_total:0,finished_total:0};
  const store={identity:async()=>{},read:async()=>entry,write:async()=>{},verify:async()=>canonical(journal)};
  const observer=Object.assign(Object.create(actual.methods),{store,enrollment:entry,journal,writer:undefined,timeoutMs:timeout,
    loaded,boot:'synthetic',dirty:false,pending:0,active:new Set(),requests:0,queue:Promise.resolve()});
  return {verify:(trace?:any)=>actual.verifyArtifacts(entry,loaded,trace),entry,loaded,names,bytes,opened,closed,waiting,observer,
    project:actual.artifactDiagnosticFields,freeze:actual.freezeArtifactProgress,lateness:actual.persistTimerLateness,step:actual.persistStep,age:actual.persistAge,budget:actual.persistBudget,
    stats:()=>({active,maximum}),bounded:(work:Promise<void>,trace:any)=>actual.methods.bounded.call({timeoutMs:timeout},work,trace),timeout};
}
async function artifactMicrotasks(){for(let i=0;i<40;i++)await Promise.resolve();}
async function drainArtifactFake(h:any){
  for(let i=0;i<20;i++){
    for(const resolve of h.waiting.splice(0))resolve();
    await artifactMicrotasks();
    if(h.stats().active===0)return;
  }
  assert.fail('owned fake handles did not close');
}

async function defineTests() {

  test('persist diagnostic origin precedes eager artifact read without moving timer',async t=>{
    let clock=0;const options={clock:()=>clock++,delayed:true};
    const h=artifactHarness(options);
    t.mock.timers.enable({apis:['setTimeout']});
    try{
      h.observer.begin();await artifactMicrotasks();
      assert.equal(h.waiting.length,2,'both first-batch reads are held in real persist');
      clock=151;t.mock.timers.tick(149);await artifactMicrotasks();
      assert.equal(h.observer.dirty,false,'existing 150ms timer has not fired early');
      clock=170;t.mock.timers.tick(1);await artifactMicrotasks();await h.observer.queue;
      options.delayed=false;await drainArtifactFake(h);
      const first=await h.observer.snapshot(NONCE);
      assert.equal(first.unavailable_reason,'persist_failed');assert.equal(first.unavailable_persist_stage,'artifacts');
      assert.equal(first.unavailable_persist_error,'deadline');assert.equal(first.unavailable_persist_timer,'on_tick');
      assert.equal(first.unavailable_persist_before_stage,'under_25');
      assert.equal(first.unavailable_persist_stage_await,'150_or_more');
      assert.equal(first.unavailable_artifact_completed,0);
      assert.deepEqual(first.unavailable_artifact_slots,[{slot:0,operation:'read'},{slot:1,operation:'read'}]);
      assert.equal(h.timeout,150);assert.equal(h.stats().active,0);
      clock=900;h.observer.invalidate('snapshot_route_rejected');
      assert.deepEqual(await h.observer.snapshot(NONCE),first,'late work cannot change first timing buckets');
      assert.ok(!JSON.stringify(first).includes('/synthetic/'));
    }finally{
      options.delayed=false;
      try{await drainArtifactFake(h);}finally{t.mock.timers.reset();}
    }
  });
  test('persist diagnostic origin refreshes before each coalesced eager write',async t=>{
    let clock=0,writes=0,releaseFirst=()=>{};const options={clock:()=>clock++,delayed:false};
    const h=artifactHarness(options);
    h.observer.store.write=async()=>{if(++writes===1)await new Promise<void>(yes=>releaseFirst=yes);};
    t.mock.timers.enable({apis:['setTimeout']});
    try{
      const id=h.observer.begin();await artifactMicrotasks();assert.equal(writes,1);
      const finished=h.observer.finish(id,'observation');
      clock=300;options.delayed=true;releaseFirst();await artifactMicrotasks();
      assert.equal(h.waiting.length,2,'next cumulative write is held at its first artifact batch');
      clock=470;t.mock.timers.tick(150);await artifactMicrotasks();await finished;
      options.delayed=false;await drainArtifactFake(h);
      const first=await h.observer.snapshot(NONCE);
      assert.equal(first.unavailable_reason,'persist_failed');assert.equal(first.unavailable_persist_stage,'artifacts');
      assert.equal(first.unavailable_persist_error,'deadline');assert.equal(first.unavailable_persist_timer,'on_tick');
      assert.equal(first.unavailable_persist_before_stage,'under_25','not the previous write origin');
      assert.equal(first.unavailable_persist_stage_await,'150_or_more');
      assert.equal(writes,2);assert.equal(h.stats().active,0);assert.equal(h.timeout,150);
      clock=999;assert.deepEqual(await h.observer.snapshot(NONCE),first);
    }finally{
      options.delayed=false;releaseFirst();
      try{await drainArtifactFake(h);}finally{t.mock.timers.reset();}
    }
  });
  test('persist budget distinguishes time spent before sync from pending sync without extending timeout',async t=>{
    for(const [entry,expected] of [[140,['75_to_149','under_25']],[10,['under_25','75_to_149']]] as const){
      let clock=0,release!:()=>void;const h=artifactHarness({clock:()=>clock});
      const held=new Promise<void>(yes=>release=yes);
      h.observer.store.write=async(_value:any,_writer:any,trace:any)=>{
        clock=entry;await h.step(trace,'snapshot_sync',()=>held);
        clock=999;await h.step(trace,'snapshot_close',async()=>{});
      };
      t.mock.timers.enable({apis:['setTimeout']});
      try{
        h.observer.begin();await artifactMicrotasks();clock=150;t.mock.timers.tick(150);
        await artifactMicrotasks();await h.observer.queue;
        const first=await h.observer.snapshot(NONCE);
        assert.equal(first.unavailable_reason,'persist_failed');assert.equal(first.unavailable_persist_stage,'snapshot_sync');
        assert.equal(first.unavailable_persist_error,'deadline');assert.equal(first.unavailable_persist_timer,'on_tick');
        assert.equal(first.unavailable_persist_before_stage,expected[0]);assert.equal(first.unavailable_persist_stage_await,expected[1]);
        assert.equal(h.timeout,150);
        release();await artifactMicrotasks();h.observer.invalidate('snapshot_route_rejected');
        assert.deepEqual(await h.observer.snapshot(NONCE),first,'late sync/close/fault cannot rewrite first frozen ages');
        assert.ok(!JSON.stringify(first).includes('999'));assert.ok(!JSON.stringify(first).includes('/synthetic/'));
      }finally{release();await artifactMicrotasks();t.mock.timers.reset();}
    }
  });
  test('persist budget projects closed ages and invalid mock clocks as unspecified',()=>{
    let clock:any=150;const h=artifactHarness({clock:()=>clock});
    for(const [value,bucket] of [[0,'under_25'],[24.9,'under_25'],[25,'25_to_74'],[74.9,'25_to_74'],
      [75,'75_to_149'],[149.9,'75_to_149'],[150,'150_or_more'],[9000,'150_or_more'],[-1,'unspecified'],[NaN,'unspecified'],[Infinity,'unspecified'],[undefined,'unspecified']] as const)
      assert.equal(h.age(value),bucket);
    const unknown={before_stage:'unspecified',stage_await:'unspecified'};
    for(const trace of [{},{budgetStart:1},{budgetStart:5,stageStart:4},{budgetStart:0,stageStart:151}])assert.deepEqual(h.budget(trace),unknown);
    for(const value of [NaN,Infinity,-1]){clock=value;assert.deepEqual(h.budget({budgetStart:0,stageStart:10}),unknown);}
    const bad=artifactHarness({clock:()=>{throw new Error('SECRET /private/path');}});
    assert.deepEqual(bad.budget({budgetStart:0,stageStart:10}),unknown);
  });
  test('persist budget preserves earlier primary faults and healthy seventeen-field response',async t=>{
    let clock=0;const h=artifactHarness({clock:()=>clock});
    const healthy=await h.observer.snapshot(NONCE);assert.equal(healthy.complete,true);assert.equal(Object.keys(healthy).length,17);
    assert.ok(!Object.keys(healthy).some(key=>key.startsWith('unavailable_')));
    let release!:()=>void;const held=new Promise<void>(yes=>release=yes);
    h.observer.store.write=async(_v:any,_w:any,trace:any)=>{clock=10;await h.step(trace,'snapshot_sync',()=>held);};
    t.mock.timers.enable({apis:['setTimeout']});
    try{
      h.observer.begin();await artifactMicrotasks();h.observer.invalidate('picker_pool_failed');clock=150;t.mock.timers.tick(150);
      await artifactMicrotasks();await h.observer.queue;
      const proof=await h.observer.snapshot(NONCE);assert.equal(proof.unavailable_reason,'picker_pool_failed');
      assert.ok(!Object.keys(proof).some(key=>key.startsWith('unavailable_persist')));
    }finally{release();await artifactMicrotasks();t.mock.timers.reset();}
  });

  test('artifact progress freezes each controlled IO slot at the original deadline',async t=>{
    t.mock.timers.enable({apis:['setTimeout']});
    try{
      for(const operation of ['open','stat','read','close'])for(const slot of [0,1,3,7]){
        let clock=0;const h=artifactHarness({pause:{operation,slot},clock:()=>clock,...(slot===7?{artifactCount:8,producer:'credential_monitor'}:{})}),trace:any={stage:'artifacts'};
        const work=h.verify(trace),outcome=h.bounded(work,trace).then(()=>null,(error:unknown)=>error);
        try{
          for(let n=0;n<8&&!h.waiting.length;n++)await artifactMicrotasks();
          assert.equal(h.waiting.length,1,operation+slot);clock=150;t.mock.timers.tick(150);await artifactMicrotasks();
          assert.ok(await outcome);assert.equal(trace.first.timer,'on_tick');
          const frozen=JSON.stringify(h.project(trace.first.artifacts));
          assert.ok(trace.first.artifacts.slots.some((row:any)=>row.slot===slot&&row.operation===operation));
          assert.ok(trace.first.artifacts.slots.length<=2);assert.ok(trace.first.artifacts.completed<=8);
          assert.equal(trace.first.artifacts.phase,'files');
          await drainArtifactFake(h);await work;
          assert.equal(JSON.stringify(h.project(trace.first.artifacts)),frozen,'late IO must not rewrite first detail');
          assert.ok(h.stats().maximum<=2);
        }finally{await drainArtifactFake(h);await work.catch(()=>{});}
      }
    }finally{t.mock.timers.reset();}
  });
  test('artifact progress actual persist and snapshot keep two-slot deadline after late failed cleanup',async t=>{
    t.mock.timers.enable({apis:['setTimeout']});let clock=0;
    const late=Object.assign(new Error('SECRET close path/token'),{code:'EIO'});
    const options:any={pause:{slot:0,operation:'read'},delayed:true,delayOnlySecond:true,closeError:late,clock:()=>clock};
    const h=artifactHarness(options),observer=h.observer;
    try{
      const identity=observer.begin();await artifactMicrotasks();assert.equal(h.waiting.length,2);
      clock=400;t.mock.timers.tick(150);await artifactMicrotasks();await observer.finish(identity,'observation');
      const frozen=JSON.stringify(observer.firstPersist);
      assert.equal(observer.firstPersist.error,'deadline');assert.equal(observer.firstPersist.timer,'delayed');
      assert.deepEqual(observer.firstPersist.artifacts.slots,[{slot:0,operation:'read'},{slot:1,operation:'read'}]);
      await drainArtifactFake(h);await observer.queue;options.closeError=undefined;options.delayed=false;
      const proof=await observer.snapshot(NONCE);
      assert.equal(proof.available,false);assert.equal(proof.unavailable_reason,'persist_failed');
      assert.equal(proof.unavailable_persist_stage,'artifacts');assert.equal(proof.unavailable_persist_error,'deadline');
      assert.equal(proof.unavailable_persist_timer,'delayed');assert.equal(proof.unavailable_artifact_phase,'files');
      assert.equal(JSON.stringify(observer.firstPersist),frozen);
      assert.ok(!JSON.stringify(proof).includes('SECRET'));assert.ok(!JSON.stringify(proof).includes('/synthetic'));
    }finally{options.closeError=undefined;options.delayed=false;await drainArtifactFake(h);t.mock.timers.reset();}
  });
  test('artifact progress preserves the primary operation despite finally replacing its thrown error',async()=>{
    const primary=Object.assign(new Error('SECRET original'),{code:'EACCES'}),replacement=Object.assign(new Error('SECRET close'),{code:'EIO'});
    const h=artifactHarness({statError:primary,closeError:replacement,closeSlot:0}),trace:any={stage:'artifacts'};
    await assert.rejects(h.verify(trace),error=>error===replacement);await artifactMicrotasks();
    assert.equal(trace.first.error,primary);assert.equal(trace.first.stage,'artifacts');
    assert.ok(trace.first.artifacts.slots.some((row:any)=>row.slot===0&&row.operation==='stat'));
    assert.ok(!JSON.stringify(h.project(trace.first.artifacts)).includes('SECRET'));assert.equal(h.stats().active,0);
    const actual=artifactHarness({statError:primary,closeError:replacement,closeSlot:0});
    actual.observer.begin();await actual.observer.queue;
    assert.equal(actual.observer.firstPersist.error,'permission','first stat failure survives finally close replacement');
    const proof=await actual.observer.snapshot(NONCE);
    assert.equal(proof.unavailable_persist_error,'permission');
    assert.ok(proof.unavailable_artifact_slots.some((row:any)=>row.slot===0&&row.operation==='stat'));
  });
  test('artifact progress actual method projection distinguishes hash and final proof errors',async()=>{
    for(const kind of ['hash','proof','membership']){
      const original=Object.assign(new Error('SECRET failed hash'),{code:'EIO'});
      const options:any=kind==='hash'?{hashError:original,hashSlot:3}:kind==='proof'?{proofError:original}:{};
      const h=artifactHarness(options);if(kind==='membership')h.entry.source_id='0'.repeat(64);
      h.observer.begin();await h.observer.queue;
      const detail=h.observer.firstPersist;assert.equal(detail.stage,'artifacts');
      if(kind==='hash')assert.ok(detail.artifacts.slots.some((row:any)=>row.slot===3&&row.operation==='hash'));
      else {assert.equal(detail.artifacts.phase,'proof');assert.deepEqual(detail.artifacts.slots,[]);assert.equal(detail.artifacts.completed,5);}
      const proof=await h.observer.snapshot(NONCE);assert.equal(proof.available,false);assert.equal(proof.unavailable_reason,'persist_failed');
      assert.equal(proof.unavailable_persist_error,kind==='membership'?'error':'io');
      assert.ok(!JSON.stringify(proof).includes('SECRET'));assert.equal(h.stats().active,0);
    }
  });
  test('artifact progress never replaces an earlier primary invalidation with observer timeout detail',async t=>{
    t.mock.timers.enable({apis:['setTimeout']});const h=artifactHarness({pause:{slot:0,operation:'open'},clock:()=>150});
    try{
      h.observer.begin();await artifactMicrotasks();h.observer.invalidate('picker_pool_failed');
      t.mock.timers.tick(150);await artifactMicrotasks();await h.observer.queue;
      await drainArtifactFake(h);const proof=await h.observer.snapshot(NONCE);
      assert.equal(proof.unavailable_reason,'picker_pool_failed');assert.equal(h.observer.firstPersist,undefined);
      assert.ok(!Object.keys(proof).some(key=>key.startsWith('unavailable_artifact')||key.startsWith('unavailable_persist')));
    }finally{await drainArtifactFake(h);t.mock.timers.reset();}
  });
  test('artifact progress closed projection refuses bad slots and preserves healthy seventeen fields',async()=>{
    const h=artifactHarness();
    for(const progress of [{slots:new Map([[8,'read']]),completed:0,phase:'files'},
      {slots:new Map([[0,'SECRET/raw/path']]),completed:0,phase:'files'},
      {slots:new Map([[0,'read'],[1,'read'],[2,'read']]),completed:0,phase:'files'},
      {slots:new Map(),completed:9,phase:'files'}]){
      assert.deepEqual(h.project(h.freeze(progress)),{unavailable_artifact_phase:'unspecified'});
    }
    const maximum=artifactHarness({artifactCount:8,producer:'credential_monitor'}),trace:any={stage:'artifacts'};
    await maximum.verify(trace);assert.deepEqual(maximum.freeze(trace.artifacts),{slots:[],completed:8,phase:'complete'});
    const healthy=await h.observer.snapshot(NONCE);assert.equal(healthy.complete,true);
    assert.equal(Object.keys(healthy).length,17);assert.ok(!Object.keys(healthy).some(key=>key.startsWith('unavailable_')));
  });
  test('artifact progress timer lateness uses coarse closed mock-clock buckets only',()=>{
    let clock:any=150;const h=artifactHarness({clock:()=>clock});
    assert.equal(h.lateness(0,150),'on_tick');clock=200;assert.equal(h.lateness(0,150),'on_tick');
    clock=201;assert.equal(h.lateness(0,150),'delayed');clock=149;assert.equal(h.lateness(0,150),'unspecified');
    clock=NaN;assert.equal(h.lateness(0,150),'unspecified');clock=150;assert.equal(h.lateness(undefined,150),'unspecified');
    const bad=artifactHarness({clock:()=>{throw new Error('SECRET clock');}});assert.equal(bad.lateness(0,150),'unspecified');
  });

  test('artifact concurrency is bounded at two and every invocation rehashes current bytes',async()=>{
    const h=artifactHarness();await h.verify();
    assert.equal(h.opened.length,5);assert.equal(h.closed.length,5);
    assert.equal(h.stats().maximum,2);assert.equal(h.stats().active,0);
    h.bytes.set(h.names[0],Buffer.from('changed current source'));
    await assert.rejects(h.verify());await artifactMicrotasks();
    assert.ok(h.opened.length>5);assert.equal(h.stats().active,0);
  });
  test('artifact parallel IO removes a provable serial deadline without changing the actual 150ms bound',async(t)=>{
    const h=artifactHarness({delayed:true});assert.equal(h.timeout,150);
    t.mock.timers.enable({apis:['setTimeout']});
    let outcome:any;
    const trace:any={stage:'artifacts'};
    const work=h.verify();
    const observed=h.bounded(work,trace).then(()=>{outcome='success';},(error:unknown)=>{outcome=error;});
    let elapsed=0;
    try{
      for(let round=0;round<8&&outcome===undefined;round++){
        await artifactMicrotasks();assert.ok(h.waiting.length>0||outcome!==undefined);
        const batch=h.waiting.splice(0);
        t.mock.timers.tick(40);elapsed+=40;
        for(const resolve of batch)resolve();
        await artifactMicrotasks();
      }
      await observed;
      assert.equal(outcome,'success');assert.equal(elapsed,120);
      assert.equal(trace.first,undefined);assert.equal(h.closed.length,5);
    }finally{await drainArtifactFake(h);t.mock.timers.reset();}
  });
  test('artifact rejection is immediate and late sibling closure cannot replace it or start another batch',async()=>{
    const original=Object.assign(new Error('synthetic rejection'),{code:'EACCES'});
    const late=Object.assign(new Error('synthetic late close'),{code:'EIO'});
    const h=artifactHarness({statError:original,delayed:true,delayOnlySecond:true,closeError:late});
    let rejected:any;
    const work=h.verify().catch((error:unknown)=>{rejected=error;});
    try{
      await artifactMicrotasks();assert.equal(rejected,original);
      assert.equal(h.opened.length,2);assert.equal(h.stats().active,1);
    }finally{await drainArtifactFake(h);await work;}
    assert.equal(rejected,original);assert.equal(h.closed.length,2);assert.equal(h.stats().active,0);
  });
  test('artifact parallel verifier retains all descriptor hash membership and source identity refusals',async()=>{
    for(const metadata of [{mode:0o100666},{nlink:2},{uid:987654},{isFile:()=>false},{size:16*1024*1024+1}]){
      const h=artifactHarness({metadata});await assert.rejects(h.verify());await artifactMicrotasks();
      assert.equal(h.stats().active,0);assert.equal(h.opened.length,h.closed.length);
    }
    for(const code of ['ELOOP','ENOENT']){
      const original=Object.assign(new Error('synthetic open rejection'),{code});
      const h=artifactHarness({openError:original});await assert.rejects(h.verify(),error=>error===original);
      assert.equal(h.stats().active,0);
    }
    for(const kind of ['duplicate','missing','loaded','source']){
      const h=artifactHarness();
      if(kind==='duplicate')h.entry.artifacts[1]=h.entry.artifacts[0];
      if(kind==='missing')h.entry.artifacts.pop();
      if(kind==='loaded')h.loaded[h.names[0]]='0'.repeat(64);
      if(kind==='source')h.entry.source_id='0'.repeat(64);
      await assert.rejects(h.verify());await artifactMicrotasks();assert.equal(h.stats().active,0);
      if(kind==='duplicate')assert.equal(h.opened.length,0);
    }
  });

  test('persist diagnostics classify only safe own errno data and never invoke Proxy or getters',async()=>{
    const {classifyPersistFailure,persistDiagnosticFields}=await import('../node/alert-evidence.ts');
    for(const [code,bucket] of Object.entries({EACCES:'permission',EPERM:'permission',ENOENT:'missing',EEXIST:'collision',
      ENOSPC:'capacity',EDQUOT:'capacity',EROFS:'readonly',EIO:'io',EMFILE:'fd_limit',ENFILE:'fd_limit'})){
      const error=Object.assign(new Error('TOKEN=secret /private/path'),{code});
      assert.equal(classifyPersistFailure(error),bucket);
    }
    let touched=0;
    const getter=new Error('secret');Object.defineProperty(getter,'code',{get(){touched++;throw new Error('getter secret');}});
    getter.toString=()=>{touched++;throw new Error('stringify secret');};
    assert.equal(classifyPersistFailure(getter),'error');
    const proxy=new Proxy(new Error('secret'),{get(){touched++;throw new Error('Proxy secret');},
      getOwnPropertyDescriptor(){touched++;throw new Error('descriptor secret');},getPrototypeOf(){touched++;throw new Error('prototype secret');}});
    assert.equal(classifyPersistFailure(proxy),'opaque');
    const revoked=Proxy.revocable(new Error('secret'),{});revoked.revoke();assert.equal(classifyPersistFailure(revoked.proxy),'opaque');
    for(const value of ['EIO',null,undefined,{code:'EIO'},Symbol('secret')])assert.equal(classifyPersistFailure(value),'opaque');
    assert.equal(classifyPersistFailure(Object.assign(new Error('observer_timeout'),{code:'ETIMEDOUT'})),'error','external timeout text/code is not our deadline');
    assert.deepEqual(persistDiagnosticFields('__proto__','toString'),{unavailable_persist_stage:'unspecified',unavailable_persist_error:'opaque'});
    assert.deepEqual(persistDiagnosticFields(proxy,proxy),{unavailable_persist_stage:'unspecified',unavailable_persist_error:'opaque'});
    assert.equal(touched,0);assert.ok(!JSON.stringify(persistDiagnosticFields('snapshot_write','permission')).includes('secret'));
  });
  test('persist diagnostics distinguish artifact, store validation, snapshot creation and writer failures',async t=>{
    for(const fault of ['artifact','identity','sentinel','create','writer','sync','close','ancestor','writer_close']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const expected:any={artifact:['artifacts','error'],identity:['store_identity','permission'],
        sentinel:['store_sentinel','missing'],create:['snapshot_create','collision'],writer:['snapshot_write','io'],
        sync:['snapshot_sync','io'],close:['snapshot_close','permission'],ancestor:['ancestor_sync','io'],writer_close:['snapshot_write','io']};
      const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,
        writer:['sync','ancestor'].includes(fault)?undefined:async(value:any,handle:any)=>{
          if(['close','writer_close'].includes(fault)){const close=handle.close.bind(handle);handle.close=async()=>{await close();throw Object.assign(new Error('private close'),{code:'EACCES'});};}
          if(['writer','writer_close'].includes(fault))throw Object.assign(new Error('TOKEN=secret'),{code:'EIO'});
          await semanticWriter(value,handle);
        }});
      t.after(()=>f.observer.close());const store=(f.observer as any).store;
      const identity=store.identity.bind(store),read=store.read.bind(store);let restore=()=>{};t.after(()=>restore());
      if(fault==='sync'){
        const probe=await openFile(join(directory,'sync-prototype'),'w',0o600);let prototype:any;
        try{prototype=Object.getPrototypeOf(probe);}finally{await probe.close();}
        const original=prototype.sync;restore=()=>{prototype.sync=original;};
        prototype.sync=async()=>{throw Object.assign(new Error('private sync'),{code:'EIO'});};
      }
      if(fault==='ancestor'){const directory=store.directory,original=directory.sync;restore=()=>{directory.sync=original;};directory.sync=async()=>{throw Object.assign(new Error('private ancestor'),{code:'EIO'});};}
      if(fault==='artifact')chmodSync(join(f.installed,'src/model-picker.ts'),0o666);
      if(fault==='identity')store.identity=async()=>{throw Object.assign(new Error('private'),{code:'EACCES'});};
      if(fault==='sentinel')store.read=async(name:string)=>{if(name==='journal.json')throw Object.assign(new Error('private'),{code:'ENOENT'});return read(name);};
      if(fault==='create')writeFileSync(join(f.state,'snapshot-'+f.observer.boot+'-1.json'),'owned collision',{mode:0o600});
      await f.observer.finish(f.observer.begin(),'observation');
      restore();store.identity=identity;store.read=read;if(fault==='artifact')chmodSync(join(f.installed,'src/model-picker.ts'),0o600);
      const proof=await f.observer.snapshot(NONCE);assert.equal(proof.available,false);assert.equal(proof.unavailable_reason,'persist_failed');
      assert.equal(proof.unavailable_persist_stage,expected[fault][0]);assert.equal(proof.unavailable_persist_error,expected[fault][1]);
      assert.ok(!JSON.stringify(proof).includes('TOKEN='));assert.ok(!JSON.stringify(proof).includes(directory));
      f.observer.invalidate('snapshot_route_rejected');assert.deepEqual(await f.observer.snapshot(NONCE),proof);
    }
  });
  test('persist diagnostics distinguish own deadline and retain its first stage after late completion',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{producer:'picker_quota_stale',timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    let entered!:()=>void,finishLate!:()=>void;const started=new Promise<void>(yes=>entered=yes),late=new Promise<void>(yes=>finishLate=yes);
    t.after(()=>finishLate());const store=(f.observer as any).store,write=store.write.bind(store);
    store.write=async(_v:any,_w:any,trace:any)=>{trace.stage='snapshot_write';entered();await late;trace.stage='ancestor_sync';};
    // Fake timer fires ONLY after artifact verification reached the held store;
    // no real sleep, VM latency assertion or production deadline increase.
    t.mock.timers.enable({apis:['setTimeout']});
    const finishing=f.observer.finish(f.observer.begin(),'observation');
    await Promise.race([started,finishing.then(()=>{throw new Error('held writer boundary not reached');})]);
    t.mock.timers.tick(2000);await finishing;t.mock.timers.reset();
    const first=await f.observer.snapshot(NONCE);assert.equal(first.unavailable_reason,'persist_failed');
    assert.equal(first.unavailable_persist_stage,'snapshot_write');assert.equal(first.unavailable_persist_error,'deadline');
    finishLate();await late;await Promise.resolve();store.write=write;
    assert.deepEqual(await f.observer.snapshot(NONCE),first,'late stage changes cannot heal/rewrite');
  });
  test('persist diagnostics never overwrite earlier faults and leave healthy seventeen bytes unchanged',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    const healthy=await f.observer.snapshot(NONCE);assert.equal(Object.keys(healthy).length,17);
    assert.deepEqual(Object.keys(healthy).filter(key=>key.startsWith('unavailable')),[]);
    const before=JSON.stringify(healthy),again=await f.observer.snapshot(NONCE);again.observed_at_ms=healthy.observed_at_ms;
    assert.equal(JSON.stringify(again),before,'healthy keys/order/value bytes except actual observation time');
    let reject!:()=>void,entered!:()=>void;const started=new Promise<void>(yes=>entered=yes);
    const failure=new Promise<void>((_yes,no)=>reject=()=>no(new Error('secret')));
    const store=(f.observer as any).store,write=store.write.bind(store);store.write=()=>{entered();return failure;};
    const id=f.observer.begin();await started;f.observer.invalidate('picker_pool_failed');reject();await (f.observer as any).queue;
    store.write=write;await f.observer.finish(id,'alert');
    const first=await f.observer.snapshot(NONCE);assert.equal(first.unavailable_reason,'picker_pool_failed');
    assert.ok(!('unavailable_persist_stage' in first));assert.ok(!('unavailable_persist_error' in first));
    const fallbackDirectory=temp();t.after(()=>rmSync(fallbackDirectory,{recursive:true,force:true}));
    const fallback=await fixture(fallbackDirectory,{timeoutMs:2000,writer:semanticWriter});t.after(()=>fallback.observer.close());
    fallback.observer.invalidate('persist_failed');const unspecified=await fallback.observer.snapshot(NONCE);
    assert.equal(unspecified.unavailable_persist_stage,'unspecified');assert.equal(unspecified.unavailable_persist_error,'opaque');
  });
  test('first fault actual Node reasons are closed sticky and preserve healthy seventeen keys',async t=>{
    const reasons=['picker_snapshot_failed','picker_pool_failed','picker_decision_incomplete',
      'picker_snapshot_no_host','picker_pool_no_host','picker_snapshot_data_unavailable',
      'picker_snapshot_policy_unavailable','picker_pool_policy_unavailable','picker_begin_failed','picker_finish_failed',
      'snapshot_route_timeout','snapshot_route_rejected','dry_run_begin_failed','dry_run_finish_timeout','dry_run_finish_failed'];
    for(const reason of reasons){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
      const healthy=await f.observer.snapshot(NONCE);assert.equal(healthy.complete,true);assert.equal(Object.keys(healthy).length,17);
      f.observer.invalidate(reason);const first=await f.observer.snapshot(NONCE);
      assert.equal(first.unavailable_reason,reason);assert.equal(first.unavailable_phase,'runtime');assert.equal(first.available,false);
      f.observer.invalidate('TOKEN=synthetic-private-error /private/path');
      assert.deepEqual(await f.observer.snapshot(NONCE),first);assert.ok(!JSON.stringify(first).includes('TOKEN='));
    }
  });
  test('first fault shared predecision pool failure poisons both real witnesses without changing business error',async t=>{
    const first=temp(),second=temp();t.after(()=>{rmSync(first,{recursive:true,force:true});rmSync(second,{recursive:true,force:true});});
    const e=await fixture(first,{producer:'picker_excluded',timeoutMs:2000,writer:semanticWriter});
    const q=await fixture(second,{producer:'picker_quota_stale',timeoutMs:2000,writer:semanticWriter});
    t.after(()=>e.observer.close());t.after(()=>q.observer.close());
    e.env.PICKER_DIAGNOSTIC_EVIDENCE.picker_quota_stale=q.observer;
    e.env.MACHINES={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({connected:false,engines:[]})})};
    const response=await e.post(false);assert.equal(response.status,503);assert.equal((await response.json()).error,'no_connected_host');
    for(const observer of [e.observer,q.observer]){
      const proof=await observer.snapshot(NONCE);assert.equal(proof.unavailable_reason,'picker_pool_no_host');assert.equal(proof.available,false);
      observer.invalidate('snapshot_route_rejected');assert.deepEqual(await observer.snapshot(NONCE),proof);
    }
  });
  test('first fault real snapshot routes distinguish rejected promise from original deadline',async t=>{
    for(const producer of ['credential_monitor','picker_excluded'])for(const mode of ['rejected','timeout']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{producer,timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
      let reason:string|undefined;
      const observer={begin:()=>1,finish:async()=>{},invalidate:(value:string)=>{reason??=value;},snapshot:async()=>{
        if(reason)return {kind:'hub_alert_evidence_unavailable_v1',available:false,reason:'coverage_unknown',nonce:NONCE,
          unavailable_phase:'runtime',unavailable_reason:reason};
        if(mode==='rejected')throw new Error('TOKEN=synthetic-rejected /private/path');
        return new Promise<never>(()=>{});
      }};
      if(producer==='credential_monitor')f.env.CREDENTIAL_MONITOR_EVIDENCE=observer;
      else f.env.PICKER_DIAGNOSTIC_EVIDENCE[producer]=observer;
      const first=await f.sample();assert.equal(first.status,200);assert.equal((await first.json()).available,false);
      assert.equal(reason,mode==='timeout'?'snapshot_route_timeout':'snapshot_route_rejected');
      const next=await (await f.sample()).json();assert.equal(next.unavailable_reason,reason);assert.ok(!JSON.stringify(next).includes('TOKEN='));
    }
  });
  test('first fault dry run observer errors retain primary response with bounded stage reasons',async t=>{
    for(const mode of ['begin','sync_finish','async_finish','timeout']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());f.model.unhealthy=false;
      let reason:string|undefined,finishes=0;
      f.env.CREDENTIAL_MONITOR_EVIDENCE={begin:()=>{if(mode==='begin')throw new Error('TOKEN=synthetic-begin');return 1;},
        finish:()=>{finishes++;if(mode==='sync_finish')throw new Error('TOKEN=synthetic-finish');
          return mode==='timeout'?new Promise<never>(()=>{}):Promise.reject(new Error('TOKEN=synthetic-finish'));},
        invalidate:(value:string)=>{reason??=value;},snapshot:async()=>({})};
      const response=await f.post();assert.equal(response.status,200);assert.ok(['codex','claude'].includes((await response.json()).engine));
      assert.equal(reason,mode==='begin'?'dry_run_begin_failed':mode==='timeout'?'dry_run_finish_timeout':'dry_run_finish_failed');
      assert.equal(finishes,mode==='begin'?0:1);
    }
  });

  test('unavailable reason: first runtime fault is closed, sticky and never changes healthy shape',async t=>{
    for(const producer of ['credential_monitor','picker_excluded']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{producer,timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
      const healthy=await f.observer.snapshot(NONCE);
      assert.equal(healthy.complete,true);assert.equal(Object.keys(healthy).length,17);
      assert.ok(!('unavailable_phase' in healthy));assert.ok(!('unavailable_reason' in healthy));
      const secret='https://user:password@private.invalid/path TOKEN=synthetic-private-value';
      f.observer.invalidate(secret);
      const proof=await f.observer.snapshot(NONCE);
      assert.equal(proof.available,false);assert.equal(proof.unavailable_phase,'runtime');assert.equal(proof.unavailable_reason,'invalidated');
      assert.ok(!JSON.stringify(proof).includes(secret));
      await f.observer.finish(null,'alert');
      assert.deepEqual(await f.observer.snapshot(NONCE),proof,'later faults cannot rewrite first reason or heal');
    }
  });
  test('unavailable reason: pending and active observations remain transient without false dirty diagnosis',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    let release!:()=>void;const held=new Promise<void>(yes=>release=yes);t.after(()=>release());
    const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:async(v:any,h:any)=>{await held;await semanticWriter(v,h);}});t.after(()=>f.observer.close());
    const id=f.observer.begin();
    const pending=await f.observer.snapshot(NONCE);assert.equal(pending.unavailable_reason,'pending_write');assert.equal(pending.available,false);
    release();await (f.observer as any).queue;
    assert.equal((await f.observer.snapshot(NONCE)).unavailable_reason,'active_invocation');
    await f.observer.finish(id,'observation');
    const complete=await f.observer.snapshot(NONCE);assert.equal(complete.complete,true);assert.equal(Object.keys(complete).length,17);
  });
  test('unavailable reason: real persistence and source faults retain first known stage without raw details',async t=>{
    for(const fault of ['writer','source']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{producer:'picker_quota_stale',timeoutMs:2000,
        writer:fault==='writer'?async()=>{throw new Error('TOKEN=synthetic-private-value');}:semanticWriter});t.after(()=>f.observer.close());
      if(fault==='writer')await f.observer.finish(f.observer.begin(),'observation');
      else writeFileSync(join(f.installed,'src/model-picker.ts'),'// synthetic source changed');
      const proof=await f.observer.snapshot(NONCE);
      assert.equal(proof.unavailable_phase,'runtime');assert.equal(proof.unavailable_reason,
        fault==='writer'?'persist_failed':'snapshot_source_verification_failed');
      assert.ok(!JSON.stringify(proof).includes('synthetic-private-value'));
      f.observer.invalidate();assert.equal((await f.observer.snapshot(NONCE)).unavailable_reason,proof.unavailable_reason);
    }
  });

  test('diagnostic authorities observe real picker decisions without a marked monitor POST or redraw',async t=>{
    for(const producer of ['picker_excluded','picker_quota_stale']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{producer,timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
      f.model.stale=producer==='picker_quota_stale';
      const primary=await f.post(false);assert.equal(primary.status,200);
      const body=await primary.json() as any;
      assert.ok(producer==='picker_excluded'?body.excluded.length:body.warnings.length);
      await (f.observer as any).queue;
      let proof=await (await f.sample()).json() as any;
      assert.equal(proof.complete,true);assert.equal(proof.producer,producer);assert.equal(proof.occurrence_count,1);
      assert.equal(proof.started_total,1);assert.equal(proof.finished_total,1);
      // A direct call uses the exact same hook as Control's serialized draw.
      const {pickModelRouteDetailed}=await import(pathToFileURL(join(f.installed,'src/model-picker.ts')).href);
      let draws=0;await pickModelRouteDetailed(f.env,{},()=>{draws++;return 0.5;});
      assert.equal(draws,1,'proof must not take an independent model sample');
      await (f.observer as any).queue;
      proof=await (await f.sample()).json() as any;assert.equal(proof.occurrence_count,2);
      f.model.unhealthy=false;f.model.stale=false;
      assert.equal((await f.post(false)).status,200);await (f.observer as any).queue;
      proof=await (await f.sample()).json() as any;assert.equal(proof.complete,true);
      assert.equal(proof.started_total,3);assert.equal(proof.occurrence_count,2,'healthy completion never counts an alert');
    }
  });
  test('diagnostic excluded counter follows console exclusions, not monitor advisory projection',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    f.model.excludeOne=false; // All unusable credentials use the existing advisory fallback.
    const body=await (await f.post(false)).json() as any;assert.ok(body.excluded.length>0);
    await (f.observer as any).queue;
    const proof=await (await f.sample()).json() as any;assert.equal(proof.complete,true);assert.equal(proof.occurrence_count,0);
    assert.equal(proof.finished_total,1);
  });
  test('diagnostic pending IO never delays primary response; barriers fail closed until durable',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    let release!:()=>void,entered!:()=>void;
    const started=new Promise<void>(yes=>entered=yes),gate=new Promise<void>(yes=>release=yes);
    const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:async(value:any,handle:any)=>{
      entered();await gate;await semanticWriter(value,handle);
    }});t.after(async()=>{release();await f.observer.close();});
    const response=f.post(false);await started;
    // An IO gate, not a sleeping fast-path assertion. If the business path
    // awaits proof IO this test cannot receive a response before release.
    let timer:ReturnType<typeof setTimeout>|undefined;
    let result:Response;
    try{result=await Promise.race([response,new Promise<never>((_,no)=>{timer=setTimeout(()=>no(new Error('primary awaited proof IO')),1000);})]);}
    finally{clearTimeout(timer);}
    assert.equal(result.status,200);assert.equal((await (await f.sample()).json() as any).available,false);
    release();await (f.observer as any).queue;
    const proof=await (await f.sample()).json() as any;assert.equal(proof.complete,true);assert.equal(proof.occurrence_count,1);
  });
  test('diagnostic uncertain decision or failed writer cannot heal; primary semantics unchanged',async t=>{
    for(const fault of ['decision','writer']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      let broken=true;
      const f=await fixture(directory,{producer:'picker_quota_stale',timeoutMs:2000,writer:async(value:any,handle:any)=>{
        if(fault==='writer'&&broken)throw new Error('synthetic fault');await semanticWriter(value,handle);
      }});t.after(()=>f.observer.close());
      f.model.failRouting=fault==='decision';f.model.stale=true;
      assert.equal((await f.post(false)).status,fault==='decision'?500:200);
      await (f.observer as any).queue;
      broken=false;f.model.failRouting=false;
      assert.equal((await f.post(false)).status,200);await (f.observer as any).queue;
      assert.equal((await (await f.sample()).json() as any).available,false);
    }
  });
  test('diagnostic decisions remain counted when selection fails after the console decisions',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    const {pickModelRouteDetailed}=await import(pathToFileURL(join(f.installed,'src/model-picker.ts')).href);
    await assert.rejects(pickModelRouteDetailed(f.env,{},()=>{throw new Error('synthetic post-decision failure');}),/post-decision/);
    await (f.observer as any).queue;
    const proof=await f.observer.snapshot(NONCE);assert.equal(proof.complete,true);assert.equal(proof.occurrence_count,1);
  });
  test('diagnostic inflight calls, nonce authorization and missing configuration remain explicit',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    let release!:()=>void,entered!:()=>void;const gate=new Promise<void>(yes=>release=yes),started=new Promise<void>(yes=>entered=yes);
    f.model.beforeRead=async()=>{entered();await gate;};t.after(()=>release());
    const request=f.post(false);await started;
    const during=await (await f.sample('2'.repeat(32))).json() as any;
    assert.equal(during.available,false);assert.equal(during.nonce,'2'.repeat(32));
    release();assert.equal((await request).status,200);await (f.observer as any).queue;
    assert.equal((await f.sample('bad')).status,400);assert.equal((await f.sample(NONCE,'POST')).status,405);
    f.model.actor={id:'synthetic-reader',kind:'operator',scopes:['session:start']};assert.equal((await f.sample()).status,403);
    f.model.actor={id:'synthetic-machine',kind:'machine',resource_id:'h1',scopes:['machine:heartbeat']};assert.equal((await f.sample()).status,403);
    f.model.actor={id:'synthetic-admin',kind:'operator',scopes:['operator:admin']};
    delete f.env.PICKER_DIAGNOSTIC_EVIDENCE;
    assert.equal((await (await f.sample()).json() as any).available,false);
    assert.equal((await f.post(false)).status,200);
  });
  test('diagnostic enrollment identity, five loaded artifacts, immutable history and restart fencing',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{producer:'picker_excluded',timeoutMs:2000,writer:semanticWriter});
    await f.post(false);await (f.observer as any).queue;
    const before=await f.observer.snapshot(NONCE);assert.equal(before.complete,true);
    const {AlertEvidence}=await import(pathToFileURL(join(f.installed,'node/alert-evidence.ts')).href);
    await f.observer.close();
    // Same immutable state, new incarnation: never continuity across restarts.
    const again=await AlertEvidence.load(f.state,{producer:'picker_excluded',loadedArtifacts:f.loadedArtifacts,timeoutMs:2000});
    t.after(()=>again.close());const after=await again.snapshot(NONCE);assert.equal(after.complete,true);assert.notEqual(after.epoch,before.epoch);
    await assert.rejects(AlertEvidence.load(f.state,{producer:'picker_quota_stale',loadedArtifacts:f.loadedArtifacts}),/unavailable/);
    await assert.rejects(AlertEvidence.load(f.state,{loadedArtifacts:f.loadedArtifacts}),/unavailable/);
    const bad={...f.loadedArtifacts,[join(f.installed,'src/model-picker.ts')]:'f'.repeat(64)};
    await assert.rejects(AlertEvidence.load(f.state,{producer:'picker_excluded',loadedArtifacts:bad}),/unavailable/);
    privateJson(join(f.state,'journal.json'),{});
    await again.finish(again.begin(),'alert');assert.equal((await again.snapshot(NONCE)).available,false);
  });
  test('exact final exclusion criterion counts marked decisions; unmarked and healthy do not count',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    // Decision/queue semantics use a deterministic writer; real durability is
    // exercised separately without assuming a 200 HTTP receipt means fsync.
    const f=await fixture(directory,{timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    const plain=await (await f.post(false)).json() as any;
    const marked=await (await f.post()).json() as any;
    assert.ok(marked.excluded.length>0);assert.deepEqual(marked.excluded,plain.excluded);
    let proof=await (await f.sample()).json() as any;
    assert.equal(proof.occurrence_count,1);assert.equal(proof.started_total,1);assert.equal(proof.finished_total,1);
    assert.equal(proof.artifact_proof,'loaded_and_current_v1');assert.equal(proof.nonce,NONCE);
    f.model.unhealthy=false;
    const healthy=await (await f.post()).json() as any;assert.deepEqual(healthy.excluded,[]);
    proof=await (await f.sample()).json() as any;
    assert.equal(proof.occurrence_count,1);assert.equal(proof.started_total,2);
  });
  test('write failure then recovery before first barrier never heals the process generation',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    let broken=true;
    const f=await fixture(directory,{writer:async()=>{if(broken)throw new Error('synthetic');}});t.after(()=>f.observer.close());
    const response=await f.post();assert.equal(response.status,200);assert.ok((await response.json() as any).excluded.length);
    broken=false;
    assert.equal((await (await f.sample()).json() as any).available,false);
    assert.equal((await f.post()).status,200);
    assert.equal((await (await f.sample()).json() as any).available,false);
  });
  test('canonical corruption before a marked POST cannot heal the persisted journal',async t=>{
    for (const corruption of ['empty','source','stale','prior']) {
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{timeoutMs:2000});t.after(()=>f.observer.close());
      // A successful fail-open HTTP receipt is not a durability barrier. Use
      // the actual writer directly with a fixture IO budget, then require a
      // complete idle snapshot before injecting independent corruption.
      await f.observer.finish(f.observer.begin(),'alert');
      const baseline=await f.observer.snapshot(NONCE);
      assert.equal(baseline.complete,true);assert.equal(baseline.finished_total,1);
      const files=snapshotFiles(f.state);
      const journal=corruption==='stale'?files.at(-1)!:corruption==='prior'?files[0]:join(f.state,'journal.json');
      const original=JSON.parse(readFileSync(journal,'utf8'));
      const replacement=corruption==='empty'||corruption==='prior'?{}:corruption==='source'?{...original,source_id:'f'.repeat(64)}:
        {...original,journal:{...original.journal,started_total:0,finished_total:0,occurrence_count:0}};
      privateJson(journal,replacement);
      assert.equal((await f.post()).status,200);
      assert.equal((await (await f.sample()).json() as any).available,false);
      assert.deepEqual(JSON.parse(readFileSync(journal,'utf8')),replacement);
      privateJson(journal,original);
      assert.equal((await f.post()).status,200);
      assert.equal((await (await f.sample()).json() as any).available,false);
    }
  });
  test('corruption after the last good sentinel bytes were captured cannot be healed',{timeout:5000},async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{timeoutMs:2000});
    const file=await openFile(join(directory,'prototype'),'w',0o600);
    const prototype=Object.getPrototypeOf(file),originalRead=prototype.read;await file.close();
    let enteredResolve!:()=>void,releaseResolve!:()=>void;
    const entered=new Promise<void>(r=>enteredResolve=r),held=new Promise<void>(r=>releaseResolve=r);
    let once=true;
    prototype.read=async function(...args:any[]){
      const result=await originalRead.apply(this,args);
      if(once&&Buffer.isBuffer(args[0])&&args[0].subarray(0,result.bytesRead).toString().includes('"kind":"hub_credential_monitor_journal_v1"')){
        once=false;enteredResolve();await held;
      }
      return result;
    };
    t.after(async()=>{releaseResolve();prototype.read=originalRead;await f.observer.close();});
    const identity=f.observer.begin();await entered;
    privateJson(join(f.state,'journal.json'),{});
    releaseResolve();await f.observer.finish(identity,'alert');
    assert.equal((await f.observer.snapshot(NONCE)).available,false);
    assert.deepEqual(JSON.parse(readFileSync(join(f.state,'journal.json'),'utf8')),{});
    assert.equal((await f.post()).status,200);
    assert.equal((await f.observer.snapshot(NONCE)).available,false);
  });
  test('corruption during real temp-file sync cannot heal before the first barrier',{timeout:5000},async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory,{timeoutMs:2000});
    const file=await openFile(join(directory,'prototype'),'w',0o600);
    const prototype=Object.getPrototypeOf(file),originalSync=prototype.sync;await file.close();
    let enteredResolve!:()=>void,releaseResolve!:()=>void;
    const entered=new Promise<void>(r=>enteredResolve=r),held=new Promise<void>(r=>releaseResolve=r);
    let once=true;
    prototype.sync=async function(){if(once){once=false;enteredResolve();await held;}return originalSync.call(this);};
    t.after(async()=>{releaseResolve();prototype.sync=originalSync;await f.observer.close();});
    const identity=f.observer.begin();await entered;
    privateJson(join(f.state,'journal.json'),{});
    releaseResolve();await f.observer.finish(identity,'alert');
    assert.equal((await f.observer.snapshot(NONCE)).available,false);
    assert.deepEqual(JSON.parse(readFileSync(join(f.state,'journal.json'),'utf8')),{});
    // Subsequent marked requests must stay available without repairing or
    // clearing the fault; this process generation is permanently unknown.
    assert.equal((await f.post()).status,200);
    assert.equal((await f.observer.snapshot(NONCE)).available,false);
    assert.deepEqual(JSON.parse(readFileSync(join(f.state,'journal.json'),'utf8')),{});
  });
  test('fail-open POST can precede a timed-out physical journal write',{timeout:5000},async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    let enteredResolve!:()=>void,releaseResolve!:()=>void,doneResolve!:()=>void,writes=0;
    const entered=new Promise<void>(r=>enteredResolve=r),held=new Promise<void>(r=>releaseResolve=r),done=new Promise<void>(r=>doneResolve=r);
    const journal=join(directory,'state/journal.json');
    const f=await fixture(directory,{timeoutMs:2000,writer:async(value:any,handle:any)=>{
      if(++writes===2){enteredResolve();await held;await semanticWriter(value,handle);doneResolve();}
      else await semanticWriter(value,handle);
    }});
    t.after(async()=>{releaseResolve();await f.observer.close();});
    const response=f.post();await entered;assert.equal((await response).status,200);
    const partial=JSON.parse(readFileSync(snapshotFiles(f.state)[0],'utf8')).journal;
    assert.equal(partial.started_total,1);assert.equal(partial.finished_total,0);
    assert.equal((await f.observer.snapshot(NONCE)).available,false);
    privateJson(journal,{...partial,source_id:'f'.repeat(64)});
    releaseResolve();await done;
    // This deliberately injected writer reproduces the CI setup race: a
    // request's 200 did not mean its previous IO was finished. It is not used
    // for the production corruption test above or as evidence of durability.
    assert.equal(JSON.parse(readFileSync(snapshotFiles(f.state).at(-1)!,'utf8')).journal.finished_total,1);
    assert.equal(JSON.parse(readFileSync(journal,'utf8')).source_id,'f'.repeat(64));
    assert.equal((await f.observer.snapshot(NONCE)).available,false);
  });
  test('pending writer timeout is bounded and stays unknown after late completion',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    let release!:()=>void;
    const held=new Promise<void>(r=>release=r);
    const f=await fixture(directory,{writer:()=>held,timeoutMs:25});t.after(()=>f.observer.close());
    const before=Date.now();assert.equal((await f.post()).status,200);assert.ok(Date.now()-before<1500);
    release();await Promise.resolve();assert.equal((await (await f.sample()).json() as any).available,false);
  });
  test('inflight and concurrent requests cannot yield a complete nonce barrier',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    // Decision/queue semantics use a deterministic writer; real durability is
    // exercised separately without assuming a 200 HTTP receipt means fsync.
    const f=await fixture(directory,{timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    const identity=f.observer.begin();
    assert.equal((await f.observer.snapshot(NONCE)).available,false);
    await f.observer.finish(identity,'observation');
    await Promise.all([f.post(),f.post(),f.post()]);
    const proof=await (await f.sample()).json() as any;
    assert.equal(proof.complete,true);assert.equal(proof.started_total,4);assert.equal(proof.finished_total,4);assert.equal(proof.occurrence_count,3);
  });
  test('auth, method and nonce fences do not create producer observations',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory);t.after(()=>f.observer.close());
    assert.equal((await f.sample('bad')).status,400);assert.equal((await f.sample(NONCE,'POST')).status,405);
    f.model.actor.scopes=['session:start'];assert.equal((await f.sample()).status,403);
    f.model.actor.scopes=['operator:admin'];
    assert.equal((await (await f.sample()).json() as any).started_total,0);
    delete f.env.CREDENTIAL_MONITOR_EVIDENCE;
    assert.equal((await f.post()).status,200);assert.equal((await (await f.sample()).json() as any).available,false);
  });
  test('artifact changes and unsafe journal fail unknown without refusing preview',async t=>{
    for(const fault of ['source','journal']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory);t.after(()=>f.observer.close());
      if(fault==='source')writeFileSync(join(f.installed,'src/worker.ts'),'changed artifact');
      if(fault==='journal')writeFileSync(join(f.state,'journal.json'),'{broken');
      assert.equal((await (await f.sample()).json() as any).available,false,fault);
      assert.equal((await f.post()).status,200,fault);
    }
  });
  test('128-request prefix rolls forward without evicting historical evidence',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    let journal='';
    const f=await fixture(directory,{timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
    journal=join(f.state,'journal.json');
    for(let n=0;n<128;n++){const identity=f.observer.begin();await f.observer.finish(identity,'observation');}
    const before=await f.observer.snapshot(NONCE);assert.equal(before.complete,true);assert.equal(before.started_total,128);
    const next=f.observer.begin();assert.notEqual(next,null);await f.observer.finish(next,'observation');
    const after=await f.observer.snapshot(NONCE);assert.equal(after.complete,true);assert.equal(after.started_total,129);
    assert.equal(after.epoch,before.epoch,'rolling storage never resets the interval');
    assert.equal((await f.post()).status,200);await (f.observer as any).queue;
    assert.equal(JSON.parse(readFileSync(journal,'utf8')).started_total,0);
    const files=snapshotFiles(f.state);assert.equal(files.length,256);
    assert.equal(readdirSync(f.state).filter(name=>name.startsWith('checkpoint-')).length,2);
    assert.equal(JSON.parse(readFileSync(files.at(-1)!,'utf8')).journal.started_total,128);
  });
  test('private journal symlink, FIFO, hardlink and duplicate keys cannot produce clean evidence',async t=>{
    for(const fault of ['symlink','fifo','hardlink','duplicate']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory);t.after(()=>f.observer.close());
      const journal=join(f.state,'journal.json'),original=readFileSync(journal);
      const {symlinkSync,linkSync}=await import('node:fs');
      if(fault==='duplicate')writeFileSync(journal,'{"kind":"duplicate",'+original.toString().trim().slice(1));
      else {
        const outside=join(directory,'outside');writeFileSync(outside,original,{mode:0o600});rmSync(journal);
        if(fault==='symlink')symlinkSync(outside,journal);
        if(fault==='hardlink')linkSync(outside,journal);
        if(fault==='fifo'){
          const {execFileSync}=await import('node:child_process');execFileSync('mkfifo',['-m','600',journal]);
        }
      }
      assert.equal((await f.observer.snapshot(NONCE)).available,false,fault);
      assert.equal((await f.post()).status,200,fault);
    }
  });
  test('immutable snapshot identity and exclusive names cannot be replaced or healed',async t=>{
    for(const fault of ['symlink','hardlink','mode','missing','collision']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const f=await fixture(directory,{timeoutMs:2000,writer:semanticWriter});t.after(()=>f.observer.close());
      const {symlinkSync,linkSync}=await import('node:fs');
      if(fault==='collision'){
        const record=join(f.state,`snapshot-${f.observer.boot}-1.json`);privateJson(record,{});
        assert.equal((await f.post()).status,200);
        assert.deepEqual(JSON.parse(readFileSync(record,'utf8')),{});
      }else{
        await f.observer.finish(f.observer.begin(),'observation');
        assert.equal((await f.observer.snapshot(NONCE)).complete,true);
        const record=snapshotFiles(f.state)[0],bytes=readFileSync(record);
        if(fault==='mode')chmodSync(record,0o644);
        else {
          rmSync(record);
          if(fault!=='missing'){
            const outside=join(directory,'outside');writeFileSync(outside,bytes,{mode:0o600});
            if(fault==='symlink')symlinkSync(outside,record);else linkSync(outside,record);
          }
        }
        // A later successful append cannot erase this older required evidence.
        assert.equal((await f.post()).status,200);
      }
      assert.equal((await f.observer.snapshot(NONCE)).available,false,fault);
      assert.equal((await f.post()).status,200);
      assert.equal((await f.observer.snapshot(NONCE)).available,false,fault);
    }
  });
  test('enrollment assertion cannot replace actual load-time artifact identity',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const f=await fixture(directory);t.after(()=>f.observer.close());
    const {AlertEvidence}=await import(pathToFileURL(join(f.installed,'node/alert-evidence.ts')).href);
    const wrong={...f.loadedArtifacts,[join(f.installed,'src/worker.ts')]:'f'.repeat(64)};
    await assert.rejects(AlertEvidence.load(f.state,{loadedArtifacts:wrong}),/unavailable/);
  });
  test('live HTTP response succeeds despite write fault; SIGKILL/new process changes generation',async t=>{
    const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
    const one=await startServerChild(directory,true);t.after(()=>stop(one.child));
    const headers={Authorization:'Bearer '+TOKEN,'X-Hub-Alert-Producer':'credential_monitor','Content-Type':'application/json'};
    const before=await (await fetch(one.base+'/api/model-picker/credential-monitor-evidence?nonce='+NONCE,{headers})).json() as any;
    assert.equal(before.complete,true);
    const response=await fetch(one.base+'/api/model-picker/pick',{method:'POST',headers,body:'{}'});
    assert.equal(response.status,200);assert.ok((await response.json() as any).excluded.length);
    await stop(one.child,'SIGKILL');
    const two=await startServerChild(directory);t.after(()=>stop(two.child));
    const after=await (await fetch(two.base+'/api/model-picker/credential-monitor-evidence?nonce='+NONCE,{headers})).json() as any;
    assert.equal(after.complete,true);assert.notEqual(before.controller_id,after.controller_id);assert.notEqual(before.epoch,after.epoch);
    // An acceptance interval cannot substitute this new epoch for its baseline.
  });
  test('required Node24 lane executes both targeted Python ingress/evidence suites',async()=>{
    const child=spawn('python3',['-m','unittest','host.tests.test_hub_alert_controller','host.tests.test_hub_alert_evidence'],
      {cwd:ROOT,env:{...process.env,PYTHONPATH:join(ROOT,'host'),TMPDIR:join(ROOT,'.local/tmp')},stdio:['ignore','pipe','pipe']});
    let output='';child.stdout!.on('data',b=>output+=b);child.stderr!.on('data',b=>output+=b);
    const code=await new Promise<number|null>((yes,no)=>{
      const timer=setTimeout(()=>{child.kill('SIGTERM');no(new Error('targeted Python contracts timed out'));},90000);
      child.once('error',error=>{clearTimeout(timer);no(error);});child.once('exit',code=>{clearTimeout(timer);yes(code);});});
    assert.equal(code,0,output);
    const count=output.match(/Ran (\d+) tests/);assert.ok(count&&Number(count[1])>=28,'required Python cases must execute, not collect zero: '+output);
  });
  test('actual main loads diagnostic opt-ins independently of absent credential or broken sibling',async t=>{
    for(const fault of ['credential-missing','diagnostic-corrupt','diagnostic-late']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const hub=join(directory,'hub2');mkdirSync(hub);
      cpSync(join(ROOT,'hub2/node'),join(hub,'node'),{recursive:true});cpSync(join(ROOT,'hub2/src'),join(hub,'src'),{recursive:true});
      copyFileSync(join(ROOT,'hub2/production-worker.ts'),join(hub,'production-worker.ts'));
      if(fault==='diagnostic-late'){
        const module=join(hub,'node/alert-evidence.ts'),original=readFileSync(module,'utf8');
        const before="    const producer = options.producer ?? 'credential_monitor';";
        assert.ok(original.includes(before));
        writeFileSync(module,original.replace(before,"    if (options.producer === 'picker_excluded') await new Promise(resolve => setTimeout(resolve, 350));\n"+before));
      }
      const create=(producer:string)=>{
        const diagnostic=producer!=='credential_monitor',state=join(directory,producer);mkdirSync(state,{mode:0o700});
        const names=['node/alert-evidence.ts','node/main.ts','src/worker.ts',...(diagnostic?['src/model-picker.ts','src/routing-pool.ts']:[])];
        const artifacts=names.map(name=>{const path=join(hub,name);chmodSync(path,0o600);return {path,sha256:sha(readFileSync(path))};});
        const source_id=sha(canonical(artifacts));
        privateJson(join(state,'enrollment.json'),{kind:diagnostic?'hub_picker_diagnostic_enrollment_v1':'hub_credential_monitor_enrollment_v1',producer,recipient:'dev@dtcdev.click',
          enrollment_id:'3'.repeat(32),source_id,release_sha:RELEASE,artifacts});
        privateJson(join(state,'journal.json'),{kind:diagnostic?'hub_picker_diagnostic_journal_v1':'hub_credential_monitor_journal_v1',...(diagnostic?{producer}:{}),
          source_id,enrollment_id:'3'.repeat(32),occurrence_count:0,started_total:0,finished_total:0});
        return state;
      };
      const excluded=create('picker_excluded'),quota=create('picker_quota_stale');
      const credential=fault==='credential-missing'?join(directory,'missing'):create('credential_monitor');
      if(fault==='diagnostic-corrupt')privateJson(join(excluded,'journal.json'),{});
      const vars=join(directory,'vars');writeFileSync(vars,'HUB2_DEV_OPERATOR_TOKEN='+TOKEN+'\nHUB2_ALERT_EVIDENCE_DIR='+credential+
        '\nHUB2_PICKER_EXCLUDED_EVIDENCE_DIR='+excluded+'\nHUB2_PICKER_QUOTA_STALE_EVIDENCE_DIR='+quota+'\n');
      const child=spawn(process.execPath,['--experimental-transform-types',join(hub,'node/main.ts'),'--host','127.0.0.1','--port','0','--data-dir',join(directory,'db'),'--dev-vars',vars],
        {stdio:['ignore','pipe','pipe'],env:syntheticEnvironment(directory)});t.after(()=>stop(child));let output='',errors='';child.stderr!.on('data',b=>errors+=b);
      const port=await new Promise<string>((yes,no)=>{const timer=setTimeout(()=>{child.kill('SIGKILL');no(new Error('diagnostic main timeout '+errors));},6000);
        child.stdout!.on('data',b=>{output+=b;const found=output.match(/listening http:\/\/127\.0\.0\.1:(\d+)/);if(found){clearTimeout(timer);yes(found[1]);}});
        child.once('exit',()=>{clearTimeout(timer);no(new Error('diagnostic main exited '+errors));});});
      const base='http://127.0.0.1:'+port,headers={Authorization:'Bearer '+TOKEN};
      assert.equal((await fetch(base+'/health')).status,200);assert.equal((await fetch(base+'/api/repos',{headers})).status,200);
      const credentialProof=await (await fetch(base+'/api/model-picker/credential-monitor-evidence?nonce='+NONCE,{headers})).json() as any;
      assert.equal(credentialProof.complete===true,fault!=='credential-missing');
      if(fault==='credential-missing'){
        assert.equal(credentialProof.unavailable_phase,'bootstrap');assert.equal(credentialProof.unavailable_reason,'load_failed');
      }
      for(const producer of ['picker_excluded','picker_quota_stale']){
        const response=await fetch(base+'/api/model-picker/diagnostic-evidence?producer='+producer+'&nonce='+NONCE,{headers});
        assert.equal(response.headers.get('cache-control'),'no-store');const proof=await response.json() as any;
        assert.equal(proof.complete===true,!(fault!=='credential-missing'&&producer==='picker_excluded'));
        if(producer==='picker_excluded'&&fault!=='credential-missing'){
          assert.equal(proof.unavailable_phase,'bootstrap');
          assert.equal(proof.unavailable_reason,fault==='diagnostic-late'?'load_after_ingress_cutoff':'load_failed');
          if(fault==='diagnostic-late'){
            await new Promise(resolve=>setTimeout(resolve,400));
            const later=await (await fetch(base+'/api/model-picker/diagnostic-evidence?producer='+producer+'&nonce='+NONCE,{headers})).json();
            assert.deepEqual(later,proof,'late completion never attaches or rewrites cutoff diagnosis');
          }
        }
      }
      assert.equal((await fetch(base+'/api/model-picker/diagnostic-evidence?producer=backup&nonce='+NONCE,{headers})).status,400);
      const primary=await fetch(base+'/api/model-picker/pick',{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:'{}'});
      assert.equal(primary.status,503,'optional authority never changes missing-policy behavior');
      const after=await (await fetch(base+'/api/model-picker/diagnostic-evidence?producer=picker_quota_stale&nonce='+NONCE,{headers})).json() as any;
      assert.equal(after.available,false,'uncertain pre-decision invocation cannot become healthy zero');
      assert.equal(after.unavailable_phase,'runtime');assert.equal(after.unavailable_reason,'picker_snapshot_data_unavailable');
      await stop(child);
    }
  });
  test('actual main optional missing/import-broken observer never prevents primary startup/routes',async t=>{
    for(const fault of ['configured','missing','broken','unconfigured']){
      const directory=temp();t.after(()=>rmSync(directory,{recursive:true,force:true}));
      const hub=join(directory,'hub2');mkdirSync(hub);
      cpSync(join(ROOT,'hub2/node'),join(hub,'node'),{recursive:true});cpSync(join(ROOT,'hub2/src'),join(hub,'src'),{recursive:true});
      copyFileSync(join(ROOT,'hub2/production-worker.ts'),join(hub,'production-worker.ts'));
      const module=join(hub,'node/alert-evidence.ts');
      if(fault==='missing')rmSync(module);else if(fault==='broken')writeFileSync(module,"throw new Error('synthetic import fault'); export const AlertEvidence = null;\n");
      const observerDir=join(directory,'observer');
      if(fault==='configured'){
        mkdirSync(observerDir,{mode:0o700});
        const artifacts=['node/alert-evidence.ts','node/main.ts','src/worker.ts'].map(name=>{
          const path=join(hub,name);chmodSync(path,0o600);return {path,sha256:sha(readFileSync(path))};});
        const source_id=sha(canonical(artifacts));
        privateJson(join(observerDir,'enrollment.json'),{kind:'hub_credential_monitor_enrollment_v1',producer:'credential_monitor',recipient:'dev@dtcdev.click',
          enrollment_id:'2'.repeat(32),source_id,release_sha:RELEASE,artifacts});
        privateJson(join(observerDir,'journal.json'),{kind:'hub_credential_monitor_journal_v1',source_id,enrollment_id:'2'.repeat(32),occurrence_count:0,started_total:0,finished_total:0});
      }
      const vars=join(directory,'vars');writeFileSync(vars,'HUB2_DEV_OPERATOR_TOKEN='+TOKEN+(fault==='unconfigured'?'':'\nHUB2_ALERT_EVIDENCE_DIR='+observerDir)+'\n');
      const child=spawn(process.execPath,['--experimental-transform-types',join(hub,'node/main.ts'),'--host','127.0.0.1','--port','0','--data-dir',join(directory,'db'),'--dev-vars',vars],{stdio:['ignore','pipe','pipe'],env:syntheticEnvironment(directory)});
      t.after(()=>stop(child));let output='',errors='';child.stderr!.on('data',b=>errors+=b);
      const port=await new Promise<string>((yes,no)=>{const timer=setTimeout(()=>{child.kill('SIGKILL');no(new Error('main timeout '+errors));},6000);
        child.stdout!.on('data',b=>{output+=b;const match=output.match(/listening http:\/\/127\.0\.0\.1:(\d+)/);if(match){clearTimeout(timer);yes(match[1]);}});
        child.once('exit',()=>{clearTimeout(timer);no(new Error('main exited '+errors));});});
      const base='http://127.0.0.1:'+port,headers={Authorization:'Bearer '+TOKEN};
      assert.equal((await fetch(base+'/health')).status,200);
      assert.equal((await fetch(base+'/api/repos',{headers})).status,200);
      const proof=await (await fetch(base+'/api/model-picker/credential-monitor-evidence?nonce='+NONCE,{headers})).json() as any;
      if(fault==='configured')assert.equal(proof.complete,true);else {
        assert.equal(proof.available,false);assert.equal(proof.unavailable_phase,'bootstrap');
        assert.equal(proof.unavailable_reason,fault==='unconfigured'?'not_configured':'load_failed');
      }
      const primary=await fetch(base+'/api/model-picker/pick',{method:'POST',headers:{...headers,'X-Hub-Alert-Producer':'credential_monitor','Content-Type':'application/json'},body:'{}'});
      assert.equal(primary.status,503,'no configured policy has the same primary refusal with optional observer present or absent');
      await stop(child);
    }
  });
}
if(process.argv.includes('--fixture-server')){
  const index=process.argv.indexOf('--fixture-server');await serverMode(resolve(process.argv[index+1]),process.argv[index+2]==='fail',Number(process.argv[index+3] || 0),process.argv[index+4] || 'credential_monitor');
}else {await import('./alert-evidence-capacity.test.ts');await defineTests();}
