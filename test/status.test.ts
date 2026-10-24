import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const source = { type: 'unknown', reference: null, timestamp: null };
let cookie: string;
let seq = 0;

async function call(path: string, bearer?: string) {
  return SELF.fetch('https://hub.test/api/status' + path, { method: 'GET',
    headers: bearer ? { Authorization: 'Bearer ' + bearer } : { Cookie: cookie, 'x-csrf-token': 'csrf' } });
}
async function ok(path: string, bearer?: string): Promise<any> {
  const res = await call(path, bearer);
  const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200);
  return data;
}
async function intakeCall(path: string, payload?: any, bearer?: string) {
  return SELF.fetch('https://hub.test/api/intake' + path, { method: payload ? 'POST' : 'GET',
    headers: { ...(bearer ? { Authorization: 'Bearer ' + bearer } : { Cookie: cookie, 'x-csrf-token': 'csrf' }) },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
}
async function intakeOk(path: string, payload?: any, bearer?: string): Promise<any> {
  const res = await intakeCall(path, payload, bearer);
  const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200);
  return data;
}
const createTask = (project = 'demo', title = 'Synthetic task', text = 'Synthetic request') =>
  intakeOk('/tasks', { key: 'status-create-' + ++seq, project, title, text, source });

async function seedAgent(actor = 'demo:assistant', family = 'family-' + ++seq): Promise<any> {
  const id = 'credential-' + ++seq, token = 'hubt_fixture_' + seq, refresh = 'hubr_fixture_' + seq;
  const hash = await sha256(token), rh = await sha256(refresh);
  await inDO(stub(), (h: any) => {
    const [project, tag] = actor.split(':');
    h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z',?,0)", new Date().toISOString());
    h.sql.exec('INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES (?,?,?,?)', tag, project, 'fixture-host', '2026-01-01T00:00:00.000Z');
    h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      id, 'agent', hash, rh, family, actor, 'fixture-host', actor, '["bus:read","bus:send"]', 'fixture-operator', '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
  });
  return { id, token, refresh, family, actor };
}
async function grant(agent: any, scope = 'project', target: string | null = 'demo', access = 'write') {
  return intakeOk('/grants', { key: 'status-grant-' + ++seq, credential_id: agent.id, scope, target, access, expires_at: Date.now() + 3600000, source });
}
function seedCanonicalHosts() {
  return inDO(stub(), (h: any) => {
    const now = new Date().toISOString();
    h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('host-a','a','a','2026-01-01T00:00:00.000Z',?,0)", now);
    h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('host-old','old','old','2026-01-01T00:00:00.000Z','2020-01-01T00:00:00.000Z',0)");
  });
}
function seedCanonicalChain(jobId: string, launchId: string, machine: string, native: string, tag: string) {
  return inDO(stub(), (h: any) => {
    const now = new Date().toISOString();
    h.sql.exec("INSERT OR IGNORE INTO routing_jobs(id,actor,request_key,request_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
      jobId, 'demo:assistant', 'key-' + jobId, '{}', 'running', now, now);
    h.sql.exec("INSERT OR IGNORE INTO fleet_launches(id,actor,requester_kind,parent_id,summary,request_key,request_json,machine_id,engine,account,profile,mode,workspace,prompt,state,native_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      launchId, 'demo:assistant', 'agent', null, 'Synthetic', 'key-' + launchId, '{}', machine, 'opencode', 'default', null, 'headless', 'demo', 'Synthetic prompt', 'running', native, now, now);
    h.sql.exec('INSERT OR REPLACE INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
      machine, native, tag, 'opencode', 'running', 'working', now, 'demo', null, 'default', now);
    h.sql.exec('UPDATE routing_jobs SET launch_id=? WHERE id=?', launchId, jobId);
  });
}

function seedHostMachine(machine: string, token: string, family = 'host-family-' + ++seq): Promise<any> {
  return (async () => {
    const hash = await sha256(token);
    const id = 'host-credential-' + seq;
    await inDO(stub(), (h: any) => {
      const now = new Date().toISOString();
      const future = new Date(Date.now() + 86400000).toISOString();
      h.sql.exec('INSERT OR REPLACE INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        id, 'host', hash, null, family, 'host:' + machine, machine, 'host:' + machine, '["host"]', 'fixture-operator', now, future);
      h.sql.exec("INSERT OR REPLACE INTO machines(id,name,hostname,registered_at,last_checkin_at,drained,credential_id) VALUES (?,?,?,?,?,0,?)",
        machine, machine, machine, now, now, id);
    });
    return { id, token, family, machine };
  })();
}
async function obsCall(payload: any, bearer?: string, csrf = 'csrf') {
  return SELF.fetch('https://hub.test/api/status/observations', { method: 'POST',
    headers: { ...(bearer ? { Authorization: 'Bearer ' + bearer } : { Cookie: cookie, 'x-csrf-token': csrf }), 'Content-Type': 'application/json' },
    body: JSON.stringify(payload) });
}
async function obsOk(payload: any, bearer?: string, csrf = 'csrf'): Promise<any> {
  const res = await obsCall(payload, bearer, csrf);
  const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200);
  return data;
}
function seedHubSession(sessionId: string, actor: string, machine: string, state: string, native: string | null) {
  return inDO(stub(), (h: any) => {
    const now = new Date().toISOString();
    h.sql.exec('INSERT OR REPLACE INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
      sessionId, actor, 'key-' + sessionId, '{}', null, machine, 'opencode', 'headless', state,
      native ? JSON.stringify({ native_id: native }) : null, now, now);
    if (native) {
      h.sql.exec('INSERT OR REPLACE INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        machine, native, 'hub-tag', 'opencode', 'running', 'working', now, 'demo', null, 'default', now);
    }
  });
}

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    for (const t of ['tasks', 'grants', 'rules', 'receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sql.exec('DELETE FROM credentials'); h.sql.exec('DELETE FROM agents');
    h.sql.exec('DELETE FROM routing_jobs'); h.sql.exec('DELETE FROM fleet_launches'); h.sql.exec('DELETE FROM fleet_sessions');
    h.sql.exec('DELETE FROM machines');
    try { h.sql.exec('DELETE FROM session_control_sessions'); } catch {}
    try { h.sql.exec('DELETE FROM session_control_commands'); } catch {}
    try { h.sql.exec('DELETE FROM session_lifecycle_observations'); } catch {}
    try { h.sql.exec('DELETE FROM session_lifecycle_decisions'); } catch {}
    try { h.sql.exec('DELETE FROM status_observations'); } catch {}
    try { h.sql.exec('DELETE FROM status_baselines'); } catch {}
  });
});

describe('M14a authorized one-call status projection', () => {
  it('surfaces stuck launch work alerts only to operators and removes resolved episodes', async () => {
    await inDO(stub(), (h: any) => {
      h.livenessService.setAlert('launch', 'never-claimed', { state: 'stuck', detail: 'offered for 601s' });
    });
    expect((await ok('')).needs_attention).toContainEqual(expect.objectContaining({
      subject: 'launch:never-claimed', title: 'Launch stuck: never-claimed', state: 'stuck',
    }));
    const agent = await seedAgent();
    await grant(agent);
    expect((await ok('', agent.token)).needs_attention.some((a: any) => a.subject === 'launch:never-claimed')).toBe(false);
    await inDO(stub(), (h: any) => h.livenessService.setAlert('launch', 'never-claimed', null));
    expect((await ok('')).needs_attention.some((a: any) => a.subject === 'launch:never-claimed')).toBe(false);
  });
  it('includes active check alerts in operator status without leaking them to task-granted agents', async () => {
    await inDO(stub(), (h: any) => {
      h.livenessService.setAlert('check', 'status-delivery-slo', { state: 'fail', detail: '2 pending messages exceed the configured SLO' });
    });
    const operator = await ok('');
    expect(operator.needs_attention).toContainEqual(expect.objectContaining({
      subject: 'check:status-delivery-slo', state: 'fail', source: 'liveness_alerts',
    }));
    const agent = await seedAgent();
    await grant(agent);
    const scoped = await ok('', agent.token);
    expect(scoped.needs_attention.some((a: any) => a.subject === 'check:status-delivery-slo')).toBe(false);
    await inDO(stub(), (h: any) => h.livenessService.setAlert('check', 'status-delivery-slo', null));
    const recovered = await ok('');
    expect(recovered.needs_attention.some((a: any) => a.subject === 'check:status-delivery-slo')).toBe(false);
  });

  it('projects multiple tasks and hosts with partial coverage for a missing host', async () => {
    await seedCanonicalHosts();
    const first = await createTask();
    const second = await createTask('demo', 'Second task');
    await intakeOk('/tasks/' + first.task.id + '/events', { key: 'link-a-' + ++seq, expected_version: 1,
      event: { kind: 'link', native_id: 'native-a', engine: 'opencode', host: 'host-a', provenance: source } });
    await intakeOk('/tasks/' + second.task.id + '/events', { key: 'link-old-' + ++seq, expected_version: 1,
      event: { kind: 'link', native_id: 'native-old', engine: 'opencode', host: 'host-old', provenance: source } });
    await intakeOk('/tasks/' + first.task.id + '/events', { key: 'rep-' + ++seq, expected_version: 2,
      event: { kind: 'report', reference: 'reports/synthetic.md', source } });
    await intakeOk('/tasks/' + first.task.id + '/events', { key: 'commit-' + ++seq, expected_version: 3,
      event: { kind: 'commit', reference: 'abc123def', source } });
    const data = await ok('');
    expect(data.tasks.length).toBe(2);
    expect(data.coverage.sessions).toBe('partial');
    expect(data.coverage.missing_hosts).toContain('host-old');
    const withOld = data.tasks.find((t: any) => t.id === second.task.id);
    expect(withOld.session_coverage.coverage).toBe('partial');
    expect(withOld.reports).toEqual([]);
    const withFirst = data.tasks.find((t: any) => t.id === first.task.id);
    expect(withFirst.reports[0]).toMatchObject({ reference: 'reports/synthetic.md', headline: 'unknown' });
    expect(withFirst.reports[0].provenance.successor).toBe('M14b');
    expect(withFirst.commits[0]).toMatchObject({ reference: 'abc123def', deploy_state: 'unknown', ci_state: 'unknown' });
    expect(data.categories.diff.state).toBe('not_available_on_get');
    expect(data.categories.reports.state).toBe('unknown_not_collected');
  });

  it('flows verified canonical launch session data while forged claims stay unverified', async () => {
    await seedCanonicalHosts();
    await seedCanonicalChain('job-real', 'launch-real', 'host-a', 'native-real', 'real-tag');
    const { task } = await createTask();
    await intakeOk('/tasks/' + task.id + '/events', { key: 'verified-link-' + ++seq, expected_version: 1,
      event: { kind: 'link', native_id: 'native-real', engine: 'opencode', host: 'host-a', routing_job_id: 'job-real', provenance: source } });
    await intakeOk('/tasks/' + task.id + '/events', { key: 'forged-link-' + ++seq, expected_version: 2,
      event: { kind: 'link', native_id: 'foreign-native', engine: 'opencode', host: 'host-a', provenance: source } });
    await inDO(stub(), (h: any) => {
      const now = new Date().toISOString();
      h.sql.exec('INSERT OR REPLACE INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,profile,account,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        'host-a', 'foreign-native', 'secret-foreign-tag', 'opencode', 'running', 'working', now, 'demo', null, 'default', now);
    });
    const data = await ok('');
    const row = data.tasks.find((t: any) => t.id === task.id);
    const verified = row.sessions.find((s: any) => s.native_id === 'native-real');
    expect(verified.linkage).toBe('verified_launch');
    expect(verified.tag).toBe('real-tag');
    expect(verified.provenance.source).toContain('canonical_join');
    const forged = row.sessions.find((s: any) => s.native_id === 'foreign-native');
    expect(forged.linkage).toBe('claimed_link_unverified');
    expect(forged.tag).toBeNull();
  });

  it('isolates cross-caller tasks and rejects expired grants without data', async () => {
    const a = await seedAgent('demo:assistant-a');
    const b = await seedAgent('other:assistant-b');
    await grant(a, 'project', 'demo', 'read');
    await grant(b, 'project', 'other', 'read');
    await createTask('demo', 'Demo task');
    await createTask('other', 'Other task');
    const seenA = await ok('', a.token);
    expect(seenA.tasks.map((t: any) => t.project)).toEqual(['demo']);
    expect(seenA.authorized_scope).toBe('explicit_grants_only');
    const seenB = await ok('', b.token);
    expect(seenB.tasks.map((t: any) => t.project)).toEqual(['other']);
    await inDO(stub(), (h: any) => {
      const rows = h.sql.exec('SELECT data FROM intake_grants').toArray() as any[];
      for (const row of rows) {
        const g = JSON.parse(row.data);
        if (g.scope === 'project' && g.target === 'demo') {
          g.expires_at = 0;
          h.sql.exec('UPDATE intake_grants SET data=? WHERE id=?', JSON.stringify(g), g.id);
        }
      }
    });
    const denied = await call('', a.token);
    expect(denied.status).toBe(403);
    expect(JSON.stringify(await denied.json())).not.toContain('Demo task');
  });

  it('rejects revoked credentials and unknown routing references without existence leaks', async () => {
    const a = await seedAgent();
    await grant(a);
    const { task } = await createTask();
    const forged = await intakeCall('/tasks/' + task.id + '/events',
      { key: 'forged-job-' + ++seq, expected_version: 1, event: { kind: 'link', routing_job_id: 'no-such-job', provenance: source } }, a.token);
    expect(forged.status).toBe(403);
    const hash = await sha256(a.token);
    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE credentials SET revoked_at='2026-01-01T00:00:00.000Z' WHERE id=?", a.id);
    });
    void hash;
    const revoked = await call('', a.token);
    expect([401, 403]).toContain(revoked.status);
    const body = JSON.stringify(await revoked.json());
    expect(body).not.toContain(task.id);
  });

  it('since filters available rows while diff stays honestly unavailable', async () => {
    const { task } = await createTask();
    const all = await ok('');
    expect(all.tasks.map((t: any) => t.id)).toContain(task.id);
    expect(all.categories.diff).toMatchObject({ state: 'not_available_on_get', successor: 'M14d' });
    const recent = await ok('?since=1d');
    expect(recent.tasks.map((t: any) => t.id)).toContain(task.id);
    const future = await ok('?since=2999-01-01T00%3A00%3A00.000Z');
    expect(future.tasks.map((t: any) => t.id)).not.toContain(task.id);
    expect(future.categories.diff.state).toBe('not_available_on_get');
    expect((await call('?since=not-a-time')).status).toBe(400);
  });

  it('surfaces canonical hub_session links with live control state and lifecycle group', async () => {
    await seedCanonicalHosts();
    const sessionId = '33333333-3333-4333-8333-333333333333';
    await seedHubSession(sessionId, 'demo:assistant', 'host-a', 'running', 'native-hub-1');
    const { task } = await createTask();
    await intakeOk('/tasks/' + task.id + '/events', { key: 'hub-link-' + ++seq, expected_version: 1,
      event: { kind: 'link', hub_session_id: sessionId, provenance: source } });
    const data = await ok('');
    const row = data.tasks.find((t: any) => t.id === task.id);
    expect(row.hub_sessions.length).toBe(1);
    expect(row.hub_sessions[0]).toMatchObject({ hub_session_id: sessionId, linkage: 'hub_session', state: 'running', machine: 'host-a' });
    expect(row.hub_sessions[0].lifecycle.group).toBe('active');
    expect(row.hub_sessions[0].lifecycle.decision).toBeNull();
    expect(row.lifecycle_summary.by_group.active).toBe(1);
    expect(data.categories.tasks_sessions.source).toContain('session_control_sessions');
  });

  it('hides foreign hub sessions without leaking control state', async () => {
    await seedCanonicalHosts();
    const sessionId = '44444444-4444-4334-8334-444444444444';
    await seedHubSession(sessionId, 'other:assistant', 'host-a', 'running', 'native-foreign-1');
    const a = await seedAgent('demo:assistant-a');
    await grant(a, 'project', 'demo', 'read');
    const { task } = await createTask('demo', 'Synthetic task');
    await intakeOk('/tasks/' + task.id + '/events', { key: 'hub-link-f-' + ++seq, expected_version: 1,
      event: { kind: 'link', hub_session_id: sessionId, provenance: source } });
    const seen = await ok('', a.token);
    const row = seen.tasks.find((t: any) => t.id === task.id);
    expect(row.hub_sessions.length).toBe(1);
    expect(row.hub_sessions[0]).toMatchObject({ hub_session_id: sessionId, linkage: 'not_visible' });
    expect(JSON.stringify(row.hub_sessions[0])).not.toContain('running');
  });

  it('treats win35 stale checkins as expected offline, not an outage', async () => {
    await seedCanonicalHosts();
    await inDO(stub(), (h: any) => {
      h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('win35','win35','DESKTOP-FP4OP26','2026-01-01T00:00:00.000Z','2020-01-01T00:00:00.000Z',0)");
    });
    const data = await ok('');
    expect(data.coverage.missing_hosts).toContain('host-old');
    expect(data.coverage.missing_hosts).not.toContain('win35');
    expect(data.coverage.expected_offline).toContain('win35');
    expect(data.coverage.expected_offline_until).toBe('2026-10-31T15:23:00Z');
    expect(typeof data.coverage.expected_offline_reason).toBe('string');
  });

  it('expires the win35 expected-offline window after 2026-10-31T15:23:00Z', async () => {
    // Live-clock expiry is covered deterministically in test/status-window.test.ts
    // (pure status_offline unit tests, plain Node): after the window a stale
    // win35 must classify as an ordinary missing host, never expected forever.
    const { isExpectedOffline, expectedOfflineWindowActive, EXPECTED_OFFLINE_UNTIL_MS } =
      await import('../src/status_offline');
    expect(expectedOfflineWindowActive(EXPECTED_OFFLINE_UNTIL_MS)).toBe(true);
    expect(isExpectedOffline('win35', 'DESKTOP-FP4OP26', EXPECTED_OFFLINE_UNTIL_MS)).toBe(true);
    expect(expectedOfflineWindowActive(EXPECTED_OFFLINE_UNTIL_MS + 1)).toBe(false);
    expect(isExpectedOffline('win35', 'DESKTOP-FP4OP26', EXPECTED_OFFLINE_UNTIL_MS + 1)).toBe(false);
  });

  it('performs no writes and never exports secrets or private bodies', async () => {    await seedCanonicalHosts();
    const sentinel = 'SENTINEL_hubt_secret_value';
    const { task } = await createTask('demo', 'Synthetic title', 'Private body ' + sentinel + ' user_code=ABC-123 device_code_hash=deadbeef');
    await intakeOk('/tasks/' + task.id + '/events', { key: 'privaterep-' + ++seq, expected_version: 1,
      event: { kind: 'report', reference: 'reports/synthetic.md', source } });
    const before = await inDO(stub(), (h: any) => ({
      revision: h.sql.exec('SELECT revision FROM intake_meta WHERE id=1').toArray()[0].revision,
      tasks: h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n,
      sessions: h.sql.exec('SELECT COUNT(*) AS n FROM fleet_sessions').toArray()[0].n,
    }));
    const data = await ok('');
    const after = await inDO(stub(), (h: any) => ({
      revision: h.sql.exec('SELECT revision FROM intake_meta WHERE id=1').toArray()[0].revision,
      tasks: h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n,
      sessions: h.sql.exec('SELECT COUNT(*) AS n FROM fleet_sessions').toArray()[0].n,
    }));
    expect(after).toEqual(before);
    const dumped = JSON.stringify(data);
    expect(dumped).not.toContain(sentinel);
    expect(dumped).not.toContain('"token_hash"');
    expect(dumped).not.toContain('"user_code"');
    expect(dumped).not.toContain('"device_code_hash"');
    const row = data.tasks.find((t: any) => t.id === task.id);
    expect(JSON.stringify(row)).not.toContain('Private body');
  });
});

