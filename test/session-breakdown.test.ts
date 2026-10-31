// Live session counts by host x engine x account (intake 93e7817b).
// Synthetic rows only: never starts a native agent.
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, sessionAccount } from '../src/session_control';
import { parseBreakdownDims, sessionBucket } from '../src/session-breakdown';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = { caller: { kind: 'agent', actor: 'demo:assistant', tokenId: 'cred-1' }, scopes: ['fleet:launch'] };

const get = (path: string) => new Request('https://hub.test' + path, { method: 'GET' });

async function fixture(fn: (s: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-breakdown-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, {}, {});
    service.init();
    await fn(service, hub.sql);
  });
}

function machine(sql: any, id: string, name: string, hostname: string, checkinAgoMs: number | null) {
  const at = new Date().toISOString();
  const seen = checkinAgoMs === null ? null : new Date(Date.now() - checkinAgoMs).toISOString();
  sql.exec('INSERT INTO machines (id, name, hostname, credential_id, registered_at, last_checkin_at) VALUES (?, ?, ?, ?, ?, ?)',
    id, name, hostname, 'cred-' + id, at, seen);
}

let n = 0;
function session(sql: any, o: { state: string; machine?: string | null; engine?: string; account?: string | null; native?: { account: string | null } ; actor?: string }) {
  n++;
  const id = crypto.randomUUID();
  const at = new Date(Date.now() - n * 1000).toISOString();
  let launchId: string | null = null;
  let binding: string | null = null;
  const machineId = o.machine ?? null;
  if (o.account !== undefined && o.account !== null) {
    launchId = 'launch-' + n;
    sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
      VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, ?, ?, NULL, 'headless', 'demo', 'goal', ?, NULL, NULL, ?, ?)`,
      launchId, 'req-' + n, machineId ?? 'none', o.engine ?? 'codex', o.account, o.state === 'held' ? 'queued' : o.state, at, at);
  }
  if (o.native) {
    const nativeId = crypto.randomUUID();
    binding = JSON.stringify({ native_id: nativeId, tag: 'native-' + n, workspace: '/srv/demo', native_created_at_ms: Date.now() });
    sql.exec(`INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account)
      VALUES (?, ?, ?, ?, 'running', 'working', ?, '/srv/demo', NULL, ?)`, machineId, nativeId, 'native-' + n, o.engine ?? 'codex', at, o.native.account);
  }
  sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, binding_json, created_at, updated_at)
    VALUES (?, ?, ?, '{"workspace":"demo"}', ?, ?, ?, 'headless', ?, ?, ?, ?)`,
    id, o.actor ?? 'test-admin', 'key-' + n, launchId, machineId, o.engine ?? 'codex', o.state, binding, at, at);
  return id;
}

describe('session breakdown helpers', () => {
  it('parses the grouping dimensions strictly', () => {
    expect(parseBreakdownDims(null)).toEqual(['host', 'engine', 'account']);
    expect(parseBreakdownDims('Account, host,host')).toEqual(['account', 'host']);
    expect(parseBreakdownDims('host,model')).toBeNull();
    expect(parseBreakdownDims(',')).toBeNull();
  });

  it('buckets projected states into running/queued/stalled and drops terminal ones', () => {
    expect(sessionBucket({ state: 'running' })).toBe('running');
    expect(sessionBucket({ state: 'waiting_until' })).toBe('running');
    expect(sessionBucket({ state: 'running', stalled: true })).toBe('stalled');
    expect(sessionBucket({ state: 'idle_with_open_ac' })).toBe('stalled');
    expect(sessionBucket({ state: 'uncertain' })).toBe('stalled');
    for (const q of ['held', 'queued', 'offered', 'starting']) expect(sessionBucket({ state: q })).toBe('queued');
    for (const t of ['stopped', 'failed', 'cancelled', 'refused', 'exited']) expect(sessionBucket({ state: t })).toBeNull();
  });

  it('records the account from the launch first, then the observed native, never inventing one', () => {
    expect(sessionAccount('shared-codex', 'other')).toEqual({ account: 'shared-codex', account_source: 'launch' });
    expect(sessionAccount(null, 'codex-orin')).toEqual({ account: 'codex-orin', account_source: 'native' });
    expect(sessionAccount(null, null)).toEqual({ account: null, account_source: null });
    expect(sessionAccount('bad label!', undefined)).toEqual({ account: null, account_source: null });
  });
});

