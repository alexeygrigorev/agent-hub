import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {MachineDO} from '../src/machine-do';
import worker from '../src/worker';
import {sha256Hex} from '../src/crypto';
const host={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat']};
const admin={id:'human:test',kind:'operator',scopes:['operator:admin']};
function fixture(mode='apply',existing?:DatabaseSync){
 const db=existing??new DatabaseSync(':memory:');let alarm:number|null=null;
 const ctx:any={id:{toString:()=> 'inventory-h1'},storage:{sql:{exec(q:string,...args:any[]){const p=db.prepare(q);let rows:any[]=[],written=0;if(p.columns().length)rows=p.all(...args);else written=Number(p.run(...args).changes);return {toArray:()=>rows,rowsRead:rows.length,rowsWritten:written};}},transactionSync(fn:()=>unknown){db.exec('BEGIN');try{const result=fn();db.exec('COMMIT');return result;}catch(e){db.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn()),getWebSockets:()=>[]};
 const unused={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({authorized:true})})};
 const env:any={HUB2_SCHEMA_MIGRATIONS:mode,CONTROL:unused,PROJECTS:unused,MACHINES:unused,ACCOUNTS:unused,MAILBOXES:unused};
 const machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:any,actor:any=host)=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{'x-hub2-actor':JSON.stringify(actor),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 const boot=()=>call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]});
 return {db,call,boot,machine,env};
}
function row(number=1,phase='running'){
 return {id:`00000000-0000-4000-8000-${String(number).padStart(12,'0')}`,tag:'peer-'+number,workspace:'/work/peer',engine:'codex',agent:null,profile:'default',agent_profile:null,phase,state:phase,worker_alive:phase==='running',cwd:'/work/peer',created_at_ms:1,last_activity_ms:null};
}
async function chunks(rows:any[],options:{id?:string;at?:number;size?:number}={}){
 const raw=JSON.stringify(rows).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
 const size=options.size??24000,id=options.id??crypto.randomUUID(),at=options.at??Date.now(),digest=await sha256Hex(raw);
 return Array.from({length:Math.ceil(raw.length/size)},(_,i)=>({snapshot_id:id,started_at_ms:at-1,observed_at_ms:at,
  row_count:rows.length,chunk_count:Math.ceil(raw.length/size),sha256:digest,chunk_index:i,data:raw.slice(i*size,(i+1)*size)}));
}
async function publish(f:ReturnType<typeof fixture>,parts:any[]){let result:any;for(const part of parts){const r=await f.call('/api/native-inventory','POST',part);assert.equal(r.status,200,await r.clone().text());result=await r.json();}return result;}
async function list(f:ReturnType<typeof fixture>,query=''){const r=await f.call('/api/sessions'+query,'GET',undefined,admin);assert.equal(r.status,200);return await r.json() as any;}

