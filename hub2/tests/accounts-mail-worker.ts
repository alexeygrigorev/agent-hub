// Local-only fixture. Never deploy: it deliberately accepts synthetic actor headers.
import { MachineDO } from '../src/machine-do';
import { ControlDO } from '../src/control-do';
import { AccountDO } from '../src/account-do';
import { MailboxDO } from '../src/mailbox-do';
export { AccountDO,MailboxDO,ControlDO };
export class MailboxFixture extends MailboxDO{
 async fetch(request:Request):Promise<Response>{
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
 const url=new URL(request.url),match=url.pathname.match(/^\/(accounts|mailboxes|control|machines)\/([^/]+)(\/.*)$/);
 if(!match)return new Response('not found',{status:404});
 const id=decodeURIComponent(match[2]),namespace=match[1]==='accounts'?env.ACCOUNTS:match[1]==='control'?env.CONTROL:match[1]==='machines'?env.MACHINES:env.MAILBOXES;
 const headers=new Headers(request.headers);const actor=JSON.parse(headers.get('x-hub2-actor')||'{}');if(actor.kind==='machine')headers.set('x-hub2-delivery-session','session-test');if(match[1]==='mailboxes')headers.set('x-hub2-mailbox',id);if(match[1]==='accounts')headers.set('x-hub2-account-id',id);
 return namespace.get(namespace.idFromName(match[1]==='mailboxes'?'global':id)).fetch(new Request('https://fixture.internal'+match[3]+url.search,{method:request.method,headers,body:request.body,duplex:'half'} as RequestInit));
}};
