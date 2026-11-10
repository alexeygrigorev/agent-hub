import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, readdirSync } from 'node:fs';
import { resolve, join } from 'node:path';
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
function fixture(binding='TEST',name='a/long-object-name-with-slashes-and-more-than-sixty-characters-to-preserve-in-full') {
  const parent=resolve('.local/tmp');mkdirSync(parent,{recursive:true});
  const dataDir=mkdtempSync(join(parent,'alarm-recovery-'));
  const options={classes:{[binding]:Probe},dataDir,env:{},entry:{fetch:(request:Request,env:any)=>env[binding].get(env[binding].idFromName(name)).fetch(request)}};
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
