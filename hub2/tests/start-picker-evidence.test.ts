import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {NodeSqlStorage,NodeDurableStorage} from '../node/sqlite-storage.ts';
import {ProjectDO} from '../src/project-do';
import {ControlDO} from '../src/control-do';
import {buildStartPickerEvidence,normalizeStartPickerEvidence,MAX_PICKER_BYTES} from '../src/start-picker-evidence';
import type {PickedRoute,PickDiagnostics} from '../src/model-picker';

const admin={id:'human:picker-test',kind:'operator',scopes:['operator:admin']};
const candidates=[
  {id:'sol-route',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex-private',weight:1,capabilities:[],enabled:true},
  {id:'sonnet-route',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude-private',weight:1,capabilities:[],enabled:true},
];
const parsed=async(response:Response)=>await response.json() as any;

function fixture(){
  const parent=resolve('.local/tmp');mkdirSync(parent,{recursive:true});
  const directory=mkdtempSync(join(parent,'picker-evidence-')),file=join(directory,'project.sqlite');
  const controlSql=new NodeSqlStorage(join(directory,'control.sqlite'));
  let control:ControlDO;
  let sql:NodeSqlStorage,project:ProjectDO,policy=structuredClone(candidates),version=1,policyReads=0;
  let engines=['codex','claude'];
  const accountHosts=new Map<string,string[]>([['codex-private',['h1']],['claude-private',['h2']]]);
  let policyGate:{entered:()=>void;wait:Promise<void>}|undefined;
  const reservations:any[]=[];
  const namespace=(handle:(id:string,request:Request)=>Promise<Response>)=>({idFromName:(id:string)=>id,
    get:(id:string)=>({fetch:(input:string|Request,init?:RequestInit)=>handle(id,input instanceof Request?input:new Request(input,init))})});
  const env:any={
    CONTROL:namespace(async(_id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/_internal/picker/draw')return control.fetch(r);
      if(path==='/_routing-policy'){
        policyReads++;
        const gate=policyGate;policyGate=undefined;
        if(gate){gate.entered();await gate.wait;}
        return Response.json({version,candidates:policy});
      }
      if(path==='/api/directory')return Response.json({items:[{id:'h1'},{id:'h2'}],next:null});
      return Response.json({registered:true,accepted:true});
    }),
    MACHINES:namespace(async(id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/_internal/routing')return Response.json({machine_id:id,connected:true,engines,capabilities:['headless']});
      const body=await r.json() as any;
      if(path==='/_internal/reserve'){
        reservations.push(body);
        assert(new TextEncoder().encode(JSON.stringify(body.launch)).length<=12_000);
        assert.equal(body.launch.picker,undefined,'evidence is not part of host launch input');
        assert.equal(body.launch._hub_picker,undefined);
        return Response.json({state:'staged',epoch:body.attempt+1});
      }
      return Response.json({state:'offered'});
    }),
    ACCOUNTS:namespace(async(id)=>Response.json({account:{enabled:true,min_remaining:0},credential_usable:true,
      credential_status:'ready',quota:{remaining:80},hosts:accountHosts.get(id)||[]})),
  };
  control=new ControlDO({storage:new NodeDurableStorage(controlSql),
    blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);
  const open=()=>{
    sql=new NodeSqlStorage(file);
    const storage=new NodeDurableStorage(sql);
    project=new ProjectDO({id:{toString:()=> 'PROJECTS/org/picker'},storage,
      blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);
  };
  open();
  const call=(path:string,method='GET',body?:unknown)=>project.fetch(new Request('https://project.test'+path,{method,
    headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(admin)},
    ...(body===undefined?{}:{body:JSON.stringify(body)})}));
  return {call,reservations,get policyReads(){return policyReads;},get sql(){return sql;},
    setPolicy(value:typeof candidates){policy=structuredClone(value);version++;},
    setEngines(value:string[]){engines=[...value];},
    setAccountHosts(account:string,hosts:string[]){accountHosts.set(account,[...hosts]);},
    pauseNextPolicy(){
      let entered!:()=>void,release!:()=>void;
      const enteredPromise=new Promise<void>(resolve=>{entered=resolve;});
      policyGate={entered,wait:new Promise<void>(resolve=>{release=resolve;})};
      return {entered:enteredPromise,release};
    },
    clearCache(){(project as any).db.cache.clear();},
    reopen(){sql.close();open();},close(){sql.close();controlSql.close();rmSync(directory,{recursive:true});}};
}
async function intake(f:ReturnType<typeof fixture>,key:string){
  const response=await f.call('/api/intakes?project=org/picker','POST',{
    key,title:'Actual picker evidence',text:'Preserve the actual selected pool',acceptance_criteria:['same draw and replay'],
  });
  assert.equal(response.status,201);return(await parsed(response)).intake;
}

test('same-draw evidence survives changed policy, replay and SQLite-backed object eviction',async()=>{
  const f=fixture(),random=Math.random;let draws=0;Math.random=()=>{draws++;return 0;};
  try{
    const own=await intake(f,'evidence-replay'),start={key:'same-start',workspace:'picker',prompt:'do work'};
    const created=await parsed(await f.call('/api/intakes/'+own.id+'/start','POST',start));
    assert(created.session.id);assert.equal(draws,1);assert.equal(f.policyReads,1);
    assert.deepEqual(created.picker,created.session.picker);
    const picker=created.picker;
    assert.equal(picker.policy_id,'sol-route');assert.equal(picker.policy_version,1);
    assert.equal(picker.machine_id,created.session.machine_id);assert.equal(picker.attempt,0);
    assert.equal(picker.pool.length,2);assert.equal(picker.history.length,1);
    assert(picker.pool.some((row:any)=>row.policy_id===picker.policy_id&&row.model===created.session.model));
    assert.deepEqual(f.reservations[0].picker,picker);
    f.setPolicy([candidates[1]]);
    const replay=await parsed(await f.call('/api/intakes/'+own.id+'/start','POST',start));
    assert.equal(replay.session.id,created.session.id);assert.deepEqual(replay.picker,picker);
    f.reopen();
    const reopened=await parsed(await f.call('/api/intakes/'+own.id+'/start','POST',start));
    assert.deepEqual(reopened.picker,picker);assert.equal(reopened.session.policy_id,picker.policy_id);
    assert.equal(draws,1);assert.equal(f.policyReads,1);
    assert.doesNotMatch(JSON.stringify(picker),/private|account_id|credential|override_reason|excluded/);
  }finally{Math.random=random;f.close();}
});

test('concurrent identical starts persist one actual draw and one immutable picker receipt',async()=>{
  const f=fixture(),random=Math.random;let draws=0;Math.random=()=>{draws++;return 0;};
  try{
    const own=await intake(f,'concurrent'),body={key:'concurrent-start',workspace:'picker'};
    const responses=await Promise.all([f.call('/api/intakes/'+own.id+'/start','POST',body),f.call('/api/intakes/'+own.id+'/start','POST',body)]);
    assert(responses.every(response=>response.ok));
    const [first,second]=await Promise.all(responses.map(parsed));
    assert.equal(first.session.id,second.session.id);assert.deepEqual(first.picker,second.picker);
    assert.equal(draws,1);assert.equal(f.policyReads,1);
    assert.equal(f.sql.exec('SELECT COUNT(*) AS n FROM dispatch_sagas').toArray()[0].n,1);
  }finally{Math.random=random;f.close();}
});

test('actual fallback records the second draw and preserves the first full pool privately',async()=>{
  const f=fixture(),random=Math.random;let draws=0;Math.random=()=>{draws++;return 0;};
  try{
    const own=await intake(f,'fallback-evidence');
    const first=await parsed(await f.call('/api/intakes/'+own.id+'/start','POST',{key:'fallback-start',workspace:'picker'}));
    const row=f.sql.exec('SELECT * FROM dispatch_sagas').toArray()[0] as any;
    const receipt={operation_id:row.operation_id,attempt_operation_id:row.attempt_operation_id,session_id:row.session_id,
      machine_id:row.machine_id,epoch:row.epoch,reason:'provider_auth_failed'};
    const response=await f.call('/_internal/auth-fallback','POST',receipt);assert.equal(response.status,200);
    assert.equal((await parsed(response)).fallback,true);
    const next=f.sql.exec('SELECT * FROM dispatch_sagas').toArray()[0] as any;
    const routing=JSON.parse(next.routing_json),picker=routing.picker;
    assert.equal(picker.policy_id,'sonnet-route');assert.equal(picker.attempt,1);
    assert.equal(picker.history.length,2);assert.deepEqual(picker.history.map((r:any)=>r.policy_id),['sol-route','sonnet-route']);
    assert.equal(picker.pool.length,1);assert.equal(picker.pool[0].share,1);
    assert.deepEqual(routing.initial_picker,first.picker);
    assert.deepEqual(f.reservations.at(-1).picker,picker);
    f.reopen();
    const shown=await parsed(await f.call('/api/intakes/'+own.id));assert.deepEqual(shown.intake.dispatch.picker,picker);
    assert.equal((await f.call('/_internal/auth-fallback','POST',receipt)).status,409);
    const exhausted=await f.call('/_internal/auth-fallback','POST',{...receipt,attempt_operation_id:next.attempt_operation_id,machine_id:next.machine_id,epoch:next.epoch});
    assert.equal(exhausted.status,200);assert.equal((await parsed(exhausted)).fallback,false);
    assert.deepEqual(JSON.parse((f.sql.exec('SELECT routing_json FROM dispatch_sagas').toArray()[0] as any).routing_json).picker,picker);
    assert.equal(draws,2);assert.equal(f.policyReads,2);
  }finally{Math.random=random;f.close();}
});

test('ambiguous workspace placement retries on a different approved host and records a visible event',async()=>{
  const f=fixture(),random=Math.random;Math.random=()=>0;
  try{
    f.setPolicy([candidates[0]]);f.setAccountHosts('codex-private',['h1','h2']);
    const own=await intake(f,'workspace-placement-fallback');
    const first=await parsed(await f.call('/api/intakes/'+own.id+'/start','POST',{
      key:'workspace-placement-start',workspace:'dataops',prompt:'verify workspace placement',
    }));
    assert.equal(first.session.machine_id,'h1');
    const saga=f.sql.exec('SELECT * FROM dispatch_sagas').toArray()[0] as any;
    const response=await f.call('/_internal/auth-fallback','POST',{
      operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,
      machine_id:saga.machine_id,epoch:saga.epoch,reason:'workspace_label_ambiguous',
    });
    assert.equal(response.status,200,JSON.stringify(await response.clone().json()));
    assert.equal((await parsed(response)).fallback,true);
    const next=f.sql.exec('SELECT * FROM dispatch_sagas').toArray()[0] as any;
    const routing=JSON.parse(next.routing_json);
    assert.equal(next.attempt,1);assert.equal(next.machine_id,'h2');
    assert.equal(routing.picker.policy_id,first.picker.policy_id);
    assert.equal(routing.picker.attempt,1);
    assert.deepEqual(routing.picker.history.map((row:any)=>row.machine_id),['h1','h2']);
    assert.deepEqual(routing.initial_picker,first.picker);
    assert.equal(f.reservations.at(-1).machine_id,'h2');
    const event=f.sql.exec('SELECT kind,body_json FROM intake_events WHERE task_id=? ORDER BY seq',own.id).toArray()
      .map((row:any)=>({...row,body:JSON.parse(row.body_json)}))
      .find((row:any)=>row.kind==='session_placement_fallback');
    assert.equal(event.body.reason,'workspace_label_ambiguous');
    assert.equal(event.body.failed_machine_id,'h1');assert.equal(event.body.next_machine_id,'h2');
    assert.equal(event.body.workspace,'dataops');
  }finally{Math.random=random;f.close();}
});

test('exhausted workspace placement fallback leaves a visible failure event',async()=>{
  const f=fixture(),random=Math.random;Math.random=()=>0;
  try{
    f.setPolicy([candidates[0]]);
    const own=await intake(f,'workspace-placement-exhausted');
    await f.call('/api/intakes/'+own.id+'/start','POST',{key:'workspace-placement-exhausted',workspace:'dataops',prompt:'verify placement'});
    const saga=f.sql.exec('SELECT * FROM dispatch_sagas').toArray()[0] as any;
    const response=await f.call('/_internal/auth-fallback','POST',{
      operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,
      machine_id:saga.machine_id,epoch:saga.epoch,reason:'workspace_label_ambiguous',
    });
    assert.equal(response.status,200);assert.equal((await parsed(response)).fallback,false);
    const intakeRow=f.sql.exec('SELECT status,final_evidence_json FROM intakes WHERE id=?',own.id).toArray()[0] as any;
    assert.equal(intakeRow.status,'failed');
    assert.equal(JSON.parse(intakeRow.final_evidence_json).error,'placement_fallback_failed');
    const event=f.sql.exec("SELECT kind,body_json FROM intake_events WHERE task_id=? AND kind='session_placement_fallback_failed'",own.id).toArray()[0] as any;
    assert.equal(JSON.parse(event.body_json).placement_reason,'workspace_label_ambiguous');
  }finally{Math.random=random;f.close();}
});

test('fallback cannot change intake, evidence or events after a claim/epoch wins during the actual picker await',async()=>{
  for(const mutation of ['claim','epoch','exhausted-claim']){
    const f=fixture(),random=Math.random;Math.random=()=>0;
    let release:(()=>void)|undefined;
    try{
      const own=await intake(f,'fallback-race-'+mutation);
      await f.call('/api/intakes/'+own.id+'/start','POST',{key:'race-start',workspace:'picker'});
      const saga=f.sql.exec('SELECT * FROM dispatch_sagas').toArray()[0] as any;
      if(mutation==='exhausted-claim')f.setPolicy([candidates[0]]);
      const barrier=f.pauseNextPolicy();release=barrier.release;
      const pending=f.call('/_internal/auth-fallback','POST',{
        operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,
        machine_id:saga.machine_id,epoch:saga.epoch,reason:'provider_auth_failed',
      });
      await barrier.entered;
      if(mutation==='epoch'){
        f.sql.exec('UPDATE dispatch_sagas SET epoch=epoch+1 WHERE operation_id=?',saga.operation_id);f.clearCache();
      }else{
        const claimed=await f.call('/_internal/claim','POST',{operation_id:saga.operation_id,
          attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch});
        assert.equal(claimed.status,200);
      }
      const before={sagas:f.sql.exec('SELECT * FROM dispatch_sagas').toArray(),intakes:f.sql.exec('SELECT * FROM intakes').toArray(),
        events:f.sql.exec('SELECT * FROM intake_events ORDER BY seq').toArray(),reservations:f.reservations.length};
      barrier.release();
      const response=await pending;assert.equal(response.status,409,mutation);
      assert.equal((await parsed(response)).error,'stale_auth_fallback');
      assert.deepEqual(f.sql.exec('SELECT * FROM dispatch_sagas').toArray(),before.sagas);
      assert.deepEqual(f.sql.exec('SELECT * FROM intakes').toArray(),before.intakes);
      assert.deepEqual(f.sql.exec('SELECT * FROM intake_events ORDER BY seq').toArray(),before.events);
      assert.equal(f.reservations.length,before.reservations);
    }finally{release?.();Math.random=random;f.close();}
  }
});

test('an unselected eligible engine accepted by the real 120-byte policy contract cannot gate a start',async()=>{
  const f=fixture(),random=Math.random;Math.random=()=>0;
  const parent=resolve('.local/tmp'),directory=mkdtempSync(join(parent,'picker-policy-contract-'));
  const sql=new NodeSqlStorage(join(directory,'control.sqlite'));
  try{
    const control=new ControlDO({storage:new NodeDurableStorage(sql),blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,{} as any);
    const policy=[{...candidates[0]}, {...candidates[1],engine:'eligible-'.padEnd(120,'e'),model:'synthetic-review-model'}];
    const checked=await control.fetch(new Request('https://control.test/api/model-policy',{method:'PUT',
      headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(admin)},body:JSON.stringify({candidates:policy})}));
    assert.equal(checked.status,200);
    f.setPolicy((await parsed(checked)).candidates);
    // No engine advertisements: the existing picker deliberately falls back
    // to its eligible policy pool. The long unselected row remains eligible.
    f.setEngines([]);
    const own=await intake(f,'long-engine');
    const response=await f.call('/api/intakes/'+own.id+'/start','POST',{key:'long-engine-start',workspace:'picker'});
    assert.equal(response.status,201);const created=await parsed(response);
    assert.equal(created.picker.pool.length,2);
    assert.equal(created.picker.pool[1].engine.length,120);
    assert.equal(created.picker.policy_id,candidates[0].id);
  }finally{Math.random=random;f.close();sql.close();rmSync(directory,{recursive:true});}
});

test('normalization projects only safe fields and rejects mismatched or oversized evidence',()=>{
  const route:PickedRoute={machine_id:'h1',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'private-account',
    policy_id:'sol-route',policy_version:1,override_reason:'private rationale',weight:1,quota_remaining:80,required_capabilities:[]};
  const diagnostics:PickDiagnostics={pool:[{policy_id:route.policy_id,engine:route.engine,model:route.model,effort:'medium',weight:1,share:1}],
    excluded:[{policy_id:'hidden',account_id:'private-account',reason:'private credential status'}]};
  const picker=buildStartPickerEvidence(route,diagnostics);
  assert.equal(picker.pool_degraded,true);
  assert.equal(normalizeStartPickerEvidence({...picker,pool_degraded:false}),null);
  const {pool_degraded: _legacyAbsent,...legacy}=picker;
  assert.deepEqual(normalizeStartPickerEvidence(legacy),legacy);
  const diverse=buildStartPickerEvidence(route,{pool:[
    {...diagnostics.pool[0],share:1/3},
    {...diagnostics.pool[0],policy_id:'second',model:'second-model',share:1/3},
    {...diagnostics.pool[0],policy_id:'third',model:'third-model',share:1/3},
  ],excluded:[]});
  assert.equal(diverse.pool_degraded,false);
  assert.equal(normalizeStartPickerEvidence({...diverse,pool_degraded:true}),null);
  const dirty={...picker,account_id:'private-account',credential:'secret',override_reason:'private rationale',excluded:diagnostics.excluded,
    pool:picker.pool.map(row=>({...row,account_id:'private-account'})),history:picker.history.map(row=>({...row,credential:'secret'}))};
  assert.deepEqual(normalizeStartPickerEvidence(dirty),picker);
  assert.doesNotMatch(JSON.stringify(picker),/secret|private|credential|excluded|override/);
  assert.equal(normalizeStartPickerEvidence({...picker,policy_id:'not-the-selected-policy'}),null);
  assert.equal(normalizeStartPickerEvidence({...picker,pool:Array(33).fill(picker.pool[0])}),null);
  assert.equal(normalizeStartPickerEvidence({...picker,history:Array(3).fill(picker.history[0])}),null);
  assert.equal(normalizeStartPickerEvidence({...picker,pool:[{...picker.pool[0],share:NaN}]}),null);
});

test('a full 32-row pool stays bounded and travels separately from an 8192-byte prompt',async()=>{
  const f=fixture();
  try{
    f.setPolicy(Array.from({length:32},(_,i)=>({...candidates[0],id:'route-'+i,model:'synthetic-model-'+i})));
    const own=await intake(f,'large-pool');
    const response=await f.call('/api/intakes/'+own.id+'/start','POST',{key:'large-start',workspace:'picker',prompt:'x'.repeat(8192)});
    assert.equal(response.status,201);const created=await parsed(response),reserved=f.reservations[0];
    assert.equal(created.picker.pool.length,32);assert(reserved.launch.prompt.startsWith('x'.repeat(8192)+'\n\n--- Hub task completion contract ---\n'));
    assert(new TextEncoder().encode(JSON.stringify(reserved.launch)).length<=12_000,'contract keeps a maximal prompt within the Machine launch limit');
    assert.deepEqual(reserved.picker,created.picker);
    assert(new TextEncoder().encode(JSON.stringify(reserved)).length<64_000);
    const rows=Array.from({length:32},(_,i)=>({policy_id:String(i).padStart(3,'0')+'p'.repeat(117),engine:'e'.repeat(120),model:'m'.repeat(120),effort:'medium',weight:1,share:1/32}));
    const row=rows[0];
    const route:PickedRoute={...row,machine_id:'h'.repeat(80),policy_version:'v'.repeat(80),account_id:'hidden',override_reason:null,quota_remaining:80,required_capabilities:[]};
    const maximal=buildStartPickerEvidence(route,{pool:rows,excluded:[]});
    const second=buildStartPickerEvidence({...route,policy_id:rows[1].policy_id},{pool:rows,excluded:[]},1,maximal);
    assert(new TextEncoder().encode(JSON.stringify(second)).length<=MAX_PICKER_BYTES);
    assert.equal(normalizeStartPickerEvidence(second)?.history.length,2);
  }finally{f.close();}
});

test('operator shell picker evidence describes its actual single selected option',async()=>{
  const f=fixture();
  try{
    const own=await intake(f,'shell-picker');
    const response=await f.call('/api/intakes/'+own.id+'/start','POST',{key:'shell-start',workspace:'picker',engine:'shell',prompt:'printf ok'});
    assert.equal(response.status,201);const created=await parsed(response);
    assert.equal(created.picker.engine,'shell');assert.equal(created.picker.policy_id,'operator-shell');
    assert.equal(created.picker.pool.length,1);assert.equal(created.picker.pool[0].share,1);
    assert.equal(f.policyReads,0);
  }finally{f.close();}
});
