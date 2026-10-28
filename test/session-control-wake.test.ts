import { describe, expect, it } from 'vitest';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { StatusService } from '../src/status';
import { IntakeService } from '../src/intake';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '22222222-2222-4222-8222-222222222222';
const NATIVE_WS = '/srv/hub/demo';
const NATIVE_CREATED = 1792223000000;
const canonTag = (launchId: string) => 'hub-' + launchId;
const bound = (tag: string) => ({ native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED });
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}

function fakeReserve() {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'codex' }, status: 201 };
  } };
}

async function fixture(fn: (s: SessionControlService, sql: any, hub: Hub, state: DurableObjectState) => Promise<void>) {
  const fake = fakeReserve();
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-wake-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fake.hook, wakeMissed: (taskId, wakeId) => hub.intakeWatchdog.enqueueWakeRecovery(taskId, wakeId) }, {});
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql, hub, state);
  });
}

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, launchState, nativeId, at, at);
}

function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string, phase = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'codex', ?, NULL, ?, ?, NULL, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, phase, at, workspace);
}

async function startAndBind(s: SessionControlService, sql: any, key: string) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo', machine: 'hetzner' }), admin);
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  const launchId = data.fleet_launch_id || 'launch-1';
  ensureFleetLaunch(sql, launchId, data.machine || 'hetzner');
  reportFleetSession(sql, data.machine || 'hetzner', NATIVE_ID, canonTag(launchId), NATIVE_WS);
  const bres = await s.handle(request(`/api/sessions/${data.id}/binding`, { native: { ...bound(canonTag(launchId)), evidence: 'fixture' } }), host(data.machine || 'hetzner'));
  const bdata = await bres.json() as any;
  expect(bres.status, JSON.stringify(bdata)).toBe(200);
  return bdata.id ? { ...bdata, id: data.id } : data;
}