describe('GET /api/sessions?view=breakdown', () => {
  it('counts live sessions by canonical host x engine x account with running/queued/stalled', async () => {
    await fixture(async (s, sql) => {
      // Two records for one physical host (RMTHZ): the stale one aliases to the live one.
      machine(sql, 'DEFLf4', 'urgent-hetzner', 'RMTHZ', 10_000);
      machine(sql, 'SnV2sx', 'hetzner', 'rmthz', 220 * 3600_000);
      machine(sql, 'win1', 'desktop', 'DESKTOP-FP4OP26', 10_000);
      session(sql, { state: 'running', machine: 'DEFLf4', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'running', machine: 'SnV2sx', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'uncertain', machine: 'DEFLf4', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'queued', machine: 'win1', engine: 'claude', account: 'claude-max' });
      session(sql, { state: 'held', machine: null, engine: 'claude' });
      session(sql, { state: 'running', machine: 'win1', engine: 'codex', native: { account: 'codex-orin' } });
      session(sql, { state: 'stopped', machine: 'DEFLf4', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'failed', machine: 'win1', engine: 'claude', account: 'claude-max' });

      const res = await s.handle(get('/api/sessions?view=breakdown&by=host,engine,account'), admin);
      expect(res.status).toBe(200);
      const body = await res.json() as any;
      expect(body.by).toEqual(['host', 'engine', 'account']);
      expect(body.totals).toEqual({ running: 3, queued: 2, stalled: 1, total: 6 });
      const row = (host: string, engine: string, account: string) => body.rows.find((r: any) => r.host === host && r.engine === engine && r.account === account);
      expect(row('DEFLf4', 'codex', 'shared-codex')).toMatchObject({ running: 2, queued: 0, stalled: 1, total: 3, host_label: 'urgent-hetzner',
        hostname: 'RMTHZ', host_aliases: [{ id: 'SnV2sx', label: 'hetzner' }] });
      expect(row('win1', 'claude', 'claude-max')).toMatchObject({ queued: 1, total: 1, host_label: 'desktop' });
      expect(row('win1', 'codex', 'codex-orin')).toMatchObject({ running: 1, total: 1 });
      expect(row('unplaced', 'claude', 'unknown')).toMatchObject({ queued: 1, total: 1 });
      // One physical host is one host entry, never "hetzner" and "urgent-hetzner" twice.
      expect(body.hosts.map((h: any) => h.host).sort()).toEqual(['DEFLf4', 'unplaced', 'win1']);
      expect(body.accounts.find((a: any) => a.account === 'shared-codex')).toMatchObject({ total: 3 });
      expect(body.truncated).toBe(false);

      // Per-account counts agree with the list projection: every listed session carries its account.
      const list = await (await s.handle(get('/api/sessions?limit=100'), admin)).json() as any;
      const live = list.sessions.filter((x: any) => sessionBucket(x));
      expect(live.filter((x: any) => x.account === 'shared-codex').length).toBe(3);
      expect(live.find((x: any) => x.account === 'codex-orin')).toMatchObject({ account_source: 'native', machine: 'win1' });
      expect(live.filter((x: any) => x.account === 'shared-codex').every((x: any) => x.account_source === 'launch')).toBe(true);
    });
  });

  it('supports partial groupings and refuses unknown dimensions', async () => {
    await fixture(async (s, sql) => {
      machine(sql, 'm1', 'm1', 'M1', 10_000);
      session(sql, { state: 'running', machine: 'm1', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'running', machine: 'm1', engine: 'claude', account: 'claude-max' });
      const byHost = await (await s.handle(get('/api/sessions?view=breakdown&by=host'), admin)).json() as any;
      expect(byHost.rows).toEqual([expect.objectContaining({ host: 'm1', engine: null, account: null, running: 2, total: 2 })]);
      const bad = await s.handle(get('/api/sessions?view=breakdown&by=host,model'), admin);
      expect(bad.status).toBe(400);
      expect(await bad.json()).toMatchObject({ error: 'invalid_breakdown' });
    });
  });

  it('scopes non-admin callers to their own sessions', async () => {
    await fixture(async (s, sql) => {
      machine(sql, 'm1', 'm1', 'M1', 10_000);
      session(sql, { state: 'running', machine: 'm1', engine: 'codex', account: 'shared-codex', actor: 'test-admin' });
      const mine = session(sql, { state: 'running', machine: 'm1', engine: 'codex', account: 'shared-codex', actor: 'demo:assistant' });
      // Live agent incarnation presenting cred-1 (same seed as session-control.test.ts).
      const at = new Date().toISOString();
      const far = new Date(Date.now() + 30 * 86400000).toISOString();
      sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
      sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
        VALUES ('cred-1', 'agent', 'hash-cred-1', 'refresh-cred-1', 'agent-family-1', 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?, 'demo:assistant')`, at, far);
      sql.exec("UPDATE session_control_sessions SET owner_token_id = 'cred-1', owner_family = 'agent-family-1' WHERE id = ?", mine);
      const own = await (await s.handle(get('/api/sessions?view=breakdown'), agent)).json() as any;
      expect(own.totals.total).toBe(1);
      const all = await (await s.handle(get('/api/sessions?view=breakdown'), admin)).json() as any;
      expect(all.totals.total).toBe(2);
    });
  });
});

describe('per-account live session counts on /api/fleet/limits', () => {
  it('reports sessions per account from real live sessions next to the admission counters', async () => {
    const { FleetService } = await import('../src/fleet');
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('breakdown-limits-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, {}, {});
      service.init();
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), Date.now);
      fleet.init();
      const sql = hub.sql as any;
      machine(sql, 'm1', 'm1', 'M1', 10_000);
      sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex','codex',30,0,1)");
      sql.exec("INSERT INTO fleet_accounts VALUES ('codex-orin','codex',8,0,1)");
      session(sql, { state: 'running', machine: 'm1', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'queued', machine: 'm1', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'stopped', machine: 'm1', engine: 'codex', account: 'shared-codex' });
      session(sql, { state: 'running', machine: 'm1', engine: 'codex', native: { account: 'codex-orin' } });
      const shown: any = await (await fleet.handle(new Request('https://hub.test/api/fleet/limits', { method: 'GET' }), admin)).json();
      expect(shown.accounts.find((a: any) => a.account === 'shared-codex')).toMatchObject({ sessions: 2 });
      expect(shown.accounts.find((a: any) => a.account === 'codex-orin')).toMatchObject({ sessions: 1 });
      const dash: any = await (await fleet.handle(new Request('https://hub.test/api/fleet', { method: 'GET' }), admin)).json();
      expect(dash.accounts.find((a: any) => a.label === 'shared-codex')).toMatchObject({ sessions: 2 });
      // Same numbers as the breakdown view.
      const breakdown: any = await (await service.handle(get('/api/sessions?view=breakdown&by=account'), admin)).json();
      expect(breakdown.accounts.find((a: any) => a.account === 'shared-codex')).toMatchObject({ total: 2 });
      expect(breakdown.accounts.find((a: any) => a.account === 'codex-orin')).toMatchObject({ total: 1 });
    });
  });
});
