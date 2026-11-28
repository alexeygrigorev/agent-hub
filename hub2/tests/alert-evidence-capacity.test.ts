import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, copyFile, writeFile, chmod, readFile, readdir, rm, link } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve, join, dirname, delimiter } from 'node:path';
import { spawnSync } from 'node:child_process';

const SOURCE = fileURLToPath(new URL('../node/alert-evidence.ts', import.meta.url));
const sha = (s: string | Buffer) => createHash('sha256').update(s).digest('hex');
function canonical(v: any): string {
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',') + '}';
  return JSON.stringify(v);
}
async function fixture(t: any, options: any = {}) {
  const parent = process.env.TMPDIR ? resolve(process.env.TMPDIR) : fileURLToPath(new URL('../../.local/tmp', import.meta.url));
  await mkdir(parent,{recursive:true,mode:0o700});
  const directory = await mkdtemp(join(parent, 'capacity-owned-'));
  t.after(()=>rm(directory,{recursive:true,force:true}));
  const installed=join(directory,'installed'), state=join(directory,'state');
  for(const name of ['node','src']) await mkdir(join(installed,name),{recursive:true,mode:0o700});
  await mkdir(state,{mode:0o700});
  await copyFile(SOURCE,join(installed,'node/alert-evidence.ts'));
  for(const name of ['node/main.ts','src/worker.ts']) await writeFile(join(installed,name),'// owned synthetic source\n',{mode:0o600});
  const artifacts=[];
  for(const name of ['node/alert-evidence.ts','node/main.ts','src/worker.ts']){
    const path=join(installed,name);await chmod(path,0o600);artifacts.push({path,sha256:sha(await readFile(path))});
  }
  const source_id=sha(canonical(artifacts)), enrollment_id='2'.repeat(32);
  const entry={kind:'hub_credential_monitor_enrollment_v1',producer:'credential_monitor',recipient:'dev@dtcdev.click',enrollment_id,source_id,release_sha:'a'.repeat(40),artifacts};
  const journal={kind:'hub_credential_monitor_journal_v1',source_id,enrollment_id,occurrence_count:0,started_total:0,finished_total:0};
  for(const [name,value] of [['enrollment.json',entry],['journal.json',journal]])await writeFile(join(state,name as string),canonical(value)+'\n',{mode:0o600});
  const {AlertEvidence}=await import(pathToFileURL(join(installed,'node/alert-evidence.ts')).href);
  const load=()=>AlertEvidence.load(state,{loadedArtifacts:Object.fromEntries(artifacts.map(a=>[a.path,a.sha256])),timeoutMs:2000,...options});
  const observer=await load();t.after(()=>observer.close());
  const run=async(n:number)=>{for(let i=0;i<n;i++){const id=observer.begin();assert.notEqual(id,null);await observer.finish(id,i%7===0?'alert':'observation');}};
  return {observer,state,run,load,AlertEvidence,journal,installed};
}

test('500 real-I/O sequential observations retain exact cumulative counts with bounded rollup files',async t=>{
  const f=await fixture(t);await f.run(128);
  const prefix=Object.fromEntries(await Promise.all((await readdir(f.state)).filter(n=>n.startsWith('snapshot-')).map(async n=>[n,await readFile(join(f.state,n),'utf8')])));
  assert.equal(Object.keys(prefix).length,256);
  const before=await f.observer.snapshot('1'.repeat(32));assert.equal(before.complete,true);
  await f.run(372);const after=await f.observer.snapshot('2'.repeat(32));
  assert.equal(after.complete,true);assert.equal(after.epoch,before.epoch);assert.equal(after.started_total,500);assert.equal(after.finished_total,500);
  assert.equal(after.occurrence_count,Math.ceil(128/7)+Math.ceil(372/7));
  const files=await readdir(f.state);assert.equal(files.length,260);assert.equal(files.filter(n=>n.startsWith('checkpoint-')).length,2);
  for(const [name,bytes] of Object.entries(prefix))assert.equal(await readFile(join(f.state,name),'utf8'),bytes);
  assert.equal(JSON.parse(await readFile(join(f.state,'journal.json'),'utf8')).started_total,0);
  const checkpoints=await Promise.all(files.filter(n=>n.startsWith('checkpoint-')).map(async n=>JSON.parse(await readFile(join(f.state,n),'utf8'))));checkpoints.sort((a,b)=>a.sequence-b.sequence);
  const prefixTail=Object.keys(prefix).find(name=>name.endsWith('-256.json'))!;
  for(const checkpoint of checkpoints)assert.equal(checkpoint.prefix_sha256,sha(canonical(JSON.parse(prefix[prefixTail]))));
  assert.equal(checkpoints[1].sequence,checkpoints[0].sequence+1);assert.equal(checkpoints[1].previous_sha256,sha(canonical(checkpoints[0])));
});

test('corrupt current or preceding checkpoint and retained prefix can never heal on another write',async t=>{
  for(const fault of ['latest','previous','prefix']){
    const f=await fixture(t);await f.run(132);const names=(await readdir(f.state)).filter(n=>n.startsWith('checkpoint-'));
    const rows=await Promise.all(names.map(async name=>({name,value:JSON.parse(await readFile(join(f.state,name),'utf8'))})));rows.sort((a,b)=>a.value.sequence-b.value.sequence);
    const target=fault==='prefix'?(await readdir(f.state)).find(n=>n.startsWith('snapshot-'))!:rows[fault==='latest'?1:0].name;
    await writeFile(join(f.state,target),'{}\n');
    await f.observer.finish(f.observer.begin(),'observation'); // Cannot overwrite/heal before verification.
    assert.equal((await f.observer.snapshot('3'.repeat(32))).available,false);
    assert.equal(f.observer.begin(),null);assert.equal((await f.observer.snapshot('4'.repeat(32))).available,false);
  }
});

