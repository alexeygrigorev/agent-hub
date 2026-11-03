import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ChecksService } from '../src/checks';
import { LivenessService } from '../src/liveness';
import { OpsLiveness } from '../src/ops-liveness';
import type { Hub } from '../src/hub';

const fixture = (fn: (hub: Hub, state: DurableObjectState) => Promise<void>) =>
  runInDurableObject((env as any).HUB.get((env as any).HUB.idFromName('cf-bg-' + crypto.randomUUID())), fn);

describe('bounded cron observations', () => {
  it('bounds message inspection before expiry filtering and reports incomplete instead of pass', () => fixture(async (hub,state) => {
    const sql = state.storage.sql;
    const now = Date.now();
    // Raw fixture setup is deliberately outside the production event budget.
    for (let i=0;i<1200;i++) sql.exec(`INSERT INTO messages
      (id,from_actor,to_actor,kind,thread_id,body,created_at,expires_at)
      VALUES (?,'test','test:reader','note','thread','x',?,?)`, 'bg-' + String(i).padStart(5,'0'),now-600000,now-1000);
    const cursors: any[] = [];
    const observed = new Proxy(sql, { get(target,key) {
      if(key==='exec') return (q:string,...args:any[])=>{ const c=target.exec(q,...args);cursors.push(c);return c; };
      const value=(target as any)[key];return typeof value==='function'?value.bind(target):value;
    }});
    const checked = new ChecksService(observed as any,()=>now);
    const result = await (checked as any).evaluate({kind:'delivery_slo',params:{max_age_s:60}},{});
    expect(result.status).toBe('error');
    expect(result.detail).toContain('truncated');
    expect(cursors.reduce((n,c)=>n+Number(c.rowsRead),0)).toBeLessThan(2100);
    const plan = sql.exec(`EXPLAIN QUERY PLAN SELECT to_actor,expires_at,registry_unbound_to FROM messages
      WHERE acked_at IS NULL AND created_at < ? ORDER BY created_at LIMIT 1001`,now).toArray() as any[];
    expect(plan.map(r=>r.detail).join(' ')).toContain('messages_check_pending_created');
  }));
  it('prunes at most100 resolved alerts per cron call', () => fixture(async (_hub,state) => {
    const sql = state.storage.sql;
    const old = '2020-01-01T00:00:00.000Z';
    for(let i=0;i<125;i++) sql.exec(`INSERT INTO liveness_alerts(subject_kind,subject,state,detail,opened_at,resolved_at)
      VALUES ('test',?,'ok','x',?,?)`,'old-'+i,old,old);
    new LivenessService(sql).prune();
    expect(Number((sql.exec("SELECT COUNT(*) AS n FROM liveness_alerts WHERE subject_kind='test'").one() as any).n)).toBe(25);
  }));
  it('bounds sparse intake joins before filtering and does not declare an incomplete census idle', () => fixture(async (_hub,state) => {
    const sql = state.storage.sql, now=Date.now(), at=new Date(now).toISOString();
    sql.exec(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<1200)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,state,created_at,updated_at)
      SELECT 'bg-session-'||i,'test','bg-key-'||i,'{}','running',?,? FROM n`,at,at);
    const cursors:any[]=[];
    const observed=new Proxy(sql,{get(target,key){
      if(key==='exec')return(q:string,...args:any[])=>{const c=target.exec(q,...args);cursors.push(c);return c;};
      const value=(target as any)[key];return typeof value==='function'?value.bind(target):value;
    }});
    const service=new OpsLiveness(observed as any,{},()=>now);
    const backlog=service.backlog();
    expect(backlog.truncated).toBe(true);
    expect(backlog.active_sessions).toBe(0);
    expect(cursors.reduce((n,c)=>n+Number(c.rowsRead),0)).toBeLessThan(1000);
    expect(sql.exec(`EXPLAIN QUERY PLAN SELECT id FROM session_control_sessions INDEXED BY ops_active_sessions
      WHERE state IN ('starting','offered','running') ORDER BY id LIMIT 201`).toArray().map((r:any)=>r.detail).join(' '))
      .toContain('ops_active_sessions');
  }));
});
