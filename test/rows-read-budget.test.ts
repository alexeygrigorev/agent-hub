/**
 * DO rows-read budgets (intake 8081a05a). Cloudflare bills Durable Object
 * SQL by rows read; the minute alarm and the cron ticks used to scan whole
 * tables per session and read ~8B rows/day. These tests seed a production-
 * shaped Hub and pin the rows each alarm step / tick step may read.
 */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import { rewriteSchemaProbe } from '../src/sql-probes';
import { normalizeRoute } from '../src/rows-meter';
import { linkedTaskIds, sessionLinkIds } from '../src/intake-session';

const NOW = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();
const SESSIONS = 150, INTAKES = 300, DECISIONS = 3000, MESSAGES = 3000;

function seed(sql: any) {
  sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('budget-host','budget-host','budget-host',?,?)", iso(NOW), iso(NOW));
  sql.exec('INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES(?,?)', 'budget-host', iso(NOW));
  for (let i = 0; i < INTAKES; i++) {
    const id = `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
    const history: any[] = [{ id: 'h' + i, kind: 'intake', text: 'x', actor: 't', captured_at: NOW }];
    if (i < SESSIONS) history.push({ id: 'l' + i, kind: 'link', hub_session_id: 'sess-' + i, native_id: null, host: 'budget-host', captured_at: NOW });
    else if (i < SESSIONS + 20) history.push({ id: 'r' + i, kind: 'report', native_id: 'native-' + (i - SESSIONS), host: null, captured_at: NOW });
    sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', id, JSON.stringify({ id, project: 'agent-hub', title: 't' + i,
      status: 'open', version: 1, acceptance_criteria: 'AC', history, created_at: NOW, updated_at: NOW }));
  }
  for (let i = 0; i < SESSIONS; i++) {
    const native = 'native-' + i;
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES(?,'operator',?,'{}','budget-host','codex','interactive','running',?,?,?)`, 'sess-' + i, 'k' + i,
      JSON.stringify({ native_id: native, tag: 't' + i, workspace: '/w', native_created_at_ms: NOW - 3600000 }), iso(NOW - 3600000), iso(NOW));
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,last_activity_at,created_at_ms,progress_json)
      VALUES('budget-host',?,?,'codex','running','idle',?,'/w',?,?,?)`, native, 't' + i, iso(NOW), iso(NOW - 60000), NOW - 3600000,
      JSON.stringify({ last_progress_at: iso(NOW - 60000), sampled_at: iso(NOW) }));
  }
  for (let i = 0; i < DECISIONS; i++) sql.exec(`INSERT INTO session_audit_decisions(run_id,at,session_id,machine_id,native_id,rule,action,features_json)
    VALUES(1,?,?,'budget-host',?,'healthy','observe','{}')`, iso(NOW - i * 1000), 'sess-' + (i % SESSIONS), 'native-' + (i % SESSIONS));
  for (let i = 0; i < MESSAGES; i++) sql.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,state)
    VALUES(?,'bot','grok-bot','note',?,'b',?,?,?,'accepted')`, 'm' + i, 't' + i, 'k' + i, NOW - 1000, NOW + 86400000);
}

/** Rows read per scope by `fn`, from the meter's persisted hour ring. */
async function measure(hub: Hub, fn: () => Promise<unknown>): Promise<Record<string, number>> {
  const read = (): Record<string, number> => {
    hub.meter.flush(hub.sql);
    return Object.fromEntries((hub.sql.exec('SELECT scope, SUM(rows_read) AS n FROM rows_read_stats GROUP BY scope').toArray() as any[]).map(r => [r.scope, Number(r.n)]));
  };
  const before = read();
  await fn();
  const after = read();
  const out: Record<string, number> = {};
  for (const [scope, n] of Object.entries(after)) if (scope !== '__total__' && scope !== 'meter:flush' && n - (before[scope] ?? 0) > 0) out[scope] = n - (before[scope] ?? 0);
  return out;
}

const withHub = (fn: (hub: Hub) => Promise<void>) => runInDurableObject(
  (env as any).HUB.get((env as any).HUB.idFromName('rows-budget-' + crypto.randomUUID())), async (hub: Hub) => { seed(hub.sql); await fn(hub); });

