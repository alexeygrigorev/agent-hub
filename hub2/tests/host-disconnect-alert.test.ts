// AC2 of intake 41ef7048 (t1801u): a host that is running but disconnected for
// more than 15 minutes raises a visible developer alert instead of failing silently.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { MachineDO, HOST_DISCONNECT_ALERT_MS, HOST_ALERT_RECIPIENT } from '../src/machine-do.ts';

const machineActor={id:'machine:desk',kind:'machine',resource_id:'desk',generation:'1',scopes:['machine:heartbeat','machine:offer','session:report']};
const header={'Content-Type':'application/json','x-hub2-actor':JSON.stringify(machineActor)};

function fixture(){
 const database=new DatabaseSync(':memory:'),sockets:any[]=[],kv=new Map<string,unknown>();
 let alarm:number|null=null;
 const ctx:any={id:{toString:()=>'machine-desk'},storage:{sql:{exec(query:string,...args:unknown[]){
  const statement=database.prepare(query);let rows:unknown[]=[],rowsRead=0,rowsWritten=0;
  if(statement.columns().length){rows=statement.all(...args as any);rowsRead=rows.length;}
  else{const result=statement.run(...args as any);rowsWritten=Number(result.changes);}
  return{toArray:()=>rows,rowsRead,rowsWritten};
 }},async get(key:string){return structuredClone(kv.get(key));},async put(key:string,value:unknown){kv.set(key,structuredClone(value));},
  async delete(key:string){return kv.delete(key);},
  async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;},
  transactionSync<T>(fn:()=>T):T{database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(error){database.exec('ROLLBACK');throw error;}}},
  blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},getWebSockets(){return sockets;}};
 const env:any={CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({current:true})})},
  MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({})})},
  PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({ok:true})})}};
 const machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown)=>machine.fetch(new Request('https://machine.test'+path,{method,headers:header,...(body===undefined?{}:{body:JSON.stringify(body)})}));
 const socket=(stopOnly=false)=>({readyState:1,close(){this.readyState=3;},send(){},
  deserializeAttachment:()=>({machine_id:'desk',actor_id:'machine:desk',generation:'1',connected_at:1,connection_id:'c',stop_only:stopOnly})});
 return{machine,call,sockets,kv,socket,database,get alarm(){return alarm;}};
}

async function withClock<T>(start:number,fn:(advance:(ms:number)=>void)=>Promise<T>):Promise<T>{
 const real=Date.now;let now=start;Date.now=()=>now;
 try{return await fn(ms=>{now+=ms;});}finally{Date.now=real;}
}

async function captureErrors<T>(fn:()=>Promise<T>):Promise<{result:T;lines:string[]}>{
 const real=console.error,lines:string[]=[];console.error=(...args:unknown[])=>{lines.push(args.map(String).join(' '));};
 try{return{result:await fn(),lines};}finally{console.error=real;}
}

const beat=(f:ReturnType<typeof fixture>)=>f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]});
const routing=async(f:ReturnType<typeof fixture>)=>await (await f.call('/_internal/routing-snapshot')).json() as any;

test('running host disconnected for more than 15 minutes raises one developer alert, cleared on reconnect',async()=>{
 await withClock(1_800_000_000_000,async advance=>{
  const f=fixture(),live=f.socket();f.sockets.push(live);
  assert.equal((await beat(f)).status,200);
  assert.equal(f.kv.size,0,'a heartbeat with an open socket starts no episode');
  live.close();
  await f.machine.webSocketClose(live as any,1006,'',false);
  const episode=f.kv.get('host_presence') as any;
  assert.equal(episode.machine_id,'desk');
  advance(5*60_000);
  assert.equal((await beat(f)).status,200);// daemon still heartbeating over HTTP
  assert.ok(f.alarm!==null&&f.alarm<=episode.disconnected_since+HOST_DISCONNECT_ALERT_MS,'running evidence arms the 15 minute deadline');
  advance(9*60_000);
  assert.equal((await beat(f)).status,200);
  let {lines}=await captureErrors(()=>f.machine.alarm());
  assert.ok(!lines.some(line=>line.includes('HOST_DISCONNECTED')),'no alert before 15 minutes');
  assert.equal((await routing(f)).host_alert,undefined);
  advance(2*60_000);
  ({lines}=await captureErrors(()=>f.machine.alarm()));
  const alert=lines.filter(line=>line.includes('HOST_DISCONNECTED alert='+HOST_ALERT_RECIPIENT));
  assert.equal(alert.length,1,'one loud developer alert');
  assert.match(alert[0],/"machine_id":"desk"/);assert.match(alert[0],/http_heartbeat_without_socket/);
  const view=await routing(f);
  assert.equal(view.connected,false);
  assert.deepEqual({kind:view.host_alert.kind,recipient:view.host_alert.recipient,evidence:view.host_alert.evidence},
   {kind:'host_disconnected',recipient:'dev@dtcdev.click',evidence:'http_heartbeat_without_socket'});
  advance(10*60_000);
  ({lines}=await captureErrors(()=>f.machine.alarm()));
  assert.ok(!lines.some(line=>line.includes('HOST_DISCONNECTED')),'an episode alerts once');
  // connect() calls hostConnected after accepting an ordinary socket; Node
  // has no WebSocketPair, so exercise that step directly.
  f.sockets.push(f.socket());
  const upgraded=await captureErrors(()=>(f.machine as any).hostConnected('desk'));
  assert.equal(f.kv.has('host_presence'),false,'reconnect ends the episode');
  assert.ok(upgraded.lines.some(line=>line.includes('HOST_RECONNECTED')));
  assert.equal((await routing(f)).host_alert,undefined);
 });
});

test('a stopped host (no heartbeat, no sessions) is not alerted; stop-only sockets do not count as connected',async()=>{
 await withClock(1_800_000_000_000,async advance=>{
  const f=fixture(),live=f.socket(),safety=f.socket(true);f.sockets.push(live,safety);
  assert.equal((await beat(f)).status,200);
  live.close();
  await f.machine.webSocketClose(live as any,1000,'',true);
  assert.ok(f.kv.has('host_presence'),'an open stop-only socket is not a connected host');
  assert.equal(f.alarm,null,'a powered-off host leaves no pending alarm');
  advance(HOST_DISCONNECT_ALERT_MS+60_000);
  const {lines}=await captureErrors(()=>f.machine.alarm());
  assert.ok(!lines.some(line=>line.includes('HOST_DISCONNECTED')),'powered-off host without running evidence stays quiet');
  assert.equal(f.alarm,null);
  advance(60_000);
  assert.equal((await beat(f)).status,200);// host comes back but still cannot open its socket
  assert.ok(f.alarm!==null,'running evidence arms the check');
  advance(5*60_000);
  const second=await captureErrors(()=>f.machine.alarm());
  assert.equal(second.lines.filter(line=>line.includes('HOST_DISCONNECTED')).length,1,'running evidence then alerts');
 });
});

test('a heartbeat from a daemon that never connects starts the watch',async()=>{
 await withClock(1_800_000_000_000,async advance=>{
  const f=fixture();
  assert.equal((await beat(f)).status,200);
  assert.ok(f.kv.has('host_presence'));
  advance(HOST_DISCONNECT_ALERT_MS-60_000);
  assert.equal((await beat(f)).status,200);
  advance(2*60_000);
  const {lines}=await captureErrors(()=>f.machine.alarm());
  assert.equal(lines.filter(line=>line.includes('HOST_DISCONNECTED alert=dev@dtcdev.click')).length,1);
 });
});