describe('M14b slice2 machine-authenticated host observations', () => {
  const HUB_SESSION = '55555555-5555-4335-8335-555555555555';
  const freshAt = () => new Date().toISOString();
  async function seedLinkedTask(machine: string, native: string) {
    await seedHubSession(HUB_SESSION, 'demo:assistant', machine, 'running', native);
    const { task } = await createTask();
    const linked = await intakeOk('/tasks/' + task.id + '/events', { key: 'hub-link-obs-' + ++seq, expected_version: 1,
      event: { kind: 'link', hub_session_id: HUB_SESSION, provenance: source } });
    void linked;
    await intakeOk('/tasks/' + task.id + '/events', { key: 'rep-obs-' + ++seq, expected_version: 2,
      event: { kind: 'report', reference: 'reports/synthetic.md', source } });
    return task;
  }

  it('accepts host observations and surfaces sanitized host-reported fields', async () => {
    const host = await seedHostMachine('host-obs', 'hubt_obs_fixture_token_1');
    const task = await seedLinkedTask('host-obs', 'native-obs-1');
    const before = await inDO(stub(), (h: any) => ({
      revision: h.sql.exec('SELECT revision FROM intake_meta WHERE id=1').toArray()[0].revision,
      tasks: h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n,
    }));
    const sess = await obsOk({ machine_id: 'host-obs', hub_session_id: HUB_SESSION, kind: 'session',
      observed_at: freshAt(), payload: { state: 'limit_hit', note: 'Weekly limit exhausted' } }, host.token);
    expect(sess).toMatchObject({ hub_session_id: HUB_SESSION, kind: 'session', host_reported: true, reporter: 'host:host-obs' });
    const rep = await obsOk({ machine_id: 'host-obs', hub_session_id: HUB_SESSION, kind: 'report',
      observed_at: freshAt(), payload: { headline: 'Synthetic\x1b[2J headline hubt_fixture_leaked_token_value', verdict: 'attention' } }, host.token);
    expect(rep.kind).toBe('report');
    const data = await ok('');
    const row = data.tasks.find((t: any) => t.id === task.id);
    const hub = row.hub_sessions.find((h: any) => h.hub_session_id === HUB_SESSION);
    expect(hub.host_observation).toMatchObject({ state: 'limit_hit', host_reported: true });
    expect(hub.host_observation.provenance.source).toBe('host_observation');
    expect(hub.report_headline.headline).toContain('Synthetic');
    expect(hub.report_headline.headline).not.toContain('\x1b');
    expect(hub.report_headline.headline).not.toContain('hubt_fixture_leaked_token_value');
    expect(row.reports[0].headline).toContain('Synthetic');
    expect(data.categories.reports.state).toBe('collected_host_reported');
    const after = await inDO(stub(), (h: any) => ({
      revision: h.sql.exec('SELECT revision FROM intake_meta WHERE id=1').toArray()[0].revision,
      tasks: h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n,
    }));
    expect(after).toEqual(before);
    expect(JSON.stringify(data)).not.toContain('hubt_fixture_leaked_token_value');
  });

  it('resolves native_id to the current canonical session and rejects ambiguity', async () => {
    const host = await seedHostMachine('host-native', 'hubt_obs_fixture_token_2');
    await seedLinkedTask('host-native', 'native-resolve-1');
    const via = await obsOk({ machine_id: 'host-native', native_id: 'native-resolve-1', kind: 'session',
      observed_at: freshAt(), payload: { state: 'working' } }, host.token);
    expect(via.hub_session_id).toBe(HUB_SESSION);
    const missing = await obsCall({ machine_id: 'host-native', native_id: 'no-such-native', kind: 'session',
      observed_at: freshAt(), payload: { state: 'working' } }, host.token);
    expect(missing.status).toBe(404);
    // Second canonical row bound to the same native identity: ambiguous, fail closed.
    await inDO(stub(), (h: any) => {
      const now = new Date().toISOString();
      h.sql.exec('INSERT OR REPLACE INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        '66666666-6666-4336-8336-666666666666', 'demo:assistant', 'key-dup', '{}', null, 'host-native', 'opencode', 'headless', 'running',
        JSON.stringify({ native_id: 'native-resolve-1' }), now, now);
    });
    const ambiguous = await obsCall({ machine_id: 'host-native', native_id: 'native-resolve-1', kind: 'session',
      observed_at: freshAt(), payload: { state: 'working' } }, host.token);
    expect(ambiguous.status).toBe(409);
  });

  it('resolves a unique native binding beyond 200 machine sessions while retaining duplicate and machine checks', async () => {
    const host = await seedHostMachine('host-many', 'hubt_obs_fixture_many');
    await seedLinkedTask('host-many', 'native-many-target');
    await inDO(stub(), (h: any) => {
      const now = new Date().toISOString();
      const insert = (id: string, machine: string, binding: string | null) => h.sql.exec(
        'INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        id, 'demo:assistant', 'many-' + id, '{}', null, machine, 'opencode', 'headless', 'stopped', binding, now, now);
      for (let i = 0; i < 240; i++) {
        insert('many-' + i, 'host-many', i === 0 ? '{malformed' : i === 1 ? null : JSON.stringify({ native_id: 'unrelated-' + i }));
      }
      insert('foreign-many', 'another-machine', JSON.stringify({ native_id: 'native-many-target' }));
      insert('array-many', 'host-many', JSON.stringify({ native_id: ['array-native'] }));
      insert('number-many', 'host-many', JSON.stringify({ native_id: 12345 }));
      // Place the only valid target after the unrelated history in row order.
      h.sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',
        JSON.stringify({ native_id: 'old-many-target' }), HUB_SESSION);
      insert('77777777-7777-4777-8777-777777777777', 'host-many', JSON.stringify({ native_id: 'native-many-target' }));
    });
    const body = { machine_id: 'host-many', native_id: 'native-many-target', kind: 'session',
      observed_at: freshAt(), payload: { state: 'working' } };
    expect((await obsOk(body, host.token)).hub_session_id).toBe('77777777-7777-4777-8777-777777777777');
    expect((await obsCall({ ...body, native_id: 'absent-many' }, host.token)).status).toBe(404);
    expect((await obsCall({ ...body, native_id: '["array-native"]' }, host.token)).status).toBe(404);
    expect((await obsCall({ ...body, native_id: '12345' }, host.token)).status).toBe(404);
    await inDO(stub(), (h: any) => {
      h.sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',
        JSON.stringify({ native_id: 'native-many-target' }), HUB_SESSION);
    });
    expect((await obsCall(body, host.token)).status).toBe(409);
  });

  it('rejects forged machines, unknown sessions, bad payloads and non-host callers', async () => {
    const host = await seedHostMachine('host-guard', 'hubt_obs_fixture_token_3');
    await seedHostMachine('host-other', 'hubt_obs_fixture_token_4');
    await seedLinkedTask('host-guard', 'native-guard-1');
    const good = { machine_id: 'host-guard', hub_session_id: HUB_SESSION, kind: 'session', observed_at: freshAt(), payload: { state: 'working' } };
    // JSON machine claim for another machine never authorizes this credential.
    expect((await obsCall({ ...good, machine_id: 'host-other' }, host.token)).status).toBe(403);
    // Unknown canonical session.
    expect((await obsCall({ ...good, hub_session_id: '77777777-7777-4337-8337-777777777777' }, host.token)).status).toBe(404);
    // Unknown fields, bad enums and bad timestamps fail closed.
    expect((await obsCall({ ...good, payload: { state: 'working', owner: 'mallory' } }, host.token)).status).toBe(400);
    expect((await obsCall({ ...good, payload: { state: 'hacked' } }, host.token)).status).toBe(400);
    expect((await obsCall({ ...good, observed_at: '2030-01-01T00:00:00.000Z' }, host.token)).status).toBe(400);
    expect((await obsCall({ ...good, observed_at: 'not-a-time' }, host.token)).status).toBe(400);
    expect((await obsCall({ ...good, kind: 'exec' }, host.token)).status).toBe(400);
    expect((await obsCall({ machine_id: 'host-guard', kind: 'session', observed_at: freshAt(), payload: { state: 'working' } }, host.token)).status).toBe(400);
    // Agent credentials carry no observation authority.
    const agent = await seedAgent('demo:assistant-obs');
    await grant(agent);
    expect((await obsCall(good, agent.token)).status).toBe(403);
    expect((await obsCall(good, 'hubt_bogus')).status).toBe(401);
  });

  it('fails closed while the observation gate is absent', async () => {
    const host = await seedHostMachine('host-gate', 'hubt_obs_fixture_token_5');
    await seedLinkedTask('host-gate', 'native-gate-1');
    const good = { machine_id: 'host-gate', hub_session_id: HUB_SESSION, kind: 'session', observed_at: freshAt(), payload: { state: 'working' } };
    await inDO(stub(), (h: any) => { h.statusService.setObservationGate(undefined); });
    try {
      const res = await obsCall(good, host.token);
      expect(res.status).toBe(503);
      expect(((await res.json()) as any).error).toBe('observation_auth_unavailable');
    } finally {
      await inDO(stub(), (h: any) => {
        h.statusService.setObservationGate((m: unknown, c: any) => h.sessionControlService.hostObservationAllowed(m, c));
      });
    }
    expect((await obsCall(good, host.token)).status).toBe(200);
  });

  it('accepts admin operator records with CSRF and rejects without', async () => {
    await seedHostMachine('host-admin', 'hubt_obs_fixture_token_6');
    await seedLinkedTask('host-admin', 'native-admin-1');
    const good = { machine_id: 'host-admin', hub_session_id: HUB_SESSION, kind: 'session', observed_at: freshAt(), payload: { state: 'waiting' } };
    const noCsrf = await obsCall(good, undefined, 'wrong');
    expect(noCsrf.status).toBe(403);
    const recorded = await obsOk(good);
    expect(recorded.reporter).toContain('operator:');
  });

  it('surfaces exact-sha probe evidence on sessions and commits without inference', async () => {
    const host = await seedHostMachine('host-probe', 'hubt_obs_fixture_token_7');
    const sha = 'a'.repeat(40);
    await seedHubSession(HUB_SESSION, 'demo:assistant', 'host-probe', 'running', 'native-probe-1');
    const { task } = await createTask();
    await intakeOk('/tasks/' + task.id + '/events', { key: 'hub-link-probe-' + ++seq, expected_version: 1,
      event: { kind: 'link', hub_session_id: HUB_SESSION, provenance: source } });
    await intakeOk('/tasks/' + task.id + '/events', { key: 'commit-probe-' + ++seq, expected_version: 2,
      event: { kind: 'commit', reference: sha, source } });
    await intakeOk('/tasks/' + task.id + '/events', { key: 'commit-other-' + ++seq, expected_version: 3,
      event: { kind: 'commit', reference: 'b'.repeat(40), source } });
    await obsOk({ machine_id: 'host-probe', hub_session_id: HUB_SESSION, kind: 'probe_git',
      observed_at: freshAt(), payload: { sha, ancestry: 'merged_in_main', ref: 'origin/main' } }, host.token);
    await obsOk({ machine_id: 'host-probe', hub_session_id: HUB_SESSION, kind: 'probe_ci',
      observed_at: freshAt(), payload: { state: 'passing', run: 'https://ci.test/r/1', sha } }, host.token);
    await obsOk({ machine_id: 'host-probe', hub_session_id: HUB_SESSION, kind: 'probe_deploy',
      observed_at: freshAt(), payload: { state: 'not_deployed', sha } }, host.token);
    const data = await ok('');
    const row = data.tasks.find((t: any) => t.id === task.id);
    const hub = row.hub_sessions.find((h: any) => h.hub_session_id === HUB_SESSION);
    expect(hub.probe_evidence.git).toMatchObject({ sha, ancestry: 'merged_in_main' });
    expect(hub.probe_evidence.ci).toMatchObject({ state: 'passing' });
    expect(hub.probe_evidence.deploy).toMatchObject({ state: 'not_deployed' });
    const matched = row.commits.find((c: any) => c.reference === sha);
    expect(matched.git_ancestry).toMatchObject({ sha, ancestry: 'merged_in_main' });
    expect(matched.ci.state).toBe('passing');
    expect(matched.deploy.state).toBe('not_deployed');
    expect(matched.ci_state).toBe('passing');
    expect(matched.deploy_state).toBe('not_deployed');
    // A commit without probe evidence stays honestly unknown: no borrowing.
    const other = row.commits.find((c: any) => c.reference === 'b'.repeat(40));
    expect(other.deploy_state).toBe('unknown');
    expect(other.ci_state).toBe('unknown');
    expect(other.git_ancestry).toBeUndefined();
    expect(data.categories.commits_deploys_ci.state).toBe('collected_probe_evidence');
  });
});

