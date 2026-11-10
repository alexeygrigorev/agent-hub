import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { registerTypeScriptResolveHook } from '../node/compat.ts';
registerTypeScriptResolveHook();
const { MachineDO } = await import('../src/machine-do.ts');

function fixture(){
 const database=new DatabaseSync(':memory:'),sockets:any[]=[],checks:any[]=[];
 let alarm:number|null=null;
 const ctx:any={storage:{sql:{exec(query:string,...args:unknown[]){
  const statement=database.prepare(query);
  if(statement.columns().length){const rows=statement.all(...args as any);return{toArray:()=>rows,rowsRead:rows.length,rowsWritten:0};}
  const result=statement.run(...args as any);return{toArray:()=>[],rowsRead:Number(result.changes),rowsWritten:Number(result.changes)};
 }},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const value=fn();database.exec('COMMIT');return value;}catch(error){database.exec('ROLLBACK');throw error;}},
 async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;}},
 blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},getWebSockets(tag:string){return sockets.filter(socket=>socket.tags.includes(tag));}};
 const env:any={CONTROL:{idFromName:(name:string)=>name,get:()=>({fetch:async(input:Request|string,init?:RequestInit)=>{
  const request=input instanceof Request?input:new Request(input,init);checks.push(await request.json());
  return Response.json({current:true},{status:checks.at(-1).actor_id==='revoked'?403:200});
 }})}};
 const machine=new MachineDO(ctx,env);
 const actor={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat']};
 const call=(path:string,body?:unknown)=>machine.fetch(new Request('https://machine.test'+path,{method:body===undefined?'GET':'POST',headers:{'x-hub2-actor':JSON.stringify(actor),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 function socket(actor_id:string,connected_at:number,extra:any={}){
  const value:any={tags:['hub2:h1'],readyState:1,closes:0,deserializeAttachment:()=>({machine_id:'h1',actor_id,generation:'1',connected_at,connection_id:String(connected_at),...extra}),close(){this.closes++;this.readyState=3;}};
  sockets.push(value);return value;
 }
 return{database,machine,call,socket,checks};
}

test('passive machine snapshot keeps newest qualified connection without pruning duplicates or writing',async()=>{
 const f=fixture();try{
  assert.equal((await f.call('/api/heartbeat',{labels:['worker'],engines:['antigravity'],capabilities:['headless','credentials:local:antigravity']})).status,200);
  const older=f.socket('older-valid',1),newer=f.socket('newer-valid',2);
  const before=(f.database.prepare('SELECT total_changes() AS n').get() as any).n;
  const view=await(await f.call('/_internal/routing-snapshot')).json() as any;
  assert.equal(view.connected,true);assert.equal(view.machine_id,'h1');assert.deepEqual(view.engines,['antigravity']);
  assert.deepEqual(view.capabilities,['credentials:local:antigravity','headless']);
  assert.equal(older.closes+newer.closes,0);assert.ok(f.checks.every(row=>row.actor_id==='newer-valid'));
  assert.equal((f.database.prepare('SELECT total_changes() AS n').get() as any).n,before);
 }finally{f.database.close();}
});

for(const condition of ['revoked','other-machine','stop-only','closed'])test(`passive ${condition} newest socket never falls back to an older valid connection`,async()=>{
 const f=fixture();try{
  await f.call('/api/heartbeat',{labels:[],engines:['codex'],capabilities:['headless']});
  const older=f.socket('older-valid',1),newer=f.socket(condition==='revoked'?'revoked':'newer',2,
   condition==='other-machine'?{machine_id:'h2'}:condition==='stop-only'?{stop_only:true}:{});
  if(condition==='closed')newer.readyState=3;
  const before=(f.database.prepare('SELECT total_changes() AS n').get() as any).n;
  const view=await(await f.call('/_internal/routing-snapshot')).json() as any;
  // A closed socket is not eligible for active newest selection; older remains current.
  assert.equal(view.connected,condition==='closed');
  assert.equal(older.closes+newer.closes,0);
  if(condition!=='closed')assert.ok(!f.checks.some(row=>row.actor_id==='older-valid'));
  assert.equal((f.database.prepare('SELECT total_changes() AS n').get() as any).n,before);
 }finally{f.database.close();}
});

test('active machine routing retains duplicate pruning and revoked-connection close behavior',async()=>{
 const f=fixture();try{
  await f.call('/api/heartbeat',{labels:[],engines:['codex'],capabilities:['headless']});
  const older=f.socket('older-valid',1),newer=f.socket('revoked',2);
  const view=await(await f.call('/_internal/routing')).json() as any;
  assert.equal(view.connected,false);assert.equal(older.closes,1);assert.equal(newer.closes,1);
 }finally{f.database.close();}
});
