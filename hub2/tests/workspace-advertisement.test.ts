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

test('large clone inventory reaches routing without dropping engine capabilities',async()=>{
 const f=fixture();try{
  const workspaces=Array.from({length:100},(_,i)=>'workspace:project-'+i);
  const capabilities=['headless','interactive:codex','effort:codex',...workspaces];
  assert.equal((await f.call('/api/heartbeat',{labels:[],engines:['codex'],capabilities})).status,200);
  f.socket('live',1);
  const view=await(await f.call('/_internal/routing-snapshot')).json() as any;
  assert.deepEqual(new Set(view.capabilities),new Set(capabilities));
  // Removal is reflected on the next observation; stale clone labels disappear.
  await f.call('/api/heartbeat',{labels:[],engines:['codex'],capabilities:['headless']});
  const next=await(await f.call('/_internal/routing-snapshot')).json() as any;
  assert.deepEqual(next.capabilities,['headless']);
 }finally{f.database.close();}
});

test('workspace allowance does not widen the ordinary capability or engine bounds',async()=>{
 const f=fixture();try{
  for(const capabilities of [Array.from({length:33},(_,i)=>'tool:item'+i),
    Array.from({length:513},(_,i)=>'workspace:repo'+i),
    Array.from({length:33},(_,i)=>'workspace:invalid/'+i)]){
   assert.equal((await f.call('/api/heartbeat',{labels:[],engines:['codex'],capabilities})).status,400);
  }
  assert.equal((await f.call('/api/heartbeat',{labels:[],engines:Array(33).fill('codex'),capabilities:[]})).status,400);
 }finally{f.database.close();}
});
