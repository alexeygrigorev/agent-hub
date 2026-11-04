// Run from repository root: node hub2/tests/run-accounts-mail-workerd.mjs
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {resolve} from 'node:path';
import {mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
const root=resolve(import.meta.dirname,'../..');
const bundle=await build({entryPoints:[resolve(root,'hub2/tests/accounts-mail-worker.ts')],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
const persist=await mkdtemp(resolve(tmpdir(),'hub2-mail-workerd-'));
const options=convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[{name:'hub2-accounts-mail-fixture',script:bundle.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',durableObjects:{MACHINES:{className:'MachineFixture',useSQLite:true},CONTROL:{className:'ControlDO',useSQLite:true},ACCOUNTS:{className:'AccountDO',useSQLite:true},MAILBOXES:{className:'MailboxFixture',useSQLite:true}},bindings:{HUB2_CREDENTIAL_KEY:'11'.repeat(32)}}]});
let mf=new Miniflare(options);
const admin={id:'operator:test',kind:'operator',scopes:['operator:admin']};
let base;
async function call(group,id,path,method='GET',body,actor=admin){
 const response=await fetch(`${base}/${group}/${encodeURIComponent(id)}${path}`,{method,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const result=await response.json();return {response,result};
}
async function delivered(id){
 const owner='agent:b',actor={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['message:delivery','mailbox:agent:b']},operation='native:'+id+':1';
 const claim=await call('mailboxes',owner,`/api/messages/${id}/delivered`,'POST',{phase:'claim',epoch:1,operation_id:operation},actor);assert.equal(claim.response.status,201);
 const receipt=await call('mailboxes',owner,`/api/messages/${id}/delivered`,'POST',{phase:'receipt',epoch:1,operation_id:operation,claim_id:claim.result.claim_id,native_receipt:{provider:'aplexer',message_id:'native:'+id,session_id:'session-test',recipient:owner}},actor);assert.equal(receipt.response.status,200);
}
const count=async(group,id)=>(await call(group,id,'/_metrics')).result.counts;
const delta=(a,b)=>({reads:b.rows_read-a.rows_read,writes:b.rows_written-a.rows_written,total:b.rows_read-a.rows_read+b.rows_written-a.rows_written,alarm_operations:b.alarm_operations-a.alarm_operations});
try{
 base=(await mf.ready).origin;
 assert.equal((await call('machines','h1','/_fixture/seed-running','POST',{})).response.status,200);
 assert.equal((await call('control','global','/_routes/register','POST',{kind:'session',id:'session-test',owner:'h1',resource_id:'synthetic-intake',participants:[]})).response.status,200);
 for(const name of ['a','b'])assert.equal((await call('control','global','/api/named-agents','POST',{name,display_name:name,forward_session:'session-test'})).response.status,200);
 const firstMachineActivation=await count('machines','h1');
 const firstControlActivation=await count('control','global');
 let result=await call('accounts','a1','/api/account','PATCH',{provider:'synthetic',enabled:true,capacity:1});assert.equal(result.response.status,200);
 await call('accounts','a1','/api/account/quota','PUT',{remaining:80,captured_at:Date.now(),valid_until:Date.now()+60000,source:'synthetic-local-evidence'});
 const accountBefore=await count('accounts','a1');assert.equal(accountBefore.measurement_valid,true);
 const reservation={operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1,request_digest:'synthetic-digest'};
 for(const [path,body] of [['reserve',reservation],['confirm',{operation_id:'op1',epoch:1}],['start',{operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1}],['release',{operation_id:'op1',session_id:'s1',machine_id:'h1',epoch:1,native_exit_confirmed:true}]])assert.equal((await call('accounts','a1',(path==='release'?'/_internal/release':'/api/account/'+path),'POST',body)).response.status,200);
 const accountAfter=await count('accounts','a1');assert.equal(accountAfter.measurement_valid,true);
 const accountFlow=delta(accountBefore,accountAfter);assert(accountFlow.total<300,JSON.stringify(accountFlow));
 const sender={id:'agent:a',kind:'agent',scopes:[]},recipient={id:'agent:b',kind:'agent',scopes:[]};
 const first=await call('mailboxes','agent:a','/api/messages','POST',{recipient:'agent:b',body:'synthetic',key:'first'},sender);assert.equal(first.response.status,201);
 const id=first.result.message.id;
 await delivered(id);
 for(const stage of ['read','ack','accept','complete'])assert.equal((await call('mailboxes','agent:b',`/api/messages/${id}/${stage}`,'POST',{epoch:1},recipient)).response.status,200);
 const status=await call('mailboxes','agent:a',`/api/messages/${id}/status`,'GET',undefined,sender);assert(status.result.message.completed_at);assert(status.result.events.some(e=>e.stage==='complete'));
 assert.equal((await call('mailboxes','agent:b','/api/messages','GET',undefined,{id:'agent:intruder',kind:'agent',scopes:[]})).response.status,403);
 const senderMetrics=await count('mailboxes','agent:a'),recipientMetrics=await count('mailboxes','agent:b');assert.equal(senderMetrics.measurement_valid,true);assert.equal(recipientMetrics.measurement_valid,true);
 // Populate retained history through actual message APIs. Measure the same bounded
 // page before/after growth; historical write costs are intentionally not amortized away.
 const beforeList=await count('mailboxes','agent:b');await call('mailboxes','agent:b','/api/messages?limit=1','GET',undefined,recipient);const smallPage=delta(beforeList,await count('mailboxes','agent:b'));
 for(let i=0;i<100;i++)assert.equal((await call('mailboxes','agent:a','/api/messages','POST',{recipient:'agent:b',body:'synthetic history '+i,key:'history-'+i},sender)).response.status,201);
 const beforeLargeList=await count('mailboxes','agent:b');await call('mailboxes','agent:b','/api/messages?limit=1','GET',undefined,recipient);const largePage=delta(beforeLargeList,await count('mailboxes','agent:b'));
 assert(largePage.total<=smallPage.total+4,JSON.stringify({smallPage,largePage}));
 const warmBeforeMachine=await count('machines','h1');
 const warmBeforeControl=await count('control','global');
 const warmBeforeA=await count('mailboxes','agent:a'),warmBeforeB=await count('mailboxes','agent:b');
 const warm=await call('mailboxes','agent:a','/api/messages','POST',{recipient:'agent:b',body:'warm lifecycle',key:'warm-full'},sender);
 await delivered(warm.result.message.id);
 for(const stage of ['read','ack','accept','complete'])assert.equal((await call('mailboxes','agent:b',`/api/messages/${warm.result.message.id}/${stage}`,'POST',{epoch:1},recipient)).response.status,200);
 const warmMachine=delta(warmBeforeMachine,await count('machines','h1'));
 const warmControl=delta(warmBeforeControl,await count('control','global'));
 const warmSender=delta(warmBeforeA,await count('mailboxes','agent:a')),warmRecipient=delta(warmBeforeB,await count('mailboxes','agent:b'));
 await mf.dispose();mf=new Miniflare(options);base=(await mf.ready).origin;
 const reactivatedSender=await count('mailboxes','agent:a'),reactivatedRecipient=await count('mailboxes','agent:b');
 assert.equal(reactivatedSender.measurement_valid,true);assert.equal(reactivatedRecipient.measurement_valid,true);
 const restored=await call('mailboxes','agent:b','/api/messages?limit=1','GET',undefined,recipient);assert.equal(restored.result.items.length,1);
 const reactivatedControl=await count('control','global');assert.equal(reactivatedControl.measurement_valid,true);
 const reactivatedAccount=await count('accounts','a1');assert.equal(reactivatedAccount.measurement_valid,true);
 const stableBefore=await count('mailboxes','agent:a');await new Promise(r=>setTimeout(r,25));const stableAfter=await count('mailboxes','agent:a');assert.equal(stableAfter.rows_written,stableBefore.rows_written);assert.equal(stableAfter.rows_read,stableBefore.rows_read);
 const profileA=(await call('mailboxes','agent:a','/_fixture/profile')).result,profileB=(await call('mailboxes','agent:b','/_fixture/profile')).result;
 console.log(JSON.stringify({profileA,profileB,passed:true,engine:'native workerd SQLite',accountFlow,firstMachineActivation,firstControlActivation,firstAccountActivation:accountBefore,messageFlowIncludingConstructors:{sender:senderMetrics,recipient:recipientMetrics},smallPage,largePage,warmFullMessage:{sender:warmSender,recipient:warmRecipient,control:warmControl,machine:warmMachine,total:warmSender.total+warmRecipient.total+warmControl.total+warmMachine.total,alarm_operations:warmSender.alarm_operations+warmRecipient.alarm_operations},reactivation:{sender:reactivatedSender,recipient:reactivatedRecipient,account:reactivatedAccount,control:reactivatedControl},note:'Subset measurement only; excludes edge auth/dispatch and does not prove whole-Hub normal-day budget.'}));
}finally{await mf.dispose();}
