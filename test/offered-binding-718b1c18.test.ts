// Intake 718b1c18: Hub session f521a611 stayed offered with a null binding
// while its native worked. Its wakes missed with "could not queue delivery".
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { seedFixtureIntake, withFixtureIntake, FIXTURE_INTAKE_ID } from './tbm-fixture';
import { SessionControlService } from '../src/session_control';
import { IdleWorkService } from '../src/idle-work';
import { settleUnstartedInteractiveLaunches } from '../src/launch-start-settlement';
import { wakeNoNativeReason } from '../src/wake-native-evidence';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId: 'host-cred-1' }, scopes: ['host'] });
const NATIVE_ID = '44444444-4444-4444-8444-444444444444';
const SECOND_NATIVE_ID = '55555555-5555-4555-8555-555555555555';
const NATIVE_WS = '/srv/hub/oneinstall';
const NATIVE_CREATED = 1793283719586;

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}

async function fixture(fn: (s: SessionControlService, sql: any, hub: Hub, clock: { now: number }) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('offered-binding-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { now: Date.now() };
    let launches = 0;
    const reserve = (input: any) => ({ value: { id: 'launch-' + (++launches), state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'codex' }, status: 201 });
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), () => clock.now,
      { reserve, wakeMissed: (taskId, wakeId) => hub.intakeWatchdog.enqueueWakeRecovery(taskId, wakeId) }, {});
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date(clock.now).toISOString();
    const far = new Date(clock.now + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql, hub, clock);
  });
}

/** An offered interactive session whose launch the host claimed `ageMs` ago. */
async function claimedOffered(s: SessionControlService, sql: any, clock: { now: number }, key: string, ageMs: number) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo', machine: 'hetzner', mode: 'interactive', intake_task_id: FIXTURE_INTAKE_ID }), admin);
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  const launchId = data.fleet_launch_id;
  const claimed = new Date(clock.now - ageMs).toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, claim_key, created_at, updated_at)
    VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', 'hetzner', 'codex', 'fixture-acct', NULL, 'interactive', 'demo', 'goal', 'starting', NULL, NULL, 'a25ba39ce081427cbaee4c480ab211fe', ?, ?)`,
    launchId, 'fleet-' + launchId, claimed, claimed);
  sql.exec("UPDATE session_control_sessions SET engine='codex' WHERE id=?", data.id);
  const row = sql.exec('SELECT * FROM session_control_sessions WHERE id=?', data.id).toArray()[0];
  expect(row).toMatchObject({ state: 'offered', binding_json: null, fleet_launch_id: launchId });
  return { id: data.id as string, launchId: launchId as string };
}

