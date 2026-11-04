import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowsMeter} from '../src/rows-meter';
import {accountFailurePrefix,routeFailureObservation,routeQuarantineObservation,unsupportedModelObservation} from '../src/fleet-failure-observation';

const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('failure-observation-'+crypto.randomUUID())),(_h:any,state:any)=>{
  const raw=state.storage.sql,now=Date.now(),meter=new RowsMeter(()=>now),sql=meter.wrap(raw);
  const seed=(prefix:string,count:number,opts:any={})=>raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
    INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,profile,mode,workspace,prompt,state,exit_reason,finished_at,model_selection_json,created_at,updated_at)
    SELECT ?||i,'test','human','test',?||i,'{}',?,'codex',?,?,'headless','/repo','test','failed',?,?,?,?,? FROM n`,
    count,prefix,prefix,opts.machine??'host',opts.account??'acct',opts.profile??'other',opts.reason??'launcher_memory_reserve',
    new Date(opts.at??now-1000).toISOString(),JSON.stringify({model:opts.model??'other',effort:opts.effort??'low'}),new Date(now-1000).toISOString(),new Date(opts.at??now-1000).toISOString());
  fn({raw,sql,meter,now,seed});
});

describe('failure observations bound raw history before route eligibility',()=>{
  it('neutral and other-machine account failures cannot become an unbounded LIMIT8 scan or a fabricated healthy score',()=>fixture(({sql,meter,now,seed}:any)=>{
    seed('neutral-',2100);
    const neutral=new Set(['launcher_memory_reserve']);
    const first=meter.runSync('neutral-prefix',()=>routeFailureObservation(accountFailurePrefix(sql,'acct',new Date(now-1800000).toISOString()),'host',neutral,8));
    expect(first).toMatchObject({n:0,limited:true});
    expect(meter.snapshot()['neutral-prefix'].rows_read).toBeLessThan(300);
    seed('positive-',8,{reason:'launcher_command_failed',at:now});
    const positive=meter.runSync('positive-prefix',()=>routeFailureObservation(accountFailurePrefix(sql,'acct',new Date(now-1800000).toISOString()),'host',neutral,8));
    expect(positive).toMatchObject({n:8,limited:false});expect(positive.launcher).toHaveLength(8);
    expect(meter.snapshot()['positive-prefix'].rows_read).toBeLessThan(300);
  }));

  it('quarantine absence is unknown behind foreign transitions while a known newer own outcome still reopens it',()=>fixture(({raw,sql,meter,now,seed}:any)=>{
    seed('foreign-',2100,{account:'foreign',machine:'other',reason:'other_failure'});
    const third=new Date(now-60000).toISOString();
    expect(meter.runSync('quarantine-unknown',()=>routeQuarantineObservation(sql,'acct','host',third))).toBeNull();
    expect(meter.snapshot()['quarantine-unknown'].rows_read).toBeLessThan(300);
    seed('own-outcome-',1,{reason:'other_failure',at:now});
    expect(meter.runSync('quarantine-positive',()=>routeQuarantineObservation(sql,'acct','host',third))).toBe(false);
    expect(meter.snapshot()['quarantine-positive'].rows_read).toBeLessThan(300);
    raw.exec("UPDATE fleet_launches SET exit_reason=NULL WHERE id='own-outcome-1'");
    expect(routeQuarantineObservation(sql,'acct','host',third)).toBeNull(); // SQL NOT(failed AND NULL) is not positive proof.
  }));

  it('model/profile/effort postfilters never scan all refusal history, and positive refusal evidence is retained',()=>fixture(({sql,meter,now,seed}:any)=>{
    seed('unsupported-',2100,{reason:'model_unsupported'});
    const route={machine_id:'host',engine:'codex',account:'acct',profile:'wanted'};
    const since=new Date(now-600000).toISOString();
    expect(meter.runSync('unsupported-unknown',()=>unsupportedModelObservation(sql,route,'headless',since,'wanted-model','high'))).toBeNull();
    expect(meter.snapshot()['unsupported-unknown'].rows_read).toBeLessThan(300);
    seed('matching-',1,{reason:'model_unsupported',profile:'wanted',model:'wanted-model',effort:'high',at:now});
    expect(meter.runSync('unsupported-positive',()=>unsupportedModelObservation(sql,route,'headless',since,'wanted-model','high'))).toBe(true);
    expect(meter.snapshot()['unsupported-positive'].rows_read).toBeLessThan(300);
  }));
  it('malformed current model evidence is unknown while a known matching refusal remains authoritative',()=>fixture(({raw,sql,now,seed}:any)=>{
    seed('unreadable-',1,{reason:'model_unsupported',profile:'wanted',at:now});
    const route={machine_id:'host',engine:'codex',account:'acct',profile:'wanted'},since=new Date(now-600000).toISOString();
    expect(unsupportedModelObservation(sql,route,'headless',since,'wanted-model','high')).toBe(false);
    raw.exec("UPDATE fleet_launches SET model_selection_json='{' WHERE id='unreadable-1'");
    expect(unsupportedModelObservation(sql,route,'headless',since,'wanted-model','high')).toBeNull();
    seed('known-',1,{reason:'model_unsupported',profile:'wanted',model:'wanted-model',effort:'high',at:now});
    expect(unsupportedModelObservation(sql,route,'headless',since,'wanted-model','high')).toBe(true);
  }));

});
