import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { commandChannelHealth, watchCommandChannels, ensureCommandChannelSchema, COMMAND_CHANNEL_BACKFILL_LIMIT } from '../src/command-channel';
import { LivenessService, DEFAULT_THRESHOLDS } from '../src/liveness';

const withHub = (fn: (hub: any, state: any) => void) => (runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('cf-command-shadow-' + crypto.randomUUID())), fn);
const iso = (ms: number) => new Date(ms).toISOString();
function seedHost(raw: any, now: number) {
  raw.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('channel-host','channel-host','channel-host',?,?)", iso(now), iso(now));
  raw.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at) VALUES('channel-session','test','channel-session','{}','channel-host','codex','interactive','running',?,?)", iso(now), iso(now));
}
function command(sql: any, id: string, state: string, created: number, poll: number | null = null) {
  sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at,poll_at)
    VALUES(?,'channel-session','capture','test',?,'{}',?,0,'test',?,?,?)`, id, id, state, iso(created), iso(created), poll === null ? null : iso(poll));
}
function measure(hub: any, scope: string, fn: () => any) {
  const before = hub.meter.snapshot()[scope]?.rows_read ?? 0;
  const value = hub.meter.runSync(scope, fn);
  return { value, rows: (hub.meter.snapshot()[scope]?.rows_read ?? 0) - before };
}

describe('bounded command-channel projection migration', () => {
  it('progresses fairly over retained commands, keeps partial health unknown, and preserves an open incident', () => withHub((hub, state) => {
    const raw = state.storage.sql, now = Date.now();
    seedHost(raw, now);
    raw.exec(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<4096)
      INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at)
      SELECT 'history-'||printf('%05d',i),'channel-session','capture','test','history-'||i,'{}','acked',0,'test',?,? FROM n`, iso(now - 86400000), iso(now - 86400000));
    command(raw, 'tail-stuck-a', 'recorded', now - 20 * 60000, now - 19 * 60000);
    command(raw, 'tail-stuck-b', 'transported', now - 18 * 60000, now - 17 * 60000);
    // Simulate upgrading the deployed schema: only disposable derived test
    // tables are removed; all 4098 canonical command rows stay untouched.
    for (const name of ['command_channel_project_insert','command_channel_project_update','command_channel_project_delete','command_channel_session_move','command_channel_session_delete'])
      raw.exec('DROP TRIGGER IF EXISTS ' + name);
    for (const name of ['command_channel_evidence','command_channel_proofs','command_channel_projection_state']) raw.exec('DROP TABLE ' + name);
    ensureCommandChannelSchema(hub.sql);
    expect(hub.rowBudget.status().stopped).toBe(false);
    expect(raw.exec("SELECT name FROM pragma_index_list('session_control_commands') WHERE name IN ('session_control_commands_proof_created_v2','session_control_commands_failed_stops','session_control_commands_pending_created')").toArray()).toHaveLength(0);
    const liveness = new LivenessService(hub.sql, DEFAULT_THRESHOLDS, () => now);
    liveness.setAlert('command_channel', 'channel-host', { state: 'command_channel_unreachable', detail: 'existing incident' });
    const machine = { id: 'channel-host', last_checkin_at: iso(now) };
    const first = measure(hub, 'channel:first-page', () => commandChannelHealth(hub.sql, machine, now));
    expect(first.value.state).toBe('unknown');
    expect(first.value.detail).toContain('backfill pending');
    expect(first.rows).toBeLessThan(1500);
    watchCommandChannels(hub.sql, liveness, now);
    expect(raw.exec("SELECT resolved_at FROM liveness_alerts WHERE subject_kind='command_channel' AND subject='channel-host'").toArray()[0].resolved_at).toBe(null);
    let health = first.value;
    const maxPages = Math.ceil(4100 / COMMAND_CHANNEL_BACKFILL_LIMIT) + 2;
    for (let i = 0; i < maxPages && health.state === 'unknown'; i++) {
      const page = measure(hub, 'channel:page-' + i, () => commandChannelHealth(hub.sql, machine, now));
      expect(page.rows).toBeLessThan(1500);
      health = page.value;
    }
    expect(health.state, health.detail).toBe('unreachable');
    expect(raw.exec('SELECT complete FROM command_channel_projection_state WHERE id=1').toArray()[0].complete).toBe(1);
    const steady = measure(hub, 'channel:steady', () => commandChannelHealth(hub.sql, machine, now));
    expect(steady.value.state).toBe('unreachable');
    expect(steady.rows).toBeLessThan(100);
    const generation = raw.exec('SELECT generation FROM command_channel_projection_state WHERE id=1').toArray()[0].generation;
    hub.sql.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at) VALUES('empty-session','test','empty-session','{}','codex','interactive','queued',?,?)", iso(now), iso(now));
    const placement = measure(hub, 'channel:new-session-placement', () => hub.sql.exec("UPDATE session_control_sessions SET machine_id='channel-host' WHERE id='empty-session'"));
    expect(placement.rows).toBeLessThan(150);
    expect(raw.exec('SELECT generation FROM command_channel_projection_state WHERE id=1').toArray()[0].generation).toBe(generation);
    hub.sql.exec("DELETE FROM session_control_sessions WHERE id='empty-session'");
    expect(raw.exec('SELECT generation FROM command_channel_projection_state WHERE id=1').toArray()[0].generation).toBe(generation);
    // A command before the migrated cursor still writes through immediately.
    command(hub.sql, 'fresh-proof', 'claimed', now - 60000);
    expect(commandChannelHealth(hub.sql, machine, now).state).toBe('ok');
    watchCommandChannels(hub.sql, liveness, now);
    expect(raw.exec("SELECT resolved_at FROM liveness_alerts WHERE subject_kind='command_channel' AND subject='channel-host'").toArray()[0].resolved_at).not.toBe(null);
    // Retraction of the latest proof invalidates completeness, rather than
    // falsely claiming that an older proof still represents the latest state.
    hub.sql.exec("UPDATE session_control_commands SET state='transported' WHERE id='fresh-proof'");
    expect(commandChannelHealth(hub.sql, machine, now).state).toBe('unknown');
  }));

  it('bounds sparse eligibility before session joins and does not clear incidents on overflow', () => withHub((hub, state) => {
    const raw = state.storage.sql, now = Date.now();
    seedHost(raw, now);
    raw.exec("UPDATE session_control_sessions SET state='stopped' WHERE id='channel-session'");
    raw.exec(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<3000)
      INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at,poll_at)
      SELECT 'pending-'||printf('%05d',i),'channel-session','capture','test','pending-'||i,'{}','recorded',0,'test',?,?,? FROM n`,
      iso(now - 20 * 60000), iso(now - 20 * 60000), iso(now - 19 * 60000));
    const machine = { id: 'channel-host', last_checkin_at: iso(now) };
    for (let i = 0; i < Math.ceil(3000 / COMMAND_CHANNEL_BACKFILL_LIMIT) + 1; i++) commandChannelHealth(hub.sql, machine, now);
    const bounded = measure(hub, 'channel:sparse-session-filter', () => commandChannelHealth(hub.sql, machine, now));
    expect(bounded.rows).toBeLessThan(1000);
    expect(bounded.value.state).toBe('unknown');
    expect(bounded.value.detail).toContain('bounded observation');
    const liveness = new LivenessService(hub.sql, DEFAULT_THRESHOLDS, () => now);
    liveness.setAlert('command_channel', 'channel-host', { state: 'command_channel_unreachable', detail: 'existing incident' });
    watchCommandChannels(hub.sql, liveness, now);
    expect(raw.exec("SELECT resolved_at FROM liveness_alerts WHERE subject_kind='command_channel' AND subject='channel-host'").toArray()[0].resolved_at).toBe(null);
    // Moving a binding invalidates the generation; source session lookup is
    // always by PK and a partial rebuild never clears the previous incident.
    hub.sql.exec("UPDATE session_control_sessions SET machine_id='elsewhere' WHERE id='channel-session'");
    expect(commandChannelHealth(hub.sql, machine, now).detail).toContain('backfill pending');
  }));
});
