import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { AccountDO as ActualAccountDO } from '../src/account-do';
function semanticEnv(env:any={}){return env;}
class AccountDO extends ActualAccountDO { constructor(ctx:any,env:any={}){super(ctx,semanticEnv(env));} }
import { ControlDO } from '../src/control-do';
import { MailboxDO as ActualMailboxDO } from '../src/mailbox-do';
class MailboxDO extends ActualMailboxDO { constructor(ctx:any,env:any){super(ctx,semanticEnv(env));} }

// Native SQLite semantics only, not a Cloudflare row-cost measurement. The
// local cursor supplies result-row/changed-row counts so guarded code paths can
// run; native workerd owns all Cloudflare billing/cost assertions.
function state() {
 const database=new DatabaseSync(':memory:');
 const ctx:any={storage:{sql:{exec(query:string,...args:unknown[]){
  const statement=database.prepare(query);let rows:unknown[]=[],rowsRead=0,rowsWritten=0;
  if(statement.columns().length){rows=statement.all(...args as any);rowsRead=rows.length;}
  else{const result=statement.run(...args as any);rowsWritten=Number(result.changes);}
  return {toArray:()=>rows,rowsRead,rowsWritten};
 }},transactionSync<T>(fn:()=>T):T{database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(e){database.exec('ROLLBACK');throw e;}},
 async setAlarm(at:number){ctx.alarm=at;},async deleteAlarm(){ctx.alarm=null;},},blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},alarm:null};
 return {ctx,database};
}
const admin={id:'human:test',kind:'operator',scopes:['operator:admin']};
function request(path:string,method='GET',body?:unknown,actor:unknown=admin,mailbox?:string){
 const headers:Record<string,string>={'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)};
 if(mailbox)headers['x-hub2-mailbox']=mailbox;
 if((actor as any)?.kind==='machine')headers['x-hub2-delivery-session']='session-test';
 if(path==='/_internal/release'&&body&&typeof body==='object')body={session_id:'s1',machine_id:'h1',...body as object};
 if(path.startsWith('/api/messages/')&&method==='POST'&&body&&typeof body==='object'){body={epoch:1,...body as object};if(path.endsWith('/readdress'))body={key:'test-readdress-'+(body as any).recipient,...body as object};}
 return new Request('https://hub2.test'+path,{method,headers,...(body===undefined?{}:{body:JSON.stringify(body)})});
}
// Raw SQL below only forces synthetic clocks/corruption; production mutations use APIs.
const clearFixtureCache=(core:any)=>core.db.cache.clear();
async function data(r:Response){return await r.json() as any;}
async function configuredAccount(){
 const s=state(),account=new AccountDO(s.ctx);
 assert.equal((await account.fetch(request('/api/account','PATCH',{provider:'synthetic',enabled:true,capacity:1,min_remaining:10}))).status,200);
 return {...s,account};
}
async function quota(account:AccountDO){return account.fetch(request('/api/account/quota','PUT',{remaining:80,captured_at:Date.now(),valid_until:Date.now()+60000,source:'synthetic-test'}));}
const reserve={operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1,request_digest:'digest1',ttl_ms:1000};

test('account refuses unknown/stale quota, enforces capacity and immutable reservation idempotency',async()=>{
 const {account}=await configuredAccount();
 assert.equal((await account.fetch(request('/api/account/reserve','POST',reserve))).status,503);
 assert.equal((await quota(account)).status,200);
 const first=await data(await account.fetch(request('/api/account/reserve','POST',reserve)));
 assert.equal(first.reservation.state,'offered');
 assert.deepEqual(await data(await account.fetch(request('/api/account/reserve','POST',reserve))),first);
 assert.equal((await account.fetch(request('/api/account/reserve','POST',{...reserve,request_digest:'changed'}))).status,409);
 assert.equal((await account.fetch(request('/api/account/reserve','POST',{...reserve,epoch:2}))).status,409);
 assert.equal((await account.fetch(request('/api/account/reserve','POST',{...reserve,operation_id:'op2',session_id:'s2'}))).status,409);
 assert.equal((await account.fetch(request('/api/account/reserve','POST',reserve,{id:'agent:bad',kind:'agent',scopes:[]}))).status,403);
});
test('expired confirmed work retains account capacity until exact-epoch native exit proof',async()=>{
 const {account,database,ctx}=await configuredAccount();await quota(account);
 await account.fetch(request('/api/account/reserve','POST',reserve));
 assert.equal((await account.fetch(request('/api/account/confirm','POST',{operation_id:'op1',epoch:1}))).status,200);
 assert.equal((await account.fetch(request('/api/account/start','POST',{operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1}))).status,200);
 database.prepare('UPDATE reservations SET expires_at=? WHERE operation_id=?').run(Date.now()-1,'op1');clearFixtureCache(account);
 await account.alarm();assert.equal(ctx.alarm,null);
 assert.equal((database.prepare('SELECT state FROM reservations WHERE operation_id=?').get('op1') as any).state,'uncertain');
 assert.equal((await account.fetch(request('/_internal/release','POST',{operation_id:'op1',epoch:1}))).status,409);
 assert.equal((await account.fetch(request('/_internal/release','POST',{operation_id:'op1',epoch:2,native_exit_confirmed:true}))).status,409);
 assert.equal((await account.fetch(request('/api/account/reserve','POST',{...reserve,operation_id:'op2'}))).status,409);
 assert.equal((await account.fetch(request('/_internal/release','POST',{operation_id:'op1',epoch:1,native_exit_confirmed:true}))).status,200);
 assert.equal((await account.fetch(request('/_internal/release','POST',{operation_id:'op1',epoch:1,native_exit_confirmed:true}))).status,200);
 assert.equal((database.prepare('SELECT occupied FROM account WHERE id=1').get() as any).occupied,0);
});
test('unclaimed account hold expiry frees once; invalid quota evidence and unauthorized reset writes refuse',async()=>{
 const {account,database}=await configuredAccount();await quota(account);
 await account.fetch(request('/api/account/reserve','POST',reserve));
 database.prepare('UPDATE reservations SET expires_at=?').run(Date.now()-1);clearFixtureCache(account);await account.alarm();await account.alarm();
 assert.equal((database.prepare('SELECT occupied FROM account WHERE id=1').get() as any).occupied,0);
 assert.equal((await account.fetch(request('/api/account/confirm','POST',{operation_id:'op1',epoch:1}))).status,409);
 assert.equal((await account.fetch(request('/api/account/quota','PUT',{remaining:null,captured_at:Date.now(),valid_until:Date.now()+1000,source:'test'}))).status,400);
 assert.equal((await account.fetch(request('/api/account/resets','PUT',{id:'r1',expires_at:Date.now()+10000,available:true,source:'test'},{id:'bad',scopes:[]}))).status,403);
});
function mailboxes(){
 const flags={blockReceipts:false,blockDirectory:false,blockMachine:false,failWakeAfterEffect:false,afterRecipient:undefined as undefined|(()=>Promise<void>)};
 const controlEnv:any=semanticEnv({}),controlState=state();let control=new ControlDO(controlState.ctx,controlEnv);
 controlState.database.prepare('INSERT INTO object_routes(kind,id,owner,resource_id,epoch,digest,participants_json) VALUES(?,?,?,?,?,?,?)').run('session','session-test','h1','task-test',1,'synthetic-fixture','[]');
 controlState.database.prepare('INSERT INTO actor_directory(id,kind,enabled) VALUES(?,?,1)').run('session:session-test','session');
 for(const name of ['a','b','c','d','e'])controlState.database.prepare('INSERT INTO named_agents(id,display_name,forward_session,enabled) VALUES(?,?,?,1)').run('agent:'+name,name,'session-test');clearFixtureCache(control);
 const nativeCalls:any[]=[];const nativeFetch=async(r:Request)=>{const b=await r.json();nativeCalls.push({path:new URL(r.url).pathname,body:b});return flags.blockMachine?Response.json({},{status:503}):Response.json({queued:true,acked:new URL(r.url).pathname.endsWith('mail-ack'),cancelled:new URL(r.url).pathname.endsWith('mail-cancel'),state:'pending',message_id:b.message_id,epoch:b.epoch,operation_id:b.operation_id});};
 const stores=new Map<string,ReturnType<typeof state>>(),shared=state();let box:MailboxDO;
 const env:any={MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:nativeFetch})},CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(r:Request)=>{const path=new URL(r.url).pathname;if(flags.blockDirectory&&path==='/_routes/register')return new Response('{}',{status:503});const response=await control.fetch(r);if(path==='/_recipient'&&flags.afterRecipient){const hook=flags.afterRecipient;flags.afterRecipient=undefined;await hook();}return response;}})},MAILBOXES:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(r:Request)=>{const path=new URL(r.url).pathname;if(flags.blockReceipts&&path==='/_receipt')return new Response('{}',{status:503});const response=await get(id).fetch(r);if(flags.failWakeAfterEffect&&path==='/_resume-native'){flags.failWakeAfterEffect=false;return new Response('{}',{status:503});}return response;}})}};
 Object.assign(controlEnv,env);
 box=new MailboxDO(shared.ctx,env);for(const id of ['global','agent:a','agent:b','agent:c','agent:d','agent:e'])stores.set(id,shared);
 function get(id:string){stores.set(id,shared);return box;}
 const fetch=(owner:string,path:string,method='GET',body?:unknown,actor:any={id:owner,kind:'agent',scopes:[]})=>get(owner).fetch(request(path,method,body,actor,owner));
 const restart=(_id:string)=>box=new MailboxDO(shared.ctx,env);
 return {get,fetch,stores,flags,restart,get control(){return control;},restartControl:()=>control=new ControlDO(controlState.ctx,controlEnv),controlState,nativeCalls};
}
async function nativeDelivered(m:ReturnType<typeof mailboxes>,owner:string,id:string,epoch=1){
 const actor={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery',`mailbox:${owner}`]},operation='native:'+id+':'+epoch;
 const claim=await data(await m.fetch(owner,`/api/messages/${id}/delivered`,'POST',{phase:'claim',epoch,operation_id:operation},actor));
 return m.fetch(owner,`/api/messages/${id}/delivered`,'POST',{phase:'receipt',epoch,operation_id:operation,claim_id:claim.claim_id,native_receipt:{provider:'aplexer',message_id:'native:'+id+':'+epoch,session_id:'session-test',recipient:owner}},actor);
}
test('sender key dedup is global across recipients and unauthorized inbox access refuses',async()=>{
 const m=mailboxes(),body={recipient:'agent:b',body:'synthetic body',key:'k1'};
 const first=await data(await m.fetch('agent:a','/api/messages','POST',body));
 const again=await data(await m.fetch('agent:a','/api/messages','POST',body));assert.equal(first.message.id,again.message.id);
 assert.equal((await m.fetch('agent:a','/api/messages','POST',{...body,recipient:'agent:c'})).status,409);
 const inbox=await data(await m.fetch('agent:b','/api/messages'));assert.equal(inbox.items.length,1);
 assert.equal((await m.fetch('agent:b','/api/messages','GET',undefined,{id:'agent:e',kind:'agent',scopes:[]})).status,403);
 const metric=await data(await m.get('agent:a').fetch(request('/_metrics')));assert.equal(metric.counts.measurement_valid,true,'local result/changes instrumentation supports semantic execution only');
});
test('readdress fences the previous recipient and epoch while preserving the sender key replay',async()=>{
 const m=mailboxes(),body={recipient:'agent:b',body:'synthetic',key:'k2'},original=(await data(await m.fetch('agent:a','/api/messages','POST',body))).message;
 const changed=await data(await m.fetch('agent:a','/api/messages/'+original.id+'/readdress','POST',{recipient:'agent:c'},admin));assert.equal(changed.message.epoch,2);
 assert.equal((await data(await m.fetch('agent:b','/api/messages'))).items.length,0);assert.equal((await data(await m.fetch('agent:c','/api/messages'))).items.length,1);
 assert.equal((await m.fetch('agent:b','/api/messages/'+original.id+'/read','POST',{epoch:1})).status,404);
 assert.equal((await m.fetch('agent:c','/api/messages/'+original.id+'/read','POST',{epoch:1})).status,409);
 const replay=await data(await m.fetch('agent:a','/api/messages','POST',body));assert.equal(replay.message.id,original.id);assert.equal(replay.message.recipient,'agent:c');
});

test('native delivery fences readdress, read/ack receipts are idempotent, reply participants are enforced',async()=>{
 const m=mailboxes();const parent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'hello',key:'parent'}))).message;
 assert.equal((await nativeDelivered(m,'agent:b',parent.id)).status,200);
 const result=await data(await m.fetch('agent:a',`/api/messages/${parent.id}/readdress`,'POST',{recipient:'agent:c'},admin));assert.equal(result.message.recipient,'agent:b');
 assert.equal((await m.fetch('agent:b',`/api/messages/${parent.id}/read`,'POST',{})).status,200);
 assert.equal((await m.fetch('agent:b',`/api/messages/${parent.id}/ack`,'POST',{})).status,200);
 assert.equal((await m.fetch('agent:b',`/api/messages/${parent.id}/ack`,'POST',{})).status,200);
 const reply=await data(await m.fetch('agent:b','/api/messages','POST',{recipient:'agent:a',body:'reply',key:'reply',reply_to:parent.id,parent_sender:'agent:a'}));assert.equal(reply.message.thread_id,parent.thread_id);
 assert.equal((await m.fetch('agent:c','/api/messages','POST',{recipient:'agent:a',body:'intrusion',key:'bad',reply_to:parent.id,parent_sender:'agent:a'})).status,403);
});
test('global authority rejects obsolete envelope injection and old native delivery after fenced reroute',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'global',key:'fence'}))).message;
 await m.fetch('agent:a','/api/messages/'+sent.id+'/readdress','POST',{recipient:'agent:c'},admin);
 assert.equal((await m.get('global').fetch(request('/_receive','POST',{mail:{id:sent.id,epoch:1,recipient:'agent:b'}}))).status,404);
 const old={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']};
 assert.equal((await m.fetch('agent:b','/api/messages/'+sent.id+'/delivered','POST',{phase:'claim',epoch:1,operation_id:'native:'+sent.id+':1'},old)).status,404);
 assert(m.nativeCalls.some(c=>c.path.endsWith('/mail-cancel')&&c.body.message_id===sent.id&&c.body.epoch===1));
});

