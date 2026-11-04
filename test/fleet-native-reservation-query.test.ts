import { fixtureStatements,registerFixtureStatements } from './helpers/fixture-statements';
import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';

const inDO: any = runInDurableObject;

it.each(['headless','interactive'])('seeks the candidate launch while preserving %s native correlation checks', async mode => {
  const stub=(env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub,async(h:any,state:any)=>{
    registerFixtureStatements(h);
    const sql=h.sql,at=new Date().toISOString(),id='target-launch';
    fixtureStatements(sql).exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES('query-host','fixture','fixture',?,?,?)",at,at,JSON.stringify({engines:['codex']}));
    const account={label:'query-account',provider:'codex',max_concurrency:1000,reserve_percent:15,enabled:1};
    fixtureStatements(sql).exec("INSERT INTO fleet_accounts VALUES('query-account','codex',1000,15,1)");
    const request=JSON.stringify({cwd:'subdir',prompt:'retained fixture '.repeat(500)});
    for(let i=0;i<200;i++) fixtureStatements(sql).exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      VALUES(?,'fixture','agent','fixture',?,?,'query-host','codex','query-account',?,'/repo','fixture','uncertain',?,?)`,
      i===0?id:'retained-'+i,'key-'+i,request,mode,at,at);
    const tag=(mode==='headless'?'task-hub-':'hub-')+id;
    const insert=(native:string,tag:string)=>fixtureStatements(sql).exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,account)
      VALUES('query-host',?,?,'codex','running',?,'/repo/subdir','query-account')`,native,tag,at);
    insert('matched',tag);
    for(let i=0;i<20;i++) insert('manual-'+i,'manual-'+i);
    let plan:string[]=[];
    const measured=new Proxy(sql,{get(target,key){
      if(key!=='exec') return Reflect.get(target,key);
      return (query:string,...args:any[])=>{
        if(query.includes('AS native_workspace FROM fleet_launches l WHERE l.id=?')) {
          plan=target.exec('EXPLAIN QUERY PLAN '+query,...args).toArray().map((r:any)=>r.detail);
        }
        return target.exec(query,...args);
      };
    }});
    const f=new FleetService(measured,fn=>state.storage.transactionSync(fn));
    // Each changed observation is an independent service event. Refill only
    // through the real event preflight, on the SAME strict20k budget day.
    const day=h.rowBudget.status().day;
    const read=async()=>{
      await h.rowBudget.begin();
      try { return f.accountStatus(account); }
      finally { await h.rowBudget.finish(); }
    };
    expect(await read()).toMatchObject({active:220,reserved:199,observed_sessions:21});
    expect(plan.some(detail=>/SEARCH l .*\(id=\?\)/.test(detail)),JSON.stringify(plan)).toBe(true);
    expect(plan.join(' | ')).not.toMatch(/TEMP B-TREE|^SCAN /);
    for(const [column,bad,good] of [
      ['workspace','/foreign','/repo/subdir'], ['engine','claude','codex'],
      ['profile','foreign',null], ['tag',tag.toUpperCase(),tag],
    ] as const) {
      fixtureStatements(sql).exec(`UPDATE fleet_sessions SET ${column}=? WHERE native_id='matched'`,bad);
      expect((await read()).active,column).toBe(221);
      fixtureStatements(sql).exec(`UPDATE fleet_sessions SET ${column}=? WHERE native_id='matched'`,good);
      expect((await read()).active,column).toBe(220);
    }
    fixtureStatements(sql).exec('UPDATE fleet_launches SET mode=? WHERE id=?',mode==='headless'?'interactive':'headless',id);
    expect((await read()).active).toBe(221);
    fixtureStatements(sql).exec('UPDATE fleet_launches SET mode=? WHERE id=?',mode,id);
    fixtureStatements(sql).exec("UPDATE fleet_launches SET request_json='invalid' WHERE id=?",id);
    expect((await read()).active).toBe(221);
    fixtureStatements(sql).exec('UPDATE fleet_launches SET request_json=? WHERE id=?',request,id);
    insert('duplicate',tag);
    expect((await read()).active).toBe(222);
    fixtureStatements(sql).exec("DELETE FROM fleet_sessions WHERE native_id='duplicate'");
    // The exact native-id branch intentionally ignores mismatched tag/engine.
    fixtureStatements(sql).exec("UPDATE fleet_launches SET native_id='matched',engine='claude' WHERE id=?",id);
    expect((await read()).active).toBe(220);
    expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  });
});
