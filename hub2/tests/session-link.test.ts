import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {MeteredDb} from '../src/db';
import {SessionIntakeLink} from '../src/session-intake-link';

const admin={id:'human:test',kind:'operator',scopes:['operator:admin']};
function fixture(){
 const sql=new DatabaseSync(':memory:');
 sql.exec(`CREATE TABLE intakes(id TEXT PRIMARY KEY,project TEXT,session_id TEXT,updated_at INTEGER);
 CREATE TABLE intake_events(task_id TEXT,seq INTEGER,event_key TEXT UNIQUE,actor_id TEXT,kind TEXT,body_json TEXT,at INTEGER,PRIMARY KEY(task_id,seq));
 INSERT INTO intakes VALUES('target','org/repo',NULL,1),('other','org/repo',NULL,1),('foreign','elsewhere',NULL,1),('assigned','org/repo','another-session',1);`);
 const db=new MeteredDb({exec(q:string,...args:any[]){const p=sql.prepare(q);return {toArray:()=>p.columns().length?p.all(...args):(p.run(...args),[])};}} as any);
 const transaction=<T>(fn:()=>T):T=>{sql.exec('BEGIN');try{const value=fn();sql.exec('COMMIT');return value;}catch(e){sql.exec('ROLLBACK');throw e;}};
 const links=new SessionIntakeLink(db,transaction,(id,key,actor,kind,body,at)=>{
  db.run('INSERT INTO intake_events VALUES(?,(SELECT COALESCE(MAX(seq),0)+1 FROM intake_events WHERE task_id=?),?,?,?,?,?)',id,id,key,actor,kind,JSON.stringify(body),at);
 });
 const payload={key:'link-key',intake_task_id:'target',session_id:'session',machine_id:'host',project_id:'org/repo',primary_intake_id:'primary',native_id:'native'};
 const call=(body:any=payload,actor:any=admin)=>links.receive(new Request('https://project.test/_internal/session-link',{method:'POST',headers:{'x-hub2-actor':JSON.stringify(actor)},body:JSON.stringify(body)}));
 return {sql,links,payload,call};
}
test('secondary linkage persists, repeats once and refuses different linkage',async()=>{
 const f=fixture();
 assert.equal((await f.call()).status,200);
 assert.equal((await (await f.call()).json() as any).already_linked,true);
 assert.equal((await (await f.call({...f.payload,key:'another-key'})).json() as any).already_linked,true);
 assert.deepEqual(f.links.links('target').map(v=>[v.session_id,v.primary_intake_id]),[['session','primary']]);
 assert.equal((await f.call({...f.payload,intake_task_id:'other'})).status,409);
 assert.equal((await f.call({...f.payload,session_id:'other-session'})).status,409);
 assert.equal((f.sql.prepare('SELECT count(*) AS n FROM intake_events').get() as any).n,1);
 assert.equal((f.sql.prepare("SELECT session_id FROM intakes WHERE id='target'").get() as any).session_id,null);
 f.sql.close();
});
test('linkage rejects unprivileged callers, bad input, foreign and assigned targets',async()=>{
 const f=fixture();
 for(const actor of [{id:'host',kind:'machine',scopes:['session:report']},{id:'agent',kind:'agent',scopes:['intake:read','intake:append'],resource_id:'target'}])assert.equal((await f.call(f.payload,actor)).status,403);
 for(const change of [{key:''},{key:'space key'},{native_id:{}},{unexpected:true}])assert.equal((await f.call({...f.payload,...change})).status,400);
 for(const target of ['missing','foreign','assigned'])assert.equal((await f.call({...f.payload,intake_task_id:target})).status,409);
 assert.equal(f.links.links('target').length,0);f.sql.close();
});