describe('schema probe rewrite', () => {
  it('maps the probe shapes in use and leaves anything else alone', () => {
    expect(rewriteSchemaProbe("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_tasks'"))
      .toBe("SELECT name FROM pragma_table_list('intake_tasks') WHERE schema='main' AND type='table'");
    expect(rewriteSchemaProbe("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"))
      .toBe("SELECT 1 FROM pragma_table_list(?) WHERE schema='main' AND type='table'");
    expect(rewriteSchemaProbe("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('a','b')")).toContain('UNION ALL');
    expect(rewriteSchemaProbe("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('a','b') LIMIT 1")).toMatch(/^SELECT \* FROM \(.*\) LIMIT 1$/);
    expect(rewriteSchemaProbe("SELECT name FROM sqlite_master WHERE type='index' AND name='x'")).toBeNull();
    expect(rewriteSchemaProbe('SELECT name, sql FROM sqlite_master ORDER BY name')).toBeNull();
    expect(rewriteSchemaProbe("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'x%'")).toBeNull();
  });

  it('answers like sqlite_master and reads at most one row', () => withHub(async hub => {
    for (const [name, present] of [['intake_tasks', true], ['no_such_table', false]] as const) {
      const cursor: any = hub.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name=?", name);
      expect(cursor.toArray().map((r: any) => r.name)).toEqual(present ? [name] : []);
      expect(cursor.rowsRead).toBeLessThanOrEqual(1);
    }
    expect(hub.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('fleet_launches','session_control_sessions')").toArray()).toHaveLength(2);
    // Indexes are not tables; the probe must keep saying so.
    expect(hub.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_expires_at'").toArray()).toHaveLength(0);
  }));
});

describe('indexed intake link lookups', () => {
  it('match the json_each scan they replace, reading a handful of rows', () => withHub(async hub => {
    const legacy = (sid: string, native: string, host: string) => (hub.sql.exec(`SELECT DISTINCT i.id FROM intake_task_projection i, json_each(i.links) e
      WHERE json_extract(e.value,'$.hub_session_id')=? OR (json_extract(e.value,'$.native_id')=? AND
      (json_extract(e.value,'$.host') IS NULL OR json_extract(e.value,'$.host')=?)) LIMIT 2`, sid, native, host).toArray() as any[]).map(r => r.id);
    for (const [sid, native] of [['sess-3', 'native-3'], ['sess-none', 'native-5'], ['sess-none', 'native-none'], ['', '']]) {
      const session = { id: sid, machine_id: 'budget-host' }, binding = { native_id: native };
      expect(sessionLinkIds(hub.sql, session, binding, 2)).toEqual(legacy(sid, native, 'budget-host'));
    }
    const rows = await measure(hub, async () => hub.meter.run('probe:link', () => sessionLinkIds(hub.sql, { id: 'sess-7', machine_id: 'budget-host' }, { native_id: 'native-7' }, 2)));
    expect(rows['probe:link'] ?? 0).toBeLessThanOrEqual(20);
    // Kind filters stay with the caller: a report entry is no 'link'.
    expect(linkedTaskIds(hub.sql, ['native-2'], e => e.kind === 'link' && e.native_id === 'native-2', 1)).toEqual([]);
    expect(linkedTaskIds(hub.sql, ['native-2'], e => e.native_id === 'native-2', 1)).toHaveLength(1);
  }));
});

describe('per-step rows-read budgets', () => {
  it('a minute alarm reads a bounded number of rows per step', () => withHub(async hub => {
    await hub.alarm(); // first alarm: idle census and once-only work
    const rows = await measure(hub, () => hub.alarm());
    const budgets: Record<string, number> = {
      'alarm:idle_work': 0, // at most every 5 minutes
      'alarm:bus_retention': 40,
      'alarm:expire_messages': 20,
      'alarm:jobs_alarms': 100,
      'alarm:webhooks': 50,
      'alarm:tbm': 20,
    };
    const over = Object.entries(budgets).filter(([scope, budget]) => (rows[scope] ?? 0) > budget).map(([scope, budget]) => `${scope}=${rows[scope]} > ${budget}`);
    expect(over).toEqual([]);
    const total = Object.values(rows).reduce((a, b) => a + b, 0);
    console.log('alarm rows by scope', JSON.stringify(rows));
    // Scales with neither the 3000 queued messages nor the 3000 audit rows nor 300 intakes.
    expect(total).toBeLessThan(5000);
  }));

  it('the idle census reads per running session, not per intake or per table row', () => withHub(async hub => {
    const rows = await measure(hub, () => hub.alarm());
    expect(rows['alarm:idle_work'] ?? 0).toBeLessThan(SESSIONS * 25);
  }));

  it('a scheduled tick keeps the audit, verifier and triage steps bounded', () => withHub(async hub => {
    const rows = await measure(hub, () => hub.scheduledTick(undefined, 'test'));
    console.log('tick rows by scope', JSON.stringify(rows));
    expect(rows['tick:session_audit'] ?? 0).toBeLessThan(SESSIONS * 40);
    expect(rows['tick:bus_retention'] ?? 0).toBeLessThanOrEqual(40);
    const total = Object.values(rows).reduce((a, b) => a + b, 0);
    // Linear in running sessions only (~70 rows each today); never in table sizes.
    expect(total).toBeLessThan(SESSIONS * 80);
  }));
});

describe('rows-read budget alert', () => {
  it('the alarm checks the hourly budget even when a request flushed just before', () => withHub(async hub => {
    const hour = Math.floor(Date.now() / 3_600_000) - 1;
    hub.sql.exec("INSERT INTO rows_read_stats(slot,scope,hour,calls,rows_read,rows_written,max_read) VALUES(?,?,?,1,?,0,0)", hour % 192, '__total__', hour, 1_000_000);
    await hub.fetch(new Request('https://hub/api/metrics/rows-read')); // 401, but flushes the meter
    await hub.alarm();
    const open: any = hub.sql.exec("SELECT state, detail FROM liveness_alerts WHERE subject_kind='do_rows_read_budget' AND resolved_at IS NULL").toArray()[0];
    expect(open?.state).toBe('over_budget');
    expect(open.detail).toContain('24000000/day');
  }));
});

describe('route normalization', () => {
  it('keeps meter scopes bounded', () => {
    expect(normalizeRoute('get', '/api/sessions/3f2a1b4c-1111-4111-8111-111111111111/send')).toBe('GET /api/sessions/:id/send');
    expect(normalizeRoute('POST', '/api/intake/tasks/8081a05a/events')).toBe('POST /api/intake/tasks/:id/events');
    expect(normalizeRoute('GET', '/api/fleet/launches')).toBe('GET /api/fleet/launches');
    expect(normalizeRoute('GET', '/api/machines/123')).toBe('GET /api/machines/:n');
  });
});
