import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {MachineDO} from '../src/machine-do';
import worker from '../src/worker';
import {spawnSync} from 'node:child_process';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {sha256Hex} from '../src/crypto';
const native='11111111-1111-4111-8111-111111111111';
const admin={id:'operator:test',kind:'operator',scopes:['operator:admin']};
const host={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','machine:offer','session:report']};
function fixture(mode='apply',existing?:DatabaseSync){
 const db=existing??new DatabaseSync(':memory:'),sockets:any[]=[],effects:string[]=[];let alarm:number|null=null;
 const ctx:any={storage:{sql:{exec(q:string,...args:any[]){const p=db.prepare(q);let rows:any[]=[],written=0;if(p.columns().length)rows=p.all(...args);else written=Number(p.run(...args).changes);return {toArray:()=>rows,rowsRead:rows.length,rowsWritten:written};}},transactionSync(fn:()=>unknown){db.exec('BEGIN');try{const result=fn();db.exec('COMMIT');return result;}catch(e){db.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn()),getWebSockets:()=>sockets};
 const namespace={idFromName:(id:string)=>id,get:()=>({fetch:async(input:any)=>{effects.push(String(input?.url??input));return Response.json({authorized:true});}})};
 const env:any={HUB2_SCHEMA_MIGRATIONS:mode,CONTROL:namespace,PROJECTS:namespace,MACHINES:namespace,ACCOUNTS:namespace,MAILBOXES:namespace};
 const machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:any,actor:any=admin)=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{'x-hub2-actor':JSON.stringify(actor),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 const boot=()=>call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]},host);
 async function publish(workspace='/work/peer'){
  const rows=[{id:native,tag:'peer',workspace,engine:'codex',agent:null,profile:null,agent_profile:null,phase:'running',state:'running',worker_alive:true,cwd:'/work/peer',created_at_ms:1,last_activity_ms:null}];
  const data=JSON.stringify(rows).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0')),at=Date.now();
  const meta={snapshot_id:crypto.randomUUID(),started_at_ms:at,observed_at_ms:at,row_count:1,chunk_count:Math.ceil(data.length/24000),sha256:await sha256Hex(data)};
  for(let i=0;i<meta.chunk_count;i++)assert.equal((await call('/api/native-inventory','POST',{...meta,chunk_index:i,data:data.slice(i*24000,(i+1)*24000)},host)).status,200);
 }
 const path='/api/native/'+native+'/commands';
 const create=(kind='stop',key='once',extra={})=>call(path,'POST',{kind,key,...extra});
 const ack=(c:any,result:any,actor:any=host)=>call(path+'/'+c.id+'/ack','POST',{native_id:native,deadline_ms:c.deadline_ms,result},actor);
 const view=async(id:string)=>(await (await call(path+'/'+id)).json() as any).command;
 function socket(stop=false){const sent:any[]=[];const attachment={machine_id:'h1',actor_id:host.id,generation:'1',stop_only:stop,connection_id:crypto.randomUUID(),connected_at:Date.now()};const socket:any={readyState:1,deserializeAttachment:()=>attachment,send:(raw:string)=>sent.push(JSON.parse(raw)),close(){this.readyState=3;}};sockets.push(socket);return{socket,sent};}
 return{db,machine,env,boot,publish,call,path,create,ack,view,socket,effects,get alarm(){return alarm;}};
}
async function ready(mode='apply'){const f=fixture(mode);assert.equal((await f.boot()).status,200);await f.publish();return f;}
async function receipt(f:ReturnType<typeof fixture>,kind='stop',key='once',extra={}){const r=await f.create(kind,key,extra);assert.equal(r.status,201,await r.clone().text());return(await r.json() as any).command;}
function proof(c:any,extra:any={}){return {stopped:true,evidence:{source:'aplexer_status',native_id:native,workspace:'/work/peer',tag:'peer',phase:'exited',containment_empty:true,exit:{code:143,exited_at_ms:Date.now()},observed_at_ms:Math.max(Date.now(),c.created_at),...extra}};}

