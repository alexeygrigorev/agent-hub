import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {ControlDO} from '../src/control-do';
import {MachineDO} from '../src/machine-do';
import {MeteredDb,initializeSchema,schemaMigrationCeiling} from '../src/db';
const admin={id:'human:bridge-test',kind:'operator',scopes:['operator:admin']};
function state(database=new DatabaseSync(':memory:')){
 let alarm:number|null=null;
 const ctx:any={id:{toString:()=> 'bridge-test'},storage:{sql:{exec(q:string,...args:any[]){const p=database.prepare(q);return{toArray:()=>p.columns().length?p.all(...args):(p.run(...args),[])};}},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const r=fn();database.exec('COMMIT');return r;}catch(e){database.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn()),getWebSockets:()=>[]};
 return{database,ctx,get alarm(){return alarm;}};
}
function fixture(kind:'control'|'machine',mode='defer',existing?:ReturnType<typeof state>){
 const s=existing||state();
 const unused={idFromName:(v:string)=>v,get:()=>({fetch:async()=>Response.json({authorized:true,accepted:true})})};
 const env:any={HUB2_SCHEMA_MIGRATIONS:mode,CONTROL:unused,PROJECTS:unused,MACHINES:unused,ACCOUNTS:unused,MAILBOXES:unused};
 const instance=kind==='control'?new ControlDO(s.ctx,env):new MachineDO(s.ctx,env);
 const call=(path:string,method='GET',body?:unknown,actor:any=admin)=>instance.fetch(new Request('https://bridge.test'+path,{method,headers:{'x-hub2-actor':JSON.stringify(actor),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{...s,instance,call};
}
const version=(s:ReturnType<typeof state>)=>(s.database.prepare('SELECT version FROM hub2_schema').get() as any).version;
const data=async(r:Response)=>await r.json() as any;
test('control bridge defers shared-memory migration, then rollback preserves post-upgrade user writes and revocation data',async()=>{
 const bridge=fixture('control');
 assert.equal((await bridge.call('/api/intake/rules')).status,503);assert.equal(version(bridge),4);
 assert.equal((await bridge.call('/api/intake/rules','GET',undefined,{id:'foreign',kind:'agent',scopes:[]})).status,403);
 bridge.database.prepare("INSERT INTO worker_credentials(session_id,epoch,operation_id,machine_id,intake_id,project_id,deadline,sealed_token,token_hash,revoked_at) VALUES('revoked-session',1,'revoked-operation','h1','revoked-intake','org/a',9999999999999,'synthetic-sealed','synthetic-hash',123)").run();
 const credential=bridge.database.prepare('SELECT * FROM worker_credentials').all();
 const preserved=(bridge.database.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").all() as any[]).map(row=>row.name);
 const applied=fixture('control','apply',bridge);
 const rule={id:'after-migration',key:'rule-bridge-1',expected_version:0,text:'Preserve this operator rule',source:{type:'user',reference:'synthetic'},decision:'Apply'};
 assert.equal((await applied.call('/api/intake/rules','POST',rule)).status,200);assert.equal(version(applied),7);
 const ingest={source:'cli',agents:[{agent_id:'bridge-writer',messages:[{msg_id:'t1u',role:'user',text:'Preserve this ask after rollback'}]}]};
 assert.equal((await applied.call('/api/asks/ingest','POST',ingest)).status,200);
 const before=applied.database.prepare('SELECT * FROM memory_messages').all();
 const rollback=fixture('control','defer',applied);
 assert.equal((await rollback.call('/api/intake/rules')).status,200);assert.equal(version(rollback),7);
 assert.equal((await data(await rollback.call('/api/intake/rules'))).operator_rules[0].text,undefined);
 assert.equal((await data(await rollback.call('/api/asks'))).asks[0].text,'Preserve this ask after rollback');
 assert.deepEqual(rollback.database.prepare('SELECT * FROM memory_messages').all(),before);
 assert.deepEqual(rollback.database.prepare('SELECT * FROM worker_credentials').all(),credential);
 const upgradedAgain=fixture('control','apply',rollback);
 assert.equal((await data(await upgradedAgain.call('/api/intake/rules'))).operator_rules[0].history[0].text,rule.text);
 assert.equal((await upgradedAgain.call('/api/intake/rules','POST',rule)).status,200);
 for(const name of preserved)assert(upgradedAgain.database.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(name));
});
async function running(f:ReturnType<typeof fixture>){
 const machine={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
 assert.equal((await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]},machine)).status,200);
 const reserve=await f.call('/_internal/reserve','POST',{operation_id:'op-bridge',dispatch_id:'dispatch-bridge',attempt:0,session_id:'bridge-session',project_id:'project-test',intake_id:'bridge-intake',machine_id:'h1',request_digest:'bridge-digest',title:'bridge task',acceptance_criteria:['real result'],account_id:'codex',launch:{workspace:'/synthetic',engine:'codex',model:'gpt-6.1-sol',prompt:'test',interactive:false}});
 assert.equal(reserve.status,201);const epoch=(await data(reserve)).epoch;
 assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-bridge',session_id:'bridge-session',epoch})).status,201);
 f.database.prepare("UPDATE sessions SET state='running',native_id='11111111-1111-4111-8111-111111111111' WHERE id='bridge-session'").run();
 return epoch;
}
test('first bridge keeps legacy capture contract; upgraded bridge preserves screen commands, replay, outbox and alarms',async()=>{
 const bridge=fixture('machine');await running(bridge);
 bridge.database.exec('DROP TABLE native_inventory_meta; DROP TABLE native_inventory_rows; DROP TABLE native_inventory_upload; DROP TABLE native_inventory_chunks; ALTER TABLE session_commands DROP COLUMN capture_mode; UPDATE hub2_schema SET version=6');
 (bridge.instance as any).db.schemaVersion=6;assert.equal(version(bridge),6);
 assert.equal((await bridge.call('/api/sessions/bridge-session/capture','POST',{key:'screen-deferred',mode:'screen'})).status,503);
 assert.equal(bridge.database.prepare('SELECT * FROM session_commands').all().length,0);
 const receipt=await bridge.call('/api/sessions/bridge-session/capture','POST',{key:'capture-before-upgrade'});
 assert.equal(receipt.status,201);const command=(await data(receipt)).command;
 const old=bridge.database.prepare('SELECT * FROM session_commands WHERE id=?').get(command.id) as any;
 assert.equal(old.text,null);assert.equal(command.mode,undefined);assert.equal(command.text,undefined);
 // Legacy schema6 hashes exactly these fields and emits the same capture wire payload.
 const legacyDigest=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify({kind:'capture',text:null,epoch:old.epoch})))).toString('hex');
 assert.equal(old.request_digest,legacyDigest);
 assert.equal(old.result_json,null);
 assert.deepEqual(command,{id:old.id,session_id:old.session_id,epoch:old.epoch,kind:'capture'});
 const due=Date.now()+2000;
 bridge.database.prepare("INSERT INTO machine_outbox VALUES('pending-receipt','finish','op-bridge','project-test','{}','pending',2,?,?,?)").run(due,Date.now(),Date.now());
 await bridge.ctx.storage.setAlarm(due);
 const outboxes=bridge.database.prepare('SELECT * FROM machine_outbox').all();
 const applied=fixture('machine','apply',bridge);
 assert.equal((await applied.call('/api/sessions/bridge-session/commands')).status,200);assert.equal(version(applied),10);
 const row=applied.database.prepare('SELECT * FROM session_commands WHERE id=?').get(command.id) as any;
 assert.equal(row.capture_mode,null);assert.equal(row.text,null);assert.equal(row.request_digest,old.request_digest);
 const replay=await applied.call('/api/sessions/bridge-session/capture','POST',{key:'capture-before-upgrade'});
 assert.equal(replay.status,200);assert.deepEqual((await data(replay)).command,command);
 assert.equal((await applied.call('/api/sessions/bridge-session/capture','POST',{key:'capture-before-upgrade',mode:'screen'})).status,409);
 assert.equal((await applied.call('/api/sessions/bridge-session/capture','POST',{key:'after-upgrade',mode:'screen'})).status,201);
 const rollback=fixture('machine','defer',applied);
 const listed=await rollback.call('/api/sessions/bridge-session/commands');assert.equal(listed.status,200);assert.equal((await data(listed)).commands.length,2);
 assert.equal(await rollback.ctx.storage.getAlarm(),due);
 assert.equal(version(rollback),10);assert.deepEqual(rollback.database.prepare('SELECT * FROM machine_outbox').all(),outboxes);
 const commands=rollback.database.prepare('SELECT * FROM session_commands').all();
 const again=fixture('machine','apply',rollback);assert.equal((await again.call('/api/sessions/bridge-session')).status,200);
 assert.deepEqual(again.database.prepare('SELECT * FROM session_commands').all(),commands);
});
test('legacy capture ACK remains unchanged across deferred, applied and rollback readers',async()=>{
 const bridge=fixture('machine');const epoch=await running(bridge);
 bridge.database.exec('DROP TABLE native_inventory_meta; DROP TABLE native_inventory_rows; DROP TABLE native_inventory_upload; DROP TABLE native_inventory_chunks; ALTER TABLE session_commands DROP COLUMN capture_mode; UPDATE hub2_schema SET version=6');
 (bridge.instance as any).db.schemaVersion=6;
 const machine={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['session:report']};
 const path='/api/sessions/bridge-session/commands';
 const queued=await data(await bridge.call(path,'POST',{kind:'capture',key:'legacy-host-capture'}));
 assert.equal(queued.command.capture_binding,undefined);
 const ack={epoch,result:{output:'retained legacy host output'}};
 const ackPath=path+'/'+queued.command.id+'/ack';
 assert.equal((await bridge.call(ackPath,'POST',ack,machine)).status,200);
 const before=bridge.database.prepare('SELECT * FROM session_commands WHERE id=?').get(queued.command.id) as any;
 assert.deepEqual(JSON.parse(before.result_json),ack.result);
 const applied=fixture('machine','apply',bridge);
 assert.equal((await applied.call(ackPath,'POST',ack,machine)).status,200);
 const fresh=await data(await applied.call(path,'POST',{kind:'capture',key:'upgraded-host-capture'}));
 assert.deepEqual(fresh.command.capture_binding,{native_id:'11111111-1111-4111-8111-111111111111',operation_id:'op-bridge',request_digest:before.request_digest});
 const rollback=fixture('machine','defer',applied);
 assert.equal((await rollback.call(ackPath,'POST',ack,machine)).status,200);
 assert.equal((await rollback.call(path+'/'+fresh.command.id+'/ack','POST',ack,machine)).status,200,'upgraded bound running capture accepts an old host without an echo');
 assert.equal(version(rollback),10);
 const after=rollback.database.prepare('SELECT * FROM session_commands WHERE id=?').get(queued.command.id) as any;
 assert.equal(after.request_digest,before.request_digest);assert.equal(after.result_json,before.result_json);
});
test('a bound stop survives the schema-six to schema-seven bridge with its dispatch evidence',async()=>{
 const bridge=fixture('machine'),epoch=await running(bridge);
 bridge.database.exec('DROP TABLE native_inventory_meta; DROP TABLE native_inventory_rows; DROP TABLE native_inventory_upload; DROP TABLE native_inventory_chunks; ALTER TABLE session_commands DROP COLUMN capture_mode; UPDATE hub2_schema SET version=6');(bridge.instance as any).db.schemaVersion=6;
 const machine={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['session:report']};
 const path='/api/sessions/bridge-session/commands';
 const queued=await bridge.call(path,'POST',{key:'stop-before-upgrade',kind:'stop'});
 assert.equal(queued.status,201);
 const {command}=await data(queued);
 assert.equal(version(bridge),6);
 const before=await data(await bridge.call(path));
 assert.equal(before.commands[0].ack,null);
 const applied=fixture('machine','apply',bridge);
 const receipt=await applied.call(path+'/'+command.id+'/ack','POST',{epoch,result:{stopped:true}},machine);
 assert.equal(receipt.status,200);
 assert.equal(version(applied),10);
 const after=await data(await applied.call(path));
 assert.equal(after.commands[0].ack.confirmed_exit,true);
 assert.equal(after.commands[0].ack.native_id,'11111111-1111-4111-8111-111111111111');
});

