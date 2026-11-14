import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync,spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {chmodSync,mkdirSync,mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {NodeSqlStorage,NodeDurableStorage} from '../node/sqlite-storage.ts';
import {ProjectDO} from '../src/project-do';
import {ControlDO} from '../src/control-do';
import worker from '../src/worker';

const admin={id:'human:cli-node-owner',kind:'operator',scopes:['operator:admin']};
const criteria=['The feature is implemented','The PR is merged with green checks (t2b)'];
test('real Hub host CLI writes merge evidence and session-finished events through Node ProjectDO',async()=>{
  const projectRoot=resolve(fileURLToPath(new URL('../..',import.meta.url)));
  const root=join(projectRoot,'.local','tmp');mkdirSync(root,{recursive:true});
  const directory=mkdtempSync(join(root,'completion-cli-node-'));
  const repo=join(directory,'repo'),bin=join(directory,'bin');mkdirSync(repo);mkdirSync(bin);
  const git=(...args:string[])=>execFileSync('git',args,{cwd:repo,encoding:'utf8',stdio:'pipe'}).trim();
  git('init');git('config','user.name','Hub CLI Test');git('config','user.email','hub-cli-test@example.invalid');
  writeFileSync(join(repo,'proof.txt'),'temporary merge-check fixture\n');git('add','proof.txt');git('commit','-m','fixture commit');git('branch','-M','main');
  const head=git('rev-parse','HEAD');git('update-ref','refs/remotes/origin/main',head);git('remote','add','origin','https://github.com/org/cli-node.git');
  const actualGit=execFileSync('which',['git'],{encoding:'utf8'}).trim();
  const gitWrapper=join(bin,'git');
  writeFileSync(gitWrapper,`#!/bin/sh\nif [ "$1" = "fetch" ] && [ "$2" = "--quiet" ] && [ "$3" = "origin" ] && [ "$4" = "main" ]; then exit 0; fi\nexec ${actualGit} "$@"\n`);
  chmodSync(gitWrapper,0o700);
  const pull=JSON.stringify({url:'https://github.com/org/cli-node/pull/23',state:'MERGED',baseRefName:'main',headRefOid:head,
    mergedAt:'2026-11-13T10:00:00Z',mergeCommit:{oid:head}});
  const checks=JSON.stringify([{check_runs:[{status:'completed',conclusion:'success',name:'CI'}]}]);
  const ghSource=`#!/usr/bin/env python3\nimport sys\na=sys.argv[1:]\nif a[:2] == ["pr", "view"]: print(${JSON.stringify(pull)})\nelif a[:3] == ["api", "--paginate", "--slurp"]: print(${JSON.stringify(checks)})\nelif a[0] == "api" and a[-1].endswith("/status"): print('{"state":"success","statuses":[]}')\nelse: sys.exit(2)\n`;
  const gh=join(bin,'gh');writeFileSync(gh,ghSource);chmodSync(gh,0o700);
  const projectSql=new NodeSqlStorage(join(directory,'project.sqlite'));
  const controlSql=new NodeSqlStorage(join(directory,'control.sqlite'));
  const candidates=[{id:'codex-sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true}];
  const namespace=(handle:(id:string,request:Request)=>Promise<Response>)=>({idFromName:(id:string)=>id,
    get:(id:string)=>({fetch:(input:string|Request,init?:RequestInit)=>handle(id,input instanceof Request?input:new Request(input,init))})});
  let control:ControlDO,project:ProjectDO;
  const publicToken='synthetic-completion-operator-token-000000000';
  const workerActors=new Map<string,any>();
  let publicCalls=0;
  const env:any={
    CONTROL:namespace(async(_id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/_verify'){
        const body=await r.json() as any,actor=workerActors.get(body.token);
        return actor?Response.json({actor}):Response.json({error:'unauthorized'},{status:401});
      }
      if(path.startsWith('/api/repos/'))return Response.json({repo:{canonical_name:'org/cli-node',status:'active'}});
      if(path.startsWith('/api/routes/intake/')){
        const id=decodeURIComponent(path.slice('/api/routes/intake/'.length));
        return (project as any).db.one('SELECT id FROM intakes WHERE id=?',id)
          ?Response.json({route:{id,owner:'org/cli-node'}}):Response.json({error:'route_not_found'},{status:404});
      }
      if(path==='/_internal/picker/draw')return control.fetch(r);
      if(path==='/_routing-policy')return Response.json({version:1,candidates});
      return Response.json({registered:true,accepted:true});
    }),
    MACHINES:namespace(async(id,r)=>{
      if(new URL(r.url).pathname==='/_internal/routing')return Response.json({machine_id:id,connected:true,engines:['codex'],capabilities:['headless']});
      return Response.json({state:'staged',epoch:1});
    }),
    ACCOUNTS:namespace(async()=>Response.json({account:{enabled:true,min_remaining:0},credential_usable:true,credential_status:'ready',quota:{remaining:80,captured_at:Date.now(),valid_until:Date.now()+900000},fresh:true,hosts:['h1']})),
    MAILBOXES:namespace(async()=>Response.json({message:{id:'synthetic-message'}},{status:201})),
  };
  control=new ControlDO({storage:new NodeDurableStorage(controlSql),blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);
  project=new ProjectDO({id:{toString:()=> 'PROJECTS/org/cli-node'},storage:new NodeDurableStorage(projectSql),
    blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);
  env.HUB2_DEV_OPERATOR_TOKEN=publicToken;
  env.PROJECTS=namespace(async(_id,r)=>project.fetch(r));
  const server=createServer(async(req,res)=>{
    try{
      const chunks:Buffer[]=[];for await(const chunk of req)chunks.push(Buffer.from(chunk));
      const headers=new Headers();
      for(const [key,value] of Object.entries(req.headers))if(typeof value==='string')headers.set(key,value);
      publicCalls++;
      const method=req.method??'GET',raw=Buffer.concat(chunks);
      const response=await worker.fetch(new Request('https://hub.test'+(req.url??'/'),{method,headers,...(raw.length?{body:raw}: {})}),env);
      const body=new Uint8Array(await response.arrayBuffer());
      res.writeHead(response.status,Object.fromEntries(response.headers));res.end(body);
    }catch(error){res.writeHead(500,{'content-type':'application/json'});res.end(JSON.stringify({error:String(error)}));}
  });
  const listen=()=>new Promise<number>((resolveListen,reject)=>{
    server.once('error',reject);server.listen(0,'127.0.0.1',()=>resolveListen((server.address() as {port:number}).port));
  });
  const closeServer=()=>new Promise<void>(resolveClose=>server.close(()=>resolveClose()));
  try{
    const port=await listen();
    const created=await project.fetch(new Request('https://project.test/api/intakes?project=org/cli-node',{method:'POST',
      headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(admin)},body:JSON.stringify({key:'create-cli-node',title:'CLI to Node',
        text:'Prove the Python CLI reaches the Node intake API.',acceptance_criteria:criteria})}));
    assert.equal(created.status,201);const id=(await created.json() as any).intake.id as string;
    const started=await project.fetch(new Request('https://project.test/api/intakes/'+id+'/start',{method:'POST',
      headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(admin)},body:JSON.stringify({key:'start-cli-node',workspace:'/srv/cli-node',prompt:'Implement and prove the CLI route.'})}));
    assert([201,202].includes(started.status));const sessionId=(await started.json() as any).session.id as string;

    const config=join(directory,'hub.json'),finishedFile=join(directory,'finished.json');
    writeFileSync(config,JSON.stringify({server:`http://127.0.0.1:${port}`,token:publicToken,project:'org/cli-node'}));
    writeFileSync(finishedFile,JSON.stringify({kind:'session_finished',session_id:sessionId,summary:'CLI event delivered to Hub.',
      acceptance:criteria.map((criterion,index)=>({status:'met',proof:`${criterion}; proof ${index+1}`}))}));
    const workingRepo=projectRoot,pythonPath=join(projectRoot,'hub2','host');
    const invoke=(...args:string[])=>new Promise<{status:number|null;stdout:string;stderr:string}>((resolveInvoke,rejectInvoke)=>{
      const child=spawn('python3',['-B','-m','agent_hub_host.cli','--config',config,...args],{
        cwd:workingRepo,env:{PATH:bin+':'+(process.env.PATH??'/usr/bin:/bin'),PYTHONPATH:pythonPath,PYTHONDONTWRITEBYTECODE:'1'},
      });
      let stdout='',stderr='';
      child.stdout.setEncoding('utf8').on('data',chunk=>{stdout+=chunk;});
      child.stderr.setEncoding('utf8').on('data',chunk=>{stderr+=chunk;});
      const timer=setTimeout(()=>child.kill('SIGKILL'),15000);
      child.once('error',error=>{clearTimeout(timer);rejectInvoke(error);});
      child.once('close',(status)=>{clearTimeout(timer);resolveInvoke({status,stdout,stderr});});
    });
    const merge=await invoke('intake','merge-check',id,'--project','org/cli-node','--key','merge-cli-node','--repo',repo,'--pr','23','--json');
    assert.equal(merge.status,0,merge.stderr);
    const evidenceRow=(project as any).db.one('SELECT body_json FROM intake_events WHERE task_id=? AND json_extract(body_json,\'$.evidence.kind\')=\'merge_gate\'',id);
    const collected=JSON.parse(evidenceRow.body_json).evidence;
    assert.equal(collected.repository,'org/cli-node');assert.equal(collected.head_sha,head);assert.equal(collected.checks_head_sha,head);
    assert.equal(collected.checks_state,'success');assert.equal(collected.merge_commit_sha,head);assert.equal(collected.merge_commit_in_main,true);

    // Shipped operator CLI is a different parser from the small daemon CLI.
    // Reach the REAL public Worker and REAL ProjectDO, never an HTTP status stub.
    const operatorConfig=join(directory,'operator.json');
    const operatorInvoke=(token:string,...args:string[])=>new Promise<{status:number|null;stdout:string;stderr:string}>((done,reject)=>{
      writeFileSync(operatorConfig,JSON.stringify({base_url:`http://127.0.0.1:${port}`,access_token:token}),{mode:0o600});
      const child=spawn('python3',['-B','-m','agent_hub_host.cli',...args],{
        cwd:workingRepo,env:{PATH:bin+':'+(process.env.PATH??'/usr/bin:/bin'),PYTHONPATH:join(projectRoot,'host'),
          HOME:directory,TMPDIR:directory,HUB_CONFIG_FILE:operatorConfig,HUB_CLI_SKIP_STALE_CHECK:'1',PYTHONDONTWRITEBYTECODE:'1'},
      });
      let stdout='',stderr='';child.stdout.setEncoding('utf8').on('data',chunk=>{stdout+=chunk;});
      child.stderr.setEncoding('utf8').on('data',chunk=>{stderr+=chunk;});
      const timer=setTimeout(()=>child.kill('SIGKILL'),15000);
      child.once('error',error=>{clearTimeout(timer);reject(error);});
      child.once('close',status=>{clearTimeout(timer);done({status,stdout,stderr});});
    });
    const ownToken='synthetic-current-worker-token-000000000';
    const foreignToken='synthetic-other-worker-token-000000000';
    const noScopeToken='synthetic-no-scope-worker-token-000000000';
    const hostToken='synthetic-machine-token-for-finish-000000000';
    const foreignSession='99999999-9999-4999-8999-999999999999';
    workerActors.set(ownToken,{id:'session:'+sessionId,kind:'agent',resource_id:id,scopes:['intake:append']});
    workerActors.set(foreignToken,{id:'session:'+foreignSession,kind:'agent',resource_id:id,scopes:['intake:append']});
    workerActors.set(noScopeToken,{id:'session:'+sessionId,kind:'agent',resource_id:id,scopes:[]});
    workerActors.set(hostToken,{id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['machine:finish']});
    const count=()=>Number((project as any).db.one('SELECT COUNT(*) AS n FROM intake_events WHERE task_id=?',id).n);
    const claim={kind:'session_finished',session_id:sessionId,summary:'Actual CLI completion claim, not native retirement.',
      acceptance:criteria.map(criterion=>({status:'met',proof:criterion+' synthetic proof'}))};
    const callFinish=(token:string,key:string)=>operatorInvoke(token,'sessions','finish',sessionId,
      '--intake-task-id',id,'--key',key,'--file',finishedFile,'--json');
    writeFileSync(finishedFile,JSON.stringify(claim));
    const beforeClaims=count();
    for(const [token,label] of [[foreignToken,'foreign-session'],[noScopeToken,'missing-scope'],[hostToken,'host-only']]){
      const refused=await callFinish(token,'deny-'+label);assert.notEqual(refused.status,0,label);
      assert.match(refused.stderr,/403|forbidden/i,label);assert.equal(count(),beforeClaims);
    }
    const publicPost=async(path:string,body:any,token=ownToken,method='POST')=>fetch(`http://127.0.0.1:${port}`+path,{
      method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(method==='POST'?{body:JSON.stringify(body)}:{})});
    const endpoint='/api/intakes/'+id+'/finished';
    for(const field of ['native_exit_confirmed','verification','host_actor']){
      const bad={...claim,key:'bad-'+field,[field]:true};
      assert.equal((await publicPost(endpoint,bad)).status,400,field);assert.equal(count(),beforeClaims);
      const beforeHttp=publicCalls;writeFileSync(finishedFile,JSON.stringify({...claim,[field]:true}));
      assert.notEqual((await callFinish(ownToken,'cli-bad-'+field)).status,0);
      assert.equal(publicCalls,beforeHttp,'malformed claim refused before HTTP');
    }
    writeFileSync(finishedFile,JSON.stringify({...claim,session_id:foreignSession}));
    const beforeMismatch=publicCalls;assert.notEqual((await callFinish(ownToken,'cli-wrong-session')).status,0);
    assert.equal(publicCalls,beforeMismatch);assert.equal(count(),beforeClaims);
    writeFileSync(finishedFile,JSON.stringify({...claim,acceptance:claim.acceptance.slice(0,1)}));
    const incomplete=await callFinish(ownToken,'incomplete-criteria');assert.notEqual(incomplete.status,0);
    assert.match(incomplete.stderr,/400|invalid_session_finished/i);assert.equal(count(),beforeClaims);
    writeFileSync(finishedFile,JSON.stringify(claim));
    const sessionBefore=(project as any).db.one('SELECT state FROM dispatch_sagas WHERE session_id=?',sessionId).state;
    const recorded=await callFinish(ownToken,'operator-cli-current-claim');assert.equal(recorded.status,0,recorded.stderr);
    assert.equal(count(),beforeClaims+1);
    const replay=await callFinish(ownToken,'operator-cli-current-claim');assert.equal(replay.status,0,replay.stderr);
    assert.equal(count(),beforeClaims+1,'same-key claim replay never duplicates');
    assert.equal((project as any).db.one('SELECT state FROM dispatch_sagas WHERE session_id=?',sessionId).state,sessionBefore,
      'claim does not fabricate native/host completion');
    const alias=await publicPost('/api/intake/tasks/'+id+'/finished',{...claim,key:'alias-claim'});
    assert.equal(alias.status,201);assert.equal(count(),beforeClaims+2,'both public aliases reach guarded claim handler');
    assert.equal((await publicPost(endpoint,undefined,ownToken,'GET')).status,404,'GET cannot record completion');
    assert.equal(count(),beforeClaims+2);
    (project as any).db.run('UPDATE intakes SET session_id=? WHERE id=?',foreignSession,id);
    const stale=await callFinish(ownToken,'stale-claim');assert.notEqual(stale.status,0);assert.match(stale.stderr,/409|stale/i);
    assert.equal(count(),beforeClaims+2);
    (project as any).db.run('UPDATE intakes SET session_id=? WHERE id=?',sessionId,id);
    writeFileSync(finishedFile,JSON.stringify(claim));

    const finish=await invoke('sessions','finish',sessionId,'--intake-task-id',id,'--key','finish-cli-node','--file',finishedFile,'--json');
    assert.equal(finish.status,0,finish.stderr);
    const events=(project as any).db.all('SELECT body_json FROM intake_events WHERE task_id=? AND kind=\'worker_append\' ORDER BY seq',id);
    assert(events.some((event:any)=>JSON.parse(event.body_json).evidence?.kind==='session_finished'));
    assert(events.some((event:any)=>JSON.parse(event.body_json).evidence?.kind==='merge_gate'));
  }finally{
    await closeServer();projectSql.close();controlSql.close();rmSync(directory,{recursive:true,force:true});
  }
});