test('complete chunks publish atomically, replay safely, and incomplete replacement preserves prior census',async()=>{
 const f=fixture();await f.boot();const old=await chunks([row()],{at:Date.now()-1000});await publish(f,old);
 const parts=await chunks([row(2),row(3)],{size:200});
 const first=await f.call('/api/native-inventory','POST',parts[0]);assert.equal((await first.json() as any).complete,false);
 assert.equal((await list(f)).items[0].native_id,row().id);
 assert.equal((await f.call('/api/native-inventory','POST',{...parts[0],data:'conflict'})).status,409);
 await publish(f,[...parts].reverse());
 const view=await list(f);assert.equal(view.items.length,2);assert.equal(view.native_inventory.complete,true);
 assert.equal((await publish(f,parts)).replayed,true);
 assert.equal((await f.call('/api/native-inventory','POST',old[0])).status,409);
 assert.equal((await f.call('/api/native-inventory','POST',{...parts[0],row_count:4})).status,409);
 f.db.close();
});
test('invalid identity, private fields, digest and stale snapshots cannot replace accepted inventory',async()=>{
 const f=fixture();await f.boot();await publish(f,await chunks([row()],{at:Date.now()-1000}));
 let tick=Date.now();
 for(const rows of [[row(),row()],[{...row(2),command:['secret']}],[{...row(2),workspace:'relative'}],[{...row(2),last_activity_ms:true}],[{...row(2),phase:'inferred-dead'}]]){
  const part=(await chunks(rows,{at:++tick}))[0];assert.equal((await f.call('/api/native-inventory','POST',part)).status,400);
 }
 const part=(await chunks([row(9)],{at:++tick}))[0];
 assert.equal((await f.call('/api/native-inventory','POST',{...part,sha256:'0'.repeat(64)})).status,409);
 assert.equal((await f.call('/api/native-inventory','POST',{...part,observed_at_ms:Date.now()-130000,started_at_ms:Date.now()-130001})).status,400);
 assert.equal((await list(f)).items[0].native_id,row().id);f.db.close();
});
test('UUID join, pagination, history and managed cap projection remain distinct',async()=>{
 const f=fixture();await f.boot();const managed=row();
 const reserve=await f.call('/_internal/reserve','POST',{operation_id:'op',dispatch_id:'dispatch',attempt:0,session_id:'managed',project_id:'org/repo',intake_id:'intake',machine_id:'h1',request_digest:'digest',title:'work',acceptance_criteria:['result'],account_id:'codex',launch:{workspace:'/work/requested',engine:'codex',model:'gpt-6.1-sol',prompt:'private',interactive:false}});
 assert.equal(reserve.status,201);
 f.db.prepare("UPDATE sessions SET state='running',native_id=? WHERE id='managed'").run(managed.id);
 await publish(f,await chunks([managed,row(2),row(3,'exited')]));
 const first=await list(f,'?live=1&limit=1');assert.equal(first.items[0].id,'managed');assert.equal(first.items[0].workspace,'/work/requested');assert.equal(first.items[0].native_observation.workspace,'/work/peer');
 const second=await list(f,'?live=1&limit=1&after='+encodeURIComponent(first.next));assert.equal(second.items[0].managed,false);assert.equal(second.items[0].native_id,row(2).id);assert.equal(second.next,null);
 const all=await list(f);assert.equal(all.items.length,3);assert(!JSON.stringify(all).includes('private'));
 const cap=await f.call('/_internal/live-sessions');assert.deepEqual((await cap.json() as any).sessions,[]);
 // Absence is not exit: managed state and binding survive replacement with empty inventory.
 await publish(f,await chunks([],{at:Date.now()+1}));assert.equal((await list(f)).items[0].state,'running');
 assert.equal((f.db.prepare("SELECT native_id FROM sessions WHERE id='managed'").get() as any).native_id,managed.id);f.db.close();
});
test('defer7 has no census DDL, migration8 and bridge reopening preserve observations',async()=>{
 const f=fixture('defer');await f.boot();assert.equal((f.db.prepare('SELECT version FROM hub2_schema').get() as any).version,7);
 assert.equal(f.db.prepare("SELECT name FROM sqlite_master WHERE name='native_inventory_rows'").get(),undefined);
 assert.equal((await f.call('/api/native-inventory','POST',{})).status,503);
 assert.equal((await list(f)).native_inventory.error,'inventory_schema_pending');
 const active=fixture('apply',f.db);await active.boot();await publish(active,await chunks([row()]));
 const rollback=fixture('defer',f.db);assert.equal((await list(rollback)).items[0].native_id,row().id);
 assert.equal((f.db.prepare('SELECT version FROM hub2_schema').get() as any).version,8);f.db.close();
});
test('only the enrolled machine can publish; stale census is exposed but not advertised live',async()=>{
 const f=fixture();await f.boot();const part=(await chunks([row()]))[0];
 assert.equal((await f.call('/api/native-inventory','POST',part,admin)).status,403);
 assert.equal((await f.call('/api/native-inventory','POST',part,{...host,resource_id:'h2'})).status,403);
 await publish(f,[part]);const metadata=JSON.parse((f.db.prepare('SELECT metadata_json FROM native_inventory_meta').get() as any).metadata_json);metadata.observed_at_ms=Date.now()-100000;
 f.db.prepare('UPDATE native_inventory_meta SET metadata_json=?').run(JSON.stringify(metadata));
 // A newly instantiated reader cannot accidentally use a stale cached freshness value.
 const reopened=fixture('apply',f.db);const live=await list(reopened,'?live=1');assert.equal(live.items.length,0);assert.equal(live.native_inventory.fresh,false);
 assert.equal((await list(reopened)).items.length,1);f.db.close();
});