function reportNative(sql: any, launchId: string, phase = 'running', clock = { now: Date.now() }, nativeId = NATIVE_ID) {
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at, created_at_ms)
    VALUES ('hetzner', ?, ?, 'codex', ?, 'idle', ?, ?, ?, NULL, 'fixture-acct', NULL, ?)`,
    nativeId, 'hub-' + launchId, phase, new Date(clock.now).toISOString(), NATIVE_WS, NATIVE_WS, NATIVE_CREATED);
}

function reportCompleteCensus(sql: any, clock: { now: number }) {
  sql.exec(`INSERT INTO fleet_catalog_census (machine_id, observed_at, complete) VALUES ('hetzner', ?, 1)
    ON CONFLICT(machine_id) DO UPDATE SET observed_at=excluded.observed_at, complete=1`, new Date(clock.now).toISOString());
}

async function bind(s: SessionControlService, id: string, launchId: string, nativeId = NATIVE_ID) {
  return s.handle(request(`/api/sessions/${id}/binding`, { native: { native_id: nativeId, tag: 'hub-' + launchId, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }), host('hetzner'));
}

const launchRow = (sql: any, id: string) => sql.exec('SELECT * FROM fleet_launches WHERE id=?', id).toArray()[0];
const sessionRow = (sql: any, id: string) => sql.exec('SELECT * FROM session_control_sessions WHERE id=?', id).toArray()[0];

describe('intake 718b1c18: offered sessions track their real native', () => {
  it('AC2: a complete census settles a claimed no-native launch in that reconcile cycle', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const fresh = await claimedOffered(s, sql, clock, 'fresh', 0);
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, false)).toEqual({ failed: 0, pending: 0 });
      expect(sessionRow(sql, fresh.id).state).toBe('offered');
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, true)).toEqual({ failed: 1, pending: 0 });
      expect(launchRow(sql, fresh.launchId)).toMatchObject({ state: 'failed', exit_reason: 'native_start_not_observed', verification_status: 'unverified' });
      expect(JSON.parse(launchRow(sql, fresh.launchId).launch_error_json).reason).toBe('native_start_not_observed');
      expect(sessionRow(sql, fresh.id).state).toBe('failed');
      const shown = await (await s.handle(request(`/api/sessions/${fresh.id}`, undefined, 'GET'), admin)).json() as any;
      expect(shown.state).toBe('failed');
      expect(JSON.stringify(shown)).toContain('native_start_not_observed');
    });
  });

  it('AC2: the complete Fleet catalog PUT invokes unstarted-launch settlement', async () => {
    await fixture(async (s, sql, hub, clock) => {
      const fresh = await claimedOffered(s, sql, clock, 'catalog-settlement', 0);
      const incomplete = await hub.fleetService.handle(
        request('/api/fleet/sessions', { sessions: [], census_complete: false }, 'PUT'), host('hetzner'));
      expect(incomplete.status).toBe(200);
      expect(sessionRow(sql, fresh.id).state).toBe('offered');
      const complete = await hub.fleetService.handle(
        request('/api/fleet/sessions', { sessions: [], census_complete: true }, 'PUT'), host('hetzner'));
      expect(complete.status).toBe(200);
      expect(sessionRow(sql, fresh.id).state).toBe('failed');
      expect(launchRow(sql, fresh.launchId).exit_reason).toBe('native_start_not_observed');
    });
  });

  it('AC2: live unbound rows cannot starve later no-native settlements', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const live: Array<{ id: string; launchId: string }> = [];
      for (let i = 0; i < 25; i++) {
        const start = await claimedOffered(s, sql, clock, 'live-' + i, 0);
        reportNative(sql, start.launchId, 'running', clock, `44444444-4444-4444-8444-${String(i + 1).padStart(12, '0')}`);
        live.push(start);
      }
      const noNative: Array<{ id: string; launchId: string }> = [];
      for (let i = 0; i < 25; i++) noNative.push(await claimedOffered(s, sql, clock, 'none-' + i, 0));
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, true)).toEqual({ failed: 25, pending: 25 });
      expect(noNative.every(item => sessionRow(sql, item.id).state === 'failed')).toBe(true);
      expect(live.every(item => sessionRow(sql, item.id).state === 'uncertain')).toBe(true);
    });
  });

  it('AC2: a native that ended before binding fails the launch with its own reason', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const gone = await claimedOffered(s, sql, clock, 'gone', 0);
      reportNative(sql, gone.launchId, 'exited', clock);
      const wake: any = await (await s.handle(request(`/api/sessions/${gone.id}/wake`, { key: 'w', at: 'now', prompt: 'resume' }), admin)).json();
      expect(wake.wake_state).toBe('missed');
      expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 0 });
      expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray()[0].n).toBe(1);
      expect(sql.exec('SELECT detail FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray()[0].detail)
        .toBe('wake_session_unbound_native_exited');
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, true)).toEqual({ failed: 1, pending: 0 });
      expect(launchRow(sql, gone.launchId).exit_reason).toBe('native_exited_before_binding');
      expect(sessionRow(sql, gone.id).state).toBe('failed');
    });
  });

  it('AC2: a previously observed but now unobserved native gets an honest absence reason', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const gone = await claimedOffered(s, sql, clock, 'unobserved', 0);
      reportNative(sql, gone.launchId, 'unobserved', clock);
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, true)).toEqual({ failed: 1, pending: 0 });
      expect(launchRow(sql, gone.launchId).exit_reason).toBe('native_unobserved_before_binding');
      expect(JSON.parse(launchRow(sql, gone.launchId).launch_error_json).message).toMatch(/absent from a complete host census/);
      expect(sessionRow(sql, gone.id).state).toBe('failed');
    });
  });

  it('AC2: a live unbound native is never left offered; binding moves it to running and settles the launch', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const live = await claimedOffered(s, sql, clock, 'live', 0);
      reportNative(sql, live.launchId, 'running', clock);
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, true)).toEqual({ failed: 0, pending: 1 });
      expect(sessionRow(sql, live.id)).toMatchObject({ state: 'uncertain', binding_json: null });
      expect(JSON.parse(launchRow(sql, live.launchId).launch_error_json).reason).toBe('binding_pending_native_observed');
      // Repeated censuses write nothing more.
      const before = launchRow(sql, live.launchId).updated_at;
      clock.now += 60000;
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, true)).toEqual({ failed: 0, pending: 0 });
      expect(launchRow(sql, live.launchId).updated_at).toBe(before);
      const res = await bind(s, live.id, live.launchId);
      expect(res.status, JSON.stringify(await res.clone().json())).toBe(200);
      expect(sessionRow(sql, live.id).state).toBe('running');
      expect(JSON.parse(sessionRow(sql, live.id).binding_json).native_id).toBe(NATIVE_ID);
      // The launch records the verified native: no endless re-offer of a
      // 'starting' launch with native_id NULL, and exit settlement can match it.
      expect(launchRow(sql, live.launchId)).toMatchObject({ state: 'running', native_id: NATIVE_ID, launch_error_json: null });
      expect(settleUnstartedInteractiveLaunches(sql, 'hetzner', clock.now, true)).toEqual({ failed: 0, pending: 0 });
    });
  });

  it('AC2: an uncertain Fleet launch remains on the host binding feed and can bind', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const live = await claimedOffered(s, sql, clock, 'uncertain-live', 0);
      sql.exec("UPDATE fleet_launches SET state='uncertain' WHERE id=?", live.launchId);
      sql.exec("UPDATE session_control_sessions SET state='uncertain' WHERE id=?", live.id);
      reportNative(sql, live.launchId, 'running', clock);
      const feed = await s.handle(request('/api/sessions/host-bindings', undefined, 'GET'), host('hetzner'));
      expect(feed.status).toBe(200);
      expect((await feed.json() as any).sessions).toContainEqual(expect.objectContaining({ id: live.id, fleet_launch_id: live.launchId }));
      const res = await bind(s, live.id, live.launchId);
      expect(res.status, JSON.stringify(await res.clone().json())).toBe(200);
      expect(sessionRow(sql, live.id).state).toBe('running');
      expect(launchRow(sql, live.launchId)).toMatchObject({ state: 'running', native_id: NATIVE_ID });
    });
  });

  it('AC3: a wake for an offered session with a live native is delivered once the host binds it', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const sess = await claimedOffered(s, sql, clock, 'wake-live', 60000);
      reportNative(sql, sess.launchId, 'running', clock);
      const wake: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w', at: 'now', prompt: 'resume' }), admin)).json();
      expect(wake.wake_id).toBeTruthy();
      expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 0 });
      expect((await bind(s, sess.id, sess.launchId)).status).toBe(200);
      expect(s.processDueWakes()).toMatchObject({ delivered: 1, missed: 0 });
      const stored = sql.exec('SELECT * FROM session_control_wakes WHERE id=?', wake.wake_id).toArray()[0];
      expect(stored.state).toBe('delivered');
      const cmd = sql.exec('SELECT * FROM session_control_commands WHERE id=?', stored.command_id).toArray()[0];
      expect(cmd).toMatchObject({ session_id: sess.id, kind: 'send' });
    });
  });

  it('AC3: absence requires a complete census at or after the launch claim', async () => {
    await fixture(async (_s, sql, _hub, clock) => {
      const sess = await claimedOffered(_s, sql, clock, 'wake-census-order', 0);
      const row = sessionRow(sql, sess.id);
      expect(wakeNoNativeReason(sql, row, null, clock.now)).toBeNull();
      sql.exec('UPDATE fleet_launches SET updated_at=? WHERE id=?', new Date(clock.now + 1000).toISOString(), sess.launchId);
      reportCompleteCensus(sql, clock);
      expect(wakeNoNativeReason(sql, row, null, clock.now)).toBeNull();
      clock.now += 2000;
      reportCompleteCensus(sql, clock);
      expect(wakeNoNativeReason(sql, row, null, clock.now)).toBe('wake_session_unbound_native_not_observed');
    });
  });

  it('AC3: a wake for a no-native session fails fast with an explicit reason, once, into idle recovery', async () => {
    await fixture(async (s, sql, hub, clock) => {
      const sess = await claimedOffered(s, sql, clock, 'wake-dead', 0);
      const wake: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w', at: 'now', prompt: 'resume' }), admin)).json();
      const reconcile = await hub.fleetService.handle(
        request('/api/fleet/sessions', { sessions: [], census_complete: true }, 'PUT'), host('hetzner'));
      expect(reconcile.status).toBe(200);
      expect(sessionRow(sql, sess.id).state).toBe('failed');
      // The next sweep misses immediately instead of waiting out the deadline.
      expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 1 });
      const alerts = sql.exec('SELECT detail FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray();
      expect(alerts.map((a: any) => a.detail)).toEqual(['wake_session_unbound_launch_failed']);
      expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 0 });
      expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray()[0].n).toBe(1);
      expect(sql.exec('SELECT reason FROM intake_restart_queue WHERE task_id=?', FIXTURE_INTAKE_ID).toArray()).toEqual([{ reason: 'missed_wake:' + wake.wake_id }]);
      const task = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', FIXTURE_INTAKE_ID).toArray()[0].data);
      expect(task.history.filter((e: any) => e.text?.includes('has no live native'))).toHaveLength(1);
      void hub;
    });
  });

  it('AC3: a fresh complete census proves an unbound start has no native and misses its wake immediately', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const sess = await claimedOffered(s, sql, clock, 'wake-absent', 0);
      const wake: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w', at: 'now', prompt: 'resume' }), admin)).json();
      reportCompleteCensus(sql, clock);
      expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 1 });
      expect(sql.exec('SELECT state FROM session_control_wakes WHERE id=?', wake.wake_id).toArray()[0].state).toBe('missed');
      expect(sql.exec('SELECT detail FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray()[0].detail)
        .toBe('wake_session_unbound_native_not_observed');
      expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 0 });
      expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray()[0].n).toBe(1);
      expect(sql.exec('SELECT reason FROM intake_restart_queue WHERE task_id=?', FIXTURE_INTAKE_ID).toArray())
        .toEqual([{ reason: 'missed_wake:' + wake.wake_id }]);
    });
  });

  it('AC3: a wake for a bound session whose native exited fails fast instead of queueing a send', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const sess = await claimedOffered(s, sql, clock, 'wake-exited', 60000);
      reportNative(sql, sess.launchId, 'running', clock);
      expect((await bind(s, sess.id, sess.launchId)).status).toBe(200);
      // A complete census no longer reports the native. Registering the wake
      // sweeps due wakes at once; it must not queue a send to a dead native.
      sql.exec("UPDATE fleet_sessions SET phase='unobserved' WHERE native_id=?", NATIVE_ID);
      reportCompleteCensus(sql, clock);
      const wake: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w', at: 'now', prompt: 'resume' }), admin)).json();
      expect(s.processDueWakes()).toMatchObject({ delivered: 0 });
      expect(sql.exec('SELECT state FROM session_control_wakes WHERE id=?', wake.wake_id).toArray()[0].state).toBe('missed');
      expect(sql.exec('SELECT detail FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray()[0].detail).toBe('wake_native_unobserved');
      expect(sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE session_id=? AND kind='send'", sess.id).toArray()[0].n).toBe(0);
    });
  });

  it('AC3: the deadline miss names the unbound state', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      const sess = await claimedOffered(s, sql, clock, 'wake-deadline', 60000);
      const wake: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w', at: 'now', prompt: 'resume' }), admin)).json();
      clock.now += 11 * 60000;
      expect(s.processDueWakes()).toMatchObject({ missed: 1 });
      expect(sql.exec('SELECT detail FROM session_control_wake_alerts WHERE wake_id=?', wake.wake_id).toArray()[0].detail).toBe('wake_delivery_deadline_exceeded');
      const task = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', FIXTURE_INTAKE_ID).toArray()[0].data);
      expect(task.history.some((e: any) => e.text?.includes('Session stayed offered with no native binding'))).toBe(true);
    });
  });

  it('AC4: a replacement only stuck in offered blocks further replacements for the intake', async () => {
    await fixture(async (s, sql, _hub, clock) => {
      // The stalled original: bound, running, idle with open AC.
      const original = await claimedOffered(s, sql, clock, 'original', 60000);
      reportNative(sql, original.launchId, 'running', clock);
      expect((await bind(s, original.id, original.launchId)).status).toBe(200);
      const otherOriginal = await claimedOffered(s, sql, clock, 'other-original', 60000);
      reportNative(sql, otherOriginal.launchId, 'running', clock, SECOND_NATIVE_ID);
      expect((await bind(s, otherOriginal.id, otherOriginal.launchId, SECOND_NATIVE_ID)).status).toBe(200);
      // A previous replacement for the same intake that never bound.
      const stuck = await claimedOffered(s, sql, clock, 'stuck-replacement', 60000);
      sql.exec('UPDATE session_control_sessions SET parent_id=? WHERE id=?', original.id, stuck.id);
      const idle = new IdleWorkService(sql, (f: any) => f(), s, {}, () => clock.now); idle.init();
      // Two failed nudges already recorded on the episode for the original.
      const identity = JSON.stringify(['hetzner', NATIVE_ID, NATIVE_CREATED, 1]);
      sql.exec(`INSERT INTO session_idle_episodes VALUES(?,?,?,1,?,?,NULL,NULL,?)`, original.id, 'ep-1', clock.now,
        JSON.stringify([{ key: 'n0', at: 0, command_id: 'c0', failed: true }, { key: 'n1', at: 0, command_id: 'c1', failed: true }]), clock.now, identity);
      const secondIdentity = JSON.stringify(['hetzner', SECOND_NATIVE_ID, NATIVE_CREATED, 1]);
      sql.exec(`INSERT INTO session_idle_episodes VALUES(?,?,?,1,?,?,NULL,NULL,?)`, otherOriginal.id, 'ep-2', clock.now,
        JSON.stringify([{ key: 'n2', at: 0, command_id: 'c2', failed: true }, { key: 'n3', at: 0, command_id: 'c3', failed: true }]), clock.now, secondIdentity);
      const stalled = new Date(clock.now - 7200000).toISOString();
      const tick = async () => {
        clock.now += 60000;
        sql.exec('INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES(?,?)', 'hetzner', new Date(clock.now).toISOString());
        for (const nativeId of [NATIVE_ID, SECOND_NATIVE_ID]) sql.exec("UPDATE fleet_sessions SET progress_json=?, last_activity_at=? WHERE native_id=?",
          JSON.stringify({ last_progress_at: stalled, sampled_at: new Date(clock.now).toISOString(), headless: false, agent_alive: true, orphaned: false, cleanup: 'none' }),
          stalled, nativeId);
        await idle.tick();
      };
      const sessionsBefore = sql.exec('SELECT COUNT(*) AS n FROM session_control_sessions').toArray()[0].n;
      const replace = (s as any).handle.bind(s);
      let created = 0;
      (s as any).handle = async (req: Request, auth: any) => { if (new URL(req.url).pathname === '/api/sessions' && req.method === 'POST') created++; return replace(req, auth); };
      (s as any).attention = () => [original, otherOriginal].map(row => ({ session_id: row.id, state: 'idle_with_open_ac', idle_for: 7200, last_activity_at: new Date(clock.now - 7200000).toISOString() }));
      for (let i = 0; i < 3; i++) await tick();
      expect(created).toBe(0);
      expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_sessions').toArray()[0].n).toBe(sessionsBefore);
      const task = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', FIXTURE_INTAKE_ID).toArray()[0].data);
      expect(task.history.filter((e: any) => e.text?.startsWith('Replacement for ' + original.id + ' held'))).toHaveLength(1);
      // Once the stuck start is settled as failed, exactly one replacement is created.
      sql.exec("UPDATE session_control_sessions SET state='failed' WHERE id=?", stuck.id);
      for (let i = 0; i < 3; i++) await tick();
      expect(created).toBe(1);
    });
  });
});
