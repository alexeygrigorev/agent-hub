import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ChecksService, validateCheck, type ActionDeps } from '../src/checks';
import { LivenessService } from '../src/liveness';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const start = Date.parse('2026-10-15T12:00:00Z');

async function fixture(fn: (checks: ChecksService, deps: ActionDeps & { log: any[] }, clock: { t: number }, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('checks-test-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => {
    const clock = { t: start };
    new FleetService(hub.sql, f => f(), () => clock.t).init();
    const liveness = new LivenessService(hub.sql, undefined, () => clock.t); liveness.init();
    const checks = new ChecksService(hub.sql, () => clock.t); checks.init();
    // Hub construction seeds with the real clock; this fixture runs on a pinned clock.
    hub.sql.exec('UPDATE hub_checks SET next_due_at = ?', new Date(start).toISOString());
    const log: any[] = [];
    const deps: ActionDeps & { log: any[] } = {
      log, liveness,
      notify: async (alert: any) => { log.push(['notify', alert.subject_kind, alert.subject, alert.state]); },
      message: async (to: string, body: string, key: string) => { log.push(['message', to, body, key]); return 'm1'; },
      launch: async (name: string, request: any, key: string) => { log.push(['launch', name, request, key]); return 'l1 offered on hetzner'; },
      fetch: async (url: string) => { log.push(['fetch', url]); return new Response(null, { status: url.includes('down') ? 503 : 200 }); },
      evaluateQuota: () => [{ label: 'shared-codex', enabled: true, status: 'ok', remaining_percent: 12 }, { label: 'shared-claude', enabled: true, status: 'ok', remaining_percent: 70 }],
    };
    await fn(checks, deps, clock, hub.sql);
  });
}
const row = (c: any) => { const v = validateCheck(c); if ('error' in v) throw new Error(v.error); return v.row; };

describe('check definitions', () => {
  it('seeds default liveness and dispatch-route checks', () => fixture(async checks => {
    expect(checks.list().map(c => [c.name, c.kind, c.interval_s, c.enabled])).toEqual([
      ['dispatch-route', 'dispatch_route', 300, true],
      ['liveness', 'liveness', 300, true],
    ]);
  }));

  it('keeps an intentionally empty configuration across restarts', () => fixture(async (checks, deps, clock, sql) => {
    checks.replace([]);
    new ChecksService(sql, () => clock.t).init();
    expect(checks.list()).toEqual([]);
  }));

  it('limits names so scheduled launch keys fit the fleet idempotency key', () => {
    expect('error' in validateCheck({ name: 'a'.repeat(41), kind: 'quota', interval_s: 300 })).toBe(true);
    const name = 'a'.repeat(40);
    expect('error' in validateCheck({ name, kind: 'quota', interval_s: 300 })).toBe(false);
    const key = `check-${name}-${Math.floor(Date.parse('2030-01-01T00:00:00Z') / 300000)}`;
    expect(key.length).toBeLessThanOrEqual(64);
    expect(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(key)).toBe(true);
  });

  it('validates bounded canonical backlog and explicit conversation delegation fields', () => {
    const valid = { name: 'followup', kind: 'conversation_backlog', interval_s: 300, params: { actor: 'example:worker', max_age_s: 3600, conversation_id: 'CID' }, action: { type: 'message', to: 'example:worker', body: '{detail}', conversation_id: 'CID', grant_id: 'GRANT' } };
    expect('row' in validateCheck(valid)).toBe(true);
    for (const max_age_s of [0, 59, 1.5, 604801, Infinity]) expect('error' in validateCheck({ ...valid, params: { ...valid.params, max_age_s } })).toBe(true);
    for (const actor of ['role:root', 'system:checks', 'root', 'human:', 'worker\n']) expect('error' in validateCheck({ ...valid, params: { ...valid.params, actor } })).toBe(true);
    expect('error' in validateCheck({ ...valid, action: { ...valid.action, grant_id: undefined } })).toBe(true);
    expect('error' in validateCheck({ ...valid, action: { ...valid.action, to: 'other:worker' } })).toBe(true);
    expect('error' in validateCheck({ ...valid, action: { ...valid.action, conversation_id: 'OTHER' } })).toBe(true);
    expect('error' in validateCheck({ ...valid, action: { ...valid.action, sender: 'human:owner' } })).toBe(true);
  });

  it('rejects unsafe or malformed definitions', () => {
    const bad = [
      { name: 'Bad Name', kind: 'routine', interval_s: 300, action: { type: 'message', to: 'a', body: 'b' } },
      { name: 'x', kind: 'shell', interval_s: 300 },
      { name: 'x', kind: 'quota', interval_s: 10 },
      { name: 'x', kind: 'http', interval_s: 300, params: { url: 'http://insecure.example' } },
      { name: 'x', kind: 'http', interval_s: 300, params: { url: 'https://user:pw@example.com' } },
      { name: 'x', kind: 'routine', interval_s: 300, action: { type: 'alert' } },
      { name: 'x', kind: 'routine', interval_s: 300, action: { type: 'launch', workspace: 'demo', prompt: 'go', mode: 'interactive' } },
      { name: 'x', kind: 'routine', interval_s: 300, action: { type: 'launch', workspace: 'demo', prompt: 'go', command: 'rm -rf /' } },
      { name: 'x', kind: 'quota', interval_s: 300, params: { token: 'secret' } },
      { name: 'x', kind: 'inbox_backlog', interval_s: 300, params: { actor: 'grok-bot', max_age_s: 5 } },
    ];
    for (const c of bad) expect('error' in validateCheck(c)).toBe(true);
    expect(row({ name: 'daily', kind: 'routine', interval_s: 86400, action: { type: 'launch', workspace: 'demo', prompt: 'Write the daily summary' } }).action.on).toBe('always');
  });
});

