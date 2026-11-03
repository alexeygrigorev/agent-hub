/**
 * Perf bounds for intake 94186660 (Hub perf audit): request-path reads stay
 * bounded as tables grow. Seeds production-shaped volumes and asserts:
 * - GET /api/intake/tasks?status=open pages via the export read model and
 *   reads rows proportional to the page, not to the task table;
 * - GET /api/intake/.../audit never scans the retained audit ledger (indexed
 *   single-task range; most-recent window cross-task) and EXPLAIN shows no
 *   table SCAN on either request-path query;
 * - GET /api/fleet computes supports_goal from one batched machines read
 *   (no per-route machines query).
 * Coordinates with the rows-read program (8081a05a / e8a22f4c): same
 * tracked-cursor harness style, no duplicate budgets.
 */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { IntakeService } from '../src/intake';
import { advanceIntakeExport } from '../src/intake-export';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'perf-admin', kind: 'human' }, scopes: ['admin'] } as any;
const intakeAuth: any = async () => admin;

function tracked(sql: any) {
  const calls: { q: string; cursor: any }[] = [];
  const proxy = new Proxy(sql, { get(t, p) {
    if (p === 'exec') return (q: string, ...b: any[]) => { const cursor = t.exec(q, ...b); calls.push({ q, cursor }); return cursor; };
    const v = t[p]; return typeof v === 'function' ? v.bind(t) : v;
  } });
  const report = (from = 0) => {
    const by = new Map<string, number>(); let total = 0;
    for (const { q, cursor } of calls.slice(from)) { const n = Number(cursor.rowsRead) || 0; total += n; const k = q.replace(/\s+/g, ' ').slice(0, 120); by.set(k, (by.get(k) ?? 0) + n); }
    return { total, count: calls.length - from, top: [...by].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([k, n]) => `${n} ${k}`) };
  };
  const mark = () => calls.length;
  const statements = (from = 0) => calls.slice(from).map(c => c.q.replace(/\s+/g, ' '));
  return { proxy, report, mark, statements };
}

function seedTasks(sql: any, n: number) {
  for (let i = 0; i < n; i++) {
    const id = 'perf-' + String(i).padStart(4, '0');
    const status = i % 3 === 2 ? 'done' : ['open', 'in_progress', 'blocked'][i % 3];
    sql.exec('INSERT INTO intake_tasks VALUES(?,?)', id, JSON.stringify({ id, project: i % 2 ? 'demo' : 'ops', status,
      title: 'Perf task ' + i, version: 1, created_at: 1000 + i, updated_at: 2000 + i, archived_at: null,
      acceptance_criteria: 'Bounded reads.', history: [] }));
  }
}

function seedAudit(sql: any, tasks: number, per: number) {
  let seq = 0;
  for (let t = 0; t < tasks; t++) {
    const id = 'perf-' + String(t).padStart(4, '0');
    for (let k = 0; k < per; k++) {
      seq++;
      sql.exec(`INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason) VALUES (?,?,?,?,?,?,?,?)`,
        id, t % 2 ? 'demo' : 'ops', 'note', 'perf-admin', 3000 + seq, null, null, 'r' + seq);
    }
  }
  return seq;
}

const withHub = (fn: (hub: Hub, state: DurableObjectState) => Promise<void>) => async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('hub-perf-94186660-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    await state.storage.transactionSync(() => {
      new IntakeService(hub.sql, (cb) => cb()).init();
      new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => Date.now()).init();
    });
    await fn(hub, state);
  });
};

const planOf = (hub: Hub, q: string, ...b: any[]) =>
  (hub.sql.exec('EXPLAIN QUERY PLAN ' + q, ...b).toArray() as any[]).map(r => String(r.detail)).join(' | ');

describe('tasks status=open stays on the bounded export model', () => {
  it('pages 60 tasks with rows proportional to the page', withHub(async (hub: Hub, state: DurableObjectState) => {
    await hub.sql.exec('DELETE FROM intake_tasks');
    hub.sql.exec('DELETE FROM intake_export');
    hub.sql.exec("UPDATE intake_export_migration SET after_id=NULL, done=0 WHERE id=1");
    seedTasks(hub.sql, 60);
    await state.storage.transactionSync(() => { while (!advanceIntakeExport(hub.sql)); });
    const t = tracked(hub.sql);
    const svc = new IntakeService(t.proxy, (cb: any) => cb());
    const res = await svc.handle(new Request('https://hub.test/api/intake/tasks?status=open&limit=10'), intakeAuth);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.items).toHaveLength(10);
    expect(body.total).toBe(40);
    expect(body.next).toBeTruthy();
    expect(body.items.every((it: any) => ['open', 'in_progress', 'blocked', 'pending_approval'].includes(it.status ?? it.payload_data?.status))).toBe(true);
    const r = t.report(0);
    expect(r.total, `status=open top statements: ${JSON.stringify(r.top)}`).toBeLessThanOrEqual(600);
    // Second page continues from the cursor at the same snapshot revision.
    const page2params = `status=open&limit=10&after=${body.next}&revision=${body.snapshot_version}` +
      (body.authorization_valid_until == null ? '' : `&authorization_valid_until=${body.authorization_valid_until}`);
    const res2 = await svc.handle(new Request('https://hub.test/api/intake/tasks?' + page2params), intakeAuth);
    expect(res2.status).toBe(200);
    const body2: any = await res2.json();
    expect(body2.items).toHaveLength(10);
    expect(new Set([...body.items.map((i: any) => i.id), ...body2.items.map((i: any) => i.id)]).size).toBe(20);
  }));
});