test('machine-side linkage resolves intake prefixes and preserves the launch binding',async()=>{
 const {linkSession}=await import('../src/session-intake-link');
 const f=fixture(),calls:string[]=[];
 const env:any={CONTROL:{idFromName:(id:string)=>{assert.equal(id,'global');return id;},get:()=>({fetch:async(request:Request)=>{
  calls.push(new URL(request.url).pathname);
  return Response.json({route:{id:'target',owner:'org/repo',resource_id:'org/repo'}});
 }})},PROJECTS:{idFromName:(id:string)=>{assert.equal(id,'org/repo');return id;},get:()=>({fetch:(request:Request)=>f.links.receive(request)})}};
 const session={id:'session',project_id:'org/repo',intake_id:'primary',machine_id:'host',native_id:'native'};
 const call=(body:any={key:'link-key',intake_task_id:'target'},actor:any=admin,row:any=session)=>linkSession(new Request('https://m.test/api/sessions/session/link',{method:'POST',headers:{'x-hub2-actor':JSON.stringify(actor)},body:JSON.stringify(body)}),row,env);
 assert.equal((await call()).status,200);
 assert.equal((await (await call()).json() as any).already_linked,true);
 assert.equal(session.intake_id,'primary');
 assert.equal((await call({key:'key',intake_task_id:'bad ref'})).status,400);
 assert.equal((await call(undefined,{id:'host',kind:'machine',scopes:['operator:admin']})).status,403);
 assert.deepEqual(calls,['/api/routes/intake/target','/api/routes/intake/target']);
 f.sql.close();
});

