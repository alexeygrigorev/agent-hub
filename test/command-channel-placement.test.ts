/** Intake 3aa850b4: per-host command-channel health for safe placement.
 *
 * AC1: health derives from recent commands — offered/queued commands
 *   unclaimed beyond a bounded window while check-in is fresh — never from an
 *   observation timeout alone, and never from one isolated command
 *   (review cc84ca6f: corroboration requires two distinct stuck commands).
 * AC2: an unhealthy host is excluded from placement (reason
 *   command_channel_unreachable in plan diagnostics and routing-machines),
 *   a needs_attention alert is raised, and it recovers automatically once a
 *   later command is claimed again.
 * AC3: Hub-side transport failures never count against a host: stale
 *   check-ins read unknown, stop_host_unreachable and exhausted claim retries
 *   are not channel evidence, and pending commands without any Hub delivery
 *   receipt (command endpoint 5xx/timeout while check-ins stay fresh) read
 *   unknown instead of unreachable.
 * AC4 (this file): regression coverage for unhealthy, recovery and
 *   Hub-failure cases, the bounded-window and slow-work guarantees, and the
 *   index-bounded query plans.
 */
import { freshCpu } from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService, isCapacityOnlyHold } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { LivenessService, DEFAULT_THRESHOLDS } from '../src/liveness';
import { commandChannelHealth, watchCommandChannels, COMMAND_CHANNEL_PLAN_SQL } from '../src/command-channel';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const START = Date.parse('2026-11-01T12:00:00Z');
const MACHINE = 'ch-3aa85';

const registry = () => ({
  accounts: [{ label: 'shared-codex', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true }],
  routes: [{ machine_id: MACHINE, engine: 'codex', account: 'shared-codex', profile: null }],
});

type Ctx = { f: FleetService; s: SessionControlService; sql: any; clock: { now: number } };

async function fixture(fn: (ctx: Ctx) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('command-channel-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { now: START };
    const iso = (delta = 0) => new Date(clock.now + delta).toISOString();
    const tx = <T>(cb: () => T): T => state.storage.transactionSync(cb);
    const f = new FleetService(hub.sql, tx, () => clock.now);
    f.init();
    const s = new SessionControlService(hub.sql, tx, () => clock.now, { reserve: (input, auth) => f.reserve(input, auth) });
    s.init();
    const sql = hub.sql;
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-${MACHINE}', 'host', 'hash', 'refresh', 'family', 'test-host', '${MACHINE}', '["host"]', 'test', ?, ?)`, iso(), iso(86400000));
    sql.exec('INSERT INTO machines (id, name, hostname, credential_id, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      MACHINE, MACHINE, MACHINE, `host-${MACHINE}`, iso(), iso(),
      JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      MACHINE, '1.0', iso(), clock.now, iso(), clock.now, 'ok', null,
      JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    expect((await f.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(registry()) }), admin)).status).toBe(200);
    await fn({ f, s, sql, clock });
  });
}

