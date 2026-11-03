/**
 * Write-through cache budgets (intake 8081a05a/e8a22f4c phase 2). The
 * host-polled routes serve from in-memory caches stamped by per-table write
 * generations: while no source table is written, a poll reads zero rows, and
 * any write to a source table rebuilds on the next call. Seeds a
 * production-shaped history and asserts the rows a repeat poll reads, the
 * freshness of each cached answer after targeted writes, and that census
 * activity-only refreshes do not rebuild authority history.
 */
import { it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { SqlStorage } from '@cloudflare/workers-types';
import { FleetService } from '../src/fleet';
import { LivenessService } from '../src/liveness';
import { WriteWatch } from '../src/write-watch';
import { ObservedCatalog } from '../src/observed-catalog';
import { RowsMeter } from '../src/rows-meter';
import type { Hub } from '../src/hub';

const now = Date.UTC(2026, 10, 2, 12, 0, 0);
const iso = (offset = 0) => new Date(now + offset).toISOString();
const HISTORY = 400;
const host = { caller: { machineId: 'h0', kind: 'host', actor: 'machine:h0' }, scopes: ['host'] } as any;
const uuid = (p: number, i: number) => `${String(p).padStart(8, '0')}-0000-4000-8000-${String(i).padStart(12, '0')}`;

/** Wrap sql so every cursor is kept; rowsRead is final once consumed. */
function tracked(sql: any) {
  const calls: { q: string; cursor: any }[] = [];
  const target: any = sql;
  const proxy: any = new Proxy(target, { get(t: any, p) {
    if (p === 'exec') return (q: string, ...b: any[]) => { const cursor: any = t.exec(q, ...b); calls.push({ q, cursor }); return cursor; };
    const v = t[p]; return typeof v === 'function' ? v.bind(t) : v;
  } });
  const report = (from = 0) => {
    let total = 0; const by = new Map<string, number>();
    for (const { q, cursor } of calls.slice(from)) {
      const n = Number(cursor.rowsRead) || 0; total += n;
      const k = String(q).replace(/\s+/g, ' ').slice(0, 90);
      by.set(k, (by.get(k) ?? 0) + n);
    }
    return { total, top: [...by].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k, n]) => `${n} ${k}`) };
  };
  const mark = () => calls.length;
  return { proxy, report, mark };
}

function seed(sql: any) {
  sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES ('h0','h0','h0',?, ?,0)", iso(), iso());
  sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES ('h1','h1','h1',?, ?,0)", iso(), iso());
  for (let m = 0; m < 2; m++) for (let i = 0; i < HISTORY; i++) {
    const n = m * HISTORY + i, launch = uuid(1, n), native = uuid(3, n), live = i < 3;
    sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, launch, 'owner', 'agent', 's', 'k' + n, '{}', 'h' + m, 'codex', 'a', 'interactive', '/w', 'p', live ? 'running' : 'exited', native, iso(-86400000 + n), iso(-3600000 + n));
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, uuid(2, n), 'owner', 'r' + n, '{}', launch, 'h' + m, 'codex', 'interactive', live ? 'running' : 'stopped',
      JSON.stringify({ native_id: native, tag: 'hub-' + launch, workspace: '/w', native_created_at_ms: now - 1000 - n }), iso(-86400000 + n), iso(-3600000 + n));
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,cwd,created_at_ms,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
      'h' + m, native, 'hub-' + launch, 'codex', live ? 'running' : 'exited', null, iso(-60000), '/w', '/w', now - 1000 - n, iso(-30000));
  }
  sql.exec("INSERT INTO fleet_catalog_seen VALUES ('h0', ?) ON CONFLICT(machine_id) DO UPDATE SET observed_at=excluded.observed_at", iso(-1000));
}

const withFleet = (fn: (s: any, t: ReturnType<typeof tracked>, watch: WriteWatch, catalog: ObservedCatalog, rawSql: SqlStorage) => Promise<void>) => async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('write-through-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    seed(hub.sql);
    const t = tracked(hub.sql);
    const watch = new WriteWatch();
    watch.trackAll(['session_control_sessions','fleet_launches','machines','intake_tasks','intake_task_projection','resource_request_projection','session_control_protections','resource_reaps','resource_settings','host_health_observations','session_idle_episodes']);
    const watched: any = watch.wrap(t.proxy as any);
    const catalog = new ObservedCatalog(watched, watch);
    const s: any = new FleetService(watched, (cb: any) => watch.transaction(() => state.storage.transactionSync(cb)), () => now, watch, catalog);
    s.init();
    await fn(s, t, watch, catalog, state.storage.sql);
  });
};