test('partial checkpoint write is retained and observer remains sticky unknown',async t=>{
  let observer:any;
  const f=await fixture(t,{writer:async(value:any,handle:any)=>{
    await handle.writeFile(canonical(value)+'\n');
    if(value.kind==='hub_credential_monitor_checkpoint_v1')throw new Error('synthetic partial publication');
  }});observer=f.observer;await f.run(128);await observer.finish(observer.begin(),'alert');
  assert.equal((await observer.snapshot('5'.repeat(32))).available,false);assert.equal(observer.begin(),null);
  assert.ok((await readdir(f.state)).some(n=>n.startsWith('.checkpoint-')));
});

test('publication directory fsync failure cannot authorize later clean snapshot',async t=>{
  const f=await fixture(t);await f.run(128);
  const store=(f.observer as any).store, directory=store.directory, original=directory.sync.bind(directory);
  directory.sync=async()=>{throw new Error('synthetic directory durability fault');};
  await f.observer.finish(f.observer.begin(),'observation');directory.sync=original;
  assert.ok((await readdir(f.state)).some(n=>n.startsWith('checkpoint-')),'publication happened before injected durability failure');
  assert.equal((await f.observer.snapshot('6'.repeat(32))).available,false);assert.equal(f.observer.begin(),null);
});

test('existing first-slot name is not overwritten; active capacity remains bounded',async t=>{
  const f=await fixture(t);await f.run(128);
  const boot=(f.observer as any).store.boot,target=join(f.state,`checkpoint-${boot}-1.json`);
  await writeFile(target,'foreign retained fixture\n',{mode:0o600});await f.observer.finish(f.observer.begin(),'observation');
  assert.equal(await readFile(target,'utf8'),'foreign retained fixture\n');assert.equal((await f.observer.snapshot('7'.repeat(32))).available,false);
  const g=await fixture(t);for(let n=0;n<128;n++)assert.notEqual(g.observer.begin(),null);assert.equal(g.observer.begin(),null);
  assert.equal((await g.observer.snapshot('8'.repeat(32))).available,false);
});

test('restart never inherits old interval and never removes old rollups',async t=>{
  const f=await fixture(t);await f.run(132);const before=await f.observer.snapshot('9'.repeat(32));
  const retained=Object.fromEntries(await Promise.all((await readdir(f.state)).map(async n=>[n,await readFile(join(f.state,n),'utf8')])));
  await f.observer.close();const next=await f.load();t.after(()=>next.close());const after=await next.snapshot('a'.repeat(32));
  assert.notEqual(after.epoch,before.epoch);assert.equal(after.started_total,0,'new epoch is not interval continuity');
  for(const [name,bytes] of Object.entries(retained))assert.equal(await readFile(join(f.state,name),'utf8'),bytes);
});


test('retained namespace exhaustion refuses a new birth without deleting or resetting history',async t=>{
  const f=await fixture(t);await f.observer.close();
  // Zero-byte private files exercise actual directory cardinality, not a mocked count.
  for(let i=0;i<7932;i++)await writeFile(join(f.state,`retained-${i}`),'',{mode:0o600});
  assert.equal((await readdir(f.state)).length,7934);
  await assert.rejects(f.load(),/unavailable/);
  assert.equal((await readdir(f.state)).length,7934);
  assert.equal(JSON.parse(await readFile(join(f.state,'journal.json'),'utf8')).started_total,0);
});

test('a two-name crash-like checkpoint inode is unknown and cannot heal after alias removal',async t=>{
  const f=await fixture(t);await f.run(130);const checkpoint=(await readdir(f.state)).find(n=>n.startsWith('checkpoint-'))!;
  const alias=join(f.state,'partial-publication-alias');await link(join(f.state,checkpoint),alias);
  assert.equal((await f.observer.snapshot('b'.repeat(32))).available,false);
  await rm(alias);assert.equal(f.observer.begin(),null);
  assert.equal((await f.observer.snapshot('c'.repeat(32))).available,false);
});


test('Python checkpoint reader interoperates under the pinned Node test runtime', {timeout:240000}, async t=>{
  const parent=process.env.TMPDIR?resolve(process.env.TMPDIR):fileURLToPath(new URL('../../.local/tmp',import.meta.url));
  await mkdir(parent,{recursive:true,mode:0o700});
  const box=await mkdtemp(join(parent,'checkpoint-python-owned-'));
  t.after(()=>rm(box,{recursive:true,force:true}));
  const home=join(box,'home'),temporary=join(box,'tmp');
  await mkdir(home,{mode:0o700});await mkdir(temporary,{mode:0o700});
  const python=fileURLToPath(new URL('./alert-evidence-stager.test.py',import.meta.url));
  // Reuse THIS pinned Node executable for Python's real fixture; no ambient
  // Hub/provider credentials or profiles enter the child environment.
  const result=spawnSync('python3',['-I','-S','-B',python],{
    env:{PATH:dirname(process.execPath)+delimiter+(process.env.PATH??''),HOME:home,TMPDIR:temporary,
      GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null',GIT_TERMINAL_PROMPT:'0'},
    encoding:'utf8',timeout:210000,maxBuffer:2*1024*1024,killSignal:'SIGTERM'});
  assert.equal(result.error,undefined,'owned Python reader process failed: '+String(result.error));
  assert.equal(result.signal,null,'owned Python reader process terminated unexpectedly');
  assert.equal(result.status,0,result.stderr);
  assert.match(result.stderr,/Ran 8 tests[\s\S]*OK/);
});
