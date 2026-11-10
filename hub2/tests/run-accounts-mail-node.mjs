// Actual SQLite slice served by the Node runtime; no provider calls or native process launches.
import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createFixture, restartFixture, makeTempDir } from './node-fixture.mjs';
const root = resolve(import.meta.dirname, '../..');
const persistDir = await makeTempDir('mail-node-');
const specBase = {
  root,
  env: { HUB2_CREDENTIAL_KEY: '11'.repeat(32) },
  dataDir: persistDir,
};
const { default: fixtureWorker, ControlDO, MachineFixture, AccountDO, MailboxFixture } = await import('./accounts-mail-worker.ts');
const spec = { ...specBase, entry: fixtureWorker, classes: { CONTROL: ControlDO, MACHINES: MachineFixture, ACCOUNTS: AccountDO, MAILBOXES: MailboxFixture } };
let fixture = await createFixture(spec), base;
const admin={id:'operator:test',kind:'operator',scopes:['operator:admin']};
const a={id:'agent:a',kind:'agent',scopes:[]},b={id:'agent:b',kind:'agent',scopes:[]};
async function call(group,id,path,method='GET',body,actor=admin){
 const response=await fetch(base+'/'+group+'/'+encodeURIComponent(id)+path,{method,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},...(body===undefined?{}:{body:JSON.stringify(body)})});
 return{response,result:await response.json()};
}
const count=async(group,id)=>(await call(group,id,'/_metrics')).result.counts;
const delta=(a,b)=>({reads:b.rows_read-a.rows_read,writes:b.rows_written-a.rows_written,total:b.rows_read-a.rows_read+b.rows_written-a.rows_written,alarm_operations:b.alarm_operations-a.alarm_operations,cache_hits:(b.read_cache?.hits||0)-(a.read_cache?.hits||0)});
async function completeNative(id){
 const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']},operation='native:'+id+':1';
 const claim=await call('mailboxes','agent:b','/api/messages/'+id+'/delivered','POST',{phase:'claim',epoch:1,operation_id:operation},host);
 if(claim.response.status!==201){const debug=await call('mailboxes','agent:b','/_fixture/outbox?id='+encodeURIComponent(id));assert.equal(claim.response.status,201,JSON.stringify({claim:claim.result,outbox:debug.result}));}
 const proof={phase:'receipt',epoch:1,operation_id:operation,claim_id:claim.result.claim_id,native_receipt:{provider:'aplexer',message_id:'native:'+id,session_id:'session-test',recipient:'agent:b'}};
 assert.equal((await call('mailboxes','agent:b','/api/messages/'+id+'/delivered','POST',proof,host)).response.status,200);
 for(const stage of ['read','ack','accept','complete'])assert.equal((await call('mailboxes','agent:b','/api/messages/'+id+'/'+stage,'POST',{epoch:1},b)).response.status,200);
 const status=await call('mailboxes','agent:a','/api/messages/'+id+'/status','GET',undefined,a);assert(status.result.message.completed_at);assert(status.result.message.delivered_at);
 assert.equal(status.result.events.filter(e=>e.stage==='delivered').length,1);
}
try{
 base=fixture.origin;
 assert.equal((await call('machines','h1','/_fixture/seed-running','POST',{})).response.status,200);
 assert.equal((await call('control','global','/_routes/register','POST',{kind:'session',id:'session-test',owner:'h1',resource_id:'synthetic-project',participants:[]})).response.status,200);
 for(const name of ['a','b'])assert.equal((await call('control','global','/api/named-agents','POST',{name,display_name:name,forward_session:'session-test'})).response.status,200);
 // This gate measures retained-history query growth and restart semantics, not
 // the production daily ceiling. Use the real audited override only inside this
 // disposable Workerd namespace so 100 synthetic sends do not stop midway;
 await call('accounts','a1','/api/account','PATCH',{provider:'synthetic',enabled:true});
 await call('accounts','a1','/api/account/quota','PUT',{remaining:80,captured_at:Date.now(),valid_until:Date.now()+60000,source:'synthetic-local-evidence'});
 const beforeAccount=await count('accounts','a1');
 for(const path of ['/api/account/reserve','/api/account/confirm','/api/account/start','/_internal/release'])
  assert.equal((await call('accounts','a1',path,'POST',{})).response.status,404);
 const accountFlow=delta(beforeAccount,await count('accounts','a1'));
 const first=await call('mailboxes','agent:a','/api/messages','POST',{recipient:'agent:b',body:'synthetic',key:'first'},a);assert.equal(first.response.status,201);await completeNative(first.result.message.id);
 assert.equal((await call('mailboxes','agent:c','/api/messages/'+first.result.message.id,'GET',undefined,{id:'agent:c',kind:'agent',scopes:[]})).response.status,404);
 assert.equal((await call('mailboxes','agent:c','/api/messages/'+first.result.message.id.slice(0,8),'GET',undefined,{id:'agent:c',kind:'agent',scopes:[]})).response.status,404);
 const smallBefore=await count('mailboxes','global');await call('mailboxes','agent:b','/api/messages?limit=1','GET',undefined,b);const smallPage=delta(smallBefore,await count('mailboxes','global'));
 for(let i=0;i<100;i++)assert.equal((await call('mailboxes','agent:a','/api/messages','POST',{recipient:'agent:b',body:'retained history '+i,key:'history-'+i},a)).response.status,201);
 const largeBefore=await count('mailboxes','global');await call('mailboxes','agent:b','/api/messages?limit=1','GET',undefined,b);const largePage=delta(largeBefore,await count('mailboxes','global'));assert(largePage.total<=smallPage.total+4);
 const before={mailbox:await count('mailboxes','global'),control:await count('control','global'),machine:await count('machines','h1')};
 const warm=await call('mailboxes','agent:a','/api/messages','POST',{recipient:'agent:b',body:'full warm lifecycle',key:'warm'},a);await completeNative(warm.result.message.id);
 const flow={mailbox:delta(before.mailbox,await count('mailboxes','global')),control:delta(before.control,await count('control','global')),machine:delta(before.machine,await count('machines','h1'))};
 const total=Object.values(flow).reduce((n,v)=>n+v.total,0),alarms=Object.values(flow).reduce((n,v)=>n+v.alarm_operations,0);
 for(const item of Object.values(before))assert.equal(item.measurement_valid,true);assert(flow.mailbox.total+flow.control.total+flow.machine.total<117,JSON.stringify(flow));
 fixture=await restartFixture(fixture,spec);base=fixture.origin;
 const reactivation={mailbox:await count('mailboxes','global'),account:await count('accounts','a1'),control:await count('control','global')};
 const after=await call('mailboxes','agent:a','/api/messages/'+warm.result.message.id+'/status','GET',undefined,a);assert(after.result.message.completed_at);assert(after.result.message.native_receipt);
 const profile=(await call('mailboxes','global','/_fixture/profile')).result;
 console.log(JSON.stringify({passed:true,engine:'node sqlite',physical_mailbox_authorities:1,mailbox_id:'global',accountFlow,warmFullMessage:{...flow,total,alarm_operations:alarms},smallPage,largePage,reactivation,profile,note:'Actual SQL and alarm calls, one global Mailbox authority counted once. Account session reservation routes are absent. Synthetic running-session precondition and native receipts; not provider/live-process or whole-day proof.'}));
}finally{await fixture.dispose();}