test('native controls require exact operator admin; send never queues',async()=>{
 const f=await ready();for(const actor of [host,{...host,kind:'agent',scopes:['operator:admin']},{...admin,scopes:['session:stop']},{id:'service:fake',kind:'service',scopes:['operator:admin']}]){
  assert.equal((await f.call(f.path,'POST',{kind:'stop',key:'x'},actor)).status,403);
  assert.equal((await f.call(f.path,'GET',undefined,actor)).status,403);
 }
 assert.equal((await f.create('send')).status,409);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM native_commands').get()!.n,0);f.db.close();
});
test('fresh exact unmanaged target required; pending receipt survives census loss and key replay does not redraw identity',async()=>{
 const f=fixture();await f.boot();assert.equal((await f.create()).status,409);await f.publish();
 assert.equal((await f.call('/api/native/22222222-2222-4222-8222-222222222222/commands','POST',{kind:'stop',key:'x'})).status,404);
 const c=await receipt(f);f.db.exec('DELETE FROM native_inventory_rows; DELETE FROM native_inventory_meta');(f.machine as any).db.cache.clear();
 const replay=await f.create();assert.equal(replay.status,200);assert.equal((await replay.json() as any).command.id,c.id);
 assert.equal((await f.create('capture')).status,409);f.db.close();
});
test('managed native binding refuses commands with no Project, credential or cap writes',async()=>{
 const f=await ready();assert.equal((await f.call('/_internal/reserve','POST',{operation_id:'op',dispatch_id:'dispatch',attempt:0,session_id:'managed',project_id:'org/repo',intake_id:'i',machine_id:'h1',request_digest:'d',title:'work',acceptance_criteria:['result'],account_id:'codex',launch:{workspace:'/work',engine:'codex',model:'m',prompt:'private',interactive:false}})).status,201);
 f.db.prepare("UPDATE sessions SET native_id=? WHERE id='managed'").run(native);(f.machine as any).db.cache.clear();
 const before=f.db.prepare('SELECT * FROM sessions').all(),effects=f.effects.length;
 assert.equal((await f.create()).status,409);assert.deepEqual(f.db.prepare('SELECT * FROM sessions').all(),before);assert.equal(f.effects.length,effects);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM session_commands').get()!.n,0);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM machine_outbox').get()!.n,0);f.db.close();
});
test('normal authenticated socket delivers independent target; safety socket does not; revoke prevents dispatch',async()=>{
 const f=await ready(),normal=f.socket();const c=await receipt(f);assert.equal(normal.sent.length,1);
 const wire=normal.sent[0];assert.equal(wire.type,'session.command');assert.equal(wire.command.target_kind,'native');assert.equal(wire.command.id,c.id);assert.equal(wire.command.session_id,undefined);
 assert.deepEqual(wire.command.identity,{id:native,workspace:'/work/peer',tag:'peer'});
 const safety=f.socket(true);await (f.machine as any).sendPendingCommands(safety.socket,'h1');assert.equal(safety.sent.length,0);
 // Revocation is checked at delivery even when the queued command exists.
 f.env.CONTROL.get=()=>({fetch:async()=>Response.json({error:'revoked'},{status:403})});
 const other=f.socket();await (f.machine as any).sendPendingCommands(other.socket,'h1');assert.equal(other.sent.length,0);f.db.close();
});
test('stop ACK requires exact identity, finite exit and empty containment; absence is never success',async()=>{
 const f=await ready(),c=await receipt(f);
 for(const result of [{stopped:true},{stopped:true,evidence:{source:'dispatch_fenced'}},proof(c,{workspace:'/different'}),proof(c,{native_id:'wrong'}),proof(c,{containment_empty:false}),proof(c,{exit:{}}),proof(c,{observed_at_ms:c.created_at-1})]){
  assert.equal((await f.ack(c,result)).status,400);assert.equal((await f.view(c.id)).state,'pending');
 }
 assert.equal((await f.ack(c,proof(c),admin)).status,403);
 const result=proof(c);assert.equal((await f.ack(c,result)).status,200);assert.equal((await f.ack(c,result)).status,200);
 assert.equal((await f.view(c.id)).ack.confirmed_exit,true);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM sessions').get()!.n,0);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM machine_outbox').get()!.n,0);f.db.close();
});
test('capture result is bounded, sanitized and receipt replay persists across eviction',async()=>{
 const f=await ready(),c=await receipt(f,'capture','cap',{mode:'screen'});
 assert.equal((await f.ack(c,{output:'\x1b[31mprivate'})).status,400);assert.equal((await f.ack(c,{output:'x'.repeat(4097)})).status,400);
 assert.equal((await f.ack(c,{output:'bounded output\n'})).status,200);
 const reopened=fixture('defer',f.db);const saved=await reopened.view(c.id);assert.equal(saved.ack.capture,'bounded output\n');
 const replay=await reopened.create('capture','cap',{mode:'screen'});assert.equal(replay.status,200);assert.equal((await replay.json() as any).command.id,c.id);
 assert.equal(f.db.prepare('SELECT version FROM hub2_schema').get()!.version,9);f.db.close();
});
test('persisted deadline expires pending independent commands without synthesizing stop; late ACK refused',async()=>{
 const f=await ready(),c=await receipt(f);assert.equal(f.alarm,c.deadline_ms);
 const lateProof=proof(c),real=Date.now;Date.now=()=>c.deadline_ms+1;
 try{await f.machine.alarm();const view=await f.view(c.id);assert.equal(view.state,'failed');assert.equal(view.ack.confirmed_exit,false);assert.equal(view.ack.error,'native_command_deadline');assert.equal((await f.ack(c,lateProof)).status,409);}finally{Date.now=real;}
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM sessions').get()!.n,0);f.db.close();
});
test('schema8 bridge lacks native command DDL; apply9 and defer rollback retain user receipts',async()=>{
 const f=await ready('defer');assert.equal(f.db.prepare('SELECT version FROM hub2_schema').get()!.version,8);
 assert.equal(f.db.prepare("SELECT name FROM sqlite_master WHERE name='native_commands'").get(),undefined);assert.equal((await f.create()).status,503);
 const applied=fixture('apply',f.db);const c=await receipt(applied);const rows=f.db.prepare('SELECT * FROM native_commands').all();
 const rollback=fixture('defer',f.db);assert.equal((await rollback.view(c.id)).state,'pending');assert.deepEqual(f.db.prepare('SELECT * FROM native_commands').all(),rows);f.db.close();
});
test('public route pins machine namespace and operator authority; host can ACK only own target',async()=>{
 const f=await ready();let actor:any=admin;const forwarded:string[]=[];
 const env={...f.env,CONTROL:{idFromName:(x:string)=>x,get:()=>({fetch:async()=>Response.json({actor})})},MACHINES:{idFromName:(x:string)=>x,get:(id:string)=>({fetch:async(req:Request)=>{forwarded.push(id);return f.machine.fetch(req);}})}};
 const path='/api/machines/h1/native/'+native+'/commands';
 const call=(path:string,method='GET',body?:any)=>worker.fetch(new Request('https://hub.test'+path,{method,headers:{Authorization:'Bearer '+'synthetic'.repeat(8),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})}),env);
 const created=await call(path,'POST',{kind:'capture',key:'public'});assert.equal(created.status,201);const c=(await created.json() as any).command;assert.deepEqual(forwarded,['h1']);
 actor=host;assert.equal((await call(path,'POST',{kind:'stop',key:'bad'})).status,403);
 assert.equal((await call(path.replace('/h1/','/other/')+'/'+c.id+'/ack','POST',{})).status,403);
 assert.equal((await call(path+'/'+c.id+'/ack','POST',{native_id:native,deadline_ms:c.deadline_ms,result:{output:'ok'}})).status,200);f.db.close();
});