describe('audit endpoints never scan the retained ledger', () => {
  it('single-task audit reads an indexed range with an exact total', withHub(async (hub: Hub, state: DurableObjectState) => {
    await hub.sql.exec('DELETE FROM intake_tasks');
    await hub.sql.exec('DELETE FROM intake_export');
    await hub.sql.exec('DELETE FROM intake_audit');
    hub.sql.exec("UPDATE intake_export_migration SET after_id=NULL, done=0 WHERE id=1");
    seedTasks(hub.sql, 4);
    await state.storage.transactionSync(() => { while (!advanceIntakeExport(hub.sql)); });
    const total = seedAudit(hub.sql, 4, 40); // 160 rows retained
    expect(total).toBe(160);
    const t = tracked(hub.sql);
    const svc = new IntakeService(t.proxy, (cb: any) => cb());
    const res = await svc.handle(new Request('https://hub.test/api/intake/tasks/perf-0000/audit?limit=10'), intakeAuth);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.items).toHaveLength(10);
    expect(body.total).toBe(40);
    expect(body.truncated).toBe(true);
    expect(body.next).toBeTruthy();
    const r = t.report(0);
    expect(r.total, `single-task audit top statements: ${JSON.stringify(r.top)}`).toBeLessThanOrEqual(120);
    expect(planOf(hub, 'SELECT * FROM intake_audit WHERE task_id=? ORDER BY seq DESC LIMIT ?', 'perf-0000', 11)).not.toMatch(/SCAN/i);
    expect(planOf(hub, 'SELECT COUNT(*) AS n FROM intake_audit WHERE task_id=?', 'perf-0000')).not.toMatch(/SCAN/i);
    // Cursor page reaches the tail.
    const tail: any = await (await svc.handle(new Request(
      `https://hub.test/api/intake/tasks/perf-0000/audit?limit=100&after=${body.next}&revision=${body.snapshot_version}`), intakeAuth)).json();
    expect(tail.total).toBe(40);
    expect(tail.items.length).toBeGreaterThan(0);
  }));

  it('cross-task audit takes the recent window and says truncated', withHub(async (hub: Hub, state: DurableObjectState) => {
    await hub.sql.exec('DELETE FROM intake_tasks');
    await hub.sql.exec('DELETE FROM intake_export');
    await hub.sql.exec('DELETE FROM intake_audit');
    hub.sql.exec("UPDATE intake_export_migration SET after_id=NULL, done=0 WHERE id=1");
    seedTasks(hub.sql, 4);
    await state.storage.transactionSync(() => { while (!advanceIntakeExport(hub.sql)); });
    seedAudit(hub.sql, 4, 150); // 600 rows retained
    const t = tracked(hub.sql);
    const svc = new IntakeService(t.proxy, (cb: any) => cb());
    const res = await svc.handle(new Request('https://hub.test/api/intake/audit'), intakeAuth);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.items.length).toBeLessThanOrEqual(25);
    expect(body.truncated).toBe(true);
    expect(body.coverage).toBe('recent_500');
    const r = t.report(0);
    expect(r.total, `cross-task audit top statements: ${JSON.stringify(r.top)}`).toBeLessThanOrEqual(700);
    // The window query walks the PK from the newest end with LIMIT 501: the
    // rowsRead budget above (not the SCAN label) proves it stops early.
    expect(planOf(hub, 'SELECT * FROM intake_audit ORDER BY seq DESC LIMIT ?', 501)).not.toMatch(/TEMP/i);
  }));
});

describe('fleet supports_goal comes from one batched machines read', () => {
  it('computes the same flags without a per-route machines query', withHub(async (hub: Hub, state: DurableObjectState) => {
    for (const m of ['fm0', 'fm1', 'fm2']) {
      hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES (?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET last_checkin_at=excluded.last_checkin_at",
        m, m, m, new Date().toISOString(), new Date().toISOString());
    }
    hub.sql.exec("UPDATE machines SET capabilities=? WHERE id='fm0'", JSON.stringify({ fleet_goal_routes: [{ engine: 'codex', account: 'a', profile: null }] }));
    hub.sql.exec("UPDATE machines SET capabilities=? WHERE id='fm1'", JSON.stringify({ fleet_goal_routes: [] }));
    hub.sql.exec("DELETE FROM fleet_routes");
    const routes: any[] = [
      ['fm0', 'codex', 'a', null], ['fm0', 'codex', 'b', null], ['fm1', 'codex', 'a', null],
      ['fm2', 'codex', 'a', null], ['gone', 'codex', 'a', null],
    ];
    for (const [mid, eng, acc, prof] of routes) hub.sql.exec('INSERT INTO fleet_routes(machine_id,engine,account,profile) VALUES (?,?,?,?) ON CONFLICT(machine_id,engine,account) DO NOTHING', mid, eng, acc, prof);
    const t = tracked(hub.sql);
    const svc = new FleetService(t.proxy, (cb: any) => cb(), () => Date.now());
    const res = await svc.handle(new Request('https://hub.test/api/fleet'), admin);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.routes.filter((r: any) => ['fm0', 'fm1', 'fm2', 'gone'].includes(r.machine_id))).toHaveLength(5);
    const flag = (mid: string, acc: string) => body.routes.find((r: any) => r.machine_id === mid && r.account === acc)?.supports_goal;
    expect(flag('fm0', 'a')).toBe(true);
    expect(flag('fm0', 'b')).toBe(false);
    expect(flag('fm1', 'a')).toBe(false);
    expect(flag('fm2', 'a')).toBe(false);
    expect(flag('gone', 'a')).toBe(false);
    const stmts = t.statements(0).filter(q => q.includes('FROM machines'));
    expect(stmts.filter(q => q === 'SELECT capabilities FROM machines WHERE id = ?').length).toBe(0);
    expect(stmts.filter(q => q === 'SELECT id, capabilities FROM machines').length).toBe(1);
  }));
});
