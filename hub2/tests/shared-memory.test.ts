import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {ControlDO} from '../src/control-do';
import {ProjectDO} from '../src/project-do';
import worker from '../src/worker';
const admin={id:'human:test',kind:'operator',scopes:['operator:admin']};
function state(){
  const database=new DatabaseSync(':memory:');
  return {database,ctx:{id:{toString:()=> 'test'},storage:{sql:{exec(q:string,...args:any[]){const p=database.prepare(q);const rows=p.columns().length?p.all(...args):(p.run(...args),[]);return{toArray:()=>rows};}},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const r=fn();database.exec('COMMIT');return r;}catch(e){database.exec('ROLLBACK');throw e;}},async getAlarm(){return null;},async setAlarm(){},async deleteAlarm(){}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())} as any};
}
function fixture(){
 const c=state(),projects=new Map<string,{database:DatabaseSync;instance:ProjectDO}>();let control:ControlDO;
 const env:any={HUB2_DEV_OPERATOR_TOKEN:'synthetic-test-token-at-least-32-characters',HUB2_ALLOW_DEV_OPERATOR:'1',CONTROL:{idFromName:(v:string)=>v,get:()=>({fetch:(r:Request|string,init?:RequestInit)=>control.fetch(r instanceof Request?r:new Request(r,init))})},PROJECTS:{idFromName:(v:string)=>v,get:(v:string)=>({fetch:(r:Request)=>{
  if(!projects.has(v)){const s=state();projects.set(v,{database:s.database,instance:new ProjectDO(s.ctx,env)});}
  return projects.get(v)!.instance.fetch(r);
 }})}};
 control=new ControlDO(c.ctx,env);
 const call=(path:string,method='GET',body?:unknown,actor:any=admin)=>control.fetch(new Request('https://control.test'+path,{method,headers:{'x-hub2-actor':JSON.stringify(actor),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 const publicCall=(path:string,method='GET',body?:unknown,headers:any={})=>worker.fetch(new Request('http://localhost'+path,{method,headers:{Authorization:'Bearer synthetic-test-token-at-least-32-characters','Content-Type':'application/json',...headers},...(body===undefined?{}:{body:JSON.stringify(body)})}),env);
 return {call,publicCall,projects,c,env};
}
const data=async(r:Response)=>await r.json() as any;
async function seed(f:ReturnType<typeof fixture>,project:string,n:number){
 await f.call('/_routes/register','POST',{kind:'intake',id:'route-'+project,owner:project,resource_id:project,participants:[]});
 await f.env.PROJECTS.get(project).fetch(new Request('https://project.test/api/intakes?project='+project,{headers:{'x-hub2-actor':JSON.stringify(admin)}}));
 const db=f.projects.get(project)!.database;
 for(let i=0;i<n;i++)db.prepare("INSERT INTO intakes(id,project,title,text,acceptance_criteria_json,status,created_at,updated_at) VALUES(?,?,?,?,?,'open',1,?)").run(project+'-'+String(i).padStart(5,'0'),project,'title '+i,i===0?'deep sparse needle':'body','[]',i+1);
}
test('public rules endpoint retains version history, source and idempotency; rejects foreign origin and nonadmin',async()=>{
 const f=fixture(),rule={key:'rule-write-1',id:'rule1',expected_version:0,project:null,text:'Standing rule',source:{type:'user',reference:'t1u',timestamp:'2026-11-10T00:00:00Z'},decision:'Apply'};
 assert.equal((await f.publicCall('/api/intake/rules','POST',rule)).status,200);
 const replay=await data(await f.publicCall('/api/intake/rules','POST',rule));assert.equal(replay.operator_rule.version,1);
 assert.equal((await f.call('/api/intake/rules','POST',{...rule,key:'rule-write-2'})).status,409);
 assert.equal((await f.call('/api/intake/rules','POST',{...rule,key:'rule-write-2',expected_version:1,text:'New version'})).status,200);
 const view=await data(await f.publicCall('/api/intake/rules'));assert.equal(view.operator_rules[0].history.length,2);assert.deepEqual(view.operator_rules[0].history[0].source,rule.source);
 assert.equal((await f.publicCall('/api/intake/rules','POST',rule,{Origin:'https://foreign.test'})).status,403);
 assert.equal((await f.call('/api/intake/rules','GET',undefined,{id:'agent:worker',kind:'agent',scopes:['intake:read']})).status,403);
});
test('CLI-shaped ask record/list preserves exact text, deduplicates and records conflicts without overwrite',async()=>{
 const f=fixture(),text='  First line\nSecond line\r\nλ  ',body={source:'cli',agents:[{agent_id:'writer',agent_name:'Writer',messages:[{msg_id:'t123u',role:'user',kind:'message',text}]}]};
 assert.equal((await f.publicCall('/api/asks/ingest','POST',body)).status,200);
 assert.equal((await data(await f.publicCall('/api/asks/ingest','POST',body))).duplicates,1);
 const changed=structuredClone(body);changed.agents[0].messages[0].text='changed';
 assert.equal((await data(await f.publicCall('/api/asks/ingest','POST',changed))).conflicts.length,1);
 const view=await data(await f.publicCall('/api/asks'));assert.equal(view.asks.length,1);assert.equal(view.asks[0].text,text);assert.equal(view.asks[0].t_id,'t123u');assert.equal(view.counts.messages,1);
 assert.equal((await f.call('/api/asks','GET',undefined,{id:'machine:test',kind:'machine',scopes:[]})).status,403);
});
test('cross-project pages of 50 return 500 unique ids; search finds old matches across all projects and context',async()=>{
 const f=fixture();await seed(f,'org/a',270);await seed(f,'org/b',230);
 const ids:string[]=[];let cursor:string|null=null,pages=0;
 do{
  const response=await f.publicCall('/api/intake/tasks?limit=50'+(cursor?'&cursor='+encodeURIComponent(cursor):''));assert.equal(response.status,200);
  const page=await data(response);ids.push(...page.items.map((r:any)=>r.id));cursor=page.next;assert(++pages<=11);
 }while(cursor);
 assert.equal(pages,10);assert.equal(ids.length,500);assert.equal(new Set(ids).size,500);
 const found=await data(await f.publicCall('/api/intake/tasks?q=sparse%20needle&limit=50'));assert.equal(found.items.length,2);assert.equal(found.next,null);
 const db=f.projects.get('org/b')!.database;
 db.prepare('INSERT INTO intake_events(task_id,seq,event_key,actor_id,kind,body_json,at) VALUES(?,1,?,?,?, ?,1)').run('org/b-00001','unique-event','human:test','note',JSON.stringify({note:'context-only phrase'}));
 assert.equal((await data(await f.publicCall('/api/intake/tasks?q=context-only'))).items[0].id,'org/b-00001');
 assert.equal((await f.publicCall('/api/intake/tasks?cursor=garbage')).status,400);
 assert.equal((await f.publicCall('/api/intake/tasks?limit=51')).status,400);
});
test('cross-project listing obeys existing project scopes and never exposes unrelated intakes',async()=>{
 const f=fixture();await seed(f,'org/a',2);await seed(f,'org/b',2);
 const reader={id:'agent:reader',kind:'agent',scopes:['intake:read','project:org/b']};
 const page=await data(await f.call('/api/intakes','GET',undefined,reader));assert.equal(page.items.length,2);assert(page.items.every((r:any)=>r.project==='org/b'));
 assert.deepEqual((await data(await f.call('/api/intakes','GET',undefined,{...reader,scopes:['intake:read']}))).items,[]);
});
test('legacy import preserves all raw values, original identities/history, is replay-safe and rolls back conflicting batches',async()=>{
 const f=fixture(),rule={id:'rule-old',project:null,version:2,history:[{version:1,text:'old'},{version:2,text:'new'}]},message={id:'message-old',agent_id:'legacy',t_id:'t99u',text:' original\n',role:'user',received_at:123,ts:null,attachments:null},ask={id:'ask-old',message_id:'message-old',state:'verified',intake_id:'intake-old',text:'original part',part:1,created_at:123,updated_at:456,evidence:'historic proof'};
 const rows=[{table:'intake_rules',row:{id:rule.id,data:JSON.stringify(rule)}},{table:'ask_messages',row:message},{table:'asks',row:ask},{table:'ask_meta',row:{id:1,last_tick:null,gap_scope:'window'}}];
 for(let i=0;i<2;i++)assert.equal((await f.call('/api/shared-memory/import','POST',{rows})).status,200);
 assert.equal((f.c.database.prepare('SELECT COUNT(*) AS n FROM memory_legacy').get() as any).n,4);
 assert.deepEqual((await data(await f.call('/api/intake/rules'))).operator_rules,[rule]);
 const view=await data(await f.call('/api/asks'));assert.equal(view.asks[0].id,ask.id);assert.equal(view.asks[0].evidence,'historic proof');assert.equal(view.asks[0].state,'verified');
 assert.deepEqual(JSON.parse((f.c.database.prepare("SELECT data_json FROM memory_legacy WHERE source_table='ask_messages'").get() as any).data_json),message);
 const conflict=await f.call('/api/shared-memory/import','POST',{rows:[{table:'ask_meta',row:{id:9}},{table:'asks',row:{...ask,text:'overwrite'}}]});assert.equal(conflict.status,409);
 assert.equal((f.c.database.prepare('SELECT COUNT(*) AS n FROM memory_legacy').get() as any).n,4);
 assert.equal((await f.call('/api/shared-memory/import','POST',{rows}, {id:'agent:x',kind:'agent',scopes:[]})).status,403);
 assert.equal((await f.call('/api/asks/'+ask.id,'DELETE')).status,404);
});

test('offline export extractor produces bounded private batches and matching server digest receipts',async()=>{
 const {mkdirSync,mkdtempSync,writeFileSync,readFileSync,statSync,rmSync}=await import('node:fs');
 const {gzipSync}=await import('node:zlib');const {spawnSync}=await import('node:child_process');
 const {fileURLToPath}=await import('node:url');
 const root=fileURLToPath(new URL('../../',import.meta.url));
 mkdirSync(root+'.local',{recursive:true});
 const dir=mkdtempSync(root+'.local/shared-export-test-');
 try{
  const tables:any={ask_messages:[],asks:[],intake_rules:[],ask_meta:[{id:1,nullable:null}],unrelated:[{text:'unrelated'}]};
  for(let i=0;i<120;i++){
   tables.ask_messages.push({id:'legacy-message-'+i,agent_id:'legacy',t_id:'t'+i+'u',text:'Unicode λ\r\n preserved '+i,role:'user',ts:null,received_at:1});
   tables.asks.push({id:'legacy-ask-'+i,message_id:'legacy-message-'+i,state:'unmapped',intake_id:null,text:'Ask '+i,part:0});
  }
  const row_counts=Object.fromEntries(Object.entries(tables).map(([k,v])=>[k,(v as any[]).length]));
  writeFileSync(dir+'/export.json.gz',gzipSync(JSON.stringify({schema:{},row_counts,tables})));
  const run=spawnSync('python3',[root+'hub2/scripts/extract-shared-memory.py',dir+'/export.json.gz','--output',dir+'/out'],{encoding:'utf8'});
  assert.equal(run.status,0,run.stderr);const manifest=JSON.parse(run.stdout);assert.equal(manifest.rows,241);
  assert.equal(statSync(dir+'/out').mode&0o777,0o700);
  const f=fixture();
  for(const batch of manifest.batches){
   const raw=readFileSync(dir+'/out/'+batch.file);assert(raw.length<=90000);assert.equal(statSync(dir+'/out/'+batch.file).mode&0o777,0o600);
   const payload=JSON.parse(raw.toString());assert(payload.rows.length<=100);
   const response=await f.call('/api/shared-memory/import','POST',payload);assert.equal(response.status,200);
   const receipt=await data(response);assert.equal(receipt.rows_digest,batch.rows_digest);
   assert.equal((await f.call('/api/shared-memory/import','POST',payload)).status,200);
  }
  assert.equal((await data(await f.call('/api/asks?limit=500'))).asks.length,120);
  // A corrupt declared count must never produce a successful manifest.
  row_counts.asks=999;writeFileSync(dir+'/bad.json.gz',gzipSync(JSON.stringify({schema:{},row_counts,tables})));
  const bad=spawnSync('python3',[root+'hub2/scripts/extract-shared-memory.py',dir+'/bad.json.gz','--output',dir+'/badout'],{encoding:'utf8'});assert.notEqual(bad.status,0);
 }finally{rmSync(dir,{recursive:true,force:true});}
});

test('schema v4 upgrades in place without changing existing control records',async()=>{
 const f=fixture();await f.call('/api/intake/rules');
 f.c.database.exec("INSERT INTO named_agents(id,display_name,enabled) VALUES('existing','Existing',1)");
 for(const table of ['memory_rules','memory_messages','memory_asks','memory_legacy','picker_receipts','picker_alert_counters','picker_alert_deliveries','picker_draws','picker_coverage'])f.c.database.exec('DROP TABLE '+table);
 f.c.database.exec('DROP INDEX memory_intake_owners; UPDATE hub2_schema SET version=4');
 const upgraded=new ControlDO(f.c.ctx,f.env);
 const response=await upgraded.fetch(new Request('https://control.test/api/intake/rules',{headers:{'x-hub2-actor':JSON.stringify(admin)}}));
 assert.equal(response.status,200);assert.deepEqual((await data(response)).operator_rules,[]);
 assert.equal((f.c.database.prepare('SELECT version FROM hub2_schema').get() as any).version,6);
 assert.equal((f.c.database.prepare("SELECT display_name FROM named_agents WHERE id='existing'").get() as any).display_name,'Existing');
});

test('import rejects alternate message identity collisions and rolls back the entire batch',async()=>{
 const f=fixture(),message={id:'first',agent_id:'legacy',t_id:'t100u',text:'keep',role:'user'};
 assert.equal((await f.call('/api/shared-memory/import','POST',{rows:[{table:'ask_messages',row:message}]})).status,200);
 const result=await f.call('/api/shared-memory/import','POST',{rows:[{table:'ask_meta',row:{id:'must-rollback'}},{table:'ask_messages',row:{...message,id:'second'}},{table:'asks',row:{id:'hidden-ask',message_id:'second',state:'unmapped'}}]});
 assert.equal(result.status,409);assert.equal((await data(result)).error,'import_conflict');
 assert.equal((f.c.database.prepare('SELECT COUNT(*) AS n FROM memory_legacy').get() as any).n,1);
 assert.equal((f.c.database.prepare('SELECT COUNT(*) AS n FROM memory_messages').get() as any).n,1);
 assert.equal((f.c.database.prepare('SELECT COUNT(*) AS n FROM memory_asks').get() as any).n,0);
});

test('ingest requires an operator or a verified machine and preserves machine provenance',async()=>{
 const f=fixture(),payload={source:'cli',agents:[{agent_id:'watcher',messages:[{msg_id:'t1u',text:'captured request'}]}]};
 for(const actor of [{id:'agent:other',kind:'agent',scopes:[]},{id:'machine:fake',kind:'machine',scopes:[]}])assert.equal((await f.call('/api/asks/ingest','POST',payload,actor)).status,403);
 const machine={id:'machine:host1',kind:'machine',resource_id:'host1',scopes:[]};
 assert.equal((await f.call('/api/asks/ingest','POST',{...payload,machine_id:'host2'},machine)).status,403);
 assert.equal((await f.call('/api/asks/ingest','POST',{...payload,source:'backfill'},machine)).status,403);
 assert.equal((await f.call('/api/asks/ingest','POST',payload,machine)).status,200);
 const message=(await data(await f.call('/api/asks/messages'))).messages[0];
 assert.equal(message.machine_id,'host1');assert.equal(message.actor,'machine:host1');
});