describe('running checks', () => {
  it('runs a due check once per interval slot', () => fixture(async (checks, deps, clock) => {
    checks.replace([row({ name: 'quota-low', kind: 'quota', interval_s: 300, params: { min_percent: 20 } })]);
    const first = await checks.runDue(deps);
    expect(first.map(r => [r.name, r.status, r.action])).toEqual([['quota-low', 'fail', 'alert opened']]);
    expect(deps.log).toEqual([['notify', 'check', 'quota-low', 'fail']]);
    expect(await checks.runDue(deps)).toEqual([]);
    clock.t += 300000;
    const second = await checks.runDue(deps);
    expect(second[0].action).toBe('alert already open');
    expect(deps.log).toHaveLength(1);
    expect(checks.runs().length).toBe(2);
  }));

  it('resolves a check alert when the check passes again', () => fixture(async (checks, deps, clock) => {
    checks.replace([row({ name: 'site', kind: 'http', interval_s: 300, params: { url: 'https://down.example/health' } })]);
    await checks.runDue(deps);
    expect(deps.liveness.alerts()[0].resolved_at).toBeNull();
    checks.replace([row({ name: 'site', kind: 'http', interval_s: 300, params: { url: 'https://up.example/health' } })]);
    clock.t += 300000;
    expect((await checks.runDue(deps))[0].status).toBe('pass');
    expect(deps.liveness.alerts()[0].resolved_at).not.toBeNull();
  }));

  it('a routine always acts; message and launch actions get stable per-slot keys', () => fixture(async (checks, deps) => {
    checks.replace([
      row({ name: 'daily-writer', kind: 'routine', interval_s: 86400, action: { type: 'launch', workspace: 'demo', prompt: 'Write the daily summary', engine: 'claude' } }),
      row({ name: 'ping-grok', kind: 'routine', interval_s: 3600, action: { type: 'message', to: 'grok-bot', body: 'Status check from {check}' } }),
    ]);
    const results = await checks.runDue(deps);
    expect(results.map(r => r.action)).toEqual(['launch l1 offered on hetzner', 'message m1']);
    const launch = deps.log.find(l => l[0] === 'launch');
    expect(launch[2]).toEqual({ workspace: 'demo', prompt: 'Write the daily summary', summary: 'Scheduled: daily-writer', mode: 'headless', engine: 'claude' });
    expect(launch[3]).toBe(`daily-writer-${Math.floor(start / 86400000)}`);
    expect(deps.log.find(l => l[0] === 'message').slice(1, 3)).toEqual(['grok-bot', 'Status check from ping-grok']);
  }));

  it('a failing-only message action stays quiet while the check passes', () => fixture(async (checks, deps) => {
    checks.replace([row({ name: 'site', kind: 'http', interval_s: 300, params: { url: 'https://up.example' }, action: { type: 'message', to: 'grok-bot', body: 'down: {detail}' } })]);
    expect((await checks.runDue(deps))[0].action).toBeNull();
    expect(deps.log.filter(l => l[0] === 'message')).toEqual([]);
  }));

  it('records action failures without losing the check result', () => fixture(async (checks, deps) => {
    deps.launch = async () => { throw new Error('no_eligible_route'); };
    checks.replace([row({ name: 'r', kind: 'routine', interval_s: 300, action: { type: 'launch', workspace: 'demo', prompt: 'go' } })]);
    const [result] = await checks.runDue(deps);
    expect(result.status).toBe('pass'); expect(result.action).toBe('action failed: no_eligible_route');
  }));

  it('detects an unacknowledged inbox backlog', () => fixture(async (checks, deps, clock, sql) => {
    sql.exec("INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('m', 'hub', 'grok-bot', 'note', 'm', 'hi', 'k', ?, ?)", start - 7200000, start + 86400000);
    checks.replace([row({ name: 'grok-inbox', kind: 'inbox_backlog', interval_s: 300, params: { actor: 'grok-bot', max_age_s: 3600 } })]);
    expect((await checks.runDue(deps))[0].status).toBe('fail');
  }));

  it('detects unacknowledged messages exceeding delivery SLO', () => fixture(async (checks, deps, clock, sql) => {
    // Insert an unacknowledged message created 400s ago (> 300s standard limit)
    sql.exec("INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('slo-1', 'hub', 'agent-a', 'note', 'slo-1', 'task', 'k1', ?, ?)", start - 400000, start + 86400000);
    checks.replace([row({ name: 'delivery-slo', kind: 'delivery_slo', interval_s: 300, params: { max_age_s: 300 } })]);
    const res = await checks.runDue(deps);
    expect(res[0].status).toBe('fail');
    expect(res[0].detail).toContain('unacknowledged message(s) older than 300s (delivery SLO violated)');

    // When message is acknowledged, check passes
    sql.exec("UPDATE messages SET acked_at = ? WHERE id = 'slo-1'", start);
    clock.t += 300000;
    const resPass = await checks.runDue(deps);
    expect(resPass[0].status).toBe('pass');
    expect(resPass[0].detail).toContain('delivery SLO met');
  }));

  it('the liveness check opens per-subject alerts and notifies once', () => fixture(async (checks, deps, clock, sql) => {
    sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES (?, ?, ?, ?, ?, 0)', 'laptop', 'laptop', 'laptop', new Date(start).toISOString(), new Date(start - 3600000).toISOString());
    checks.replace([row({ name: 'liveness', kind: 'liveness', interval_s: 300 })]);
    const [result] = await checks.runDue(deps);
    expect(result.status).toBe('fail');
    expect(deps.log).toEqual([['notify', 'machine', 'laptop', 'offline']]);
    clock.t += 300000;
    await checks.runDue(deps);
    expect(deps.log).toHaveLength(1);
  }));

  it('validates dispatch_route params', () => {
    const valid = { name: 'dr', kind: 'dispatch_route', interval_s: 300, params: { max_consecutive: 3, window_s: 900 } };
    expect('row' in validateCheck(valid)).toBe(true);
    expect('row' in validateCheck({ ...valid, params: {} })).toBe(true);
    for (const max_consecutive of [1, 21, 1.5, '3']) expect('error' in validateCheck({ ...valid, params: { max_consecutive } })).toBe(true);
    for (const window_s of [59, 3601, 1.5]) expect('error' in validateCheck({ ...valid, params: { window_s } })).toBe(true);
    expect('error' in validateCheck({ ...valid, params: { max_consecutive: 3, actor: 'grok-bot' } })).toBe(true);
    expect('error' in validateCheck({ name: 'live', kind: 'liveness', interval_s: 300, params: { max_consecutive: 3 } })).toBe(true);
    expect('error' in validateCheck({ name: 'q', kind: 'quota', interval_s: 300, params: { window_s: 900 } })).toBe(true);
  });

  it('fires when consecutive dispatches fail at dispatch_route with the same reason', () => fixture(async (checks, deps, clock, sql) => {
    const diag = (reason: string) => JSON.stringify({ reason, message: 'refused', stage: 'dispatch_route', error_type: 'ValueError' });
    const fail = (id: string, reason: string, ageMs: number) =>
      sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, exit_reason, launch_error_json, created_at, updated_at) VALUES (?, 'a', 'human', 's', ?, '{}', 'm', 'opencode', 'urgent-muse', 'headless', 'w', 'p', 'failed', ?, ?, ?, ?)",
        id, 'k-' + id, reason, diag(reason), new Date(start - ageMs).toISOString(), new Date(start - ageMs).toISOString());
    fail('f1', 'dispatch_route_unconfigured', 600000);
    fail('f2', 'dispatch_route_unconfigured', 300000);
    fail('f3', 'dispatch_route_unconfigured', 60000);
    checks.replace([row({ name: 'dr', kind: 'dispatch_route', interval_s: 300, params: { max_consecutive: 3, window_s: 900 } })]);
    const [result] = await checks.runDue(deps);
    expect(result.status).toBe('fail');
    expect(result.detail).toContain('3 consecutive dispatch_route failures with reason dispatch_route_unconfigured');
    expect(result.action).toBe('alert opened');
    expect(deps.log).toEqual([['notify', 'check', 'dr', 'fail']]);
  }));

  it('a success or a different reason breaks the dispatch_route streak', () => fixture(async (checks, deps, clock, sql) => {
    const diag = (reason: string) => JSON.stringify({ reason, message: 'refused', stage: 'dispatch_route', error_type: 'ValueError' });
    sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, created_at, updated_at) VALUES ('ok1', 'a', 'human', 's', 'k-ok1', '{}', 'm', 'opencode', 'urgent-muse', 'headless', 'w', 'p', 'running', ?, ?)",
      new Date(start - 60000).toISOString(), new Date(start - 60000).toISOString());
    sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, exit_reason, launch_error_json, created_at, updated_at) VALUES ('f1', 'a', 'human', 's', 'k-f1', '{}', 'm', 'opencode', 'urgent-muse', 'headless', 'w', 'p', 'failed', 'dispatch_route_unconfigured', ?, ?, ?)",
      diag('dispatch_route_unconfigured'), new Date(start - 300000).toISOString(), new Date(start - 300000).toISOString());
    sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, exit_reason, launch_error_json, created_at, updated_at) VALUES ('f2', 'a', 'human', 's', 'k-f2', '{}', 'm', 'opencode', 'urgent-muse', 'headless', 'w', 'p', 'failed', 'dispatch_route_unconfigured', ?, ?, ?)",
      diag('dispatch_route_unconfigured'), new Date(start - 600000).toISOString(), new Date(start - 600000).toISOString());
    checks.replace([row({ name: 'dr', kind: 'dispatch_route', interval_s: 300, params: { max_consecutive: 3, window_s: 900 } })]);
    const [result] = await checks.runDue(deps);
    expect(result.status).toBe('pass');
    checks.replace([row({ name: 'dr2', kind: 'dispatch_route', interval_s: 300, params: { max_consecutive: 2, window_s: 900 } })]);
    const [second] = await checks.runDue(deps);
    expect(second.status).toBe('pass');
  }));

  it('a healthy route does not mask a broken route', () => fixture(async (checks, deps, clock, sql) => {
    const diag = (reason: string) => JSON.stringify({ reason, message: 'refused', stage: 'dispatch_route', error_type: 'ValueError' });
    const fail = (id: string, machine: string, engine: string, account: string, reason: string, ageMs: number) =>
      sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, exit_reason, launch_error_json, created_at, updated_at) VALUES (?, 'a', 'human', 's', ?, '{}', ?, ?, ?, 'headless', 'w', 'p', 'failed', ?, ?, ?, ?)",
        id, 'k-' + id, machine, engine, account, reason, diag(reason), new Date(start - ageMs).toISOString(), new Date(start - ageMs).toISOString());
    fail('b1', 'm', 'opencode', 'urgent-muse', 'dispatch_route_unconfigured', 700000);
    fail('b2', 'm', 'opencode', 'urgent-muse', 'dispatch_route_unconfigured', 600000);
    fail('b3', 'm', 'opencode', 'urgent-muse', 'dispatch_route_unconfigured', 500000);
    // Newest row: a healthy codex launch on the same Hub must not break the
    // opencode route streak; pre-claim rows never join or break a streak.
    sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, created_at, updated_at) VALUES ('ok1', 'a', 'human', 's', 'k-ok1', '{}', 'm', 'codex', 'shared-codex', 'headless', 'w', 'p', 'running', ?, ?)",
      new Date(start - 60000).toISOString(), new Date(start - 60000).toISOString());
    sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, created_at, updated_at) VALUES ('q1', 'a', 'human', 's', 'k-q1', '{}', 'm', 'opencode', 'urgent-muse', 'headless', 'w', 'p', 'offered', ?, ?)",
      new Date(start - 30000).toISOString(), new Date(start - 30000).toISOString());
    checks.replace([row({ name: 'dr', kind: 'dispatch_route', interval_s: 300, params: { max_consecutive: 3, window_s: 900 } })]);
    const [result] = await checks.runDue(deps);
    expect(result.status).toBe('fail');
    expect(result.detail).toContain('3 consecutive dispatch_route failures with reason dispatch_route_unconfigured on route m/opencode/urgent-muse');
  }));

  it('ignores dispatch_route failures outside the window', () => fixture(async (checks, deps, clock, sql) => {
    const diag = JSON.stringify({ reason: 'dispatch_route_unconfigured', message: 'refused', stage: 'dispatch_route', error_type: 'ValueError' });
    for (const [id, ageMs] of [['f1', 2000000], ['f2', 1900000], ['f3', 1800000]] as const) {
      sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, exit_reason, launch_error_json, created_at, updated_at) VALUES (?, 'a', 'human', 's', ?, '{}', 'm', 'opencode', 'urgent-muse', 'headless', 'w', 'p', 'failed', 'dispatch_route_unconfigured', ?, ?, ?)",
        id, 'k-' + id, diag, new Date(start - ageMs).toISOString(), new Date(start - ageMs).toISOString());
    }
    checks.replace([row({ name: 'dr', kind: 'dispatch_route', interval_s: 300, params: { max_consecutive: 3, window_s: 900 } })]);
    const [result] = await checks.runDue(deps);
    expect(result.status).toBe('pass');
    expect(result.detail).toBe('no recent dispatch_route failures');
  }));
});

