import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, readdirSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { registerTypeScriptResolveHook } from '../node/compat.ts';
import { DatabaseSync } from 'node:sqlite';
import { Hub2Runtime } from '../node/runtime.ts';

class Probe {
  static calls = 0;
  constructor(private ctx: any) {}
  async fetch(request: Request) {
    if(new URL(request.url).searchParams.has('arm'))await this.ctx.storage.setAlarm(Date.now()-1);
    return new Response('ok');
  }
  async alarm() { Probe.calls++; }
}
function fixture(binding='TEST',name='a/long-object-name-with-slashes-and-more-than-sixty-characters-to-preserve-in-full',doClass:any=Probe) {
  const parent=resolve('.local/tmp');mkdirSync(parent,{recursive:true});
  const dataDir=mkdtempSync(join(parent,'alarm-recovery-'));
  const options={classes:{[binding]:doClass},dataDir,env:{},entry:{fetch:(request:Request,env:any)=>env[binding].get(env[binding].idFromName(name)).fetch(request)}};
  return {dataDir,options};
}

test('an alarm armed after the initial empty sweep is delivered',async()=>{
 const f=fixture(),runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/'));
  await (runtime as any).sweepAlarms();
  const before=Probe.calls;
  await runtime.fetch(new Request('https://test/?arm'));
  await (runtime as any).sweepAlarms();
  assert.equal(Probe.calls,before+1);
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

test('restart restores a persisted alarm with its full object identity without a fetch',async()=>{
 const f=fixture();let runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/?arm'));await runtime.dispose();
  const before=Probe.calls;
  runtime=new Hub2Runtime(f.options);
  await (runtime as any).sweepAlarms();
  assert.equal(Probe.calls,before+1);
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

test('legacy project alarms recover from retained project names with hash verification',async()=>{
 const name='owner/project',f=fixture('PROJECTS',name);let runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/?arm'));await runtime.dispose();
  const file=readdirSync(f.dataDir).find(file=>file.endsWith('.sqlite'))!;
  const db=new DatabaseSync(join(f.dataDir,file));
  db.exec('DROP TABLE hub2_identity; CREATE TABLE intakes(project TEXT)');
  db.prepare('INSERT INTO intakes VALUES(?)').run(name);db.close();
  const before=Probe.calls;
  runtime=new Hub2Runtime(f.options);await (runtime as any).sweepAlarms();
  assert.equal(Probe.calls,before+1);
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

function sqliteFile(dataDir:string):string {
 const files=readdirSync(dataDir).filter(file=>file.endsWith('.sqlite'));
 assert.equal(files.length,1,'one physical object database');
 return join(dataDir,files[0]);
}
function readSql(dataDir:string,query:string):any {
 const db=new DatabaseSync(sqliteFile(dataDir),{readOnly:true});
 try{return db.prepare(query).get();}finally{db.close();}
}
function dueAt(dataDir:string):number|null {
 return readSql(dataDir,'SELECT due FROM hub2_alarm WHERE singleton=1')?.due??null;
}

test('a concurrent rearm while the retry write resolves survives successful delivery',async()=>{
 let storage:any,deliveries=0;
 class Concurrent {
  constructor(ctx:any){storage=ctx.storage;}
  async fetch(){await storage.setAlarm(Date.now()-1);return new Response('ok');}
  async alarm(){deliveries++;}
 }
 const f=fixture('TEST','concurrent-rearm',Concurrent),runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/'));
  const wanted=Date.now()+120_000,original=storage.setAlarm.bind(storage);
  let inject=true;
  storage.setAlarm=async(at:number)=>{
   await original(at);
   if(inject){inject=false;await original(wanted);}
  };
  await (runtime as any).sweepAlarms();
  assert.equal(deliveries,1);
  assert.equal(dueAt(f.dataDir),wanted,'the newer request owns its wake');
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

for(const phase of ['initial-read','failure-read'])test(`concurrent rearm during ${phase} is preserved`,async()=>{
 let storage:any,deliveries=0;
 class Concurrent {
  constructor(ctx:any){storage=ctx.storage;}
  async fetch(){await storage.setAlarm(Date.now()-1);return new Response('ok');}
  async alarm(){deliveries++;await storage.deleteAlarm();throw new Error('retry after clear');}
 }
 const f=fixture('TEST',phase,Concurrent),runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/'));
  const wanted=Date.now()+120_000,original=storage.getAlarm.bind(storage);
  let reads=0;
  storage.getAlarm=async()=>{
   const due=await original();
   if(++reads===(phase==='initial-read'?1:2))await storage.setAlarm(wanted);
   return due;
  };
  await (runtime as any).sweepAlarms().catch(()=>{});
  assert.equal(deliveries,phase==='initial-read'?0:1);
  assert.equal(dueAt(f.dataDir),wanted,'read continuation cannot overwrite a newer wake');
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

for(const binding of ['PROJECTS','MAILBOXES','MACHINES'])test(`unknown legacy ${binding} alarm uses the existing file and later binds the exact canonical name`,async()=>{
 let constructions=0,deliveries=0;
 class Legacy {
  constructor(private ctx:any){constructions++;assert.equal(ctx.id,undefined,'never guess object identity');}
  async fetch(request:Request){if(new URL(request.url).searchParams.has('arm'))await this.ctx.storage.setAlarm(Date.now()-1);return new Response('ok');}
  async alarm(){deliveries++;await this.ctx.storage.put('delivered',deliveries);}
 }
 const name='owner/long/legacy-name-with-slashes-'+ 'x'.repeat(90),f=fixture(binding,name,Legacy);
 let runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/?arm'));await runtime.dispose();
  const file=sqliteFile(f.dataDir),db=new DatabaseSync(file);db.exec('DROP TABLE hub2_identity');db.close();
  constructions=0;
  runtime=new Hub2Runtime(f.options);
  assert.equal(runtime.containerOf(binding,name)!==undefined,true,'recovered physical container before canonical access');
  await (runtime as any).sweepAlarms(); // no initial fetch, no intakes/machines name rows
  assert.equal(deliveries,1);assert.equal(constructions,1);
  assert.equal(readSql(f.dataDir,"SELECT name FROM sqlite_master WHERE name='hub2_identity'"),undefined,'unknown full name must not be guessed');
  const original=runtime.containerOf(binding,name);
  await runtime.fetch(new Request('https://test/'));
  assert.equal(runtime.containerOf(binding,name),original,'canonical namespace reuses restored instance');
  assert.equal(constructions,1);
  assert.deepEqual({...readSql(f.dataDir,'SELECT binding,name FROM hub2_identity')},{binding,name});
  await (runtime as any).sweepAlarms();assert.equal(deliveries,1,'one wake, one delivery');
  assert.equal(readSql(f.dataDir,"SELECT v FROM hub2_kv WHERE k='delivered'")?.v,'1');
  assert.equal(sqliteFile(f.dataDir),file);
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

test('a throwing handler leaves a durable future retry that survives restart',async(t)=>{
 t.mock.method(console,'error',()=>{});
 let calls=0,fail=true;
 class Retry {
  constructor(private ctx:any){}
  async fetch(){await this.ctx.storage.setAlarm(Date.now()-1);return new Response('ok');}
  async alarm(){calls++;if(fail)throw new Error('synthetic delivery failure');}
 }
 const f=fixture('TEST','retry',Retry);let runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/'));
  const before=Date.now();await (runtime as any).sweepAlarms();
  const retry=dueAt(f.dataDir)!;assert.ok(retry>=before+1000);assert.equal(calls,1);
  await (runtime as any).sweepAlarms();assert.equal(calls,1,'no immediate retry loop');
  await runtime.dispose();fail=false;runtime=new Hub2Runtime(f.options);
  const container=runtime.containerOf('TEST','retry')!;
  await container.pollAlarm(retry-1);assert.equal(calls,1);
  await container.pollAlarm(retry);assert.equal(calls,2);assert.equal(dueAt(f.dataDir),null);
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

test('process exit inside delivery cannot erase the already persisted retry lease',{timeout:15000},async()=>{
 const f=fixture('TEST','crash-during-alarm');let runtime:Hub2Runtime|undefined;
 try{
  const child=join(f.dataDir,'crash-child.mjs');
  writeFileSync(child,`import {Hub2Runtime} from ${JSON.stringify(pathToFileURL(resolve('hub2/node/runtime.ts')).href)};
   class Crash {
    constructor(ctx){this.ctx=ctx;}
    async fetch(){await this.ctx.storage.setAlarm(Date.now()-1);return new Response('ok');}
    async alarm(){
     if(!(await this.ctx.storage.getAlarm()>Date.now()))throw new Error('retry not persisted before handler');
     await this.ctx.storage.put('handler_entered',true);process.exit(23);
    }
   }
   const runtime=new Hub2Runtime({classes:{TEST:Crash},dataDir:${JSON.stringify(f.dataDir)},env:{},entry:{fetch:(r,e)=>e.TEST.get(e.TEST.idFromName('crash-during-alarm')).fetch(r)}});
   await runtime.fetch(new Request('https://test/'));await runtime.containerOf('TEST','crash-during-alarm').pollAlarm(Date.now());
   process.exit(99);
  `);
  const result=spawnSync(process.execPath,['--experimental-transform-types',child],{encoding:'utf8',timeout:10000});
  assert.equal(result.error,undefined);assert.equal(result.signal,null);assert.equal(result.status,23,result.stderr);
  assert.equal(readSql(f.dataDir,"SELECT v FROM hub2_kv WHERE k='handler_entered'")?.v,'true');
  const retry=dueAt(f.dataDir);assert.equal(typeof retry,'number');
  const before=Probe.calls;runtime=new Hub2Runtime(f.options);
  const container=runtime.containerOf('TEST','crash-during-alarm')!;
  await container.pollAlarm(retry!-1);assert.equal(Probe.calls,before);
  await container.pollAlarm(retry!);assert.equal(Probe.calls,before+1);assert.equal(dueAt(f.dataDir),null);
 }finally{await runtime?.dispose();rmSync(f.dataDir,{recursive:true});}
});

for(const outcome of ['success','failure'])for(const rearm of ['future','same-retry'])test(`${outcome} preserves a handler's ${rearm} rearm`,async(t)=>{
 t.mock.method(console,'error',()=>{});
 let calls=0,chosen=0;
 class Rearm {
  constructor(private ctx:any){}
  async fetch(){await this.ctx.storage.setAlarm(Date.now()-1);return new Response('ok');}
  async alarm(){calls++;const lease=await this.ctx.storage.getAlarm();chosen=rearm==='same-retry'?lease:lease+12345;await this.ctx.storage.setAlarm(chosen);if(outcome==='failure')throw new Error('synthetic');}
 }
 const f=fixture('TEST','rearm',Rearm),runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/'));await (runtime as any).sweepAlarms();
  assert.equal(calls,1);assert.equal(dueAt(f.dataDir),chosen,'callback rearm must win even at the identical retry timestamp');
  await runtime.containerOf('TEST','rearm')!.pollAlarm(chosen-1);assert.equal(calls,1);
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

test('failures back off to the bounded cap, and a successful delivery resets the counter',async(t)=>{
 t.mock.method(console,'error',()=>{});
 let now=Date.now(),calls=0,fail=true;
 t.mock.method(Date,'now',()=>now);
 class Backoff {
  constructor(private ctx:any){}
  async fetch(){await this.ctx.storage.setAlarm(now-1);return new Response('ok');}
  async alarm(){calls++;if(fail)throw new Error('synthetic');}
 }
 const f=fixture('TEST','backoff',Backoff),runtime=new Hub2Runtime(f.options);
 try{
  await runtime.fetch(new Request('https://test/'));const container=runtime.containerOf('TEST','backoff')!;
  for(const delay of [1000,2000,4000,8000,16000,32000,60000,60000]){
   await container.pollAlarm(now);assert.equal(dueAt(f.dataDir),now+delay);
   const count=calls;await container.pollAlarm(now);assert.equal(calls,count,'no hot loop');now+=delay;
  }
  fail=false;await container.pollAlarm(now);assert.equal(dueAt(f.dataDir),null);
  const successful=calls;await container.pollAlarm(now);assert.equal(calls,successful,'cleared success is not redelivered');
  await runtime.fetch(new Request('https://test/'));fail=true;await container.pollAlarm(now);
  assert.equal(dueAt(f.dataDir),now+1000,'success resets exponential retry delay');
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});

test('real AccountDO encrypted credentials keep their historical AAD across restart',async()=>{
 registerTypeScriptResolveHook();
 const {AccountDO}=await import('../src/account-do.ts');
 const {credentialKey,openCredential}=await import('../src/credential-crypto.ts');
 const key='ab'.repeat(32),f=fixture('ACCOUNTS','provider/account-with-full-canonical-name',AccountDO);
 f.options.env={HUB2_CREDENTIAL_KEY:key};let runtime=new Hub2Runtime(f.options);
 const files={'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic-access',refresh_token:'synthetic-refresh'}})};
 try{
  await runtime.fetch(new Request('https://test/_metrics'));
  let instance=(runtime.containerOf('ACCOUNTS','provider/account-with-full-canonical-name') as any).instance;
  assert.equal(instance.ctx.id,undefined);
  const sealed=await instance.seal(files,'codex',1);
  instance.db.run('INSERT INTO credentials(id,generation,sealed) VALUES(1,?,?)',1,sealed);
  const envelope=JSON.parse(sealed),aad=new TextEncoder().encode(JSON.stringify(['test-account','codex',1]));
  const plain=await openCredential(await credentialKey(key),new Uint8Array(envelope.iv),new Uint8Array(envelope.data),aad);
  assert.deepEqual(JSON.parse(new TextDecoder().decode(plain)),files,'ciphertext uses the prior test-account AAD');
  await instance.ctx.storage.setAlarm(Date.now()+60000);
  await runtime.dispose();runtime=new Hub2Runtime(f.options);
  assert.ok(runtime.containerOf('ACCOUNTS','provider/account-with-full-canonical-name'),'persisted alarm restores the account container before any fetch');
  await runtime.containerOf('ACCOUNTS','provider/account-with-full-canonical-name')!.pollAlarm(0);
  instance=(runtime.containerOf('ACCOUNTS','provider/account-with-full-canonical-name') as any).instance;
  const stored=instance.db.one('SELECT generation,sealed FROM credentials WHERE id=1');
  assert.equal(stored.generation,1);assert.equal(stored.sealed,sealed);
  assert.equal(instance.ctx.id,undefined);assert.deepEqual(await instance.unseal(stored.sealed,'codex',stored.generation),files);
  await runtime.fetch(new Request('https://test/_metrics'));
  assert.equal((runtime.containerOf('ACCOUNTS','provider/account-with-full-canonical-name') as any).instance,instance,'canonical fetch reuses the recovered account');
 }finally{await runtime.dispose();rmSync(f.dataDir,{recursive:true});}
});
