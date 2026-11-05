// Local-only composition. Never deploy: fixture routes seed invented records only.
import production,{Hub,ProjectDO as ProductionProject,ControlDO as ProductionControl,MachineDO as ProductionMachine,AccountDO as ProductionAccount,MailboxDO as ProductionMailbox,BudgetDO} from '../production-worker';
import {fixtureBudgetEnvelopes} from './budget-fixture';
export {BudgetDO};
const fixtureEnv=(env:any)=>({...env,HUB2_BUDGET_ENVELOPES:fixtureBudgetEnvelopes});
export class ControlDO extends ProductionControl{constructor(ctx:any,env:any){super(ctx,fixtureEnv(env));}}
export class MachineDO extends ProductionMachine{constructor(ctx:any,env:any){super(ctx,fixtureEnv(env));}}
export class AccountDO extends ProductionAccount{constructor(ctx:any,env:any){super(ctx,fixtureEnv(env));}}
export class MailboxDO extends ProductionMailbox{constructor(ctx:any,env:any){super(ctx,fixtureEnv(env));}}
export class ProjectDO extends ProductionProject{
 constructor(private fixtureCtx:DurableObjectState,env:any){super(fixtureCtx,fixtureEnv(env));}
 async fetch(request:Request):Promise<Response>{
  if(new URL(request.url).pathname==='/_fixture/check')return Response.json({
   intakes:this.fixtureCtx.storage.sql.exec('SELECT COUNT(*) AS n FROM intakes').one().n,
   sagas:this.fixtureCtx.storage.sql.exec('SELECT COUNT(*) AS n FROM dispatch_sagas').one().n,
   events:this.fixtureCtx.storage.sql.exec('SELECT COUNT(*) AS n FROM intake_events').one().n,
   alarm:await this.fixtureCtx.storage.getAlarm()});
  return super.fetch(request);
 }
}
export class LegacyHub extends Hub{
 constructor(private fixtureCtx:DurableObjectState,private fixtureBindings:any){super(fixtureCtx,fixtureBindings);}
 async fetch(request:Request):Promise<Response>{
  if(new URL(request.url).pathname==='/_fixture/seed'){
   const payload=await request.json() as any,records=payload.records as any[],sql=this.fixtureCtx.storage.sql;
   this.fixtureCtx.storage.transactionSync(()=>{
    sql.exec('CREATE TABLE intake_tasks(id TEXT PRIMARY KEY,data TEXT)');
    sql.exec('CREATE TABLE intake_task_projection(id TEXT PRIMARY KEY,links TEXT) WITHOUT ROWID');
    sql.exec('CREATE TABLE intake_link_refs(ref TEXT,task_id TEXT,PRIMARY KEY(ref,task_id)) WITHOUT ROWID');
    sql.exec('CREATE TABLE intake_note_archive(id TEXT PRIMARY KEY,task_id TEXT,seq INTEGER,data TEXT,UNIQUE(task_id,seq))');
    for(const record of records){
     sql.exec('INSERT INTO intake_tasks VALUES(?,?)',record.id,JSON.stringify(record));
     sql.exec('INSERT INTO intake_task_projection VALUES(?,?)',record.id,'[]');
    }
    for(const row of payload.archives||[])sql.exec('INSERT INTO intake_note_archive VALUES(?,?,?,?)',row.id,row.task_id,row.seq,JSON.stringify(row.event));
   });
   return Response.json({seeded:records.length});
  }
  if(new URL(request.url).pathname==='/_fixture/mutate-same-count'){
   const sql=this.fixtureCtx.storage.sql,before=sql.exec<{n:number}>('SELECT COUNT(*) AS n FROM intake_task_projection').one().n;
   sql.exec('UPDATE intake_task_projection SET links=? WHERE id=?','[{"kind":"link","native_id":"same-count-mutation"}]','11111111-2222-4333-8444-555555555551');
   const after=sql.exec<{n:number}>('SELECT COUNT(*) AS n FROM intake_task_projection').one().n;
   return Response.json({before,after,content_changed:true});
  }
  return super.fetch(request);
 }
}
export default{async fetch(request:Request,env:any):Promise<Response>{
 const url=new URL(request.url);
 if(url.pathname.startsWith('/_fixture/')){
  if(request.headers.get('Authorization')!=='Bearer '+env.HUB2_DEV_OPERATOR_TOKEN)return new Response(null,{status:401});
  if(url.pathname==='/_fixture/seed')return env.HUB.get(env.HUB.idFromName('hub')).fetch(request);
  if(url.pathname==='/_fixture/mutate-same-count')return env.HUB.get(env.HUB.idFromName('hub')).fetch(request);
  if(url.pathname==='/_fixture/check')return env.PROJECTS.get(env.PROJECTS.idFromName('synthetic/project')).fetch(request);
  if(url.pathname.startsWith('/_fixture/public/')&&(request.method==='GET'||request.method==='PATCH'&&url.pathname.startsWith('/_fixture/public/api/repos/'))){
   url.pathname=url.pathname.replace('/_fixture/public','');
   return production.fetch(new Request(url,request),fixtureEnv({...env,HUB2_RELEASE_MODE:'active'}));
  }
  return new Response(null,{status:404});
 }
 return production.fetch(request,fixtureEnv(env));
}};
