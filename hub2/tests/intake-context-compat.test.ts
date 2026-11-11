import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {ProjectDO} from '../src/project-do';

test('official CLI context update persists, clears and rejects stale or unknown fields',async()=>{
 const sql=new DatabaseSync(':memory:');
 const ctx:any={storage:{sql:{exec(query:string,...args:any[]){const statement=sql.prepare(query);return{toArray:()=>statement.columns().length?statement.all(...args):(statement.run(...args),[])};}},transactionSync(fn:()=>unknown){sql.exec('BEGIN');try{const result=fn();sql.exec('COMMIT');return result;}catch(error){sql.exec('ROLLBACK');throw error;}},async getAlarm(){return null;},async setAlarm(){},async deleteAlarm(){}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
 const admin={id:'human:test',kind:'operator',scopes:['operator:admin']};
 const env:any={CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({registered:true})})}};
 let project=new ProjectDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown)=>project.fetch(new Request('https://project.test'+path,{method,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(admin)},...(body?{body:JSON.stringify(body)}:{})}));
 try{
  const created=await call('/api/intakes?project=owner/repo','POST',{key:'context-create',project:'owner/repo',title:'Context smoke',text:'Request',acceptance_criteria:['Check context']});
  assert.equal(created.status,201);const initial:any=await created.json();const id=initial.intake.id,path='/api/intakes/'+id;
  const update={key:'context-update',expected_version:initial.intake.updated_at,current_state:'Started live smoke',next_step:'Check note',owner:'smoke-owner',uncertainty:'Stop proof pending'};
  const response=await call(path,'POST',update);assert.equal(response.status,200,await response.clone().text());
  const result:any=await response.json();assert.equal(result.intake.current_state,update.current_state);assert.equal(result.intake.next_step,update.next_step);
  const replay=await call(path,'POST',update);assert.equal(replay.status,200);assert.equal((await replay.json() as any).duplicate,true);
  project=new ProjectDO(ctx,env);const shown:any=await(await call(path)).json();assert.equal(shown.intake.owner,'smoke-owner');assert.equal(shown.task.current_state,update.current_state);
  const cleared=await call(path,'POST',{key:'context-clear',expected_version:shown.intake.updated_at,next_step:null});assert.equal(cleared.status,200);const state:any=await cleared.json();assert.equal(state.intake.next_step,null);assert.equal(state.intake.owner,'smoke-owner');assert.equal(state.intake.text,'Request');assert.deepEqual(state.intake.original_ask,{title:'Context smoke',text:'Request'});
  assert.equal((await call(path,'POST',{key:'context-stale',expected_version:0,current_state:'Wrong'})).status,409);
  assert.equal((await call(path,'POST',{key:'context-invalid',current_state:{bad:true}})).status,400);
  assert.equal((await call(path,'POST',{key:'context-unknown',current_state:'Wrong',unknown:'field'})).status,400);
  assert.equal((await call(path,'PATCH',{key:'context-unknown-patch',unexpected:'field'})).status,400);
  const final:any=await(await call(path+'/context')).json();assert.equal(final.intake.current_state,update.current_state);assert.equal(final.intake.uncertainty,update.uncertainty);
  assert.equal((await call(path,'POST',{key:'context-note',note:'Ordinary note'})).status,200);
  assert.equal((await call(path,'POST',{key:'context-close',status:'done',reason:'Disposable smoke complete'})).status,200);
  assert.equal((await(await call(path)).json() as any).intake.status,'archived');
 }finally{sql.close();}
});
