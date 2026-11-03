import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { sha256 } from '../src/auth/crypto';
import { formatNativeActor } from '../src/work_claim';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake 6022786b: a renewable worker credential (the durable agent-side Hub
// auth) can run `hub claim`/`hub claims`/`hub release` on its own intake's
// project as its own worker actor, and nothing beyond that.
const uuid = () => crypto.randomUUID();
async function setup(state = 'starting') {
  const stub = (env as unknown as Env).HUB.get((env as unknown as Env).HUB.idFromName('worker-claims-' + uuid()));
  const launch = uuid(), task = uuid(), session = uuid();
  const hostToken = 'hubt_host_fixture_' + uuid(), now = new Date().toISOString();
  await runInDurableObject<Hub, any>(stub, async (h: any) => {
    h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('worker-host','host','host',?)", now);
    h.sql.exec("INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at) VALUES('worker-host-cred','host',?,'fixture-refresh','host-family','worker-host','worker-host','[\"host\"]','fixture',?,?)",
      await sha256(hostToken), now, new Date(Date.now() + 3600000).toISOString());
    h.sql.exec("UPDATE machines SET credential_id='worker-host-cred' WHERE id='worker-host'");
    h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', task, JSON.stringify({ id: task, project: 'agent-hub', status: 'open', version: 1,
      created_at: Date.now(), updated_at: Date.now(), title: 'fixture', scope: null, history: [], acceptance_criteria: 'fixture' }));
    h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      VALUES(?,'tester','human','s',?,'{}','worker-host','opencode','shared-muse','headless','/repo','task','starting',?,?)`, launch, 'key-' + launch, now, now);
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at)
      VALUES(?,'tester',?,?,?,'worker-host','opencode','headless','starting',?,?)`, session, 'session-' + session, JSON.stringify({ intake_task_id: task, workspace: '/repo' }), launch, now, now);
  });
  const call = (path: string, body: any, bearer: string, method = body === undefined ? 'GET' : 'POST') => stub.fetch('https://hub.test' + path, {
    method, headers: { Authorization: 'Bearer ' + bearer, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const issued = await call('/api/fleet/launches/' + launch + '/worker-credential', {}, hostToken);
  expect(issued.status, await issued.clone().text()).toBe(200);
  const worker: any = await issued.json();
  if (state !== 'starting') await runInDurableObject<Hub, any>(stub, (h: any) => h.sql.exec('UPDATE session_control_sessions SET state=? WHERE id=?', state, session));
  const actor = 'hub-workers:worker-' + launch;
  const api = (path: string, body?: any, method?: string) => call(path, body, worker.access_token, method);
  return { stub, launch, task, session, worker, actor, api, call };
}

describe('worker credential work claims', () => {
  it('creates, renews, lists and releases its own claim on the intake project', async () => {
    const { actor, api, launch } = await setup();
    const created = await api('/api/bus/claims', { project: 'agent-hub', scope: 'src/./auth/', holder: actor, ttl_ms: 60000 });
    expect(created.status, await created.clone().text()).toBe(200);
    const { id } = await created.json() as any;
    // The bare tag is the same holder; renewing returns the same claim.
    const renewed = await api('/api/bus/claims', { project: 'agent-hub', scope: 'src/auth', holder: 'worker-' + launch });
    expect(await renewed.json()).toEqual({ id });
    const listed: any[] = await (await api('/api/bus/claims')).json() as any;
    expect(listed).toEqual([expect.objectContaining({ id, project: 'agent-hub', scope: 'src/auth', holder: actor, released_at: null })]);
    expect((await api('/api/bus/claims?project=agent-hub&active=false')).status).toBe(200);
    expect((await api('/api/bus/claims/' + id + '/release', {})).status).toBe(200);
    expect(await (await api('/api/bus/claims')).json()).toEqual([]);
    // Release by project:scope; missing claims fail closed for worker credentials.
    const again: any = await (await api('/api/bus/claims', { project: 'agent-hub', scope: 'docs', holder: actor })).json();
    expect((await api('/api/bus/claims/' + encodeURIComponent('agent-hub:docs') + '/release', undefined, 'POST')).status).toBe(200);
    expect(await (await api('/api/bus/claims?active=false')).json()).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: again.id, released_at: expect.any(Number) })]));
    expect((await api('/api/bus/claims/missing-claim/release', {})).status).toBe(403);
  });

  it('preserves exact-incarnation release for a host-attested native claim', async () => {
    const { stub, session, api } = await setup();
    const tag = 'worker-native-test';
    const holder = formatNativeActor('worker-host', tag, session);
    let owned = '', otherIncarnation = '';
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      h.sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',
        JSON.stringify({ native_id: session, tag, workspace: '/repo' }), session);
      owned = uuid();
      otherIncarnation = uuid();
      const now = Date.now();
      const insert = "INSERT INTO claims(id,project,scope,holder,created_at,expires_at,worktree,claim_source,native_claim_id,native_session_id) VALUES(?,?,?,?,?,?,?,'native','fixture-native',?)";
      h.sql.exec(insert, owned, 'agent-hub', 'native/owned', holder, now, now + 60000, '/repo', session);
      h.sql.exec(insert, otherIncarnation, 'agent-hub', 'native/other-incarnation',
        formatNativeActor('worker-host', tag, uuid()), now, now + 60000, '/repo', session);
    });

    expect((await api('/api/bus/claims/' + otherIncarnation + '/release', {})).status).toBe(403);
    expect((await api('/api/bus/claims/' + owned + '/release', {})).status).toBe(200);
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      expect(h.sql.exec('SELECT released_at FROM claims WHERE id=?', owned).one().released_at).not.toBeNull();
      expect(h.sql.exec('SELECT released_at FROM claims WHERE id=?', otherIncarnation).one().released_at).toBeNull();
    });
  });

  it('cannot claim another project or holder, nor release or list foreign claims', async () => {
    const { stub, actor, api } = await setup();
    let foreign = '', other = '';
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      const admin = { isAdmin: true, actor: 'human:tester', kind: 'human' };
      foreign = h.busService.claim(admin, 'agent-hub', 'src/hub.ts', 'agent-hub:someone-else', 60000).id;
      other = h.busService.claim(admin, 'dtc-website', 'gha', actor, 60000).id;
    });
    for (const body of [{ project: 'dtc-website', scope: 'x', holder: actor }, { project: 'agent-hub', scope: 'x', holder: 'agent-hub:someone-else' },
      { project: 'agent-hub', scope: 'x', holder: 'role:root' }, { project: 'agent-hub', scope: 'x', holder: actor, lease_epoch: 1 }]) {
      const res = await api('/api/bus/claims', body);
      expect(res.status).toBe(403);
      expect(await res.json()).toEqual({ error: 'worker_claim_forbidden' });
    }
    expect((await api('/api/bus/claims', { project: 'agent-hub', scope: 'src', holder: actor })).status).toBe(409);
    expect((await api('/api/bus/claims', { project: 'agent-hub', scope: 'x', holder: actor, extra: 1 })).status).toBe(400);
    expect((await api('/api/bus/claims/' + foreign + '/release', {})).status).toBe(403);
    expect((await api('/api/bus/claims/' + encodeURIComponent('agent-hub:src/hub.ts') + '/release', {})).status).toBe(403);
    expect((await api('/api/bus/claims/' + other + '/release', {})).status).toBe(403);
    expect((await api('/api/bus/claims/' + encodeURIComponent('dtc-website:gha') + '/release', {})).status).toBe(403);
    expect((await api('/api/bus/claims?project=dtc-website')).status).toBe(403);
    const listed: any[] = await (await api('/api/bus/claims')).json() as any;
    expect(listed.map(c => c.id)).toEqual([foreign]);
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      expect(h.sql.exec('SELECT COUNT(*) AS n FROM claims WHERE released_at IS NULL').one().n).toBe(2);
    });
    // Other bus routes stay outside the worker capability.
    expect((await api('/api/bus/activity')).status).toBe(403);
    expect((await api('/api/bus/inbox')).status).toBe(403);
  });

  it('bounds claim lifetime by the worker access window', async () => {
    const { stub, actor, api } = await setup();
    const res: any = await (await api('/api/bus/claims', { project: 'agent-hub', scope: 'long', holder: actor, ttl_ms: 29 * 86400000 })).json();
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      const row: any = h.sql.exec('SELECT created_at,expires_at FROM claims WHERE id=?', res.id).one();
      expect(row.expires_at - row.created_at).toBeLessThanOrEqual(24 * 3600000);
    });
  });

  it.each(['exited', 'stopped'])('fails closed once the worker session is %s', async state => {
    const { actor, api } = await setup(state);
    expect((await api('/api/bus/claims')).status).toBe(403);
    expect((await api('/api/bus/claims', { project: 'agent-hub', scope: 'x', holder: actor })).status).toBe(403);
  });

  it('fails closed for a closed intake, expired worker access or revoked credential', async () => {
    for (const change of ["UPDATE intake_tasks SET data=json_set(data,'$.status','done')", 'UPDATE worker_access SET expires_at=1',
      "UPDATE credentials SET revoked_at='2026-01-01T00:00:00Z' WHERE kind='agent'"]) {
      const { stub, actor, api } = await setup();
      const created: any = await (await api('/api/bus/claims', { project: 'agent-hub', scope: 'x', holder: actor })).json();
      await runInDurableObject<Hub, any>(stub, (h: any) => h.sql.exec(change));
      expect([401, 403]).toContain((await api('/api/bus/claims')).status);
      expect([401, 403]).toContain((await api('/api/bus/claims/' + created.id + '/release', {})).status);
    }
  });
});
