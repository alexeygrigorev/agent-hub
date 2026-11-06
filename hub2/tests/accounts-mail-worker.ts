// Local-only fixture. Never deploy: it deliberately accepts synthetic actor headers.
import { MachineDO } from '../src/machine-do';
import { ControlDO } from '../src/control-do';
import {BudgetDO as ActualBudgetDO} from '../src/budget-do';
import { AccountDO as ActualAccountDO } from '../src/account-do';
import {fixtureBudgetEnvelopes} from './budget-fixture';
export class BudgetDO extends ActualBudgetDO {
 async fetch(request:Request):Promise<Response>{
  if(new URL(request.url).pathname==='/_fixture/reservations'){
   const db=(this as any).db;
   return Response.json({reservations:db.all('SELECT id,kind,state,estimate_pusd,actual_pusd,estimate_json,actual_json FROM budget_reservations ORDER BY created_at,id')});
  }
  return super.fetch(request);
 }
}
// Persistence stress only: the dedicated normal-day budget gate uses the
// conservative production candidate. This disposable history fixture needs 100
// sends to complete so it can measure indexed growth independently of the $0.25
// daily affordability gate; its override/profile is never deployed or reused.
const historyProfile=JSON.parse(fixtureBudgetEnvelopes);
historyProfile.normal.by_kind={native_delivery:{provenance:'Synthetic retained-history Workerd fixture only; not production cost sizing',max_do_calls:4,max_wall_ms:2000,units:{worker_requests:1,worker_cpu_ms:100,do_requests:7,do_ws_messages:0,do_gb_ms:250,background_invocations:0,sqlite_rows_read:512,sqlite_rows_written:160,sqlite_byte_days:1,kv_reads:1,kv_writes:1,kv_deletes:1,kv_lists:1,kv_byte_days:1}}};
const HISTORY_STRESS_ENVELOPES=JSON.stringify(historyProfile);
class AccountDO extends ActualAccountDO {constructor(ctx:any,env:any){super(ctx,{HUB2_BUDGET_ENVELOPES:fixtureBudgetEnvelopes,...env});}}
import { MailboxDO as ActualMailboxDO } from '../src/mailbox-do';
class MailboxDO extends ActualMailboxDO {constructor(ctx:any,env:any){super(ctx,{HUB2_BUDGET_ENVELOPES:HISTORY_STRESS_ENVELOPES,...env});}}
export { AccountDO,MailboxDO,ControlDO };
export class MailboxFixture extends MailboxDO{
 async fetch(request:Request):Promise<Response>{
  if(new URL(request.url).pathname==='/_fixture/outbox'){
   const db=(this as any).db,id=new URL(request.url).searchParams.get('id');
   const outbox=id?db.all("SELECT message_id,epoch,operation_id,state,next_attempt,session_id,machine_id,attempts FROM native_outbox WHERE message_id=? ORDER BY epoch",id):db.all("SELECT message_id,epoch,operation_id,state,next_attempt,session_id,machine_id,attempts FROM native_outbox ORDER BY message_id,epoch LIMIT 16");
   return Response.json({outbox});
  }
  if(new URL(request.url).pathname==='/_fixture/profile'){
   const db=(this as any).db,rows=[];
   for(const table of ['native_outbox','readdress_operations']){
    const tail=table==='native_outbox'?'message_id,epoch':'sender,key';
    const query='SELECT next_attempt FROM '+table+' WHERE due=1 ORDER BY next_attempt,'+tail+' LIMIT 1';
    const before=db.snapshot();db.all(query);const after=db.snapshot();
    rows.push({table,reads:after.rows_read-before.rows_read,writes:after.rows_written-before.rows_written,plan:db.all('EXPLAIN QUERY PLAN '+query)});
   }return Response.json({queries:rows});
  }return super.fetch(request);
 }
}
// Synthetic RUNNING precondition for queue storage/cost tests only. It never
// launches a process and is not live-native evidence. Production entry excludes this fixture.
export class MachineFixture extends MachineDO{
 private fixtureState:DurableObjectState;
 constructor(ctx:DurableObjectState,env:any){super(ctx,env);this.fixtureState=ctx;}
 async fetch(request:Request):Promise<Response>{
  if(new URL(request.url).pathname==='/_fixture/seed-running'){
   const now=Date.now(),db=(this as any).db;
   db.run('INSERT INTO machines(machine_id,alias,os,arch,labels_json,last_seen,next_epoch,active_session_id) VALUES(?,?,?,?,?,?,?,?)','h1','fixture','linux','x64','[]',now,1,'session-test');
   db.run('INSERT INTO sessions(id,operation_id,request_digest,dispatch_id,attempt,account_id,project_id,intake_id,machine_id,epoch,title,acceptance_criteria_json,launch_json,state,native_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)','session-test','synthetic-operation','synthetic-digest','synthetic-dispatch',1,null,'synthetic-project','synthetic-intake','h1',1,'synthetic precondition','["no actual native process"]','{}','running','synthetic-native-id',now,now);
   return Response.json({synthetic_precondition:true});
  }
  return super.fetch(request);
 }
}
export default {async fetch(request:Request,env:any){
 const url=new URL(request.url),match=url.pathname.match(/^\/(accounts|mailboxes|control|machines|budget)\/([^/]+)(\/.*)$/);
 if(!match)return new Response('not found',{status:404});
 const id=decodeURIComponent(match[2]),namespace=match[1]==='accounts'?env.ACCOUNTS:match[1]==='control'?env.CONTROL:match[1]==='machines'?env.MACHINES:match[1]==='budget'?env.BUDGET:env.MAILBOXES;
 const headers=new Headers(request.headers);const actor=JSON.parse(headers.get('x-hub2-actor')||'{}');if(actor.kind==='machine')headers.set('x-hub2-delivery-session','session-test');if(match[1]==='mailboxes')headers.set('x-hub2-mailbox',id);if(match[1]==='accounts')headers.set('x-hub2-account-id',id);
 return namespace.get(namespace.idFromName(match[1]==='mailboxes'?'global':id)).fetch(new Request('https://fixture.internal'+match[3]+url.search,{method:request.method,headers,body:request.body,duplex:'half'} as RequestInit));
}};