it('rejects oversized definitions before slot storage', () => {
  const result = validateCheck({ name: 'large', kind: 'routine', interval_s: 300, params: { url: 'x'.repeat(128 * 1024) }, action: { type: 'message', to: 'local', body: 'bounded' } });
  expect(result).toEqual({ error: 'check_too_large' });
});

it('holds an oversized legacy definition without blocking a valid current check or freezing an oversized result', () => fixture(async (checks, deps, _clock, sql) => {
  checks.replace([
    row({ name: 'a-legacy', kind: 'quota', interval_s: 300 }),
    row({ name: 'b-current', kind: 'routine', interval_s: 300, action: { type: 'message', to: 'local', body: 'current' } }),
    row({ name: 'c-result', kind: 'quota', interval_s: 300 }),
  ]);
  sql.exec('UPDATE hub_checks SET params_json=? WHERE name=?', JSON.stringify({ url: 'x'.repeat(128 * 1024) }), 'a-legacy');
  deps.evaluateQuota = () => [{ label: 'x'.repeat(70000), enabled: true, status: 'blocked' }];
  const result = await checks.runDue(deps);
  expect(result[0]).toMatchObject({ name: 'a-legacy', status: 'error', detail: 'check_too_large' });
  expect(result[1]).toMatchObject({ name: 'b-current', status: 'pass', action: 'message m1' });
  expect(result[2]).toMatchObject({ name: 'c-result', status: 'error', detail: 'check_result_too_large' });
  expect(sql.exec("SELECT name FROM hub_check_slots WHERE name='a-legacy'").toArray()).toHaveLength(0);
  expect(sql.exec("SELECT length(result_json) n FROM hub_check_slots WHERE name='c-result'").toArray()[0].n).toBeLessThan(1000);
}));
