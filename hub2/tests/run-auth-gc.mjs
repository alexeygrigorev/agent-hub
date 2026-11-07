// Auth GC bounded-cleanup semantics on the Node runtime; all rows are synthetic.
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { createFixture, restartFixture } from './node-fixture.mjs';
const root = resolve(import.meta.dirname, '../..');
const hub = resolve(root, 'hub2');
const sources = ['control-do', 'control-auth', 'auth', 'db', 'sql-receipt'];
const hashes = async () => Object.fromEntries(await Promise.all(sources.map(async n => [n, createHash('sha256').update(await readFile(resolve(hub, 'src', n + '.ts'))).digest('hex')])));
const before = await hashes();
const { default: gcWorker, AuthGcControl } = await import('./auth-gc-worker.ts');
await mkdir(resolve(root, '.local/tmp'), { recursive: true });
const persist = await mkdtemp(resolve(root, '.local/tmp/auth-gc-'));
const spec = {
  root,
  entry: gcWorker,
  classes: { CONTROL: AuthGcControl },
  env: { HUB_ADMINS:'fixture@example.test' },
  dataDir: persist,
};
let fixture = await createFixture(spec);
const results = [];
let base;
base = fixture.origin;
try{
 for(const id of ['device','revoke']){
  const token='synthetic-auth-gc-'+id+'-000000000000000000000000';
  const seed=await fetch(base+'/_fixture/seed?case='+id,{method:'POST',body:JSON.stringify({token})});assert.equal(seed.status,200);assert.equal((await seed.json()).seeded,75);
  fixture=await restartFixture(fixture,spec);base=fixture.origin;
  const path=id==='device'?'/device':'/api/token/revoke';
  const response=await fetch(base+path+'?case='+id,{method:id==='device'?'GET':'POST',headers:{Authorization:'Bearer '+token},redirect:'manual'});
  assert.equal(response.status,id==='device'?303:200,await response.text());
  const remaining=await(await fetch(base+'/_fixture/remaining?case='+id)).json();assert.deepEqual(remaining.remaining,[0,0,0]);
 }
 const after=await hashes();assert.deepEqual(after,before,'production source changed during run');
 const report={passed:true,tests:2,source_stable:true,source_hashes:before,results,synthetic_fixture:true,production_data_mutated:false};
 await writeFile(resolve(root,'.local/tmp/hub2-auth-gc-node.json'),JSON.stringify(report,null,2),{mode:0o600});console.log(JSON.stringify(report));
}finally{await fixture.dispose();await rm(persist,{recursive:true,force:true});}