test('public upload authenticates exact machine and forwards bounded body; both list paths retain inventory metadata',async()=>{
 const f=fixture();await f.boot();let actor:any=host;const forwarded:string[]=[];
 const env={...f.env,
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{
   const url=new URL(request.url);
   if(url.pathname==='/_verify')return Response.json({actor});
   if(url.pathname==='/api/directory')return Response.json(url.searchParams.get('after')==='h1'?{items:[{id:'h2'}],next:null}:{items:[{id:'h1'}],next:'h1'});
   throw new Error('unexpected control route');
  }})},
  MACHINES:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(request:Request)=>{
   forwarded.push(id+':'+new URL(request.url).pathname);
   if(id==='h2')return Response.json({items:[],next:null,native_inventory:{complete:false,fresh:false,error:'inventory_schema_pending'}});
   return f.machine.fetch(request);
  }})},
 };
 const call=(path:string,method='GET',body?:any)=>worker.fetch(new Request('https://hub.test'+path,{method,headers:{Authorization:'Bearer '+'synthetic-token'.repeat(3),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})}),env);
 const parts=await chunks([row(1),row(2),row(3,'exited')],{size:200});
 for(const part of parts){assert.equal((await call('/api/machines/h1/native-inventory','POST',part)).status,200);}
 const before=forwarded.length;
 assert.equal((await call('/api/machines/h2/native-inventory','POST',parts[0])).status,403);
 actor=admin;assert.equal((await call('/api/machines/h1/native-inventory','POST',parts[0])).status,403);
 actor={...host,scopes:[]};assert.equal((await call('/api/machines/h1/native-inventory','POST',parts[0])).status,403);
 assert.equal(forwarded.length,before);
 actor=admin;
 for(const machine of ['h1','']){
  const query=new URLSearchParams({limit:'1',live:'1'});if(machine)query.set('machine_id',machine);
  const ids:string[]=[],inventories:any[]=[];let cursor:string|null=null;
  do{
   if(cursor)query.set('after',cursor);
   const response=await call('/api/sessions?'+query);assert.equal(response.status,200);
   const page=await response.json() as any;ids.push(...page.sessions.map((item:any)=>item.native_id));
   inventories.push(...(page.native_inventories??[page.native_inventory]));cursor=page.next_cursor;
  }while(cursor);
  assert.deepEqual(ids,[row(1).id,row(2).id]);assert(inventories.some(item=>item.complete===true));
  if(!machine)assert(inventories.some(item=>item.machine_id==='h2'&&item.error==='inventory_schema_pending'));
 }
 f.db.close();
});
test('complete inventory exceeds old managed100 cap without truncation and uses observed activity only',async()=>{
 const f=fixture();await f.boot();const rows=Array.from({length:130},(_,i)=>row(i+1));await publish(f,await chunks(rows));
 let after='',count=0;do{const page=await list(f,'?live=1&limit=100&after='+encodeURIComponent(after));count+=page.items.length;assert(page.items.every((item:any)=>item.last_activity_at===null));after=page.next;}while(after);
 assert.equal(count,130);f.db.close();
});


test('derived dead and unknown workers never appear live despite persisted running phase',async()=>{
 const f=fixture();await f.boot();
 await publish(f,await chunks([{...row(1),state:'broken',worker_alive:false},
  {...row(2),state:'exited',worker_alive:false},{...row(3),state:'unknown',worker_alive:null},row(4)]));
 const live=await list(f,'?live=1');assert.deepEqual(live.items.map((item:any)=>item.native_id),[row(4).id]);
 const history=await list(f);assert.equal(history.items.length,4);
 assert.equal(history.items[0].phase,'running');assert.equal(history.items[0].state,'broken');assert.equal(history.items[0].worker_alive,false);
 assert.equal(history.items[2].state,'unknown');assert.equal(history.items[2].worker_alive,null);f.db.close();
});
test('snapshot replacement invalidates both selected-machine and fleet continuations',async()=>{
 for(const selected of ['h1','']){
  const f=fixture();await f.boot();const at=Date.now();await publish(f,await chunks([row(2),row(3)],{at}));
  const env={...f.env,CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{
   if(new URL(request.url).pathname==='/_verify')return Response.json({actor:admin});
   return Response.json({items:[{id:'h1'}],next:null});
  }})},MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:(request:Request)=>f.machine.fetch(request)})}};
  const query=new URLSearchParams({live:'1',limit:'1'});if(selected)query.set('machine_id',selected);
  const call=()=>worker.fetch(new Request('https://hub.test/api/sessions?'+query,{headers:{Authorization:'Bearer '+'synthetic-token'.repeat(3)}}),env);
  const first=await (await call()).json() as any;assert.equal(first.sessions[0].native_id,row(2).id);assert(first.next_cursor);
  await publish(f,await chunks([row(1),row(3)],{at:at+1}));query.set('after',first.next_cursor);
  const next=await call();assert.equal(next.status,409);assert.equal((await next.json() as any).error,'native_inventory_changed');
  query.delete('after');const restarted=await (await call()).json() as any;assert.equal(restarted.sessions[0].native_id,row(1).id);f.db.close();
 }
});
test('freshness transition invalidates a generation-bound cursor even without replacement',async()=>{
 const f=fixture();await f.boot();await publish(f,await chunks([row(1),row(2)]));
 const first=await list(f,'?live=1&limit=1'),now=Date.now;
 try{
  const future=now()+95_000;Date.now=()=>future;
  const response=await f.call('/api/sessions?live=1&limit=1&after='+encodeURIComponent(first.next),'GET',undefined,admin);
  assert.equal(response.status,409);assert.equal((await response.json() as any).error,'native_inventory_changed');
 }finally{Date.now=now;f.db.close();}
});
