import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {ControlDO} from '../src/control-do';
import {HUB2_FORWARD_BODY_LIMIT} from '../src/http';
import worker from '../src/worker';
const candidate={id:'sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true};
const hydrogenCandidate={id:'antigravity-hydrogen-medium',engine:'antigravity',model:'gemini-4-hydrogen-medium',effort:'medium',account_id:'gemini',weight:1,capabilities:[],enabled:true};
const carbonCandidate={id:'antigravity-carbon-medium',engine:'antigravity',model:'gemini-4-carbon-medium',effort:'medium',account_id:'gemini',weight:1,capabilities:['think-only'],enabled:true};
function fixture(){
 const database=new DatabaseSync(':memory:');
 const state:any={storage:{async setAlarm(){},async deleteAlarm(){},sql:{exec(query:string,...args:any[]){
  const statement=database.prepare(query);const rows=statement.columns().length?statement.all(...args):(statement.run(...args),[]);return{toArray:()=>rows};
 }},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const value=fn();database.exec('COMMIT');return value;}catch(error){database.exec('ROLLBACK');throw error;}}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
 const control=new ControlDO(state,{} as any);
 const call=(method='GET',body?:unknown,admin=true,path='/api/model-policy')=>control.fetch(new Request('https://control.test'+path,{method,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify({id:'operator:test',kind:'operator',scopes:admin?['operator:admin']:['model-policy:read']})},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 const token='synthetic-operator-token-for-policy-recovery';
 const env:any={
  HUB2_DEV_OPERATOR_TOKEN:token,
  CONTROL:{idFromName:(name:string)=>name,get:()=>({fetch:(input:Request|string,init?:RequestInit)=>control.fetch(new Request(input,init))})},
 };
 const publicCall=(method:string,body?:unknown)=>{
  const encoded=body===undefined?undefined:JSON.stringify(body);
  const headers=new Headers({'Content-Type':'application/json',Authorization:'Bearer '+token});
  if(encoded!==undefined)headers.set('Content-Length',String(Buffer.byteLength(encoded)));
  return worker.fetch(new Request('https://hub.test/api/model-policy',{method,headers,body:encoded}),env);
 };
 return{database,call,publicCall};
}
const body=async(response:Response)=>await response.json() as any;

test('unconfigured restore keeps monotonically versioned tombstones and prevents ABA',async()=>{
 const f=fixture();try{
  let response=await f.call();assert.equal(response.status,404);assert.equal((await body(response)).version,0);
  assert.equal((await f.call('PUT',{candidates:[]})).status,400);
  response=await f.call('PUT',{candidates:[candidate],expected_version:0});assert.equal((await body(response)).version,1);
  assert.equal((await f.call('PUT',{candidates:[],restore_unconfigured:true})).status,400);
  assert.equal((await f.call('PUT',{candidates:[],restore_unconfigured:true,expected_version:1},false)).status,403);
  response=await f.call('PUT',{candidates:[],restore_unconfigured:true,expected_version:1});assert.equal(response.status,200);assert.equal((await body(response)).version,2);
  response=await f.call();assert.equal(response.status,404);assert.deepEqual(await body(response),{error:'model_policy_unconfigured',version:2});
  assert.equal((await f.call('GET',undefined,true,'/_routing-policy')).status,404);
  assert.equal((f.database.prepare('SELECT version,candidates_json FROM model_policy').get() as any).candidates_json,'[]');
  assert.equal((await f.call('PUT',{candidates:[candidate],expected_version:0})).status,409);
  response=await f.call('PUT',{candidates:[candidate],expected_version:2});assert.equal((await body(response)).version,3);
 }finally{f.database.close();}
});

test('CAS precedes digest idempotence and competing writers cannot both win',async()=>{
 const f=fixture();try{
  await f.call('PUT',{candidates:[candidate],expected_version:0});
  let response=await f.call('PUT',{candidates:[candidate],expected_version:1});assert.equal((await body(response)).unchanged,true);
  assert.equal((await f.call('PUT',{candidates:[candidate],expected_version:0})).status,409);
  const responses=await Promise.all([2,3].map(weight=>f.call('PUT',{candidates:[{...candidate,weight}],expected_version:1})));
  assert.deepEqual(responses.map(r=>r.status).sort(),[200,409]);
  assert.equal((await body(await f.call())).version,2);
  assert.equal((await f.call('PUT',{candidates:[],restore_unconfigured:false,expected_version:2})).status,400);
  assert.equal((await f.call('PUT',{candidates:[candidate],expected_version:true})).status,400);
  assert.equal((await f.call('PUT',{candidates:[candidate],expected_version:-1})).status,400);
 }finally{f.database.close();}
});

const protocol = 'model-policy-recovery-v1';
const recovery = (action: string, expected_version: number, candidates: unknown[] = [candidate]) =>
  ({protocol, action, expected_version, candidates});

test('public policy writes require the exact Carbon identity and think-only contract', async () => {
  const f = fixture();
  try {
    const malformed = [
      {...carbonCandidate, capabilities: []},
      {...carbonCandidate, id: 'generic-route', capabilities: []},
      {...carbonCandidate, id: 'generic-carbon-alias'},
      {...carbonCandidate, model: 'gemini-4-carbon-alias'},
      {...carbonCandidate, engine: 'codex'},
      {...carbonCandidate, effort: 'low'},
      {...carbonCandidate, account_id: 'codex'},
      {...carbonCandidate, capabilities: ['think-only', 'review-only']},
    ];
    for (const item of malformed) {
      const response = await f.publicCall('PUT', {candidates: [item]});
      const result = await body(response);
      assert.equal(response.status, 400, JSON.stringify(result));
      assert.deepEqual(result, {error: 'carbon_policy_contract_invalid'});
    }
    assert.deepEqual(await body(await f.publicCall('GET')),
      {error: 'model_policy_unconfigured', version: 0});
    const accepted = await f.publicCall('PUT', {candidates: [carbonCandidate]});
    assert.equal(accepted.status, 200);
    assert.equal((await body(accepted)).version, 1);
  } finally { f.database.close(); }
});

test('Carbon policy recovery restores the prior Hydrogen-only policy before code rollback', async () => {
  const f = fixture();
  try {
    const initial = await f.publicCall('PUT', {candidates: [hydrogenCandidate], expected_version: 0});
    assert.equal(initial.status, 200);
    assert.equal((await body(initial)).version, 1);
    const desired = [hydrogenCandidate, carbonCandidate];
    const preflight = await f.publicCall('POST', recovery('validate_recovery', 1, desired));
    assert.equal(preflight.status, 200);
    assert.equal((await body(preflight)).changed, true);
    const applied = await f.publicCall('POST', recovery('apply_recoverable', 1, desired));
    assert.equal(applied.status, 200);
    assert.equal((await body(applied)).version, 2);

    // The deploy transaction restores this snapshot before checking out old code.
    const restored = await f.publicCall('PUT', {candidates: [hydrogenCandidate], expected_version: 2});
    assert.equal(restored.status, 200);
    assert.equal((await body(restored)).version, 3);
    assert.deepEqual(await body(await f.publicCall('GET')),
      {version: 3, candidates: [hydrogenCandidate]});
  } finally { f.database.close(); }
});

test('recovery validates the stored legacy policy using the operator PUT rules', async () => {
  const f = fixture();
  try {
    await f.call();
    const legacy = JSON.stringify([{...candidate,id:'astra',model:'gpt-6.1-astra'}]);
    f.database.prepare('INSERT INTO model_policy VALUES(1,4,?,?)').run(legacy,'legacy-digest');
    const changes = () => (f.database.prepare('SELECT total_changes() AS n').get() as any).n;
    const before = changes();
    const operator = await f.call('PUT',{candidates:JSON.parse(legacy),expected_version:4});
    assert.equal(operator.status,400);
    assert.deepEqual(await body(operator),{error:'astra_review_only_required'});
    for (const action of ['validate_recovery','apply_recoverable']) {
      const response = await f.call('POST',recovery(action,4));
      assert.equal(response.status,409);
      assert.deepEqual(await body(response),{error:'model_policy_prior_unrestorable'});
      assert.equal(changes(),before);
    }
    assert.equal((await body(await f.call())).version,4);
    // The separately authorized operator repair still uses ordinary PUT.
    assert.equal((await f.call('PUT',{candidates:[candidate],expected_version:4})).status,200);
    assert.equal((await f.call('POST',recovery('validate_recovery',5))).status,200);
  } finally { f.database.close(); }
});

test('preflight is read-only and reports the normalized prior even when field order differs', async () => {
  const f = fixture();
  try {
    await f.call();
    const prior = {...candidate,capabilities:['z','a','z']};
    f.database.prepare('INSERT INTO model_policy VALUES(1,4,?,?)').run(JSON.stringify([prior]),'legacy-digest');
    const desired = {capabilities:['a','z'],...Object.fromEntries(Object.entries(candidate).filter(([k])=>k!=='capabilities'))};
    const preflight = await f.call('POST',recovery('validate_recovery',4,[desired]));
    assert.equal(preflight.status,200);
    assert.deepEqual(await body(preflight),{
      protocol,action:'validate_recovery',version:4,candidates:[{...candidate,capabilities:['a','z']}],changed:false,
    });
    const result = await body(await f.call('POST',recovery('apply_recoverable',4,[desired])));
    assert.equal(result.unchanged,true);
    assert.equal(result.version,4);
    assert.deepEqual((f.database.prepare('SELECT version,candidates_json,digest FROM model_policy').get() as any).candidates_json,JSON.stringify([prior]));
  } finally { f.database.close(); }
});

test('recoverable apply rechecks CAS and supports versioned rollback', async () => {
  const f = fixture();
  try {
    assert.equal((await f.call('POST',recovery('validate_recovery',0),false)).status,403);
    assert.equal((await f.call('POST',recovery('validate_recovery',0),true,'/_routing-policy')).status,403);
    assert.equal((await f.call('POST',recovery('validate_recovery',0))).status,200);
    assert.equal((await f.call('PUT',{candidates:[candidate],expected_version:0})).status,200);
    assert.equal((await f.call('POST',recovery('apply_recoverable',0))).status,409);
    const desired = [{...candidate,weight:2}];
    const applied = await f.call('POST',recovery('apply_recoverable',1,desired));
    assert.equal(applied.status,200);
    assert.deepEqual(await body(applied),{protocol,action:'apply_recoverable',version:2,candidates:desired});
    const restored = await f.call('PUT',{candidates:[candidate],expected_version:2});
    assert.equal(restored.status,200);
    assert.deepEqual(await body(restored),{version:3,candidates:[candidate]});
  } finally { f.database.close(); }
});

test('changing recovery requires headroom for both apply and rollback', async () => {
  const f = fixture();
  try {
    await f.call();
    const version = Number.MAX_SAFE_INTEGER-1;
    f.database.prepare('INSERT INTO model_policy VALUES(1,?,?,?)').run(version,JSON.stringify([candidate]),'legacy-digest');
    for (const action of ['validate_recovery','apply_recoverable']) {
      const response = await f.call('POST',recovery(action,version,[{...candidate,weight:2}]));
      assert.equal(response.status,409);
      assert.equal((await body(response)).error,'model_policy_recovery_version_exhausted');
    }
    assert.equal((await body(await f.call('POST',recovery('apply_recoverable',version)))).unchanged,true);
    assert.equal((await body(await f.call())).version,version);
  } finally { f.database.close(); }
});

test('recovery rejects malformed actions and extra fields before changing policy', async () => {
  const f = fixture();
  try {
    const request = recovery('validate_recovery',0);
    for (const invalid of [
      {...request,action:['validate_recovery']},
      {...request,action:['apply_recoverable']},
      {...request,action:'apply_recoverable',restore_unconfigured:true},
    ]) {
      const response = await f.call('POST',invalid);
      assert.equal(response.status,400);
      assert.deepEqual(await body(response),{error:'invalid_model_policy_recovery'});
    }
    assert.deepEqual(await body(await f.call()),{error:'model_policy_unconfigured',version:0});
  } finally { f.database.close(); }
});

function sizedPolicy(size: number, envelope: {expected_version?: number} = {}) {
  const candidates = Array.from({length:32}, (_, i) => ({
    ...candidate,id:'sol-'+i,capabilities:Array.from({length:32},(_, j)=>'cap-'+j),
  }));
  const bytes = () => Buffer.byteLength(JSON.stringify({candidates,...envelope}));
  let remaining = size-bytes();
  for (const row of candidates) {
    for (let j=0;j<row.capabilities.length && remaining;j++) {
      // A control character consumes six JSON bytes and one capability byte.
      const escapes = Math.min(70,Math.floor(remaining/6));
      const suffix = remaining-escapes*6;
      const plain = suffix<6?suffix:0;
      row.capabilities[j] += '\0'.repeat(escapes)+'x'.repeat(plain);
      remaining -= escapes*6+plain;
    }
  }
  assert.equal(bytes(),size);
  return candidates;
}

test('public recovery accepts an exact-limit rollback and refuses a prior whose CAS envelope exceeds it', async () => {
  const f = fixture();
  try {
    const prior = sizedPolicy(HUB2_FORWARD_BODY_LIMIT,{expected_version:2});
    assert.equal((await f.publicCall('PUT',{candidates:prior})).status,200);
    assert.equal((await f.publicCall('POST',recovery('validate_recovery',1))).status,200);
    assert.equal((await f.publicCall('POST',recovery('apply_recoverable',1))).status,200);
    assert.equal((await f.publicCall('PUT',{candidates:prior,expected_version:2})).status,200);
    assert.equal((await body(await f.publicCall('GET'))).version,3);

    const oversizedRollback = sizedPolicy(HUB2_FORWARD_BODY_LIMIT);
    assert.equal((await f.publicCall('PUT',{candidates:oversizedRollback})).status,200);
    const rejected = await f.publicCall('PUT',{candidates:oversizedRollback,expected_version:4});
    assert.equal(rejected.status,413);
    for (const action of ['validate_recovery','apply_recoverable']) {
      const response = await f.publicCall('POST',recovery(action,4));
      assert.equal(response.status,409);
      assert.deepEqual(await body(response),{error:'model_policy_rollback_body_too_large'});
    }
    assert.equal((await body(await f.publicCall('GET'))).version,4);
  } finally { f.database.close(); }
});