test('unknown future schemas remain refused in both bridge and apply mode',async()=>{
 for(const kind of ['control','machine'] as const)for(const mode of ['defer','apply']){
  const s=state();s.database.exec('CREATE TABLE hub2_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL); INSERT INTO hub2_schema VALUES(1,999)');
  const f=fixture(kind,mode,s);assert.equal((await f.call('/api/intake/rules')).status,500);assert.equal(version(f),999);
 }
});
test('invalid ceiling and partial failed migration never commit a version or table',async()=>{
 const s=state(),db=new MeteredDb(s.ctx.storage.sql);
 await assert.rejects(initializeSchema(s.ctx,db,2,[],[],undefined,1,0),/invalid_schema_migration_ceiling/);
 await initializeSchema(s.ctx,db,2,['CREATE TABLE retained(id TEXT PRIMARY KEY)'],[['CREATE TABLE new_table(id TEXT PRIMARY KEY)','INVALID SQL']],undefined,1,1);
 db.run("INSERT INTO retained VALUES('before-upgrade')");
 await assert.rejects(initializeSchema(s.ctx,new MeteredDb(s.ctx.storage.sql),2,[],[['CREATE TABLE new_table(id TEXT PRIMARY KEY)','INVALID SQL']]),/syntax/);
 assert.equal(version(s),1);assert.equal(s.database.prepare('SELECT * FROM retained').all().length,1);
 assert.equal(s.database.prepare("SELECT name FROM sqlite_master WHERE name='new_table'").all().length,0);
 assert.equal(schemaMigrationCeiling('defer',1,2),1);assert.throws(()=>schemaMigrationCeiling('unknown',1,2),/invalid_schema_migration_mode/);
});
