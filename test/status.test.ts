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
  });
});

describe('M14a authorized one-call status projection', () => {
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
    expect(data.categories.diff.state).toBe('not_available');
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
    expect(all.categories.diff).toMatchObject({ state: 'not_available', successor: 'M14d' });
    const recent = await ok('?since=1d');
    expect(recent.tasks.map((t: any) => t.id)).toContain(task.id);
    const future = await ok('?since=2999-01-01T00%3A00%3A00.000Z');
    expect(future.tasks.map((t: any) => t.id)).not.toContain(task.id);
    expect(future.categories.diff.state).toBe('not_available');
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
  });

  it('performs no writes and never exports secrets or private bodies', async () => {
    await seedCanonicalHosts();
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
