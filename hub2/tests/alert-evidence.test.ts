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
async function defineTests() {
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
      assert.equal(after.unavailable_phase,'runtime');assert.equal(after.unavailable_reason,'invalidated');
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
