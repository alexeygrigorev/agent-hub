import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';

const inDO: any = runInDurableObject;

it.each(['headless','interactive'])('seeks the candidate launch while preserving %s native correlation checks', async mode => {
  const stub=(env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub,(h:any,state:any)=>{
    const sql=h.sql,at=new Date().toISOString(),id='target-launch';
    sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES('query-host','fixture','fixture',?,?,?)",at,at,JSON.stringify({engines:['codex']}));
    const account={label:'query-account',provider:'codex',max_concurrency:1000,reserve_percent:15,enabled:1};
    sql.exec("INSERT INTO fleet_accounts VALUES('query-account','codex',1000,15,1)");
    const request=JSON.stringify({cwd:'subdir',prompt:'retained fixture '.repeat(500)});
    for(let i=0;i<200;i++) sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      VALUES(?,'fixture','agent','fixture',?,?,'query-host','codex','query-account',?,'/repo','fixture','uncertain',?,?)`,
      i===0?id:'retained-'+i,'key-'+i,request,mode,at,at);
    const tag=(mode==='headless'?'task-hub-':'hub-')+id;
    const insert=(native:string,tag:string)=>sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,account)
      VALUES('query-host',?,?,'codex','running',?,'/repo/subdir','query-account')`,native,tag,at);
    insert('matched',tag);
    for(let i=0;i<20;i++) insert('manual-'+i,'manual-'+i);
    let plan:string[]=[];
    const measured=new Proxy(sql,{get(target,key){
      if(key!=='exec') return Reflect.get(target,key);
      return (query:string,...args:any[])=>{
        if(query.includes('FROM fleet_sessions s WHERE s.account = ?') && query.includes('NOT EXISTS')) {
          plan=target.exec('EXPLAIN QUERY PLAN '+query,...args).toArray().map((r:any)=>r.detail);
        }
        return target.exec(query,...args);
      };
    }});
    const f=new FleetService(measured,fn=>state.storage.transactionSync(fn));
    const read=()=>f.accountStatus(account);
    expect(read()).toMatchObject({active:220,reserved:200,observed_sessions:20});
    expect(plan.some(detail=>/SEARCH l .*\(id=\?\)/.test(detail)),JSON.stringify(plan)).toBe(true);
    for(const [column,bad,good] of [
      ['workspace','/foreign','/repo/subdir'], ['engine','claude','codex'],
      ['profile','foreign',null], ['tag',tag.toUpperCase(),tag],
    ] as const) {
      sql.exec(`UPDATE fleet_sessions SET ${column}=? WHERE native_id='matched'`,bad);
      expect(read().active,column).toBe(221);
      sql.exec(`UPDATE fleet_sessions SET ${column}=? WHERE native_id='matched'`,good);
      expect(read().active,column).toBe(220);
    }
    sql.exec('UPDATE fleet_launches SET mode=? WHERE id=?',mode==='headless'?'interactive':'headless',id);
    expect(read().active).toBe(221);
    sql.exec('UPDATE fleet_launches SET mode=? WHERE id=?',mode,id);
    sql.exec("UPDATE fleet_launches SET request_json='invalid' WHERE id=?",id);
    expect(read().active).toBe(221);
    sql.exec('UPDATE fleet_launches SET request_json=? WHERE id=?',request,id);
    insert('duplicate',tag);
    expect(read().active).toBe(222);
    sql.exec("DELETE FROM fleet_sessions WHERE native_id='duplicate'");
    // The exact native-id branch intentionally ignores mismatched tag/engine.
    sql.exec("UPDATE fleet_launches SET native_id='matched',engine='claude' WHERE id=?",id);
    expect(read().active).toBe(220);
  });
});
