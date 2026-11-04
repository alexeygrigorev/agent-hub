import {fixtureSql} from './helpers/fixture-budget';
import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowsMeter} from '../src/rows-meter';
import {workerChildObservation} from '../src/worker-child-observation';

const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('child-observation-'+crypto.randomUUID())),(hub:any,state:any)=>{
  const raw=state.storage.sql,now=Date.now(),at=new Date(now).toISOString(),old=new Date(now-86400000).toISOString();
  const setup=fixtureSql(hub,state);
  const seed=(prefix:string,count:number,extra:any={})=>setup.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
    INSERT INTO session_control_sessions(id,actor,request_key,request_json,parent_id,mode,state,created_at,updated_at)
    SELECT ?||i,'synthetic',?||i,json_object('intake_task_id',?),?,'headless',?,?,? FROM n`,count,prefix,prefix,extra.intake??'other',extra.parent??'foreign',extra.state??'exited',extra.at??old,at);
  const meter=new RowsMeter(()=>now),sql=meter.wrap(hub.sql);
  fn({raw,now,at,seed,meter,sql});
});

describe('worker child admission observations',()=>{
  it('ignores large old unrelated terminal history using deployed index seeks',()=>fixture(({seed,now,meter,sql}:any)=>{
    seed('terminal-',2100);
    const observed=meter.runSync('complete',()=>workerChildObservation(sql,'parent','intake',now));
    expect(observed).toEqual({concurrent:{count:0,limited:false},per_intake:{count:0,limited:false},per_hour:{count:0,limited:false}});
    expect(meter.snapshot()['complete'].rows_read).toBeLessThan(50);
  }));
  it('reports unknown behind foreign active/recent populations while retaining known child-limit evidence',()=>fixture(({seed,now,at,meter,sql}:any)=>{
    seed('active-',2100,{state:'running',at});
    const unknown=meter.runSync('unknown',()=>workerChildObservation(sql,'parent','intake',now));
    expect(unknown).toMatchObject({concurrent:{count:0,limited:true},per_hour:{count:0,limited:true}});
    expect(meter.snapshot()['unknown'].rows_read).toBeLessThan(650);
    seed('zz-child-',4,{state:'running',at,parent:'parent',intake:'intake'});
    const known=meter.runSync('known',()=>workerChildObservation(sql,'parent','intake',now));
    expect(known.concurrent).toEqual({count:4,limited:true});
    expect(known.per_intake).toEqual({count:4,limited:false});
    expect(meter.snapshot()['known'].rows_read).toBeLessThan(650);
  }));
  it('never silently clears the per-intake limit behind retained terminal children',()=>fixture(({seed,now,meter,sql}:any)=>{
    seed('linked-terminal-',2100,{intake:'intake',parent:'parent'});
    expect(meter.runSync('linked',()=>workerChildObservation(sql,'parent','intake',now)).per_intake).toEqual({count:0,limited:true});
    expect(meter.snapshot()['linked'].rows_read).toBeLessThan(350);
  }));
});
