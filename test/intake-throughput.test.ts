import { freshFixtureBudgetDay, fixtureSql } from './helpers/fixture-budget';
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { OpsLiveness, THROUGHPUT_OBSERVATION_LIMIT, THROUGHPUT_QUERIES } from '../src/ops-liveness';

const NOW = Date.parse('2026-11-01T10:00:00Z'), DAY = 86400000;
async function fixture(fn: (h: any, ops: OpsLiveness, alerts: any[]) => Promise<void> | void) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('throughput-' + crypto.randomUUID()));
  await (runInDurableObject as any)(stub, async (h: any) => {
    await freshFixtureBudgetDay(h);
    const alerts: any[] = [];
    const ops = new OpsLiveness(h.sql, { queuePolicy: () => ({ enabled: true, valid: true, max_inflight: 8,
      capacity: { source: 'live_host_account', active: 3, available_slots: 5 } }),
      holdReasons: () => ({ workspace_configuration_required: 2 }),
      alert: async (subject, detail) => { alerts.push({ subject, detail }); } }, () => NOW);
    ops.init();
    await fn(h, ops, alerts);
  });
}
function task(h: any, id: string, at: number) {
  fixtureSql(h).exec('INSERT INTO intake_task_projection(id,data,links) VALUES(?,?,?)', id,
    JSON.stringify({id, project:'demo', status:'open', created_at:at}), '[]');
}
function session(h: any, id: string, taskId: string, at: number, automatic = true, bound = true) {
  fixtureSql(h).exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at,binding_json)
    VALUES(?,?,?,?,'headless',?,?,?,?)`, id, automatic ? 'hub:service:intake-watchdog' : 'operator', id,
    JSON.stringify({intake_task_id:taskId}), bound ? 'running' : 'offered', new Date(at).toISOString(), new Date(at).toISOString(),
    bound ? JSON.stringify({native_id:id,native_created_at_ms:at}) : null);
}
describe('intake throughput evidence', () => {
  it('counts distinct native-started intakes, not offers, and uses a half-open 24h window', () => fixture((h, ops) => {
    session(h, 'retry1', 'one', NOW - 1000); session(h, 'retry2', 'one', NOW - 500);
    session(h, 'manual', 'two', NOW - DAY, false); session(h, 'offer', 'three', NOW - 100, true, false);
    session(h, 'old', 'four', NOW - DAY - 1); session(h, 'future', 'five', NOW);
    expect(ops.throughput()).toMatchObject({ started_24h: 2, automatic_started_24h: 1 });
  }));
  it('counts lifecycle closes once, excludes unverified/archives, and exposes growing backlog with idle capacity', async () => fixture(async (h, ops, alerts) => {
    task(h, 'created-one', NOW - 100); task(h, 'created-two', NOW - DAY); task(h, 'future', NOW);
    for (const [id, kind, at] of [['one','close',NOW-100], ['one','archive',NOW-100], ['two','close_unverified',NOW-100], ['old','cancel',NOW-DAY-1]] as const)
      fixtureSql(h).exec("INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,reason) VALUES(?,'demo',?,'operator',?,'test')", id, kind, at);
    const result = ops.throughput();
    expect(result).toMatchObject({ created_24h: 2, closed_24h: 1, net_24h: 1,
      active_vs_capacity: { active_sessions: 3, effective_capacity: 8, idle_capacity: 5 },
      holds_by_reason: { workspace_configuration_required: 2 } });
    expect(result.alert).toContain('created 2 > closed 1');
    expect(ops.status().throughput).toEqual(result);
    await ops.tick();
    expect(alerts).toContainEqual({subject:'ops-backlog-throughput',detail:result.alert});
  }));
  it('reports unknown capacity and never fabricates an idle-capacity alert', () => fixture((h) => {
    task(h, 'one', NOW - 100);
    const ops = new OpsLiveness(h.sql, {}, () => NOW);
    expect(ops.throughput()).toMatchObject({ active_vs_capacity: {active_sessions:null,idle_capacity:null}, alert:null });
  }));
  it('keeps close and cancellation evidence distinct within a half-open window',()=>fixture((h,ops)=>{
    for(const [id,kind,at] of [['edge','close',NOW-DAY],['edge','cancel',NOW-DAY],['old','cancel',NOW-DAY-1],['future','close',NOW]] as const)
      fixtureSql(h).exec("INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,reason) VALUES(?,'demo',?,'operator',?,'test')",id,kind,at);
    expect(ops.throughput()).toMatchObject({complete:true,closed_24h:1});
  }));
  it.each(['starts','created','closed'])('reports unknown on overflowing %s observations without fabricated growth alerts', kind => fixture(async (h,ops,alerts) => {
    for(let i=0;i<=THROUGHPUT_OBSERVATION_LIMIT;i++) {
      if(kind==='starts') session(h,'start-'+i,'same-intake',NOW-1000);
      if(kind==='created') task(h,'created-'+i,NOW-1000);
      if(kind==='closed') fixtureSql(h).exec("INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,reason) VALUES('same-intake','demo','close','operator',?,'test')",NOW-1000);
    }
    const result=ops.throughput();
    expect(result.complete).toBe(false);
    expect(result.observation_limit).toBe(THROUGHPUT_OBSERVATION_LIMIT);
    if(kind==='starts') expect(result).toMatchObject({started_24h:null,automatic_started_24h:null,created_24h:0,closed_24h:0});
    if(kind==='created') expect(result).toMatchObject({created_24h:null,closed_24h:0,net_24h:null,alert:null,alert_evaluable:false});
    if(kind==='closed') expect(result).toMatchObject({created_24h:0,closed_24h:null,net_24h:null,alert:null,alert_evaluable:false});
    expect(result.incomplete_metrics).toContain(kind==='starts'?'started_24h':kind+'_24h');
    if(kind!=='starts') {
      await ops.tick();
      // Unknown evidence must not resolve a previously open growth alert.
      expect(alerts.some(a=>a.subject==='ops-backlog-throughput')).toBe(false);
    }
  }));
  it('keeps exact distinct counts at the budget boundary and excludes unrelated retained audit history',()=>fixture((h,ops)=>{
    for(let i=0;i<THROUGHPUT_OBSERVATION_LIMIT;i++) {
      session(h,'retry-'+i,'same-intake',NOW-1000,i%2===0);
      task(h,'created-'+i,NOW-1000);
      fixtureSql(h).exec("INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,reason) VALUES('same-intake','demo',?,'operator',?,'test')",i%2?'close':'cancel',NOW-1000);
    }
    for(let i=0;i<THROUGHPUT_OBSERVATION_LIMIT+1;i++)
      fixtureSql(h).exec("INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,reason) VALUES('unrelated','demo','note','operator',?,'test')",NOW-1000);
    expect(ops.throughput()).toMatchObject({complete:true,incomplete_metrics:[],started_24h:1,automatic_started_24h:1,
      created_24h:THROUGHPUT_OBSERVATION_LIMIT,closed_24h:1,net_24h:THROUGHPUT_OBSERVATION_LIMIT-1});
  }));
  it('uses bounded range-index reads with no DISTINCT or temporary B-tree on every throughput path',()=>fixture((h)=>{
    const expected={starts:'ops_intake_native_started',created:'ops_intake_created',closed:'ops_intake_closed_time'};
    for(const [name,query] of Object.entries(THROUGHPUT_QUERIES)) {
      const detail=h.sql.exec('EXPLAIN QUERY PLAN '+query,NOW-DAY,NOW).toArray().map((r:any)=>r.detail).join('\n');
      expect(detail).toMatch(new RegExp('USING (?:COVERING )?INDEX '+expected[name as keyof typeof expected]));
      expect(detail).toMatch(/SEARCH/);
      expect(detail).not.toMatch(/TEMP B-TREE|SCAN /);
      expect(query).not.toMatch(/COUNT|DISTINCT|GROUP BY/i);
      expect(query).toContain('LIMIT '+(THROUGHPUT_OBSERVATION_LIMIT+1));
    }
  }));

});