import {MachineDO} from '../src/machine-do';
import {ProjectDO} from '../src/project-do';
import worker from '../src/worker';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {mkdtemp,writeFile,rm,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
const cliRoot=fileURLToPath(new URL('../../',import.meta.url));
const token='synthetic-session-link-token-0000000000';
const routeAdmin={id:'human:dev',kind:'operator',scopes:['operator:admin']};
const sessionId='cb75b9bc-27f7-589a-a5e0-75f6cd6ebf60';
function context(){
 const sql=new DatabaseSync(':memory:');let alarm:number|null=null;
 const ctx:any={storage:{sql:{exec(q:string,...args:any[]){const s=sql.prepare(q);return {toArray:()=>s.columns().length?s.all(...args):(s.run(...args),[])};}},transactionSync(fn:()=>unknown){sql.exec('BEGIN');try{const value=fn();sql.exec('COMMIT');return value;}catch(e){sql.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn()),getWebSockets:()=>[]};
 return {ctx,sql};
}
test('official public route links a second intake through real stores and a hermetic CLI child',async()=>{
 const m=context(),p=context();let machine:MachineDO,project:ProjectDO;
 const routes=new Map<string,{id:string;owner:string;resource_id:string}>();
 const control={idFromName:(id:string)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{
  const req=input instanceof Request?input:new Request(input,init),path=new URL(req.url).pathname;
  if(path==='/_actors/register'||path==='/_routes/register')return Response.json({registered:true});
  if(path==='/api/repos/org%2Frepo')return Response.json({repo:{canonical_name:'org/repo',status:'active'}});
  const lookup=path.match(/^\/api\/routes\/(intake|session)\/([^/]+)$/);
  if(lookup){const route=routes.get(lookup[1]+':'+decodeURIComponent(lookup[2]));return route?Response.json({route}):Response.json({error:'route_not_found'},{status:404});}
  return Response.json({error:'unexpected_control_call'},{status:500});
 }})};
 const env:any={CONTROL:control,MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:(request:Request)=>machine.fetch(request)})},PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:(request:Request)=>project.fetch(request)})},HUB2_DEV_OPERATOR_TOKEN:token};
 project=new ProjectDO(p.ctx,env);machine=new MachineDO(m.ctx,env);
 const headers={'x-hub2-actor':JSON.stringify(routeAdmin),'Content-Type':'application/json'};
 const direct=(obj:any,path:string,body:any)=>obj.fetch(new Request('https://internal.test'+path,{method:'POST',headers,body:JSON.stringify(body)}));
 const create=await direct(project,'/api/intakes?project=org/repo',{key:'target',title:'Related ask',text:'Shared implementation',acceptance_criteria:['Linked']});
 assert.equal(create.status,201,await create.clone().text());const target=(await create.json() as any).intake.id;
 routes.set('intake:'+target,{id:target,owner:'org/repo',resource_id:'org/repo'});
 routes.set('intake:'+target.slice(0,8),{id:target,owner:'org/repo',resource_id:'org/repo'});
 routes.set('session:'+sessionId,{id:sessionId,owner:'host',resource_id:'org/repo'});
 const reserve=await direct(machine,'/_internal/reserve',{operation_id:'op',dispatch_id:'dispatch',attempt:0,session_id:sessionId,project_id:'org/repo',intake_id:'primary-intake',machine_id:'host',request_digest:'digest',title:'Task',acceptance_criteria:['Done'],launch:{workspace:'/work/import',engine:'codex',model:'gpt-6.1-sol',prompt:'Work',interactive:true}});
 assert.equal(reserve.status,201,await reserve.clone().text());
 const call=(body:any={key:'official-link',intake_task_id:target})=>worker.fetch(new Request('https://hub.test/api/sessions/'+sessionId+'/link',{method:'POST',headers:{Authorization:'Bearer '+token,Origin:'https://hub.test','Content-Type':'application/json'},body:JSON.stringify(body)}),env);
 const first=await call();assert.equal(first.status,200,await first.clone().text());
 assert.equal((await first.json() as any).link.session_id,sessionId);
 assert.equal((await (await call({key:'official-link',intake_task_id:target.slice(0,8)})).json() as any).already_linked,true);
 const show=await project.fetch(new Request('https://p.test/api/intakes/'+target,{headers}));
 const intake=(await show.json() as any).intake;
 assert.equal(intake.linked_sessions[0].session_id,sessionId);
 assert.equal(intake.linked_sessions[0].primary_intake_id,'primary-intake');
 assert.equal((m.sql.prepare('SELECT intake_id FROM sessions').get() as any).intake_id,'primary-intake');
 assert.equal((m.sql.prepare('SELECT count(*) AS n FROM session_commands').get() as any).n,0);
 // A normal append after linkage must use the next Project event sequence.
 const append=await direct(project,'/api/intakes/'+target+'/append',{key:'after-link',note:'Following event'});
 assert.equal(append.status,201,await append.clone().text());
 assert.equal((await call()).status,200);
 // Reopening the same durable databases must retain the official linkage.
 project=new ProjectDO(p.ctx,env);machine=new MachineDO(m.ctx,env);
 assert.equal((await (await call()).json() as any).already_linked,true);
 // Exercise the maintained CLI against an actual HTTP listener as well.
 const server=createServer(async(req,res)=>{
  try{
   const parts:Buffer[]=[];for await(const part of req)parts.push(Buffer.from(part));
   const request=new Request('http://127.0.0.1'+req.url,{method:req.method,headers:req.headers as any,...(parts.length?{body:Buffer.concat(parts)}:{})});
   const response=await worker.fetch(request,env);res.writeHead(response.status,Object.fromEntries(response.headers));res.end(await response.text());
  }catch{res.writeHead(500);res.end('{"error":"fixture_error"}');}
 });
 const tempRoot=resolve(process.env.TMPDIR||resolve(cliRoot,'.local/tmp'));await mkdir(tempRoot,{recursive:true});
 const scratch=await mkdtemp(resolve(tempRoot,'session-link-cli-'));
 try{
  await new Promise<void>(done=>server.listen(0,'127.0.0.1',done));
  const address=server.address() as {port:number};
  const config=resolve(scratch,'cli.json');
  await writeFile(config,JSON.stringify({client_kind:'human',access_token:token,base_url:'http://127.0.0.1:'+address.port}),{mode:0o600});
  const cli=async(parentEnv:typeof process.env,...args:string[])=>{
  const child=spawn('python3',['-m','agent_hub_host.cli',...args],{
   cwd:cliRoot,env:{PATH:parentEnv.PATH||'/usr/bin:/bin',HOME:scratch,XDG_CONFIG_HOME:scratch,TMPDIR:tempRoot,HUB_CONFIG_FILE:config,PYTHONPATH:resolve(cliRoot,'host')},stdio:['ignore','pipe','pipe'],
  });
  let stdout='',stderr='';child.stdout.on('data',v=>stdout+=v);child.stderr.on('data',v=>stderr+=v);
  const timeout=setTimeout(()=>child.kill(),15000);
  try{
   const code=await new Promise<number|null>((done,reject)=>{child.once('error',reject);child.once('close',done);});
   assert.equal(code,0,stderr);return JSON.parse(stdout);
  }finally{clearTimeout(timeout);}
  };
  // A synthetic polluted parent must not redirect the child away from its fixture or supply auth.
  const pollutedParentEnv={...process.env,HUB_URL:'https://ambient.invalid',HUB_BASE_URL:'https://ambient-base.invalid',HUB_TOKEN:'ambient-user-token',HUB_JOB_TOKEN:'ambient-job-token'};
  assert.equal((await cli(pollutedParentEnv,'sessions','link',sessionId,'--intake-task-id',target,'--key','official-cli-link')).link.session_id,sessionId);
  assert.equal((await cli(pollutedParentEnv,'intake','show',target)).intake.linked_sessions[0].session_id,sessionId);
 }finally{
  await new Promise<void>(done=>server.close(()=>done()));await rm(scratch,{recursive:true,force:true});
 }
 m.sql.close();p.sql.close();
});
