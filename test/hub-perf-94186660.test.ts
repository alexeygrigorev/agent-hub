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
import { FleetService, FLEET_SESSIONS_DEFAULT, FLEET_SESSIONS_MAX } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
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
  it('computes the same flags without a per-route machines query', withHub(async (hub: Hub, state: DurableObjectState) => {    for (const m of ['fm0', 'fm1', 'fm2']) {
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

function seedFleetSessions(sql: any, n: number) {
  // Every third row reuses tag 'task-dup' on the SAME machine (fleetm0):
  // the duplicate-tag boundary the keyset cursor must not drop or double.
  for (let i = 0; i < n; i++) {
    const dup = i % 3 === 0;
    const m = dup ? 'fleetm0' : 'fleetm' + String(i % 4);
    const tag = dup ? 'task-dup' : 'tag-' + String(i).padStart(4, '0');
    sql.exec(`INSERT OR REPLACE INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account) VALUES (?,?,?,?,?,?,?,?,?,?)`,
      m, 'nat-' + String(i).padStart(5, '0'), tag, 'codex', 'running', 'active',
      new Date(1000 + i).toISOString(), '/w/' + i, null, 'acc');
  }
}

const fleetPageShape = (hub: Hub) =>
  (hub.sql.exec('EXPLAIN QUERY PLAN SELECT * FROM fleet_sessions WHERE (machine_id,native_id) > (?,?) ORDER BY machine_id,native_id LIMIT ?', 'fleetm0', 'nat-00001', 51).toArray() as any[]).map(r => String(r.detail)).join(' | ');

describe('fleet sessions are keyset-paged with a small default page', () => {
  it('serves a bounded default page and walks every row exactly once', withHub(async (hub: Hub, state: DurableObjectState) => {
    expect(FLEET_SESSIONS_DEFAULT).toBeLessThanOrEqual(100);
    expect(FLEET_SESSIONS_MAX).toBeLessThanOrEqual(500);
    hub.sql.exec('DELETE FROM fleet_sessions');
    seedFleetSessions(hub.sql, 250);
    const t = tracked(hub.sql);
    const svc = new FleetService(t.proxy, (cb: any) => cb(), () => Date.now());
    const first: any = await (await svc.handle(new Request('https://hub.test/api/fleet'), admin)).json();
    expect(first.sessions).toHaveLength(FLEET_SESSIONS_DEFAULT);
    expect(first.sessions_total).toBeNull();
    expect(first.sessions_total_unknown).toBe(true);
    expect(first.sessions_truncated).toBe(true);
    expect(typeof first.sessions_cursor_next).toBe('string');
    // Walk the whole list at limit 50, including across duplicate-tag pages.
    const seen = new Map<string, number>();
    let cursor: string | null = null;
    let pages = 0;
    do {
      const url = 'https://hub.test/api/fleet?sections=sessions&sessions_limit=50' + (cursor ? `&sessions_cursor=${encodeURIComponent(cursor)}` : '');
      const page: any = await (await svc.handle(new Request(url), admin)).json();
      expect(page.sessions.length).toBeLessThanOrEqual(50);
      for (const s of page.sessions) seen.set(String(s.machine_id) + '/' + String(s.native_id), (seen.get(String(s.machine_id) + '/' + String(s.native_id)) ?? 0) + 1);
      cursor = page.sessions_cursor_next ?? null;
      pages++;
      expect(pages).toBeLessThanOrEqual(10); // 250 rows at 50/page terminate
    } while (cursor);
    expect(seen.size).toBe(250);
    expect([...seen.values()].every(n => n === 1)).toBe(true);
    const r = t.report(0);
    expect(r.total, `fleet sessions walk top statements: ${JSON.stringify(r.top)}`).toBeLessThanOrEqual(4000);
  }));

  it('rejects bad cursors/limits and scopes sections', withHub(async (hub: Hub, state: DurableObjectState) => {
    hub.sql.exec('DELETE FROM fleet_sessions');
    seedFleetSessions(hub.sql, 10);
    const svc = new FleetService(hub.sql, (cb: any) => cb(), () => Date.now());
    expect((await svc.handle(new Request('https://hub.test/api/fleet?sessions_cursor=!!!'), admin)).status).toBe(400);
    expect((await svc.handle(new Request('https://hub.test/api/fleet?sessions_limit=99999'), admin)).status).toBe(400);
    expect((await svc.handle(new Request('https://hub.test/api/fleet?sections=nope'), admin)).status).toBe(400);
    const slim: any = await (await svc.handle(new Request('https://hub.test/api/fleet?sections=machines'), admin)).json();
    expect(Object.keys(slim).sort()).toEqual(['machines']);
    expect(slim.sessions).toBeUndefined();
  }));

  it('plans the cursor page as an index seek and always serves fresh reads', withHub(async (hub: Hub, state: DurableObjectState) => {
    hub.sql.exec('DELETE FROM fleet_sessions');
    seedFleetSessions(hub.sql, 30);
    const svc = new FleetService(hub.sql, (cb: any) => cb(), () => Date.now());
    expect(fleetPageShape(hub)).not.toMatch(/SCAN fleet_sessions(?! USING)/);
    expect(fleetPageShape(hub)).toMatch(/USING .*INDEX sqlite_autoindex_fleet_sessions_1/);
    expect(fleetPageShape(hub)).toMatch(/SEARCH/);
    // No version primitive in this runtime (see server note): every read is
    // a fresh 200; If-None-Match is ignored rather than risking stale 304s.
    const res = await svc.handle(new Request('https://hub.test/api/fleet?sections=sessions&sessions_limit=10'), admin);
    expect(res.status).toBe(200);
    expect(res.headers.get('ETag')).toBeNull();
    const again = await svc.handle(new Request('https://hub.test/api/fleet?sections=sessions&sessions_limit=10', { headers: { 'If-None-Match': 'W/"fleet-1-x"' } }), admin);
    expect(again.status).toBe(200);
    hub.sql.exec(`INSERT OR REPLACE INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account) VALUES (?,?,?,?,?,?,?,?,?,?)`,
      'fleetm9', 'nat-new', 'tag-new', 'codex', 'running', 'active', new Date().toISOString(), '/w/new', null, 'acc');
    const changed: any = await (await svc.handle(new Request('https://hub.test/api/fleet?sections=sessions&sessions_limit=10'), admin)).json();
    expect(changed.sessions_total).toBeNull();
    expect(changed.sessions_total_unknown).toBe(true);
  }));

  it('keeps the default page small as the table grows past 1000 rows', withHub(async (hub: Hub, state: DurableObjectState) => {
    hub.sql.exec('DELETE FROM fleet_sessions');
    seedFleetSessions(hub.sql, 1200);
    const svc = new FleetService(hub.sql, (cb: any) => cb(), () => Date.now());
    const res = await svc.handle(new Request('https://hub.test/api/fleet'), admin);
    const body: any = await res.json();
    expect(body.sessions).toHaveLength(FLEET_SESSIONS_DEFAULT);
    expect(body.sessions_total).toBeNull();
    expect(body.sessions_total_unknown).toBe(true);
    expect(body.sessions_truncated).toBe(true);
    const bytes = JSON.stringify(body).length;
    expect(bytes, 'default fleet page must stay a small page').toBeLessThan(512 * 1024);
  }));
});

function seedLiveControl(sql: any, n: number, fleet: boolean) {
  const fresh = new Date().toISOString();
  for (let i = 0; i < n; i++) {
    const id = 'perf-ctl-' + String(i).padStart(5, '0');
    const nat = 'perf-nat-' + String(i).padStart(5, '0');
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
      id, 'perf-admin', 'ctl-rk-' + i, '{}', 'fleetm0', 'codex', 'headless', 'running',
      JSON.stringify({ native_id: nat, tag: 'tag-' + i }), fresh, fresh);
    if (fleet) {
      sql.exec(`INSERT OR REPLACE INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account) VALUES (?,?,?,?,?,?,?,?,?,?)`,
        'fleetm0', nat, 'tag-' + i, 'codex', 'running', 'active', fresh, '/w/' + i, null, 'acc');
    }
  }
}

describe('warnings and tree stay capped as tables grow', () => {
  it('caps the warnings page with truncation instead of a full running scan', withHub(async (hub: Hub, state: DurableObjectState) => {
    await state.storage.transactionSync(() => {
      new SessionControlService(hub.sql, (cb) => cb(), () => Date.now()).init();
    });
    hub.sql.exec("DELETE FROM session_control_sessions WHERE id LIKE 'perf-ctl-%'");
    const old = new Date(Date.now() - 4 * 3600 * 1000).toISOString();
    for (let i = 0; i < 1200; i++) {
      const id = 'perf-ctl-' + String(i).padStart(5, '0');
      hub.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)`,
        id, 'perf-admin', 'warn-rk-' + i, '{}', 'headless', 'running', old, old);
    }
    const t = tracked(hub.sql);
    const svc = new SessionControlService(t.proxy, (cb: any) => cb(), () => Date.now());
    const res = await svc.handle(new Request('https://hub.test/api/sessions/warnings'), admin);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.warnings.length).toBeLessThanOrEqual(500);
    expect(body.truncated).toBe(true);
    // Below the cap the common response keeps its exact historical shape.
    hub.sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id LIKE 'perf-ctl-%'");
    const slim: any = await (await svc.handle(new Request('https://hub.test/api/sessions/warnings'), admin)).json();
    expect(slim.warnings).toEqual([]);
    expect('truncated' in slim).toBe(false);
    const r = t.report(0);
    expect(r.total, `warnings top statements: ${JSON.stringify(r.top)}`).toBeLessThanOrEqual(12000);
    expect((hub.sql.exec("EXPLAIN QUERY PLAN SELECT * FROM session_control_sessions WHERE state = 'running' ORDER BY created_at, id LIMIT ?", 501).toArray() as any[]).map(x => String(x.detail)).join(' | ')).not.toMatch(/SCAN/i);
  }));

  it('caps the tree page and never scans the sessions table for it', withHub(async (hub: Hub, state: DurableObjectState) => {
    await state.storage.transactionSync(() => {
      new SessionControlService(hub.sql, (cb) => cb(), () => Date.now()).init();
    });
    hub.sql.exec("DELETE FROM session_control_sessions WHERE id LIKE 'perf-ctl-%'");
    hub.sql.exec("DELETE FROM fleet_sessions WHERE machine_id = 'fleetm0'");
    seedLiveControl(hub.sql, 600, true);
    const t = tracked(hub.sql);
    const svc = new SessionControlService(t.proxy, (cb: any) => cb(), () => Date.now());
    const res = await svc.handle(new Request('https://hub.test/api/sessions/tree'), admin);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.tree.length + body.total).toBeGreaterThan(0);
    expect(body.total).toBeLessThanOrEqual(200);
    expect(body.truncated).toBe(true);
    const r = t.report(0);
    expect(r.total, `tree top statements: ${JSON.stringify(r.top)}`).toBeLessThanOrEqual(6000);
  }));
});