test('actual Python Bridge journal ACK composes with real Machine native stop protocol',async()=>{
 const f=fixture();await f.boot();
 const base=resolve(import.meta.dirname,'../../.local/tmp');mkdirSync(base,{recursive:true});const dir=mkdtempSync(join(base,'native-command-wire-'));
 try{
  await f.publish(dir);const c=await receipt(f),socket=f.socket();await (f.machine as any).sendPendingCommands(socket.socket,'h1');
  const command=socket.sent[0].command;
  const script=`import json,sys,subprocess,time
from pathlib import Path
from agent_hub_host.agent import Bridge, Journal
command=json.loads(sys.stdin.read())
root=Path(command['identity']['workspace'])
journal=Journal(root/'private'/'journal.sqlite')
row={'id':command['native_id'],'tag':'peer','workspace':str(root),'phase':'running'}
calls=[]
def runner(args,**kwargs):
 calls.append(args[1])
 if args[1]=='kill':
  row.update(phase='exited',containment_empty=True,exit={'code':143,'exited_at_ms':round(time.time()*1000)})
 return subprocess.CompletedProcess(args,0,json.dumps(row),'')
bridge=Bridge(None,journal,[root],['codex'],executable=sys.executable,runner=runner,machine_id='h1',launcher_config={'private_provider_homes':True,'aplexer_state_dir':str(root/'state')})
bridge.command(command)
bridge.command(command)
payload=json.loads(journal.db.execute('SELECT payload FROM outbox').fetchone()[0])
assert calls.count('kill')==1
assert journal.db.execute('SELECT COUNT(*) FROM launches').fetchone()[0]==0
print(json.dumps(payload))
journal.db.close()
`;
  const child=spawnSync('python3',['-c',script],{input:JSON.stringify(command),encoding:'utf8',timeout:10000,maxBuffer:16384,
   env:{...process.env,PYTHONPATH:resolve(import.meta.dirname,'../host')}});
  assert.equal(child.status,0,child.stderr);const payload=JSON.parse(child.stdout);
  assert.equal((await f.call(f.path+'/'+c.id+'/ack','POST',payload,host)).status,200);
  assert.equal((await f.view(c.id)).ack.confirmed_exit,true);assert.equal(f.db.prepare('SELECT COUNT(*) n FROM machine_outbox').get()!.n,0);
 }finally{f.db.close();rmSync(dir,{recursive:true});}
});

