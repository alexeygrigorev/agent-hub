import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {ControlDO} from '../src/control-do';
const candidate={id:'sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true};
function fixture(){
 const database=new DatabaseSync(':memory:');
 const state:any={storage:{async setAlarm(){},async deleteAlarm(){},sql:{exec(query:string,...args:any[]){
  const statement=database.prepare(query);const rows=statement.columns().length?statement.all(...args):(statement.run(...args),[]);return{toArray:()=>rows};
 }},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const value=fn();database.exec('COMMIT');return value;}catch(error){database.exec('ROLLBACK');throw error;}}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
 const control=new ControlDO(state,{} as any);
 const call=(method='GET',body?:unknown,admin=true,path='/api/model-policy')=>control.fetch(new Request('https://control.test'+path,{method,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify({id:'operator:test',kind:'operator',scopes:admin?['operator:admin']:['model-policy:read']})},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{database,call};
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