describe('M14d caller-scoped durable baseline with complete POST summary', () => {
  async function postCall(path: string, payload: any = {}, bearer?: string, csrf = 'csrf') {
    return SELF.fetch('https://hub.test/api/status' + path, { method: 'POST',
      headers: { ...(bearer ? { Authorization: 'Bearer ' + bearer } : { Cookie: cookie, 'x-csrf-token': csrf }), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) });
  }
  async function postOk(path: string, payload: any = {}, bearer?: string, csrf = 'csrf'): Promise<any> {
    const res = await postCall(path, payload, bearer, csrf);
    const data = await res.json();
    expect(res.status, JSON.stringify(data)).toBe(200);
    return data;
  }
  async function revisionAndTasks() {
    return inDO(stub(), (h: any) => ({
      revision: h.sql.exec('SELECT revision FROM intake_meta WHERE id=1').toArray()[0].revision,
      tasks: h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n,
    }));
  }

  it('returns ALL authorized tasks beyond the GET page in one projecting+advancing call', async () => {
    const ids: string[] = [];
    for (let i = 0; i < 27; i++) {
      const { task } = await createTask('demo', 'Bulk task ' + i);
      ids.push(task.id);
    }
    const page = await ok('');
    expect(page.tasks.length).toBe(25);
    expect(page.pagination.next).not.toBeNull();
    expect(page.categories.diff.state).toBe('not_available_on_get');
    const before = await revisionAndTasks();
    // ONE client request: complete summary + first baseline together.
    const first = await postOk('', {});
    expect(first.tasks.length).toBe(27);
    expect(first.pagination.next).toBeNull();
    expect(first.summary).toMatchObject({ total: 27, truncated: false });
    expect(first.summary.by_status.open).toBe(27);
    expect(first.categories.diff).toMatchObject({ state: 'first_baseline', baseline_advanced: true });
    expect(first.categories.diff.added.length).toBe(27);
    expect(await revisionAndTasks()).toEqual(before);
    // Baselines store compact digests only, never task histories or bodies.
    const stored = await inDO(stub(), (h: any) =>
      h.sql.exec('SELECT snapshot_json FROM status_baselines').toArray().map((r: any) => r.snapshot_json).join('\n'));
    expect(stored).not.toContain('"history"');
    expect(stored).not.toContain('Bulk task');
    // Second identical call: available diff with zero changes.
    const second = await postOk('', {});
    expect(second.tasks.length).toBe(27);
    expect(second.categories.diff).toMatchObject({ state: 'available', baseline_advanced: true });
    expect(second.categories.diff.added).toEqual([]);
    expect(second.categories.diff.removed).toEqual([]);
    expect(second.categories.diff.changed).toEqual([]);
    // New and changed work surfaces as a short id-only diff.
    const { task: added } = await createTask('demo', 'Bulk newcomer');
    await intakeOk('/tasks/' + ids[0] + '/events', { key: 'bump-' + ++seq, expected_version: 1,
      event: { kind: 'report', reference: 'reports/bump.md', source } });
    const third = await postOk('', {});
    expect(third.tasks.length).toBe(28);
    expect(third.categories.diff.added).toEqual([added.id]);
    const changed = third.categories.diff.changed.find((c: any) => c.id === ids[0]);
    expect(changed.fields).toContain('version');
    expect(JSON.stringify(third.categories.diff)).not.toContain('bump.md');
  });

  it('isolates baselines and task visibility across callers', async () => {
    const a = await seedAgent('demo:assistant-a');
    const b = await seedAgent('other:assistant-b');
    await grant(a, 'project', 'demo', 'read');
    await grant(b, 'project', 'other', 'read');
    await createTask('demo', 'Demo task');
    await createTask('other', 'Other task');
    const seenA = await postOk('', {}, a.token);
    expect(seenA.tasks.map((t: any) => t.project)).toEqual(['demo']);
    expect(seenA.categories.diff.state).toBe('first_baseline');
    const seenB = await postOk('', {}, b.token);
    expect(seenB.tasks.map((t: any) => t.project)).toEqual(['other']);
    expect(seenB.categories.diff.state).toBe('first_baseline');
    // A re-post with no changes: empty diff on A's own baseline only.
    const againA = await postOk('', {}, a.token);
    expect(againA.categories.diff).toMatchObject({ state: 'available' });
    expect(againA.categories.diff.added).toEqual([]);
    // Operator sees everything on an independent baseline.
    const op = await postOk('', {});
    expect(op.tasks.length).toBe(2);
    expect(op.categories.diff.state).toBe('first_baseline');
  });

  it('rejects pagination, bad bodies and missing CSRF on POST', async () => {
    expect((await postCall('?limit=5')).status).toBe(400);
    expect((await postCall('?after=task-1')).status).toBe(400);
    expect((await postCall('', { unexpected: true })).status).toBe(400);
    expect((await postCall('', {}, undefined, 'wrong')).status).toBe(403);
    // GET never advances a baseline: a later first POST still reports first_baseline.
    await createTask();
    await ok('');
    await ok('');
    const first = await postOk('', {});
    expect(first.categories.diff.state).toBe('first_baseline');
    // Malformed JSON is a clean 400, never a baseline write.
    const bad = await SELF.fetch('https://hub.test/api/status', { method: 'POST',
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf', 'Content-Type': 'application/json' }, body: '{oops' });
    expect(bad.status).toBe(400);
  });

  it('carries available handoff context without history bodies', async () => {
    const { task } = await createTask('demo', 'Context task');
    await intakeOk('/tasks/' + task.id, { key: 'ctx-update-' + ++seq, expected_version: 1,
      current_state: 'Synthetic state', next_step: 'Synthetic step', owner: 'demo:assistant' });
    const data = await postOk('', {});
    const row = data.tasks.find((t: any) => t.id === task.id);
    expect(row.current_state).toContain('Synthetic state');
    expect(row.next_step).toContain('Synthetic step');
    expect(row.owner).toBe('demo:assistant');
    expect(row.history).toBeUndefined();
  });
});
