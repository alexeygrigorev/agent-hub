import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {MeteredDb} from '../src/db';import {ProjectWork,PROJECT_WORK_SCHEMA} from '../src/project-work';import {parseCron,cronNext} from '../src/cron';
const actor={id:'human:a',kind:'operator',scopes:['operator:admin']};
function fixture(){const sql=new DatabaseSync(':memory:');const state:any={storage:{transactionSync(fn:any){sql.exec('BEGIN');try{const x=fn();sql.exec('COMMIT');return x;}catch(e){sql.exec('ROLLBACK');throw e;}},sql:{exec(q:string,...a:any[]){const p=sql.prepare(q),rows=p.columns().length?p.all(...a):(p.run(...a),[]);return{toArray:()=>rows};}}}};const db=new MeteredDb(state.storage.sql);for(const s of PROJECT_WORK_SCHEMA)db.run(s);const effects=new Map<string,any>();let fail=false,active=false;const work=new ProjectWork(state,db,{authorize:(_a,id)=>id==='intake1',active:async()=>active,occurrence:async(o)=>{const result={intake_id:'child:'+o.id};effects.set(o.id,{...o,result});if(fail){fail=false;throw Error('lost response');}return result;}});return{db,work,sql,effects,setFail:()=>fail=true,setActive:(v:boolean)=>active=v,call:(p:string,m='GET',b?:any,a:any=actor)=>work.fetch(new Request('https://p.test'+p,{method:m,headers:{'x-hub2-actor':JSON.stringify(a),'Content-Type':'application/json'},...(b===undefined?{}:{body:JSON.stringify(b)})}))};}
const data=async(r:any)=>await r.json();
test('claims conflict on ancestors/descendants while readers observe; renew/release authorize exact owner',async()=>{
 const f=fixture(),c={key:'a',intake_id:'intake1',paths:['src'],mode:'edit'};
 const first=await data(await f.call('/api/claims','POST',c));assert(first.claim.id);
 assert.equal((await f.call('/api/claims','POST',{...c,key:'b',paths:['src/file']},{...actor,id:'human:other'}))!.status,409);
 assert.equal((await f.call('/api/claims','POST',{...c,key:'r',mode:'review'}))!.status,201);
 assert.equal((await f.call('/api/claims','POST',c))!.status,200);
 assert.equal((await f.call('/api/claims','POST',{...c,paths:['other']}))!.status,409);
 assert.equal((await f.call('/api/claims/'+first.claim.id+'/renew','POST',{ttl_ms:30000},{...actor,id:'human:other',scopes:['claim:write']}))!.status,403);
 assert.equal((await f.call('/api/claims/'+first.claim.id+'/release','POST',{}))!.status,200);
 assert.equal((await f.call('/api/claims','POST',{...c,key:'b',paths:['src/file']}))!.status,201);
 assert.equal((await f.call('/api/claims','POST',{...c,key:'escape',paths:['../private']}))!.status,400);
});
test('expired lock projection is removed and cannot be renewed; scoped intake is enforced',async()=>{
 const f=fixture(),r=await data(await f.call('/api/claims','POST',{key:'a',intake_id:'intake1',paths:['src'],mode:'coord'}));
 f.db.run('UPDATE work_claims SET expires_at=?',Date.now()-1);
 assert.equal((await f.call('/api/claims/'+r.claim.id+'/renew','POST',{}))!.status,409);
 assert.equal((await f.call('/api/claims','POST',{key:'b',intake_id:'intake1',paths:['src/file'],mode:'edit'}))!.status,201);
 assert.equal((await f.call('/api/claims','POST',{key:'c',intake_id:'other',paths:['a'],mode:'edit'}))!.status,403);
});
test('durable scheduled occurrence retries identical effect id, distinct provenance and empty idle deadlines',async()=>{
 const f=fixture();assert.equal(f.work.nextDeadline(),null);
 const created=await data(await f.call('/api/schedules','POST',{key:'daily',intake_id:'intake1',kind:'writer',interval_ms:60000,payload:{title:'scheduled work'},overlap:'allow'}));
 const s=created.schedule;f.setFail();await f.work.alarm(s.next_due);
 assert.equal(f.effects.size,1);const effect=[...f.effects.values()][0];assert.equal(effect.parent_intake_id,'intake1');assert.equal(effect.schedule_id,s.id);assert.equal(effect.kind,'writer');
 await f.work.alarm(s.next_due+2000);assert.equal(f.effects.size,1);
 const history=await data(await f.call('/api/schedules/'+s.id+'/occurrences'));assert.equal(history.items[0].state,'completed');
 await f.work.alarm(s.next_due+60000);assert.equal(f.effects.size,2);
 await f.call('/api/schedules/'+s.id+'/pause','POST',{});assert.equal(f.work.nextDeadline(),null);
 await f.call('/api/schedules/'+s.id+'/resume','POST',{});assert.notEqual(f.work.nextDeadline(),null);
 await f.call('/api/schedules/'+s.id+'/cancel','POST',{});assert.equal(f.work.nextDeadline(),null);
 assert.equal((await f.call('/api/schedules/'+s.id+'/resume','POST',{}))!.status,409);
});
test('schedule overlap skips or queues one coalesced due occurrence and schedule config replay is immutable',async()=>{
 const f=fixture(),b={key:'s',intake_id:'intake1',kind:'root',interval_ms:60000,payload:{title:'root'},overlap:'queue'};
 const s=(await data(await f.call('/api/schedules','POST',b))).schedule;assert.equal((await f.call('/api/schedules','POST',{...b,kind:'wake'}))!.status,409);
 f.setActive(true);await f.work.alarm(s.next_due);assert.equal(f.effects.size,0);f.setActive(false);await f.work.alarm(s.next_due+60000);assert.equal(f.effects.size,1);
});
test('maintained cron parser preserves timezone DST gaps and rejects malformed input',()=>{
 assert.equal(parseCron('bad'),null);assert.equal(parseCron('0 25 * * *'),null);
 const next=cronNext(parseCron('30 2 * * *')!,'Europe/Berlin',Date.parse('2026-03-28T03:00:00Z'));assert.equal(new Date(next!).toISOString(),'2026-03-30T00:30:00.000Z');
});

