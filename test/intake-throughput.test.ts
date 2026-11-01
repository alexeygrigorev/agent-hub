import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { OpsLiveness } from '../src/ops-liveness';

const NOW = Date.parse('2026-11-01T10:00:00Z'), DAY = 86400000;
async function fixture(fn: (h: any, ops: OpsLiveness, alerts: any[]) => Promise<void> | void) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('throughput-' + crypto.randomUUID()));
  await (runInDurableObject as any)(stub, async (h: any) => {
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
  h.sql.exec('INSERT INTO intake_task_projection(id,data,links) VALUES(?,?,?)', id,
    JSON.stringify({id, project:'demo', status:'open', created_at:at}), '[]');
}
function session(h: any, id: string, taskId: string, at: number, automatic = true, bound = true) {
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at,binding_json)
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
      h.sql.exec("INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,reason) VALUES(?,'demo',?,'operator',?,'test')", id, kind, at);
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
});