test('central credential generations are encrypted, CAS fenced and provisioned only to approved hosts',async()=>{
 const s=state();let refreshes=0;
 const provider=async(url:string,init:any)=>{
  if(url==='https://auth.openai.com/oauth/token'){refreshes++;return Response.json({access_token:'synthetic-rotated-access',refresh_token:'synthetic-rotated-refresh',expires_in:3600});}
  throw new Error('unexpected test provider URL');
 };
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'11'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 const files={'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic-access',refresh_token:'synthetic-refresh'},last_refresh:'2000-01-01T00:00:00Z'})};
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files,expected_generation:0}))).status,200);
 const stored=(s.database.prepare('SELECT sealed FROM credentials WHERE id=1').get() as any).sealed;assert.equal(stored.includes('synthetic-access'),false);
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files,expected_generation:0}))).status,409);
 const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['machine:offer']};
 assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,host))).status,403);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));
 const provision=await data(await account.fetch(request('/api/account/credentials','GET',undefined,host)));assert.equal(provision.generation,2);assert.equal(refreshes,1);assert.match(provision.files['.codex/auth.json'],/synthetic-rotated-access/);
 assert.equal((await account.fetch(request('/api/account/credentials','POST',{generation:1},host))).status,409);
 assert.equal((await account.fetch(request('/api/account/credentials','POST',{generation:2},host))).status,200);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:[]}));assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,host))).status,403);
 assert.equal((await account.fetch(request('/api/account','PATCH',{provider:'claude',enabled:true,capacity:1}))).status,409);
});
test('scheduled provider discovery records real quota, reset expiry alerts, and deduplicates alert stages',async()=>{
 const s=state();const called:string[]=[];const expiry=Date.now()+40*3600000;
 const provider=async(url:string)=>{called.push(url);
  if(url==='https://chatgpt.com/backend-api/wham/usage')return Response.json({rate_limit:{primary_window:{used_percent:12},secondary_window:{used_percent:60}}});
  if(url==='https://chatgpt.com/backend-api/wham/rate-limit-reset-credits')return Response.json({credits:[{id:'grant1',expires_at:expiry,status:'available',title:'Full reset'}]});
  throw new Error('unexpected URL');
 };
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'22'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic-access',refresh_token:'synthetic-refresh'}})},expected_generation:0}));
 await account.fetch(request('/api/account/discovery','PUT',{enabled:true,interval_ms:21600000}));
 s.database.prepare('UPDATE discovery SET next_at=? WHERE id=1').run(Date.now()-1);clearFixtureCache(account);await account.alarm();assert.equal(called.length,2);
 const observed=await data(await account.fetch(request('/api/account/quota')));assert.equal(observed.quota.remaining,40);assert.equal(observed.fresh,true);
 let alerts=await data(await account.fetch(request('/api/account/alerts')));assert.equal(alerts.items.length,1);assert.equal(alerts.items[0].kind,'reset_expiry_36h');assert(alerts.items[0].remaining_ms>36*3600000);
 // Keep the same grant expiry identity while advancing the test clock near expiry.
 const realNow=Date.now;Date.now=()=>expiry-23*3600000;
 try{await account.alarm();await account.alarm();}finally{Date.now=realNow;}
 alerts=await data(await account.fetch(request('/api/account/alerts')));assert.equal(alerts.items.filter((a:any)=>a.kind==='reset_expiry_24h').length,1);
});
test('unknown provider collector fails closed with an explicit discovery alert, never a fabricated quota',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'33'.repeat(32)});
 await account.fetch(request('/api/account','PATCH',{provider:'gemini',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.gemini/oauth_creds.json':'{"access_token":"synthetic"}'},expected_generation:0}));
 const result=await account.fetch(request('/api/account/discovery','POST',{}));assert.equal(result.status,503);
 const observed=await data(await account.fetch(request('/api/account/quota')));assert.equal(observed.quota,null);assert.equal(observed.fresh,false);
 assert.equal((await data(await account.fetch(request('/api/account/alerts')))).items[0].kind,'provider_collector_unavailable');
});
test('message completion and acceptance retain distinct idempotent receipt stages',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'work',key:'stages'}))).message;
 for(const stage of ['accept','complete','complete'])assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/${stage}`,'POST',{})).status,200);
 const status=await data(await m.fetch('agent:a',`/api/messages/${sent.id}/status`));
 assert.equal(status.events.filter((e:any)=>e.stage==='complete').length,1);assert.equal(status.events.filter((e:any)=>e.stage==='accept').length,1);assert(status.message.completed_at);assert(status.message.accepted_at);
});
test('one durable message stage is immediately shared by both views without a receipt-forwarding transport',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'work',key:'atomic-stage'}))).message;
 m.flags.blockReceipts=true;assert.equal((await m.fetch('agent:b','/api/messages/'+sent.id+'/complete','POST',{})).status,200);
 const sender=await data(await m.fetch('agent:a','/api/messages/'+sent.id+'/status')),recipient=await data(await m.fetch('agent:b','/api/messages/'+sent.id+'/status'));assert(sender.message.completed_at);assert.equal(sender.message.completed_at,recipient.message.completed_at);
 m.restart('global');assert.equal((await m.fetch('agent:b','/api/messages/'+sent.id+'/complete','POST',{})).status,200);
 const replay=await data(await m.fetch('agent:a','/api/messages/'+sent.id+'/status'));assert.equal(replay.events.filter((e:any)=>e.stage==='complete').length,1);assert.equal(m.stores.get('global')!.ctx.alarm,null);
});

test('refresh has one owner and a host revoked during refresh receives no credential bundle',async()=>{
 const s=state();let refreshes=0;let release!:()=>void;let started!:()=>void;
 const gate=new Promise<void>(resolve=>{release=resolve;}),seen=new Promise<void>(resolve=>{started=resolve;});
 const provider=async(url:string)=>{assert.equal(url,'https://auth.openai.com/oauth/token');refreshes++;started();await gate;return Response.json({access_token:'rotated-synthetic',refresh_token:'rotated-refresh'});};
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'44'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic-old',refresh_token:'synthetic-refresh'}})},expected_generation:0}));
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]};
 const first=account.fetch(request('/api/account/credentials','GET',undefined,host));await seen;
 assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,host))).status,409);
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'other',refresh_token:'other'}})},expected_generation:1}))).status,409);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:[]}));release();const response=await first;assert.equal(response.status,403);assert.equal(refreshes,1);assert.equal((await response.text()).includes('rotated-synthetic'),false);
});
test('failed provider quota invalidates a prior fresh observation and refuses admission',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'55'.repeat(32),HUB2_PROVIDER_FETCH:(async()=>new Response('{}',{status:503})) as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic',refresh_token:'synthetic'}})},expected_generation:0}));await quota(account);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));
 assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,503);
 assert.equal((await data(await account.fetch(request('/api/account/quota')))).fresh,false);
 assert.equal((await account.fetch(request('/api/account/reserve','POST',reserve))).status,503);
});
test('an older failed probe cannot invalidate a newer successful observation',async()=>{
 const s=state();let release!:()=>void,started!:()=>void;const gate=new Promise<void>(r=>{release=r;}),seen=new Promise<void>(r=>{started=r;});let calls=0;
 const provider=async(url:string)=>{if(url.endsWith('/usage')){calls++;if(calls===1){started();await gate;return new Response('{}',{status:503});}return Response.json({rate_limit:{primary_window:{used_percent:10}}});}return Response.json({credits:[]});};
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'66'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic',refresh_token:'synthetic'}})},expected_generation:0}));
 const older=account.fetch(request('/api/account/discovery','POST',{}));await seen;
 assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,200);release();assert.equal((await older).status,409);
 const current=await data(await account.fetch(request('/api/account/quota')));assert.equal(current.fresh,true);assert.equal(current.quota.remaining,90);
});
test('mailbox conjunctively applies both filters with bounded continuation instead of dropping state',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'work',key:'filter'}))).message;
 const empty=await data(await m.fetch('agent:b',`/api/messages?thread=${sent.thread_id}&state=completed`));assert.equal(empty.items.length,0);assert.equal(empty.next,null);
 await m.fetch('agent:b',`/api/messages/${sent.id}/complete`,'POST',{epoch:1});const found=await data(await m.fetch('agent:b',`/api/messages?thread=${sent.thread_id}&state=completed`));assert.equal(found.items[0].id,sent.id);
 assert.equal((await m.fetch('agent:a',`/api/messages/${sent.id}/readdress`,'POST',{recipient:'agent:c'},admin)).status,409);
});
test('reset-only transport and parsing failures preserve separately proved provider quota',async()=>{
 for(const failure of ['transport','json','oversize']){
  const s=state(),provider=async(url:string)=>{if(url.endsWith('/usage'))return Response.json({rate_limit:{primary_window:{used_percent:5}}});if(failure==='transport')throw new Error('synthetic transport failure');return failure==='json'?new Response('invalid json'):new Response('x'.repeat(131073));};
  const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'77'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
  await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
  await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic',refresh_token:'synthetic'}})},expected_generation:0}));
  assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,503);
  const current=await data(await account.fetch(request('/api/account/quota')));assert.equal(current.fresh,true,failure);assert.equal(current.quota.remaining,95);
 }
});
test('approved host refresh lease supports blocked-egress repair, CAS writeback, rejection, and candidate reconciliation',async()=>{
 const s=state();let providerCalls=0;const provider=async(url:string)=>{providerCalls++;assert.equal(url,'https://chatgpt.com/backend-api/wham/usage');return Response.json({rate_limit:{primary_window:{used_percent:10}}});};
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'88'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 const auth=(stamp:string,token:string)=>({'.codex/auth.json':JSON.stringify({tokens:{access_token:token,refresh_token:'synthetic-refresh'},last_refresh:stamp})});
 await account.fetch(request('/api/account/credentials','PUT',{files:auth('2000-01-01T00:00:00Z','synthetic-old'),expected_generation:0}));await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1','h2']}));
 const h1={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]},h2={id:'machine:h2',kind:'machine',resource_id:'h2',scopes:[]};
 const lease=await data(await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h1)));assert(lease.lease);
 assert.equal((await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h2))).status,409);
 const repair=await data(await account.fetch(request('/api/account/credentials?refresh_lease='+encodeURIComponent(lease.lease),'GET',undefined,h1)));assert.equal(repair.generation,1);assert.equal(providerCalls,0,'leased host reads repair bundle without blocked central refresh');
 const rotated=auth('2026-11-04T12:00:00Z','synthetic-host-rotated');
 const written=await data(await account.fetch(request('/api/account/rotation','POST',{expected_generation:1,lease:lease.lease,files:rotated},h1)));assert.equal(written.generation,2);
 const current=await data(await account.fetch(request('/api/account/rotation','POST',{expected_generation:2,files:rotated},h1)));assert.equal(current.unchanged,true);assert.equal(current.generation,2);
 assert.equal((await account.fetch(request('/api/account/credential-rejection','POST',{generation:2,reason:'provider_unauthorized'},h2))).status,200);
 assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,h2))).status,409);
 const newer=auth('2026-11-04T13:00:00Z','synthetic-newer-host');
 const conflict=await account.fetch(request('/api/account/rotation','POST',{expected_generation:1,files:newer},h1));assert.equal(conflict.status,409);const candidate=await data(conflict);assert(candidate.candidate_id);
 const reconciled=await data(await account.fetch(request('/api/account/reconcile','POST',{candidate_id:candidate.candidate_id,expected_generation:2})));assert.equal(reconciled.generation,3);assert.equal(providerCalls,1);
 assert.equal((s.database.prepare('SELECT state FROM credential_candidates WHERE id=?').get(candidate.candidate_id) as any).state,'published');
});
test('credential ciphertext authenticates provider and generation as well as object identity',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'99'.repeat(32)});
 await account.fetch(request('/api/account','PATCH',{provider:'gemini',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.gemini/oauth_creds.json':'{"access_token":"synthetic"}'},expected_generation:0}));await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));
 const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]};
 s.database.prepare('UPDATE credentials SET generation=2 WHERE id=1').run();clearFixtureCache(account);assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,host))).status,503);
 s.database.prepare('UPDATE credentials SET generation=1 WHERE id=1').run();clearFixtureCache(account);s.database.prepare('UPDATE account SET provider=? WHERE id=1').run('zai');clearFixtureCache(account);assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,host))).status,503);
});
test('provider limit_reached overrides apparently positive quota and preserves actual reset timestamp',async()=>{
 const s=state(),reset=Math.floor(Date.now()/1000)+3600;
 const provider=async(url:string)=>url.endsWith('/usage')?Response.json({rate_limit:{limit_reached:true,primary_window:{used_percent:5,reset_at:reset}}}):Response.json({credits:[]});
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'aa'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic',refresh_token:'synthetic'}})},expected_generation:0}));
 assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,200);const observed=await data(await account.fetch(request('/api/account/quota')));assert.equal(observed.quota.remaining,0);assert.equal(observed.quota.reset_at,reset*1000);
 assert.equal((await account.fetch(request('/api/account/reserve','POST',reserve))).status,409);
});
test('OpenCode Go central collector accepts maintained active-device array and real usage shape',async()=>{
 const s=state();let calls=0;const provider=async(url:string,init:any)=>{calls++;assert.equal(url,'https://opencode.ai/zen/go/v1/usage');assert.equal(init.headers['User-Agent'],'opencode/1.19');return Response.json({usage:{rolling:{percent:10,resetsAt:'2026-11-05T00:00:00Z'},weekly:{usagePercent:30},monthly:{percent:60}}});};
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'bb'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'go',enabled:true,capacity:1}));
 const files={'.local/share/opencode-hub/device-auth.json':JSON.stringify([{active:false,value:{access:'unused'}},{active:true,value:{access:'synthetic-active'}}])};
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files,expected_generation:0}))).status,200);
 assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,200);
 const observed=await data(await account.fetch(request('/api/account/quota')));assert.equal(observed.quota.remaining,40);assert.equal(observed.fresh,true);assert.equal(calls,1);
});
test('confirmed pre-offer reservation survives restart until exact unclaimed expiry proof; start replay is fenced',async()=>{
 const {account,database,ctx}=await configuredAccount();await quota(account);await account.fetch(request('/api/account/reserve','POST',reserve));
 assert.equal((await data(await account.fetch(request('/api/account/confirm','POST',{operation_id:'op1',epoch:1})))).reservation.state,'confirmed');
 assert.equal((await account.fetch(request('/api/account/confirm','POST',{operation_id:'op1',epoch:1}))).status,200);
 database.prepare('UPDATE reservations SET expires_at=?').run(Date.now()-1);clearFixtureCache(account);await account.alarm();assert.equal((database.prepare('SELECT state FROM reservations').get() as any).state,'awaiting-unclaimed-proof');
 const restarted=new AccountDO(ctx);const proof={kind:'offer-expired-unclaimed',operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1,observed_at:Date.now(),offer_expires_at:Date.now()-1000};
 assert.equal((await restarted.fetch(request('/_internal/release','POST',{operation_id:'op1',epoch:1,unclaimed_proof:{...proof,machine_id:'wrong'}}))).status,409);
 assert.equal((database.prepare('SELECT occupied FROM account').get() as any).occupied,1);
 assert.equal((await restarted.fetch(request('/_internal/release','POST',{operation_id:'op1',epoch:1,unclaimed_proof:proof}))).status,200);
 assert.equal((await restarted.fetch(request('/_internal/release','POST',{operation_id:'op1',epoch:1,unclaimed_proof:proof}))).status,200);
 assert.equal((database.prepare('SELECT occupied FROM account').get() as any).occupied,0);
 const next={...reserve,operation_id:'op2',session_id:'s2'};await restarted.fetch(request('/api/account/reserve','POST',next));
 assert.equal((await restarted.fetch(request('/api/account/start','POST',{operation_id:'op2',session_id:'s2',machine_id:'h1',epoch:1}))).status,409);
 await restarted.fetch(request('/api/account/confirm','POST',{operation_id:'op2',epoch:1}));const start={operation_id:'op2',session_id:'s2',machine_id:'h1',epoch:1};
 assert.equal((await restarted.fetch(request('/api/account/start','POST',start))).status,200);
 assert.equal((await restarted.fetch(request('/api/account/start','POST',start))).status,200);
 assert.equal((await restarted.fetch(request('/api/account/start','POST',{...start,session_id:'other'}))).status,409);
 assert.equal((database.prepare('SELECT occupied FROM account').get() as any).occupied,1);
});
test('caller epoch and readdress keys prevent stale receipts and delayed retries undoing a newer route',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'route',key:'route-fence'}))).message;
 const move={recipient:'agent:c',epoch:1,key:'move1'};
 const first=await data(await m.fetch('agent:a',`/api/messages/${sent.id}/readdress`,'POST',move,admin));assert.equal(first.message.epoch,2);
 assert.equal((await m.fetch('agent:a',`/api/messages/${sent.id}/readdress`,'POST',{...move,recipient:'agent:d'},admin)).status,409);
 const second=await data(await m.fetch('agent:a',`/api/messages/${sent.id}/readdress`,'POST',{recipient:'agent:b',epoch:2,key:'move2'},admin));assert.equal(second.message.epoch,3);
 const replay=await data(await m.fetch('agent:a',`/api/messages/${sent.id}/readdress`,'POST',move,admin));assert.equal(replay.message.epoch,2,'original operation returns its immutable result');
 const current=await data(await m.fetch('agent:a',`/api/messages/${sent.id}`));assert.equal(current.message.recipient,'agent:b');assert.equal(current.message.epoch,3);
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/ack`,'POST',{epoch:1})).status,409);
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/ack`,'POST',{epoch:3})).status,200);
});
test('native delivery claim is intent only, blocks readdress under uncertainty, and requires immutable owner receipt',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'native',key:'native-claim'}))).message;
 const h1={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']},h2={...h1,id:'machine:h2',resource_id:'h2'};
 const claimBody={phase:'claim',epoch:1,operation_id:'native:'+sent.id+':1'};
 const claim=await data(await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',claimBody,h1));assert.equal(claim.phase,'claimed');
 const status=await data(await m.fetch('agent:a',`/api/messages/${sent.id}/status`));assert.equal(status.message.delivered_at,null);assert.equal(status.events.some((e:any)=>e.stage==='delivered'),false);
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',claimBody,h2)).status,403);
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',{...claimBody,operation_id:'changed'},h1)).status,409);
 assert.equal((await m.fetch('agent:a',`/api/messages/${sent.id}/readdress`,'POST',{recipient:'agent:c',epoch:1,key:'uncertain-move'},admin)).status,409);
 const body={phase:'receipt',epoch:1,operation_id:'native:'+sent.id+':1',claim_id:claim.claim_id,native_receipt:{provider:'aplexer',message_id:'native-physical-id',session_id:'session-test',recipient:'agent:b'}};
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',{...body,native_receipt:{...body.native_receipt,session_id:'wrong'}},h1)).status,400);
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',body,h1)).status,200);
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',body,h1)).status,200);
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',{...body,native_receipt:{...body.native_receipt,message_id:'changed-id'}},h1)).status,409);
 const final=await data(await m.fetch('agent:a',`/api/messages/${sent.id}/status`));assert(final.message.delivered_at);assert.equal(final.message.native_receipt.machine_id,'h1');assert.equal(final.events.filter((e:any)=>e.stage==='delivered').length,1);
});
test('refresh response not safely published stays reconciliable and is never blindly reissued',async()=>{
 const s=state();let refreshes=0;const env:any={HUB2_CREDENTIAL_KEY:'cc'.repeat(32)};
 env.HUB2_PROVIDER_FETCH=async(url:string)=>{assert.equal(url,'https://auth.openai.com/oauth/token');refreshes++;env.HUB2_CREDENTIAL_KEY='invalid-key';return Response.json({access_token:'synthetic-rotated',refresh_token:'synthetic-rotated-refresh'});};
 const account=new AccountDO(s.ctx,env);await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic',refresh_token:'synthetic'}})},expected_generation:0}));await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]};
 assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,host))).status,503);assert.equal(refreshes,1);
 const attempt=s.database.prepare('SELECT state,sealed FROM credential_candidates WHERE base_generation=1').get() as any;assert.equal(attempt.state,'issued');assert.equal(attempt.sealed,'');
 env.HUB2_CREDENTIAL_KEY='cc'.repeat(32);assert.equal((await account.fetch(request('/api/account/credentials','GET',undefined,host))).status,503);assert.equal(refreshes,1,'unknown external outcome does not replay refresh token');
 assert.equal((await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},host))).status,503,'unknown central effect cannot be replayed through a host lease');
 const attention=await data(await account.fetch(request('/api/account/alerts')));assert(attention.items.some((v:any)=>v.kind==='refresh_publication_reconciliation_required'));
});
test('expired host refresh permit remains uncertain until its exact owner proves no request occurred',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'dd'.repeat(32)});
 await account.fetch(request('/api/account','PATCH',{provider:'gemini',enabled:true,capacity:1}));await account.fetch(request('/api/account/credentials','PUT',{files:{'.gemini/oauth_creds.json':'{"access_token":"synthetic"}'},expected_generation:0}));await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1','h2']}));
 const h1={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]},h2={...h1,id:'machine:h2',resource_id:'h2'};
 const lease=await data(await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h1)));
 s.database.prepare('UPDATE credentials SET refresh_until=?').run(Date.now()-1);clearFixtureCache(account);
 assert.equal((await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h2))).status,503);
 const proof={generation:1,lease:lease.lease,outcome:'not-started'};
 assert.equal((await account.fetch(request('/api/account/refresh-outcome','POST',proof,h2))).status,409);
 assert.equal((await account.fetch(request('/api/account/refresh-outcome','POST',proof,h1))).status,200);
 assert.equal((await account.fetch(request('/api/account/refresh-outcome','POST',proof,h1))).status,200);
 assert.equal((await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h2))).status,200);
});
test('host rotation rejects primitive GO JSON instead of publishing a broken generation',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'ee'.repeat(32)});
 await account.fetch(request('/api/account','PATCH',{provider:'go',enabled:true,capacity:1}));
 const files={'.local/share/opencode-hub/device-auth.json':JSON.stringify([{active:true,value:{access:'synthetic'}}])};await account.fetch(request('/api/account/credentials','PUT',{files,expected_generation:0}));await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));
 const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]};assert.equal((await account.fetch(request('/api/account/rotation','POST',{files:{'.local/share/opencode-hub/device-auth.json':'1'},expected_generation:1},host))).status,400);
 assert.equal((s.database.prepare('SELECT generation FROM credentials').get() as any).generation,1);
});
test('native proof normalizes authenticated machine identity and both views survive missing alarm and restart',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'native',key:'proof-auth'}))).message;
 const h={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']},operation='native:'+sent.id+':1';
 const claim=await data(await m.fetch('agent:b','/api/messages/'+sent.id+'/delivered','POST',{phase:'claim',epoch:1,operation_id:operation},h));
 const proof={phase:'receipt',epoch:1,operation_id:operation,claim_id:claim.claim_id,native_receipt:{provider:'aplexer',message_id:'physical-identity',session_id:'session-test',recipient:'agent:b',machine_id:'forged'}};
 assert.equal((await m.fetch('agent:b','/api/messages/'+sent.id+'/delivered','POST',proof,h)).status,200);
 m.stores.get('global')!.ctx.alarm=null;m.restart('global');
 assert.equal((await m.fetch('agent:b','/api/messages/'+sent.id+'/delivered','POST',proof,h)).status,200);
 const status=await data(await m.fetch('agent:a','/api/messages/'+sent.id+'/status'));assert.equal(status.message.native_receipt.machine_id,'h1');assert.equal(status.events.filter((e:any)=>e.stage==='delivered').length,1);
});

test('global authority resolves authorized IDs and prefixes directly without a central message ACL projection',async()=>{
 const m=mailboxes();m.flags.blockDirectory=true;
 const sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'direct owner authority',key:'owner'}))).message;
 assert.equal((await m.fetch('agent:a','/api/messages/'+sent.id.slice(0,8))).status,200);
 assert.equal((await m.fetch('agent:b','/api/messages/'+sent.id.slice(0,8))).status,200);
 assert.equal((await m.fetch('agent:c','/api/messages/'+sent.id)).status,404);
 assert.equal((await m.fetch('agent:c','/api/messages/'+sent.id.slice(0,8))).status,404);
 assert.equal((m.controlState.database.prepare("SELECT COUNT(*) AS n FROM object_routes WHERE kind='message'").get() as any).n,0);
 m.restart('global');assert.equal((await data(await m.fetch('agent:b','/api/messages'))).items[0].id,sent.id);
});

test('combined thread/state filter continues across nonmatching indexed pages without false completion',async()=>{
 const m=mailboxes();for(let i=0;i<3;i++){const sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'thread '+i,key:'thread-'+i}))).message;await m.fetch('agent:b',`/api/messages/${sent.id}/complete`,'POST',{epoch:1});}
 const all=await data(await m.fetch('agent:b','/api/messages?state=completed')),target=all.items[all.items.length-1];let cursor=null,items:any[]=[];let pages=0;
 do{const result=await data(await m.fetch('agent:b','/api/messages?state=completed&thread='+target.thread_id+'&limit=1'+(cursor?'&cursor='+encodeURIComponent(cursor):'')));items.push(...result.items);cursor=result.next;pages++;}while(cursor&&pages<5);
 assert.equal(pages,3);assert.deepEqual(items.map(v=>v.id),[target.id]);
});

test('native producer survives failed queue and ack handoffs without claiming delivery early',async()=>{
 const m=mailboxes();m.flags.blockMachine=true;
 const sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'durable native',key:'producer-retry'}))).message;
 const store=m.stores.get('agent:b')!,h={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']};
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'pending-machine');
 assert.equal((await m.fetch('agent:b',`/api/messages/${sent.id}/delivered`,'POST',{epoch:1,phase:'claim',operation_id:'native:'+sent.id+':1'},h)).status,409);
 m.restart('agent:b');m.flags.blockMachine=false;store.database.prepare('UPDATE native_outbox SET next_attempt=?').run(Date.now()-1);clearFixtureCache(m.get('agent:b'));await m.get('agent:b').alarm();
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'queued');
 assert.equal((await data(await m.fetch('agent:a',`/api/messages/${sent.id}/status`))).message.delivered_at,null);
 m.flags.blockMachine=true;assert.equal((await nativeDelivered(m,'agent:b',sent.id)).status,200);
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'pending-ack');
 m.restart('agent:b');m.flags.blockMachine=false;store.database.prepare('UPDATE native_outbox SET next_attempt=?').run(Date.now()-1);clearFixtureCache(m.get('agent:b'));await m.get('agent:b').alarm();
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'acked');
 assert(m.nativeCalls.some(c=>c.path.endsWith('/mail-ack')&&c.body.native_receipt.message_id==='native:'+sent.id+':1'));
});
test('unrouted native producer is idle until explicit durable directory wake, then queues once',async()=>{
 const m=mailboxes();m.controlState.database.prepare("UPDATE named_agents SET forward_session=NULL WHERE id='agent:b'").run();clearFixtureCache(m.control);
 const sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'later forward',key:'unrouted'}))).message,store=m.stores.get('agent:b')!;
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'unrouted');assert.equal(store.ctx.alarm,null);
 m.controlState.database.prepare("UPDATE named_agents SET forward_session='session-test' WHERE id='agent:b'").run();clearFixtureCache(m.control);
 m.restart('agent:b');await m.get('agent:b').fetch(request('/_resume-native','POST',{owner:'agent:b'}));await m.get('agent:b').alarm();
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'queued');
 assert.equal(m.nativeCalls.filter(c=>c.path.endsWith('/mail')&&c.body.message_id===sent.id).length,1);
});
test('account public release is unavailable and internal release binds exact machine/session even on replay',async()=>{
 const {account,database}=await configuredAccount();await quota(account);await account.fetch(request('/api/account/reserve','POST',reserve));await account.fetch(request('/api/account/confirm','POST',{operation_id:'op1',epoch:1}));await account.fetch(request('/api/account/start','POST',{operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1}));
 const proof={operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1,native_exit_confirmed:true};
 assert.equal((await account.fetch(request('/api/account/release','POST',proof))).status,404);
 assert.equal((await account.fetch(request('/_internal/release','POST',{...proof,machine_id:'other'}))).status,409);
 assert.equal((database.prepare('SELECT occupied FROM account').get() as any).occupied,1);
 assert.equal((await account.fetch(request('/_internal/release','POST',proof))).status,200);
 assert.equal((await account.fetch(request('/_internal/release','POST',{...proof,session_id:'other'}))).status,409);
 assert.equal((await account.fetch(request('/_internal/release','POST',proof))).status,200);
});

test('actual forward update atomically journals wake and effect-then503 survives Control restart',async()=>{
 const m=mailboxes();m.controlState.database.prepare("UPDATE named_agents SET forward_session=NULL WHERE id='agent:b'").run();clearFixtureCache(m.control);
 await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'wake via control',key:'actual-forward'});
 const store=m.stores.get('agent:b')!;assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'unrouted');
 assert.equal((await m.control.fetch(request('/api/named-agents','POST',{name:'b',display_name:'B',forward_session:'session-test'}))).status,200);
 assert.equal((m.controlState.database.prepare('SELECT COUNT(*) AS n FROM mailbox_wakes').get() as any).n,1);
 m.flags.failWakeAfterEffect=true;await m.control.alarm();
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'pending-target');
 assert.equal((m.controlState.database.prepare('SELECT COUNT(*) AS n FROM mailbox_wakes').get() as any).n,1);
 m.restartControl();m.controlState.database.prepare('UPDATE mailbox_wakes SET next_attempt=?').run(Date.now()-1);clearFixtureCache(m.control);await m.control.alarm();
 assert.equal((m.controlState.database.prepare('SELECT COUNT(*) AS n FROM mailbox_wakes').get() as any).n,0);assert.equal(m.controlState.ctx.alarm,null);
 await m.get('agent:b').alarm();assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'queued');
});
test('phone read and ack stages remain independent of subsequent queued native claim and proof',async()=>{
 for(const stage of ['read','ack','accept','complete']){
  const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'phone first',key:'phone-'+stage}))).message;
  await m.fetch('agent:b','/api/messages/'+sent.id+'/'+stage,'POST',{epoch:1});
  const before=(m.stores.get('agent:b')!.database.prepare('SELECT state FROM messages').get() as any).state;
  assert.equal((await nativeDelivered(m,'agent:b',sent.id)).status,200);
  assert.equal((m.stores.get('agent:b')!.database.prepare('SELECT state FROM messages').get() as any).state,before);
  const status=await data(await m.fetch('agent:a','/api/messages/'+sent.id+'/status'));assert(status.message.delivered_at);
 }
});
test('directory forward update and completed wake cannot race an older null target into permanent idle',async()=>{
 const m=mailboxes();m.controlState.database.prepare("UPDATE named_agents SET forward_session=NULL WHERE id='agent:b'").run();clearFixtureCache(m.control);
 // First recipient resolution belongs to sender validation, so arm the race only
 // after that validation has run and before the recipient producer resolves.
 let recipientReads=0;
 const original=m.control.fetch.bind(m.control);
 m.control.fetch=async(r:Request)=>{
  const response=await original(r);
  if(new URL(r.url).pathname==='/_recipient'&&++recipientReads===2)m.flags.afterRecipient=async()=>{
   await original(request('/api/named-agents','POST',{name:'b',display_name:'B',forward_session:'session-test'}));
   await m.control.alarm();
  };
  return response;
 };
 const sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'forward race',key:'forward-race'}))).message;
 const store=m.stores.get('agent:b')!;
 assert.equal((m.controlState.database.prepare('SELECT COUNT(*) AS n FROM mailbox_wakes').get() as any).n,0);
 assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'pending-target');
 await m.get('agent:b').alarm();assert.equal((store.database.prepare('SELECT state FROM native_outbox').get() as any).state,'queued');
 assert(m.nativeCalls.some(c=>c.body.message_id===sent.id));
});
test('queued immutable target rotation raises durable sender/recipient attention and supports authorized epoch repair',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'rotating target',key:'rotation'}))).message;
 await m.control.fetch(request('/_routes/register','POST',{kind:'session',id:'session-new',owner:'h2',resource_id:'task2',participants:[]}));
 await m.control.fetch(request('/api/named-agents','POST',{name:'b',display_name:'B',forward_session:'session-new'}));
 m.flags.failWakeAfterEffect=true;await m.control.alarm();m.restartControl();m.controlState.database.prepare('UPDATE mailbox_wakes SET next_attempt=?').run(Date.now()-1);clearFixtureCache(m.control);await m.control.alarm();
 m.flags.blockMachine=true;await m.get('agent:b').alarm();
 assert.equal((m.stores.get('agent:b')!.database.prepare('SELECT state FROM native_outbox').get() as any).state,'attention-pending');
 m.restart('agent:b');m.flags.blockMachine=false;m.stores.get('agent:b')!.database.prepare('UPDATE native_outbox SET next_attempt=?').run(Date.now()-1);clearFixtureCache(m.get('agent:b'));await m.get('agent:b').alarm();
 assert.equal((m.stores.get('agent:b')!.database.prepare('SELECT state FROM native_outbox').get() as any).state,'attention');
 for(const owner of ['agent:a','agent:b']){const status=await data(await m.fetch(owner,'/api/messages/'+sent.id+'/status'));assert.equal(status.message.native_attention.reason,'forward_target_changed');assert.equal(status.message.native_attention.previous_session,'session-test');assert.equal(status.message.native_attention.current_session,'session-new');assert.equal(status.message.delivered_at,null);}
 const repaired=await data(await m.fetch('agent:a','/api/messages/'+sent.id+'/readdress','POST',{recipient:'agent:b',epoch:1,key:'repair-rotation'},admin));assert.equal(repaired.message.epoch,2);assert.equal(repaired.message.native_attention,null);assert.equal((await data(await m.fetch('agent:b','/api/messages/'+sent.id+'/status'))).message.native_attention,null);
 assert(m.nativeCalls.some(c=>c.path.endsWith('/mail-cancel')&&c.body.epoch===1));
 assert(m.nativeCalls.some(c=>c.path.endsWith('/mail')&&c.body.epoch===2&&c.body.session_id==='session-new'));
});
test('claimed native effect stays pinned on forward rotation and is never duplicated to a new target',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'pinned',key:'claimed-rotation'}))).message;
 const h={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']},claim={phase:'claim',epoch:1,operation_id:'native:'+sent.id+':1'};
 assert.equal((await m.fetch('agent:b','/api/messages/'+sent.id+'/delivered','POST',claim,h)).status,201);
 await m.control.fetch(request('/_routes/register','POST',{kind:'session',id:'session-new',owner:'h2',resource_id:'task2',participants:[]}));
 await m.control.fetch(request('/api/named-agents','POST',{name:'b',display_name:'B',forward_session:'session-new'}));await m.control.alarm();
 assert.equal((await data(await m.fetch('agent:b','/api/messages/'+sent.id+'/status'))).message.native_attention,null);
 assert.equal((await nativeDelivered(m,'agent:b',sent.id)).status,200);
 assert.equal(m.nativeCalls.filter(c=>c.path.endsWith('/mail')&&c.body.session_id==='session-new').length,0);
});

test('zai-central imports plaintext coding-plan key, collects maintained token windows, syncs hosts and never OAuth-refreshes static keys',async()=>{
 const s=state();s.ctx.id={name:'zai-central',toString:()=> 'synthetic-zai-central'};const calls:string[]=[];let unauthorized=false,invalid=false;
 const provider=async(url:string,init:any)=>{calls.push(url);assert.equal(url,'https://api.z.ai/api/monitor/usage/quota/limit');assert.equal(init.headers.Authorization,'Bearer synthetic-zai-coding-key');return unauthorized?new Response('{}',{status:401}):Response.json(invalid?{data:{limits:[{type:'TOKENS_LIMIT',unit:3}]}}:{data:{limits:[{type:'TOKENS_LIMIT',unit:3,percentage:12,nextResetTime:Date.now()+18000000},{type:'TOKENS_LIMIT',unit:6,percentage:41,nextResetTime:Date.now()+604800000},{type:'TIME_LIMIT',unit:5,percentage:0}]}});};
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'44'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 assert.equal((await account.fetch(request('/api/account','PATCH',{provider:'zai',capacity:1,enabled:true}))).status,200);
 const files={'.config/goz/config.json':JSON.stringify({zai_token:'synthetic-zai-coding-key'})};
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files,expected_generation:0,account_id:'shared-zai',provider:'zai'}))).status,400);
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files:{'.config/goz/config.json':'{"zai_token":"enc:v1:encrypted"}'},expected_generation:0}))).status,400);
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files:{'.zcode/v2/credentials.json':'{"token":"encrypted-app-store"}'},expected_generation:0}))).status,400);
 assert.equal((await account.fetch(request('/api/account/credentials','PUT',{files,expected_generation:0,account_id:'zai-central',provider:'zai'}))).status,200);
 const observed=await data(await account.fetch(request('/api/account/discovery','POST',{})));assert.equal(observed.remaining,59);
 const snapshot=await data(await account.fetch(request('/api/account/quota')));assert.equal(snapshot.fresh,true);assert.equal(snapshot.quota.source,'provider:zai:generation:1');
 const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['machine:offer']};await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));
 const sync=await data(await account.fetch(request('/api/account/credentials','GET',undefined,host)));assert.deepEqual(sync.files,files);assert.equal(sync.generation,1);
 assert.equal((await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},host))).status,409);
 invalid=true;assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,503);assert.equal((await data(await account.fetch(request('/api/account/quota')))).fresh,false);
 invalid=false;unauthorized=true;assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,503);
 assert.equal(calls.every(url=>url==='https://api.z.ai/api/monitor/usage/quota/limit'),true);assert.equal((s.database.prepare('SELECT COUNT(*) AS n FROM credential_candidates').get() as any).n,0);
});
test('zai configuration and policy refuse retired account aliases without banning the maintained engine name',async()=>{
 const s=state();s.ctx.id={name:'shared-zai',toString:()=> 'synthetic-retired'};const account=new AccountDO(s.ctx);
 assert.equal((await account.fetch(request('/api/account','PATCH',{provider:'zai',capacity:1,enabled:true}))).status,400);
 const control=new ControlDO(state().ctx,{} as any),candidate={id:'central',engine:'zcodex',model:'glm-flash',account_id:'zai-central',weight:1,capabilities:['interactive'],enabled:true};
 assert.equal((await control.fetch(request('/api/model-policy','PUT',{candidates:[{...candidate,account_id:'shared-zai'}]}))).status,400);
 assert.equal((await control.fetch(request('/api/model-policy','PUT',{candidates:[{...candidate,account_id:'zcodex'}]}))).status,400);
 assert.equal((await control.fetch(request('/api/model-policy','PUT',{candidates:[candidate]}))).status,200);
});

test('concurrent global reroute retries share one operation and a no-change key cannot later mutate another target',async()=>{
 const m=mailboxes(),sent=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'concurrent',key:'concurrent-move'}))).message,path='/api/messages/'+sent.id+'/readdress',move={recipient:'agent:c',key:'move-once',epoch:1};
 const results=await Promise.all([m.fetch('agent:a',path,'POST',move,admin),m.fetch('agent:a',path,'POST',move,admin)]);
 assert.equal(results.every(r=>r.status===200||r.status===202),true);
 const current=(await data(await m.fetch('agent:a','/api/messages/'+sent.id))).message;assert.equal(current.epoch,2);assert.equal(current.recipient,'agent:c');
 assert.equal((await m.fetch('agent:a',path,'POST',{recipient:'agent:c',key:'no-change',epoch:2},admin)).status,200);
 assert.equal((await m.fetch('agent:a',path,'POST',{recipient:'agent:d',key:'no-change',epoch:2},admin)).status,409);
});
test('global physical native receipt cannot be reused for another sender or recipient message',async()=>{
 const m=mailboxes(),h={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']};
 const first=(await data(await m.fetch('agent:a','/api/messages','POST',{recipient:'agent:b',body:'one',key:'physical-one'}))).message;
 assert.equal((await nativeDelivered(m,'agent:b',first.id)).status,200);
 const second=(await data(await m.fetch('agent:c','/api/messages','POST',{recipient:'agent:b',body:'two',key:'physical-two'}))).message,op='native:'+second.id+':1';
 const claim=await data(await m.fetch('agent:b','/api/messages/'+second.id+'/delivered','POST',{phase:'claim',epoch:1,operation_id:op},h));
 const replay={phase:'receipt',epoch:1,operation_id:op,claim_id:claim.claim_id,native_receipt:{provider:'aplexer',message_id:'native:'+first.id+':1',session_id:'session-test',recipient:'agent:b'}};
 assert.equal((await m.fetch('agent:b','/api/messages/'+second.id+'/delivered','POST',replay,h)).status,409);
 assert.equal((await data(await m.fetch('agent:c','/api/messages/'+second.id+'/status'))).message.delivered_at,null);
});

test('stale authorized views and placement coalesce with actual scheduled collector work, without quota PUT',async()=>{
 const s=state();let calls=0,release!:()=>void,entered!:()=>void;const waiting=new Promise<void>(r=>entered=r),gate=new Promise<void>(r=>release=r);
 const provider=async()=>{calls++;entered();await gate;return Response.json({usage:{rolling:{percent:20},weekly:{usagePercent:30}}});};
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'55'.repeat(32),HUB2_PROVIDER_FETCH:provider as any});
 await account.fetch(request('/api/account','PATCH',{provider:'go',capacity:1,enabled:true}));
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:0,files:{'.local/share/opencode-hub/device-auth.json':JSON.stringify([{active:true,value:{access:'synthetic-go-token'}}])}}));
 await account.fetch(request('/api/account/discovery','PUT',{enabled:true,interval_ms:21600000}));
 (account as any).db.run('UPDATE discovery SET next_at=?',Date.now()-1);
 assert.equal((await account.fetch(request('/api/account/quota','GET',undefined,{id:'agent:denied',kind:'agent',scopes:[]}))).status,403);assert.equal(calls,0);
 const views=Array.from({length:8},()=>account.fetch(request('/api/account/quota'))),placement=account.fetch(request('/_internal/routing'));
 await waiting;const scheduled=account.alarm();await Promise.resolve();assert.equal(calls,1);release();
 const results=await Promise.all(views);await placement;await scheduled;
 for(const result of results){const snapshot=await data(result);assert.equal(snapshot.fresh,true);assert.equal(snapshot.quota.remaining,70);assert.equal(snapshot.quota.source,'provider:go:generation:1');}
 assert.equal(calls,1);
});
test('failed lazy probe has durable backoff while explicit operator probe can repair immediately',async()=>{
 const s=state();let calls=0,fail=true;const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'66'.repeat(32),HUB2_PROVIDER_FETCH:(async()=>{calls++;return fail?new Response('{}',{status:503}):Response.json({usage:{rolling:{percent:10}}});}) as any});
 await account.fetch(request('/api/account','PATCH',{provider:'go',capacity:1,enabled:true}));
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:0,files:{'.local/share/opencode-hub/device-auth.json':JSON.stringify([{active:true,value:{access:'synthetic-go-token'}}])}}));
 assert.equal((await data(await account.fetch(request('/api/account/quota')))).fresh,false);
 assert.equal((await data(await account.fetch(request('/_internal/routing')))).fresh,false);assert.equal(calls,1);
 const restarted=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'66'.repeat(32),HUB2_PROVIDER_FETCH:(async()=>{calls++;return Response.json({usage:{rolling:{percent:10}}});}) as any});
 assert.equal((await data(await restarted.fetch(request('/api/account/quota')))).fresh,false);assert.equal(calls,1);
 fail=false;assert.equal((await restarted.fetch(request('/api/account/discovery','POST',{}))).status,200);
 assert.equal((await data(await restarted.fetch(request('/api/account/quota')))).fresh,true);assert.equal(calls,2);
});

test('default native homes receive access-only material and approved host metadata is account scoped',async()=>{
 const s=state(),expiry=Math.floor(Date.now()/1000)+864000,token='e30.'+btoa(JSON.stringify({exp:expiry})).replaceAll('=','')+'.synthetic',account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'77'.repeat(32),HUB2_PROVIDER_FETCH:(async(url:any)=>Response.json(String(url).includes('resetcredits')?{credits:[]}:{rate_limit:{primary_window:{used_percent:20,reset_at:expiry},secondary_window:{used_percent:30,reset_at:expiry}}})) as any});
 await account.fetch(request('/api/account','PATCH',{provider:'codex',capacity:1,enabled:true}));
 const files={'.codex/auth.json':JSON.stringify({tokens:{access_token:token,refresh_token:'synthetic-controller-refresh'},last_refresh:new Date().toISOString()})};
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:0,files}));
 const host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]};
 assert.equal((await account.fetch(request('/api/account','GET',undefined,host))).status,403);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));
 const metadata=await data(await account.fetch(request('/api/account/credentials?metadata=1','GET',undefined,host)));assert.equal(metadata.generation,1);assert.equal(metadata.files,undefined);
 const access=await data(await account.fetch(request('/api/account/credentials?min_validity_ms=7560000','GET',undefined,host)));
 assert.equal(access.generation,1);assert.equal(access.refresh_material,false);assert.equal(access.access_expires_at,expiry*1000);assert.equal(JSON.parse(access.files['.codex/auth.json']).tokens.refresh_token,'');assert.equal(JSON.parse(access.files['.codex/auth.json']).tokens.access_token,token);assert.equal(JSON.parse(access.files['.codex/auth.json']).last_refresh,JSON.parse(files['.codex/auth.json']).last_refresh);
 const lease=await data(await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},host)));
 const controller=await data(await account.fetch(request('/api/account/credentials?refresh_lease='+lease.lease,'GET',undefined,host)));assert.equal(JSON.parse(controller.files['.codex/auth.json']).tokens.refresh_token,'synthetic-controller-refresh');assert.equal(controller.refresh_material,true);
 const view=await data(await account.fetch(request('/api/account','GET',undefined,host)));assert.equal(view.account.provider,'codex');
 await account.fetch(request('/api/account/hosts','PUT',{hosts:[]}));assert.equal((await account.fetch(request('/api/account','GET',undefined,host))).status,403);
});
test('expired issued refresh permit still fences a different host rotation and wrong material retrieval',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'88'.repeat(32)}),h1={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]},h2={...h1,id:'machine:h2',resource_id:'h2'};
 await account.fetch(request('/api/account','PATCH',{provider:'codex',capacity:1,enabled:true}));
 const auth=(stamp:string)=>({'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic-access',refresh_token:'synthetic-refresh'},last_refresh:stamp})});
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:0,files:auth('2026-11-01T00:00:00Z')}));
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1','h2']}));
 const lease=await data(await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h1)));
 (account as any).db.run('UPDATE credentials SET refresh_until=?',Date.now()-1);
 assert.equal((await account.fetch(request('/api/account/credentials?refresh_lease='+lease.lease,'GET',undefined,h2))).status,409);
 const denied=await account.fetch(request('/api/account/rotation','POST',{expected_generation:1,files:auth('2026-11-04T00:00:00Z')},h2));assert.equal(denied.status,409);
 assert.equal((s.database.prepare('SELECT generation FROM credentials').get() as any).generation,1);
 const owned=await account.fetch(request('/api/account/rotation','POST',{expected_generation:1,lease:lease.lease,files:auth('2026-11-04T00:00:00Z')},h1));assert.equal(owned.status,200);
});
test('refresh material export rechecks the exact permit after real decrypt completes asynchronously',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'99'.repeat(32)}),h1={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]},h2={...h1,id:'machine:h2',resource_id:'h2'};
 await account.fetch(request('/api/account','PATCH',{provider:'codex',capacity:1,enabled:true}));
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:0,files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:'synthetic-access',refresh_token:'synthetic-refresh'},last_refresh:new Date().toISOString()})}}));
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1','h2']}));
 const lease=await data(await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h1)));
 let entered!:()=>void,release!:()=>void;const started=new Promise<void>(r=>entered=r),gate=new Promise<void>(r=>release=r),real=(account as any).unseal.bind(account);
 (account as any).unseal=async(...args:any[])=>{const value=await real(...args);entered();await gate;return value;};
 const pending=account.fetch(request('/api/account/credentials?refresh_lease='+lease.lease,'GET',undefined,h1));await started;
 assert.equal((await account.fetch(request('/api/account/refresh-outcome','POST',{generation:1,lease:lease.lease,outcome:'not-started'},h1))).status,200);
 assert.equal((await account.fetch(request('/api/account/refresh-lease','POST',{generation:1},h2))).status,200);
 release();const refused=await pending;assert.equal(refused.status,409);assert.equal((await data(refused)).files,undefined);
});
test('minimum-validity provisioning collects genuine same-generation quota after central refresh and fails before launch if unavailable',async()=>{
 const s=state(),exp=Math.floor(Date.now()/1000)+14400,jwt=(expiry:number)=>'e30.'+btoa(JSON.stringify({exp:expiry})).replaceAll('=','')+'.synthetic';let refreshes=0,usage=0,fail=true;
 const provider=async(url:string)=>{
  if(url==='https://auth.openai.com/oauth/token'){refreshes++;return Response.json({access_token:jwt(exp),refresh_token:'synthetic-new-controller'});}
  if(url.endsWith('/wham/usage')){usage++;return fail?new Response('{}',{status:503}):Response.json({rate_limit:{primary_window:{used_percent:20}}});}
  if(url.endsWith('/rate-limit-reset-credits'))return Response.json({credits:[]});
  throw Error('unexpected deterministic provider URL');
 };
 const account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'aa'.repeat(32),HUB2_PROVIDER_FETCH:provider as any}),host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]};
 await account.fetch(request('/api/account','PATCH',{provider:'codex',enabled:true,capacity:1}));
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:0,files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:jwt(1),refresh_token:'synthetic-old-controller'},last_refresh:'2000-01-01T00:00:00Z'})}}));
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));
 const path='/api/account/credentials?min_validity_ms=7560000',denied=await account.fetch(request(path,'GET',undefined,host));assert.equal(denied.status,503);assert.equal((await data(denied)).files,undefined);assert.equal(refreshes,1);assert.equal(usage,1);
 fail=false;assert.equal((await account.fetch(request('/api/account/discovery','POST',{}))).status,200);
 const granted=await data(await account.fetch(request(path,'GET',undefined,host)));assert.equal(granted.generation,2);assert.equal(granted.refresh_material,false);assert.equal(refreshes,1);
 const quota=await data(await account.fetch(request('/api/account/quota')));assert.equal(quota.fresh,true);assert.equal(quota.quota.source,'provider:codex:generation:2');
});
test('current host approval and credential generation rejection fence reserve confirm and admission without freeing uncertain effects',async()=>{
 const s=state(),account=new AccountDO(s.ctx,{HUB2_CREDENTIAL_KEY:'bb'.repeat(32)}),host={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:[]};
 await account.fetch(request('/api/account','PATCH',{provider:'go',enabled:true,capacity:2}));
 const files=(token:string)=>({'.local/share/opencode-hub/device-auth.json':JSON.stringify([{active:true,value:{access:token}}])});
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:0,files:files('synthetic-one')}));
 const quota=()=>account.fetch(request('/api/account/quota','PUT',{remaining:80,source:'synthetic admission fixture',captured_at:Date.now(),valid_until:Date.now()+60000}));
 await quota();const binding={operation_id:'host-policy-op',session_id:'host-policy-session',machine_id:'h1',request_digest:'synthetic',epoch:1};
 assert.equal((await account.fetch(request('/api/account/reserve','POST',binding))).status,409);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));assert.equal((await account.fetch(request('/api/account/reserve','POST',binding))).status,200);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:[]}));assert.equal((await account.fetch(request('/api/account/confirm','POST',binding))).status,409);
 assert.equal((s.database.prepare('SELECT occupied FROM account').get() as any).occupied,1);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:['h1']}));assert.equal((await account.fetch(request('/api/account/confirm','POST',binding))).status,200);
 assert.equal((await account.fetch(request('/api/account/credential-rejection','POST',{generation:1,reason:'credential_rejected'},host))).status,200);
 assert.equal((await account.fetch(request('/api/account/start','POST',binding))).status,409);
 assert.equal((s.database.prepare('SELECT occupied FROM account').get() as any).occupied,1);
 await account.fetch(request('/api/account/credentials','PUT',{expected_generation:1,files:files('synthetic-two')}));await quota();
 assert.equal((await account.fetch(request('/api/account/start','POST',binding))).status,200);
 await account.fetch(request('/api/account/hosts','PUT',{hosts:[]}));assert.equal((await account.fetch(request('/api/account/start','POST',binding))).status,409);
 assert.equal((await account.fetch(request('/_internal/release','POST',{...binding,native_exit_confirmed:true}))).status,200);
 assert.equal((s.database.prepare('SELECT occupied FROM account').get() as any).occupied,0);
});