it('a repeat offers poll with nothing to offer reads zero rows', withFleet(async (s, t) => {
  const poll = () => s.handle(new Request('https://hub.test/api/fleet/offers'), host);
  expect((await poll()).status).toBe(200);
  const from = t.mark();
  const second = await poll();
  expect(second.status).toBe(200);
  const body: any = await second.json();
  expect(body.offers).toEqual([]);
  expect(t.report(from)).toStrictEqual({ total: 0, top: [] });
}));

it('the observed catalog hydrates with machine-indexed reads and serves warm global reads from memory', withFleet(async (_s, t, _watch, catalog) => {
  expect(catalog.rows()).toHaveLength(2 * HISTORY);
  const from = t.mark();
  expect(catalog.rows()).toHaveLength(2 * HISTORY);
  expect(t.report(from)).toStrictEqual({ total: 0, top: [] });
  expect(t.report().top.join('\n')).not.toMatch(/SELECT \* FROM fleet_sessions(?!\s+WHERE\b)/i);
}));

it('an offered launch invalidates the cached all-clear on the next poll', withFleet(async (s, t) => {
  const poll = () => s.handle(new Request('https://hub.test/api/fleet/offers'), host);
  expect((await poll()).status).toBe(200);
  (s as any).sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
    VALUES ('launch-new','owner','agent','s','kn','{}','h0','codex','a','interactive','/w','p','offered',?,?)`, iso(), iso());
  const body: any = await (await poll()).json();
  expect(body.offers.map((o: any) => o.id)).toEqual(['launch-new']);
}));

it('a repeat resources/watchdog poll reads zero rows until a source table is written', withFleet(async (s, t) => {
  const get = () => s.handle(new Request('https://hub.test/api/fleet/resources/watchdog'), host);
  const first: any = await (await get()).json();
  expect(first.sessions.length).toBeGreaterThan(0);
  const from = t.mark();
  const second: any = await (await get()).json();
  expect(second.sessions.length).toBe(first.sessions.length);
  expect(t.report(from).total).toBe(0);
  // An authority write must be visible immediately on the next poll.
  (s as any).sql.exec(`UPDATE session_control_sessions SET state='failed', updated_at=? WHERE id=?`, iso(), uuid(2, 0));
  const third: any = await (await get()).json();
  const changed = third.sessions.find((x: any) => x.session_id === uuid(2, 0));
  expect(changed.state).toBe('failed');
}));

it('census activity-only refreshes patch the catalog without rebuilding it', withFleet(async (s, t, watch) => {
  const get = () => s.handle(new Request('https://hub.test/api/fleet/resources/watchdog'), host);
  const census = (activity: string) => s.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({
    census_complete: false,
    sessions: [{ native_id: uuid(3, 0), tag: 'hub-' + uuid(1, 0), engine: 'codex', phase: 'running', confirmed_exit: false, agent_state: null,
      workspace: '/w', cwd: '/w', profile: null, account: null, last_activity_at: activity, created_at_ms: now - 1000 }] }) }), host);
  // Let first-census adoption and cache hydration finish before measuring the
  // following activity-only update, which must not rebuild authority history.
  expect((await census(iso(-30_000))).status).toBe(200);
  // The first observation may also finish late-linking metadata. Run that
  // reconciliation once before warming the route snapshot used below.
  expect((await census(iso(-30_000))).status).toBe(200);
  const before: any = await (await get()).json();
  expect(before.sessions.length).toBeGreaterThan(0);
  const from = t.mark();
  const cacheTables = ['session_control_sessions','fleet_launches','machines','session_control_protections','resource_reaps',
    'intake_tasks','intake_task_projection','resource_request_projection','fleet_sessions_meta','resource_settings',
    'host_health_observations','session_idle_episodes'];
  const stamps = Object.fromEntries(cacheTables.map(table => [table, watch.stamp(table)]));
  // One native's activity moves: identical identity fields, new timestamp, so
  // the census writes the row and patches the map without a metadata bump.
  const native = uuid(3, 0);
  const put = await census(iso());
  expect(put.status).toBe(200);
  const invalidated = cacheTables.filter(table => watch.stamp(table) !== stamps[table]);
  expect(invalidated).toEqual([]);
  const mid = t.report(from);
  // The census itself reads per changed row (PK lookups), never per history.
  expect({ total: mid.total, top: mid.top }).toMatchObject({ total: expect.any(Number) });
  expect(mid.total).toBeLessThan(1000);
  const from2 = t.mark();
  const after: any = await (await get()).json();
  const row = after.sessions.find((x: any) => x.native_id === native);
  expect(Date.parse(row.last_activity_at)).toBe(Date.parse(iso()));
  // The catalog was not rebuilt for the activity change: zero rows read.
  expect(t.report(from2)).toStrictEqual({ total: 0, top: [] });
}));

it('does not retain SQL memo rows read inside a rolled-back transaction', withFleet(async (s, t) => {
  const read = () => (s as any).sql.exec('SELECT state FROM fleet_launches WHERE id = ?', uuid(1, 0)).one().state;
  expect(read()).toBe('running');
  expect(() => (s as any).transaction(() => {
    (s as any).sql.exec("UPDATE fleet_launches SET state = 'failed' WHERE id = ?", uuid(1, 0));
    expect(read()).toBe('failed');
    throw new Error('rollback memo row');
  })).toThrow('rollback memo row');
  const from = t.mark();
  expect(read()).toBe('running');
  expect(t.report(from).total).toBeGreaterThan(0);
}));

it('liveness alerts() reads a bounded page of a long alert history', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('liveness-rows-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => {
    const t = tracked(hub.sql);
    const liveness = new LivenessService(t.proxy as any);
    liveness.init();
    for (let i = 0; i < 2000; i++) {
      hub.sql.exec(`INSERT INTO liveness_alerts (subject_kind, subject, state, detail, opened_at, resolved_at)
        VALUES ('machine', ?, 'offline', 'd', ?, ?)`, 'm' + i, iso(-i * 1000), i > 5 ? iso(-i * 1000 + 500) : null);
    }
    const from = t.mark();
    const alerts = liveness.alerts(100) as any[];
    const r = t.report(from);
    expect(alerts).toHaveLength(100);
    expect(alerts[0].resolved_at).toBeNull();
    expect(alerts.filter((a: any) => a.resolved_at === null).length).toBe(6);
    expect(r.total).toBeLessThanOrEqual(400);
  });
});

it('cachedAccountStatus serves the admission probe from memory until a fleet write', withFleet(async (s, t) => {
  const sql = (s as any).sql;
  sql.exec(`INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled)
    VALUES ('a0','codex',10,0,1)`);
  const account: any = sql.exec(`SELECT * FROM fleet_accounts WHERE label='a0'`).toArray()[0];
  expect(s.cachedAccountStatus(account).status).toBeDefined();
  const from = t.mark();
  expect(s.cachedAccountStatus(account).status).toBeDefined();
  expect(t.report(from).total).toBe(0);
  sql.exec(`UPDATE fleet_launches SET updated_at=? WHERE id=?`, iso(), uuid(1, 0));
  expect(s.cachedAccountStatus(account).status).toBeDefined();
}));

it('write watch stamps move exactly with the observed writes', () => {
  const w = new WriteWatch();
  const a = w.stamp('fleet_launches', 'machines');
  w.note('SELECT * FROM fleet_launches WHERE id = ?');
  expect(w.stamp('fleet_launches', 'machines')).toBe(a);
  w.note("UPDATE fleet_launches SET state = 'offered' WHERE id = 'x'");
  expect(w.stamp('fleet_launches', 'machines')).not.toBe(a);
  const b = w.stamp('machines', 'fleet_launches');
  w.note("INSERT INTO machines VALUES ('m9','m9','m9',null,null,0)");
  expect(w.stamp('machines', 'fleet_launches')).not.toBe(b);
  // DDL bumps every stamp; rollback over-invalidation is acceptable by design.
  const before = w.stamp('machines');
  w.note('CREATE TABLE IF NOT EXISTS scratch (id INTEGER)');
  expect(w.stamp('machines')).not.toBe(before);
});

it('memoizes complete stable SQL reads and invalidates them on direct and trigger writes', withFleet(async (s, t, watch) => {
  const sql = (s as any).sql;
  const firstCursor: any = sql.exec('SELECT name FROM machines WHERE id=?', 'h0');
  expect(firstCursor.toArray()[0].name).toBe('h0');
  expect(Number(firstCursor.rowsRead)).toBeGreaterThan(0);

  const cachedCursor: any = sql.exec('SELECT name FROM machines WHERE id=?', 'h0');
  const cached = cachedCursor.toArray();
  expect(cached[0].name).toBe('h0');
  expect(Number(cachedCursor.rowsRead)).toBe(0);

  // A returned object is a copy; mutating it cannot poison a later cache hit.
  cached[0].name = 'corrupted';
  expect(sql.exec('SELECT name FROM machines WHERE id=?', 'h0').toArray()[0].name).toBe('h0');

  sql.exec('UPDATE machines SET name=? WHERE id=?', 'renamed', 'h0');
  const afterWrite: any = sql.exec('SELECT name FROM machines WHERE id=?', 'h0');
  expect(afterWrite.toArray()[0].name).toBe('renamed');
  expect(Number(afterWrite.rowsRead)).toBeGreaterThan(0);

  const beforeNoop = watch.stamp('machines');
  const noOp: any = sql.exec('UPDATE machines SET name=name WHERE id=?', 'missing-machine');
  expect(Number(noOp.rowsWritten)).toBe(0);
  expect(watch.stamp('machines')).toBe(beforeNoop);

  sql.exec('CREATE TABLE memo_source(id TEXT PRIMARY KEY,value TEXT NOT NULL)');
  sql.exec('CREATE TABLE memo_dest(id TEXT PRIMARY KEY,value TEXT NOT NULL)');
  sql.exec('INSERT INTO memo_source VALUES(?,?)', 'x', 'before');
  sql.exec('INSERT INTO memo_dest VALUES(?,?)', 'x', 'before');
  sql.exec(`CREATE TRIGGER memo_source_update AFTER UPDATE ON memo_source BEGIN
    UPDATE memo_dest SET value=NEW.value WHERE id=NEW.id;
  END`);
  expect(sql.exec('SELECT value FROM memo_dest WHERE id=?', 'x').toArray()[0].value).toBe('before');
  sql.exec('UPDATE memo_source SET value=? WHERE id=?', 'after', 'x');
  const triggered: any = sql.exec('SELECT value FROM memo_dest WHERE id=?', 'x');
  expect(triggered.toArray()[0].value).toBe('after');
  expect(Number(triggered.rowsRead)).toBeGreaterThan(0);
}));

it('does not cache a cursor across a write that occurs before it is consumed', withFleet(async (s) => {
  const sql = (s as any).sql;
  const pending: any = sql.exec('SELECT name FROM machines WHERE id=?', 'h1');
  sql.exec('UPDATE machines SET name=? WHERE id=?', 'during-read', 'h1');
  pending.toArray();
  const fresh: any = sql.exec('SELECT name FROM machines WHERE id=?', 'h1');
  expect(fresh.toArray()[0].name).toBe('during-read');
  expect(Number(fresh.rowsRead)).toBeGreaterThan(0);
}));

it('leaves comma-source reads uncached so every dependency stays fresh', withFleet(async (s) => {
  const sql = (s as any).sql;
  sql.exec('CREATE TABLE memo_a(id TEXT PRIMARY KEY,value TEXT NOT NULL)');
  sql.exec('CREATE TABLE memo_b(id TEXT PRIMARY KEY,value TEXT NOT NULL)');
  sql.exec("INSERT INTO memo_a VALUES('x','a')");
  sql.exec("INSERT INTO memo_b VALUES('x','b1')");
  const query = 'SELECT b.value AS value FROM memo_a a, memo_b b WHERE a.id=b.id';
  expect(sql.exec(query).toArray()[0].value).toBe('b1');
  sql.exec("UPDATE memo_b SET value='b2' WHERE id='x'");
  const cursor: any = sql.exec(query);
  expect(cursor.toArray()[0].value).toBe('b2');
  expect(Number(cursor.rowsRead)).toBeGreaterThan(0);
}));

it('counts cache hits as zero-row route cursors when the meter wraps the write watch', withFleet(async (_s, _t, _w, _catalog, rawSql) => {
  const watch = new WriteWatch();
  const meter = new RowsMeter(() => now);
  const sql = meter.wrap(watch.wrap(rawSql));
  const poll = () => meter.runSync('route:GET /memo', () => sql.exec('SELECT name FROM machines WHERE id=?', 'h0').toArray());

  expect(poll()[0].name).toBe('h0');
  const first = meter.snapshot()['route:GET /memo'];
  expect(first.rows_read).toBeGreaterThan(0);
  expect(poll()[0].name).toBe('h0');
  const second = meter.snapshot()['route:GET /memo'];
  expect(second.calls).toBe(first.calls + 1);
  expect(second.rows_read).toBe(first.rows_read);
}));

it('does not memoize reads that depend on random or current time', withFleet(async (s) => {
  const sql = (s as any).sql;
  const first: any = sql.exec('SELECT random() AS value FROM fleet_sessions LIMIT 1');
  const firstValue = first.toArray()[0].value;
  expect(Number(first.rowsRead)).toBeGreaterThan(0);
  const second: any = sql.exec('SELECT random() AS value FROM fleet_sessions LIMIT 1');
  const secondValue = second.toArray()[0].value;
  expect(Number(second.rowsRead)).toBeGreaterThan(0);
  expect(secondValue).not.toBe(firstValue);
}));

it('preserves pending retention across unrelated alarm scheduling and caps re-arms durably', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('retention-alarm-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const schedule = (hub as any).scheduleDueAlarm.bind(hub);
    await schedule([], true);
    const firstCount = Number(await state.storage.get('bus_retention_fast_rearms'));
    expect(firstCount).toBe(1);
    await schedule([Date.now() + 1]); // webhook work has no retention result
    expect(await state.storage.get('bus_retention_pending')).toBe(true);
    expect(Number(await state.storage.get('bus_retention_fast_rearms'))).toBe(firstCount);

    for (let i = firstCount; i < 30; i++) {
      await state.storage.deleteAlarm(); // each pass represents a fired alarm
      await schedule([], true);
    }
    expect(Number(await state.storage.get('bus_retention_fast_rearms'))).toBe(30);
    await state.storage.deleteAlarm();
    const before = Date.now();
    await schedule([], true);
    expect(Number(await state.storage.get('bus_retention_fast_rearms'))).toBe(31);
    expect((await state.storage.getAlarm())!).toBeGreaterThanOrEqual(before + 55_000);

    await schedule([], false);
    expect(await state.storage.get('bus_retention_pending')).toBe(false);
    expect(Number(await state.storage.get('bus_retention_fast_rearms'))).toBe(0);
  });
});

it('an alarm reloads and drains the persisted retention backlog after DO eviction', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('retention-reload-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const h: any = hub;
    await state.storage.put('bus_retention_pending', true);
    let cleanups = 0;
    let tbmCalls = 0;
    h.busService.retentionCleanup = () => { cleanups++; return { hasMore: false }; };
    h.runTbmTick = () => { tbmCalls++; };
    h.jobsService.processAlarms = () => undefined;
    h.jobsService.getAlarmTime = () => null;
    h.webhookService.pump = async () => undefined;
    h.sessionControlService.processDueWakes = () => undefined;
    h.nextWakeTimeSafe = () => null;
    h.flushRowsMeter = async () => undefined;

    await h.alarm();
    expect(cleanups).toBe(1);
    expect(tbmCalls).toBe(0);
    expect(await state.storage.get('bus_retention_pending')).toBe(false);
    expect(Number(await state.storage.get('bus_retention_fast_rearms'))).toBe(0);
  });
});

it('query plans use the new indexes on the high-volume lookups', withFleet(async (s) => {
  const sql = (s as any).sql;
  const plan = (query: string, ...bindings: any[]) => (sql.exec('EXPLAIN QUERY PLAN ' + query, ...bindings).toArray() as any[])
    .map(row => String(row.detail)).join('\n');

  expect(plan('SELECT data FROM intake_tasks WHERE id COLLATE NOCASE LIKE ? LIMIT 2', 'ffff%'))
    .toContain('intake_tasks_id_nocase');
  expect(plan(`SELECT 1 FROM worker_access w JOIN credentials c ON c.family_id=w.family_id
    WHERE w.session_id=? AND c.revoked_at IS NULL LIMIT 1`, 'h0')).toContain('worker_access_session_family');
  expect(plan(`SELECT * FROM session_control_wakes WHERE session_id=? AND state='pending'
    ORDER BY wake_at_ms, created_at, id LIMIT 1`, 'sess'))
    .toContain('session_control_wakes_session_due_order');
  expect(plan(`SELECT s.id FROM session_control_sessions s JOIN session_control_protections p ON p.session_id=s.id
    WHERE s.machine_id=? AND json_extract(CASE WHEN json_valid(s.binding_json) THEN s.binding_json ELSE '{}' END,'$.native_id')=? LIMIT 1`, 'h0', 'native'))
    .toMatch(/USING INDEX (?:session_control_native_binding|intake_queue_session_native)/);
  expect(plan(`SELECT quota_json FROM usage_quota_history
    WHERE account=? AND provider=? AND observed_at_ms=? LIMIT 1`, 'a0', 'codex', now))
    .toContain('usage_quota_history_account_provider_time');
  expect(plan(`SELECT account,provider,observed_at_ms,quota_json FROM usage_quota_history
    WHERE observed_at_ms>=? AND observed_at_ms<=? ORDER BY observed_at_ms LIMIT 4096`, now - 15 * 60_000, now))
    .toContain('usage_quota_history_time');
}));