test('concurrent identical keys create one command and different intent conflicts',async()=>{
 const f=await ready();const responses=await Promise.all([f.create('capture','race'),f.create('capture','race')]);
 assert.deepEqual(responses.map(r=>r.status).sort(),[200,201]);
 const views=await Promise.all(responses.map(r=>r.json() as Promise<any>));assert.equal(views[0].command.id,views[1].command.id);
 assert.equal(f.db.prepare('SELECT COUNT(*) n FROM native_commands').get()!.n,1);
 assert.equal((await f.create('stop','race')).status,409);f.db.close();
});
test('SQLite close and reopen retains deadline, dispatch and ACK receipt without a managed session',async()=>{
 const base=resolve(import.meta.dirname,'../../.local/tmp');mkdirSync(base,{recursive:true});const dir=mkdtempSync(join(base,'native-command-restart-'));
 let db=new DatabaseSync(join(dir,'machine.sqlite'));
 try{
  const f=fixture('apply',db);await f.boot();await f.publish();const c=await receipt(f,'capture','disk');db.close();
  db=new DatabaseSync(join(dir,'machine.sqlite'));const reopened=fixture('defer',db);
  const view=await reopened.view(c.id);assert.equal(view.deadline_ms,c.deadline_ms);assert.equal(reopened.alarm,c.deadline_ms);
  const socket=reopened.socket();await (reopened.machine as any).sendPendingCommands(socket.socket,'h1');assert.equal(socket.sent[0].command.id,c.id);
  assert.equal((await reopened.ack(c,{output:'after restart'})).status,200);db.close();
  db=new DatabaseSync(join(dir,'machine.sqlite'));const final=fixture('defer',db);assert.equal((await final.view(c.id)).ack.capture,'after restart');
  assert.equal(db.prepare('SELECT COUNT(*) n FROM sessions').get()!.n,0);
 }finally{db.close();rmSync(dir,{recursive:true});}
});


test('maximum accepted Unicode workspace remains ACK-able with actual ASCII host wire framing',async()=>{
 const f=fixture();await f.boot();const workspace='/'+'界'.repeat(2730);await f.publish(workspace);const c=await receipt(f);
 const result=proof(c,{workspace});const payload={native_id:native,deadline_ms:c.deadline_ms,result};
 const ascii=JSON.stringify(payload).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
 assert(ascii.length>12288);
 const response=await f.machine.fetch(new Request('https://machine.test'+f.path+'/'+c.id+'/ack',{method:'POST',headers:{'x-hub2-actor':JSON.stringify(host),'Content-Type':'application/json'},body:ascii}));
 assert.equal(response.status,200,await response.clone().text());assert.equal((await f.view(c.id)).ack.confirmed_exit,true);f.db.close();
});


test('target bound to a managed row after enqueue is cancelled before native delivery',async()=>{
 const f=await ready(),c=await receipt(f);
 assert.equal((await f.call('/_internal/reserve','POST',{operation_id:'op-late',dispatch_id:'dispatch-late',attempt:0,session_id:'managed-late',project_id:'org/repo',intake_id:'i',machine_id:'h1',request_digest:'d',title:'work',acceptance_criteria:['result'],account_id:'codex',launch:{workspace:'/work',engine:'codex',model:'m',prompt:'private',interactive:false}})).status,201);
 f.db.prepare("UPDATE sessions SET native_id=? WHERE id='managed-late'").run(native);(f.machine as any).db.cache.clear();
 const socket=f.socket();await (f.machine as any).sendPendingCommands(socket.socket,'h1');assert.equal(socket.sent.length,0);
 assert.equal((await f.view(c.id)).ack.error,'native_target_managed');assert.equal(f.db.prepare('SELECT COUNT(*) n FROM machine_outbox').get()!.n,0);f.db.close();
});