test('expired locks beyond bounded cleanup do not block ancestor or descendant claims or consume capacity',async()=>{
 const f=fixture();
 for(let i=0;i<20;i++)await f.call('/api/claims','POST',{key:'stale'+i,intake_id:'intake1',paths:['src/'+i],mode:'edit'});
 f.db.run('UPDATE work_claims SET expires_at=?',Date.now()-1000);
 const request={key:'new',intake_id:'intake1',paths:['src'],mode:'edit'};
 assert.equal((await f.call('/api/claims','POST',request,{...actor,id:'human:new'}))!.status,201);
 assert.equal((await f.call('/api/claims','POST',{...request,key:'other',paths:['src/child']},{...actor,id:'human:other'}))!.status,409);
 const g=fixture();for(let i=0;i<20;i++)await g.call('/api/claims','POST',{key:'ancestor'+i,intake_id:'intake1',paths:['parent'],mode:'edit'});
 g.db.run('UPDATE work_claims SET expires_at=?',Date.now()-1000);
 assert.equal((await g.call('/api/claims','POST',{...request,key:'descendant',paths:['parent/child']},{...actor,id:'human:new'}))!.status,201);
});
test('queued overlap preserves original due provenance through repeated active polls',async()=>{
 const f=fixture(),s=(await data(await f.call('/api/schedules','POST',{key:'queued',intake_id:'intake1',kind:'root',interval_ms:60000,payload:{},overlap:'queue'}))).schedule;
 f.setActive(true);await f.work.alarm(s.next_due);await f.work.alarm(s.next_due+60000);assert.equal(f.effects.size,0);
 f.setActive(false);await f.work.alarm(s.next_due+120000);
 assert.equal([...f.effects.values()][0].due_at,s.next_due);assert.equal([...f.effects.keys()][0],s.id+':'+s.next_due);
});
test('cron retains numeric grammar, DOM/DOW OR and first-only repeated wall minutes',()=>{
 for(const expression of ['@daily','0 0 0 * * *','0 0 * JAN *','0 0 ? * *','H * * * *','0 0 L * *','0 0 * * 1#2','*/61 * * * *','0 0 *-3 * *'])assert.equal(parseCron(expression),null,expression);
 const next=(expression:string,date:string)=>new Date(cronNext(parseCron(expression)!,'Europe/Berlin',Date.parse(date))!).toISOString();
 assert.equal(next('30 2 * * *','2026-10-25T00:40:00Z'),'2026-10-26T01:30:00.000Z');
 assert.equal(next('30 2 * * *','2026-10-25T01:20:00Z'),'2026-10-26T01:30:00.000Z');
 assert.equal(next('* * * * *','2026-10-25T01:20:00Z'),'2026-10-25T02:00:00.000Z');
 assert.equal(next('0 12 1 * 1','2026-11-02T00:00:00Z'),'2026-11-02T11:00:00.000Z');
 assert.equal(next('0 12 */2 * 1','2026-11-02T00:00:00Z'),'2026-11-02T11:00:00.000Z');
 assert.equal(cronNext(parseCron('0 0 31 2 *')!,'UTC',Date.parse('2026-01-01')),null);
});