describe('session scheduled wakes', () => {
  it.each(['offered', 'starting', 'uncertain'])('retries a due wake during %s and delivers after startup', async (phase) => {
      await fixture(async (s, sql) => {
        const sess = await startAndBind(s, sql, 'wake-startup-' + phase);
        const result: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`,
          { key: 'startup', at: new Date(Date.now() + 3600000).toISOString(), prompt: 'resume' }), admin)).json();
        sql.exec('UPDATE session_control_sessions SET state=? WHERE id=?', phase, sess.id);
        sql.exec('UPDATE session_control_wakes SET wake_at_ms=? WHERE id=?', Date.now(), result.wake_id);
        expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 0 });
        expect(sql.exec('SELECT state FROM session_control_wakes WHERE id=?', result.wake_id).toArray()[0].state).toBe('pending');
        sql.exec("UPDATE session_control_sessions SET state='running' WHERE id=?", sess.id);
        expect(s.processDueWakes()).toMatchObject({ delivered: 1, missed: 0 });
      });
  });

  it.each(['offered', 'starting', 'uncertain'])('expires a due wake still %s after the delivery deadline', async (phase) => {
      await fixture(async (s, sql) => {
        const sess = await startAndBind(s, sql, 'wake-startup-expired-' + phase);
        const result: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`,
          { key: 'expired', at: new Date(Date.now() + 3600000).toISOString(), prompt: 'resume' }), admin)).json();
        sql.exec('UPDATE session_control_sessions SET state=? WHERE id=?', phase, sess.id);
        sql.exec('UPDATE session_control_wakes SET wake_at_ms=? WHERE id=?', Date.now() - 660000, result.wake_id);
        expect(s.processDueWakes()).toMatchObject({ delivered: 0, missed: 1 });
        expect(sql.exec('SELECT detail FROM session_control_wake_alerts WHERE wake_id=?', result.wake_id).toArray()[0].detail).toBe('wake_delivery_deadline_exceeded');
      });
  });

  it('registers a wake and reports waiting_until without stall', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-start-1');
      const at = new Date(Date.now() + 3600_000).toISOString();
      const wres = await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w1', at, prompt: 'resume work' }), admin);
      const wdata = await wres.json() as any;
      expect(wres.status, JSON.stringify(wdata)).toBe(201);
      expect(wdata.wake_at).toBe(at);
      const show = await (await s.handle(request(`/api/sessions/${sess.id}`, undefined, 'GET'), admin)).json() as any;
      expect(show.waiting_until).toBe(at);
      expect(show.state).toBe('waiting_until');
      expect(show.stalled).toBe(false);
      const grouped: any = await (await s.handle(request('/api/sessions?view=lifecycle', undefined, 'GET'), admin)).json();
      expect(Object.values(grouped.groups).flat().find((v: any) => v.refs?.session_id === sess.id)).toMatchObject({ state: 'waiting_until', waiting_until: at });
      const status: any = new StatusService(sql, cb => cb(), new IntakeService(sql, cb => cb()));
      const row = sql.exec('SELECT * FROM session_control_sessions WHERE id=?', sess.id).toArray()[0];
      const projected = status.hubSessionsFor({ history: [{ kind: 'link', hub_session_id: sess.id }] },
        { admin: true }, new Map(), Date.now(), null, null, new Map(), new Map([[sess.id, row]]));
      expect(projected[0]).toMatchObject({ state: 'waiting_until', waiting_until: at });
    });
  });

  it('arms the durable alarm through REST and preserves an earlier alarm', async () => {
    await fixture(async (s, sql, hub, state) => {
      const sess = await startAndBind(s, sql, 'alarm-start');
      const cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
      const post = (key: string, at: string) => hub.fetch(new Request(`https://hub.test/api/sessions/${sess.id}/wake`, {
        method: 'POST', headers: { Cookie: cookie, 'x-csrf-token': 'csrf', 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, at, prompt: 'resume' }),
      }));
      await state.storage.deleteAlarm();
      const due = Date.now() + 300000;
      const response = await post('alarm-one', new Date(due).toISOString());
      expect(response.status, JSON.stringify(await response.json())).toBe(201);
      expect(await state.storage.getAlarm()).toBe(due);
      await post('alarm-two', new Date(due + 300000).toISOString());
      expect(await state.storage.getAlarm()).toBe(due);
    });
  });

  it('replays at now across time and survives service reconstruction', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-restart');
      const at = new Date(Date.now() + 3600000).toISOString();
      const first = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'restart', at, prompt: 'resume' }), admin)).json() as any;
      const restarted = new SessionControlService(sql, cb => cb(), Date.now);
      restarted.init();
      expect(restarted.nextWakeTime()).toBe(Date.parse(at));
      const restored = await (await restarted.handle(request(`/api/sessions/${sess.id}`, undefined, 'GET'), admin)).json() as any;
      expect(restored.waiting_until).toBe(at);
      const input = { key: 'now-replay', at: 'now', prompt: 'result' };
      const now = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, input), admin)).json() as any;
      const later = new SessionControlService(sql, cb => cb(), () => Date.now() + 1000);
      const replay = await (await later.handle(request(`/api/sessions/${sess.id}/wake`, input), admin)).json() as any;
      expect(replay.wake_id).toBe(now.wake_id);
      expect(replay.replay).toBe(true);
      expect(sql.exec('SELECT id FROM session_control_wakes WHERE id=?', first.wake_id).toArray()).toHaveLength(1);
    });
  });

  it('rejects invalid wake payloads and conflicts on key', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-start-2');
      const at = new Date(Date.now() + 3600_000).toISOString();
      const bad = await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w2' }), admin);
      expect(bad.status).toBe(400);
      const w1 = await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w2', at, prompt: 'a' }), admin);
      expect(w1.status).toBe(201);
      const w2 = await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w2', at, prompt: 'different' }), admin);
      expect(w2.status).toBe(409);
    });
  });

  it('delivers due wake via send path and acks on command ack', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-start-3');
      const wres = await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w3', at: 'now', prompt: 'bank window opened' }), admin);
      expect(wres.status).toBe(201);
      const wdata = await wres.json() as any;
      const sweep = (s as any).processDueWakes();
      expect(wdata.wake_state).toBe('delivered');
      expect(sweep.delivered).toBe(0); // registration already queued this due wake
      const cmds = await (await s.handle(request(`/api/sessions/${sess.id}/commands`, undefined, 'GET'), admin)).json() as any;
      const send = cmds.commands.find((c: any) => c.kind === 'send' && c.request_key.startsWith('wake-'));
      expect(send).toBeTruthy();
      const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const offer = offers.offers.find((o: any) => o.command_id === send.id);
      expect(offer).toBeTruthy();
      const claimResponse = await s.handle(request(`/api/session-commands/${send.id}/claim`, { correlation: offer.correlation, attempt_id: ATTEMPT }), host('hetzner'));
      expect(claimResponse.status).toBe(200);
      const claim = await claimResponse.json() as any;
      const base = { correlation: offer.correlation, outcome: 'delivered', native: { native_id: NATIVE_ID, tag: 'hub-launch-1', workspace: NATIVE_WS }, claim_token: claim.claim_token };
      // Queue transport alone never proves the wake was acknowledged.
      expect((await s.handle(request(`/api/session-commands/${send.id}/ack`, base), host('hetzner'))).status).toBe(409);
      const ack = await s.handle(request(`/api/session-commands/${send.id}/ack`, { ...base, proof: {
        native_message_id: 'wake-message', reply_to_native_id: 'wake-reply', reply_digest: 'a'.repeat(64),
        sender_native_id: NATIVE_ID, sender_tag: 'hub-launch-1', sender_workspace: NATIVE_WS,
        sender_generation: 1, sender_created_at_ms: NATIVE_CREATED,
      } }), host('hetzner'));
      expect(ack.status, JSON.stringify(await ack.json())).toBe(200);
      const sweep2 = (s as any).processDueWakes();
      expect(sweep2.acked).toBeGreaterThanOrEqual(1);
      const wakes = await (await s.handle(request(`/api/sessions/${sess.id}/wakes`, undefined, 'GET'), admin)).json() as any;
      expect(wakes.wakes.find((w: any) => w.id === wdata.wake_id)?.state).toBe('acked');
    });
  });

  it('never aliases a user send key and rolls back a partial enqueue', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-command-isolation');
      const wake: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, {
        key:'isolated', at:new Date(Date.now()+300000).toISOString(), prompt:'scheduled evidence',
      }), admin)).json();
      const unrelated: any = await (await s.handle(request(`/api/sessions/${sess.id}/send`, {
        key:'wake-'+wake.wake_id.slice(0,32), body:'unrelated prompt',
      }), admin)).json();
      sql.exec('UPDATE session_control_wakes SET wake_at_ms=? WHERE id=?',Date.now()-1,wake.wake_id);
      sql.exec(`CREATE TRIGGER reject_wake_delivery BEFORE UPDATE ON session_control_wakes
        WHEN NEW.state='delivered' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END`);
      expect(s.processDueWakes().delivered).toBe(0);
      expect(sql.exec('SELECT id FROM session_control_commands').toArray()).toHaveLength(1);
      sql.exec('DROP TRIGGER reject_wake_delivery');
      expect(s.processDueWakes().delivered).toBe(1);
      const stored = sql.exec('SELECT command_id FROM session_control_wakes WHERE id=?',wake.wake_id).toArray()[0];
      expect(stored.command_id).not.toBe(unrelated.command_id);
      expect(JSON.parse(sql.exec('SELECT payload_json FROM session_control_commands WHERE id=?',stored.command_id).toArray()[0].payload_json)).toEqual({body:'scheduled evidence'});
      s.processDueWakes();
      expect(sql.exec('SELECT id FROM session_control_commands').toArray()).toHaveLength(2);
    });
  });

  it('misses wake after 10 minutes without ack with alert and intake note', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-start-4');
      const wres = await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w4', at: 'now', prompt: 'resume please' }), admin);
      const wdata = await wres.json() as any;
      expect(wres.status).toBe(201);
      (s as any).processDueWakes();
      // Backdate delivery beyond the ack window.
      const past = new Date(Date.now() - 11 * 60_1000).toISOString();
      sql.exec("UPDATE session_control_wakes SET delivered_at = ? WHERE id = ?", past, wdata.wake_id);
      const sweep = (s as any).processDueWakes();
      expect(sweep.missed).toBeGreaterThanOrEqual(1);
      const stored: any = sql.exec('SELECT state FROM session_control_wakes WHERE id = ?', wdata.wake_id).toArray()[0];
      expect(stored.state).toBe('missed');
      const alerts = sql.exec('SELECT * FROM session_control_wake_alerts WHERE wake_id = ?', wdata.wake_id).toArray();
      expect(alerts.length).toBeGreaterThanOrEqual(1);
      const status: any = new StatusService(sql, cb => cb(), new IntakeService(sql, cb => cb()));
      expect(status.needsAttention(true, [])).toContainEqual(expect.objectContaining({ subject: 'missed_wake:' + wdata.wake_id }));
      expect(status.needsAttention(false, []).some((a: any) => a.subject === 'missed_wake:' + wdata.wake_id)).toBe(false);
      const intake = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', 'fixture-intake').toArray()[0].data);
      expect(intake.history.some((e: any) => e.actor === 'hub:service:wake' && e.text.includes('no ack within 10 min'))).toBe(true);
      (s as any).processDueWakes();
      expect(sql.exec('SELECT id FROM session_control_wake_alerts WHERE wake_id=?', wdata.wake_id).toArray()).toHaveLength(1);
    });
  });

  it('alerts immediately when a delivered wake loses its session and reconciles late proof', async () => {
    await fixture(async (s, sql, hub) => {
      const sess = await startAndBind(s, sql, 'wake-gone-after-delivery');
      const result: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'gone-after', at: 'now', prompt: 'resume' }), admin)).json();
      expect(result.wake_state).toBe('delivered');
      sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", sess.id);
      const sweep = s.processDueWakes();
      expect(sweep.missed).toBe(1);
      expect(sql.exec('SELECT reason FROM intake_restart_queue WHERE task_id=?', 'fixture-intake').toArray()[0].reason).toBe('missed_wake:' + result.wake_id);
      const task = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', 'fixture-intake').toArray()[0].data);
      // A stale canonical stop cannot replace a still-running native owner.
      expect(hub.intakeWatchdog.project([task])[0].forgotten).toBe(false);
      sql.exec('INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES(?,?)', 'hetzner', new Date().toISOString());
      sql.exec("UPDATE fleet_sessions SET phase='exited',created_at_ms=? WHERE machine_id='hetzner' AND native_id=?", NATIVE_CREATED, NATIVE_ID);
      expect(hub.intakeWatchdog.project([task])[0]).toMatchObject({ forgotten:true, reason:'missed_wake_terminal_session' });
      sql.exec('DELETE FROM fleet_catalog_seen');
      expect(hub.intakeWatchdog.project([task])[0]).toMatchObject({ forgotten:false, state:'unknown' });
      const status: any = new StatusService(sql, cb => cb(), new IntakeService(sql, cb => cb()));
      expect(status.needsAttention(true, [])).toContainEqual(expect.objectContaining({ subject: 'missed_wake:' + result.wake_id }));
      // Existing proof validation owns command ack. A later verified ack clears
      // attention while preserving the historical missed-wake alert row.
      sql.exec("UPDATE session_control_commands SET state='acked' WHERE id=?", result.command_id);
      s.processDueWakes();
      expect(sql.exec('SELECT state FROM session_control_wakes WHERE id=?', result.wake_id).toArray()[0].state).toBe('acked');
      expect(status.needsAttention(true, []).some((a: any) => a.subject === 'missed_wake:' + result.wake_id)).toBe(false);
      expect(sql.exec('SELECT id FROM session_control_wake_alerts WHERE wake_id=?', result.wake_id).toArray()).toHaveLength(1);
    });
  });

  it('retains the missed deadline when a late ack precedes the next sweep', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-late-proof');
      const result: any = await (await s.handle(request(`/api/sessions/${sess.id}/wake`, { key:'late', at:'now', prompt:'resume' }), admin)).json();
      sql.exec('UPDATE session_control_wakes SET delivered_at=? WHERE id=?',new Date(Date.now()-660000).toISOString(),result.wake_id);
      sql.exec("UPDATE session_control_commands SET state='acked',updated_at=? WHERE id=?",new Date().toISOString(),result.command_id);
      expect(s.processDueWakes()).toMatchObject({missed:1,acked:1});
      expect(sql.exec('SELECT detail FROM session_control_wake_alerts WHERE wake_id=?',result.wake_id).toArray()[0].detail).toBe('wake_ack_after_10m');
      expect(sql.exec('SELECT state FROM session_control_wakes WHERE id=?',result.wake_id).toArray()[0].state).toBe('acked');
    });
  });

  it('misses wake when session is gone', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'wake-start-5');
      const at = new Date(Date.now() + 3600_000).toISOString();
      const wres = await s.handle(request(`/api/sessions/${sess.id}/wake`, { key: 'w5', at, prompt: 'later' }), admin);
      const wdata = await wres.json() as any;
      expect(wres.status).toBe(201);
      sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id = ?", sess.id);
      // Force due.
      sql.exec("UPDATE session_control_wakes SET wake_at_ms = ?, wake_at = ? WHERE id = ?", Date.now() - 1000, new Date().toISOString(), wdata.wake_id);
      const sweep = (s as any).processDueWakes();
      expect(sweep.missed).toBeGreaterThanOrEqual(1);
    });
  });
});