/** A live session bound to the machine plus one direct command row. */
function seedCommand(ctx: Ctx, opts: { id: string; kind?: string; state: string; createdDelta: number; sessionState?: string; notBeforeDelta?: number | null; ack?: object; pollAtDelta?: number | null; claimToken?: boolean }) {
  const { sql, clock } = ctx;
  const iso = (delta = 0) => new Date(clock.now + delta).toISOString();
  sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
    VALUES (?, 'test', ?, '{}', ?, 'codex', 'headless', ?, NULL, ?, ?)`,
    'sess-' + opts.id, 'key-sess-' + opts.id, MACHINE, opts.sessionState ?? 'running', iso(-3600_000), iso(0));
  sql.exec(`INSERT INTO session_control_commands (id, session_id, kind, actor, request_key, payload_json, state, generation, correlation, claim_token, not_before, poll_at, ack_json, created_at, updated_at)
    VALUES (?, 'sess-${opts.id}', ?, 'test', ?, '{}', ?, 1, ?, ?, ?, ?, ?, ?, ?)`,
    'cmd-' + opts.id, opts.kind ?? 'capture', 'key-' + opts.id, opts.state,
    `cmd:${opts.id}:gen:1`, opts.claimToken ? `token-${opts.id}` : null,
    opts.notBeforeDelta == null ? null : iso(opts.notBeforeDelta),
    opts.pollAtDelta == null ? null : iso(opts.pollAtDelta),
    opts.ack === undefined ? null : JSON.stringify(opts.ack), iso(opts.createdDelta), iso(0));
}

const health = (ctx: Ctx) => commandChannelHealth(ctx.sql, { id: MACHINE, last_checkin_at: new Date(ctx.clock.now).toISOString() }, ctx.clock.now);
const place = (ctx: Ctx) => ctx.f.plan({ workspace: 'demo', machine: MACHINE, prompt: 'command-channel probe', mode: 'headless' });

describe('command-channel health (intake 3aa850b4)', () => {
  it('AC1/AC2: corroborated undelivered commands are unreachable, excluded from placement, alerted', () => fixture(async ctx => {
    const { f, sql, clock } = ctx;
    // Two distinct commands beyond the window; one currently carries an
    // offers receipt (the Hub delivered it and the host never claimed).
    seedCommand(ctx, { id: 'stuck1', kind: 'capture', state: 'recorded', createdDelta: -11 * 60_000 });
    seedCommand(ctx, { id: 'stuck2', kind: 'capture', state: 'transported', createdDelta: -10 * 60_000, pollAtDelta: -3 * 60_000 });
    const h = health(ctx);
    expect(h.state, h.detail).toBe('unreachable');
    expect(h.reason).toBe('command_channel_unreachable');
    expect(h.stuck_unclaimed).toBe(2);
    expect(h.oldest_stuck_age_s).not.toBeNull();
    expect(h.oldest_stuck_age_s as number).toBeGreaterThanOrEqual(660);
    const plan: any = place(ctx);
    expect(plan.selected).toBeFalsy();
    expect(plan.rejected).toContainEqual({ machine: MACHINE, engine: 'codex', reason: 'command_channel_unreachable' });
    // routing-machines carries the reason for orchestrator diagnostics.
    const hostAuth = { caller: { machineId: MACHINE, kind: 'host', actor: 'machine:' + MACHINE }, scopes: ['host'] };
    const caps = await f.routing.handle(new Request('https://hub.test/api/routing/capabilities', { method: 'PUT', body: JSON.stringify({ needs: [], max_agents: 4, runners: [{ engine: 'codex', profile: null, model: 'gpt-6.1-sol', effort: null, account: 'shared-codex' }] }) }), hostAuth);
    expect(caps.status).toBe(200);
    const machines: any = await (await f.routing.handle(new Request('https://hub.test/api/routing/machines', { method: 'GET' }), admin)).json();
    const entry = machines.machines.find((m: any) => m.id === MACHINE);
    expect(entry.command_channel.state).toBe('unreachable');
    expect(entry.command_channel.reason).toBe('command_channel_unreachable');
    // needs_attention alert opens once.
    const liveness = new LivenessService(sql, DEFAULT_THRESHOLDS, () => clock.now);
    const opened = watchCommandChannels(sql, liveness, clock.now);
    expect(opened).toHaveLength(1);
    expect(opened[0].subject_kind).toBe('command_channel');
    expect(opened[0].state).toBe('command_channel_unreachable');
    expect(watchCommandChannels(sql, liveness, clock.now)).toHaveLength(0);
  }));

  it('review cc84ca6f: one isolated stranded command never triggers exclusion', () => fixture(async ctx => {
    seedCommand(ctx, { id: 'lone', kind: 'capture', state: 'transported', createdDelta: -11 * 60_000, pollAtDelta: -2 * 60_000 });
    const h = health(ctx);
    expect(h.state, h.detail).toBe('ok');
    expect(h.reason).toBeNull();
    expect(h.stuck_unclaimed).toBe(1);
    expect(place(ctx).selected).toBeTruthy();
  }));

  it('AC3: Hub command-endpoint failure with fresh check-ins (no delivery receipt) reads unknown', () => fixture(async ctx => {
    // Two commands stranded while the Hub command endpoint was failing:
    // never transported (no poll_at), no claims anywhere, check-in fresh.
    seedCommand(ctx, { id: 'noreceipt1', kind: 'capture', state: 'recorded', createdDelta: -11 * 60_000 });
    seedCommand(ctx, { id: 'noreceipt2', kind: 'capture', state: 'recorded', createdDelta: -12 * 60_000 });
    const h = health(ctx);
    expect(h.state, h.detail).toBe('unknown');
    expect(h.reason).toBeNull();
    const plan: any = place(ctx);
    expect(plan.selected).toBeTruthy();
    expect(plan.rejected?.some((r: any) => r.reason === 'command_channel_unreachable')).toBe(false);
    const liveness = new LivenessService(ctx.sql, DEFAULT_THRESHOLDS, () => ctx.clock.now);
    expect(watchCommandChannels(ctx.sql, liveness, ctx.clock.now)).toHaveLength(0);
  }));

  it('AC2: a later claimed command proves the channel, recovers placement, resolves the alert', () => fixture(async ctx => {
    const { f, sql, clock } = ctx;
    seedCommand(ctx, { id: 'stuck2a', kind: 'capture', state: 'recorded', createdDelta: -15 * 60_000 });
    seedCommand(ctx, { id: 'stuck2b', kind: 'capture', state: 'transported', createdDelta: -14 * 60_000, pollAtDelta: -2 * 60_000 });
    expect(health(ctx).state).toBe('unreachable');
    expect(place(ctx).selected).toBeFalsy();
    const liveness = new LivenessService(sql, DEFAULT_THRESHOLDS, () => clock.now);
    watchCommandChannels(sql, liveness, clock.now);
    // A newer command reaches claimed on the same host: the channel works.
    seedCommand(ctx, { id: 'proof2', kind: 'send', state: 'claimed', createdDelta: -5 * 60_000 });
    const h = health(ctx);
    expect(h.state, h.detail).toBe('ok');
    expect(h.reason).toBeNull();
    expect(h.last_channel_proof_at).not.toBeNull();
    expect(place(ctx).selected).toBeTruthy();
    const notifications = watchCommandChannels(sql, liveness, clock.now);
    expect(notifications).toHaveLength(1);
    expect(notifications[0].subject_kind).toBe('host_recovery');
    expect(notifications[0].state).toBe('RECOVERED');
    expect(sql.exec("SELECT COUNT(*) AS n FROM liveness_alerts WHERE subject_kind = 'command_channel' AND resolved_at IS NOT NULL").one().n).toBe(1);
  }));

  it('AC2: a failed command with a recorded claim token proves the host channel recovered', () => fixture(async ctx => {
    seedCommand(ctx, { id: 'stuck-a', state: 'recorded', createdDelta: -20 * 60_000, pollAtDelta: -19 * 60_000 });
    seedCommand(ctx, { id: 'stuck-b', state: 'transported', createdDelta: -18 * 60_000, pollAtDelta: -17 * 60_000 });
    expect(health(ctx).state).toBe('unreachable');
    seedCommand(ctx, { id: 'claimed-then-failed', state: 'failed', createdDelta: -60_000,
      claimToken: true, ack: { outcome: 'failed', reason: 'Inbox refused' } });
    expect(health(ctx).state).toBe('ok');
  }));

  it('AC3: stale check-in (Hub transport failure) reads unknown, blocks only via machine_unavailable, alerts nothing', () => fixture(async ctx => {
    const { f, sql, clock } = ctx;
    seedCommand(ctx, { id: 'stuck3a', kind: 'capture', state: 'recorded', createdDelta: -11 * 60_000 });
    seedCommand(ctx, { id: 'stuck3b', kind: 'capture', state: 'transported', createdDelta: -10 * 60_000, pollAtDelta: -3 * 60_000 });
    // Hub 5xx/timeout: no successful transaction, so the persisted check-in ages out.
    sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', new Date(clock.now - 600_000).toISOString(), MACHINE);
    const h = commandChannelHealth(ctx.sql, { id: MACHINE, last_checkin_at: new Date(ctx.clock.now - 600_000).toISOString() }, ctx.clock.now);
    expect(h.state, h.detail).toBe('unknown');
    expect(h.reason).toBeNull();
    const plan: any = ctx.f.plan({ workspace: 'demo', machine: MACHINE, prompt: 'command-channel probe', mode: 'headless' });
    expect(plan.selected).toBeFalsy();
    expect(plan.rejected.some((r: any) => r.reason === 'machine_unavailable')).toBe(true);
    expect(plan.rejected.some((r: any) => r.reason === 'command_channel_unreachable')).toBe(false);
    const liveness = new LivenessService(sql, DEFAULT_THRESHOLDS, () => clock.now);
    expect(watchCommandChannels(sql, liveness, clock.now)).toHaveLength(0);
  }));

  it('AC3: stop_host_unreachable and exhausted claim retries are not evidence; corroborated stop_not_claimed is', () => fixture(async ctx => {
    seedCommand(ctx, { id: 'offhost', kind: 'stop', state: 'failed', createdDelta: -5 * 60_000, sessionState: 'uncertain', ack: { outcome: 'failed', reason: 'stop_host_unreachable' } });
    seedCommand(ctx, { id: 'retries', kind: 'capture', state: 'failed', createdDelta: -4 * 60_000, ack: { outcome: 'failed', reason: 'claim_expired_retry_exhausted' } });
    const h = health(ctx);
    expect(h.state, h.detail).toBe('ok');
    // The online-host stop signature counts within the lookback window once
    // it is corroborated by a second failed stop.
    seedCommand(ctx, { id: 'notclaim1', kind: 'stop', state: 'failed', createdDelta: -3 * 60_000, sessionState: 'uncertain', ack: { outcome: 'failed', reason: 'stop_not_claimed' } });
    expect(health(ctx).state, health(ctx).detail).toBe('ok');
    seedCommand(ctx, { id: 'notclaim2', kind: 'stop', state: 'failed', createdDelta: -2 * 60_000, sessionState: 'uncertain', ack: { outcome: 'failed', reason: 'stop_not_claimed' } });
    const after = health(ctx);
    expect(after.state, after.detail).toBe('unreachable');
    expect(after.stuck_failed_stops).toBe(2);
  }));

  it('terminal sessions and not_before-deferred messages are not evidence', () => fixture(async ctx => {
    seedCommand(ctx, { id: 'dead', kind: 'capture', state: 'recorded', createdDelta: -20 * 60_000, sessionState: 'stopped' });
    seedCommand(ctx, { id: 'defer', kind: 'message', state: 'recorded', createdDelta: -20 * 60_000, notBeforeDelta: +120_000 });
    expect(health(ctx).state).toBe('ok');
  }));

  it('terminal stop evidence ages out of the bounded lookback window', () => fixture(async ctx => {
    seedCommand(ctx, { id: 'old', kind: 'stop', state: 'failed', createdDelta: -2 * 3600_000, sessionState: 'uncertain', ack: { outcome: 'failed', reason: 'stop_not_claimed' } });
    const h = health(ctx);
    expect(h.state, h.detail).toBe('ok');
    expect(h.reason).toBeNull();
  }));

  it('review d7a50639: stuck evidence is age-bounded and ordered by its partial index', () => fixture(async ctx => {
    const { sql, clock } = ctx;
    const iso = new Date(clock.now).toISOString();
    const planOf = (q: string, params: unknown[]) => (sql.exec('EXPLAIN QUERY PLAN ' + q, ...params).toArray() as any[]).map(r => String(r.detail));
    const proof = planOf(COMMAND_CHANNEL_PLAN_SQL.proof, [iso, MACHINE]);
    expect(proof.some(d => d.includes('session_control_commands_proof_created_v2')), proof.join(' | ')).toBe(true);
    const stuck = planOf(COMMAND_CHANNEL_PLAN_SQL.stuck, [MACHINE, iso, iso, '', iso]);
    expect(stuck.some(d => d.includes('session_control_commands_pending_created')), stuck.join(' | ')).toBe(true);
    expect(stuck.some(d => d.startsWith('SEARCH') && d.includes('created_at>?') && d.includes('created_at<?')), stuck.join(' | ')).toBe(true);
    expect(stuck.some(d => d.includes('USE TEMP B-TREE')), stuck.join(' | ')).toBe(false);
    const stops = planOf(COMMAND_CHANNEL_PLAN_SQL.failedStops, [iso, '', MACHINE]);
    expect(stops.some(d => d.includes('session_control_commands_failed_stops')), stops.join(' | ')).toBe(true);
    // No plain table scan of the commands or sessions history: every SCAN
    // must go through an index (partial-index scans report USING INDEX).
    for (const detail of [...proof, ...stuck, ...stops]) {
      expect(detail.startsWith('SCAN') && !detail.includes('USING'), detail).toBe(false);
    }

    // A machine can accumulate years of old pending rows. They must stay
    // outside the finite lookback range instead of being walked to satisfy
    // the per-machine LIMIT.
    for (let i = 0; i < 256; i++) {
      seedCommand(ctx, { id: `old-${i}`, state: i % 2 ? 'recorded' : 'transported', createdDelta: -24 * 3600_000 });
    }
    const oldRows = sql.exec(COMMAND_CHANNEL_PLAN_SQL.stuck, MACHINE, iso, iso, '', iso).toArray();
    expect(oldRows).toHaveLength(0);
  }));

  it('command_channel_unreachable is incidental: it never queues alone, never hard-fails capacity waits', () => {
    const sole = [{ machine: MACHINE, engine: 'codex', reason: 'command_channel_unreachable' }];
    expect(isCapacityOnlyHold(sole)).toBe(false);
    const mixed = [...sole, { machine: MACHINE, engine: 'codex', reason: 'launcher_memory_reserve' }];
    expect(isCapacityOnlyHold(mixed)).toBe(true);
  });
});
