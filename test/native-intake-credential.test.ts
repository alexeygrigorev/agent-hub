import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { sha256 } from '../src/auth/crypto';
import { revokeEndedNativeWorkerCredentials } from '../src/worker-access';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake-scoped credential of an observed native session (no Hub launch), e.g.
// an aplexer drain brief: minted for a host-reported running session that has a
// server-verified intake link, grant project-scoped reads and own-intake writes,
// revoked with the session.
const uuid = () => crypto.randomUUID();

async function setup(fn: (x: any) => Promise<void>) {
  const stub = (env as unknown as Env).HUB.get((env as unknown as Env).HUB.idFromName('native-' + uuid()));
  const task = uuid(), sibling = uuid(), foreign = uuid(), session = uuid(), native = uuid();
  const token = 'hubt_host_fixture_' + uuid(), hash = await sha256(token), now = new Date().toISOString();
  const otherToken = 'hubt_host_fixture_' + uuid(), otherHash = await sha256(otherToken);
  await runInDurableObject<Hub, any>(stub, async (h: any) => {
    for (const [m, cred, tok] of [['native-host', 'native-host-cred', hash], ['other-host', 'other-host-cred', otherHash]]) {
      h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES(?,?,?,?)", m, m, m, now);
      h.sql.exec("INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at) VALUES(?,'host',?,?,?,?,?,'[\"host\"]','fixture',?,?)",
        cred, tok, 'refresh-' + cred, 'family-' + cred, m, m, now, new Date(Date.now() + 3600000).toISOString());
      h.sql.exec('UPDATE machines SET credential_id=? WHERE id=?', cred, m);
    }
    for (const [t, project] of [[task, 'agent-hub'], [sibling, 'agent-hub'], [foreign, 'other-project']] as const) {
      h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', t, JSON.stringify({ id: t, project, status: 'open', version: 1, created_at: Date.now(), updated_at: Date.now(), title: 'fixture', scope: null, history: [{ id: uuid(), kind: 'intake', text: 'fixture', authority: 'untrusted_task_data', actor: 'tester', captured_at: Date.now() }], acceptance_criteria: 'x' }));
    }
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES(?,'tester',?,'{}','native-host','shell','interactive','running',?,?,?)`,
      session, 'session-' + session, JSON.stringify({ native_id: native, tag: 'drain-x', workspace: '/home/u/git/drain-x' }), now, now);
  });
  const call = (path: string, body?: any, bearer = token, method?: string) => stub.fetch('https://hub.test' + path, { method: method ?? (body === undefined ? 'GET' : 'POST'), headers: { Authorization: 'Bearer ' + bearer, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const link = (taskId: string, key: string) => runInDurableObject<Hub, any>(stub, (h: any) => { h.sql.exec('INSERT INTO intake_session_links(session_key,task_id,authority) VALUES(?,?,?)', key, taskId, 'host_census_verified'); });
  await fn({ stub, task, sibling, foreign, session, native, call, link, otherToken });
}

describe('native session intake credential', () => {
  it('requires a verified link, then scopes reads to its project and writes to its own intake', () => setup(async ({ stub, task, sibling, foreign, session, native, call, link, otherToken }: any) => {
    // No link: nothing listed, nothing minted. A START note or tag is not authority.
    expect(await (await call('/api/session-intake/credentials')).json()).toEqual({ sessions: [] });
    const refused = await call('/api/sessions/' + session + '/intake-credential', {});
    expect(refused.status).toBe(409);
    await link(task, native);
    const listed: any = await (await call('/api/session-intake/credentials')).json();
    expect(listed.sessions).toEqual([{ session_id: session, native_id: native, workspace: '/home/u/git/drain-x', intake_task_id: task }]);
    // Another machine's host can neither list nor mint it.
    expect(await (await call('/api/session-intake/credentials', undefined, otherToken)).json()).toEqual({ sessions: [] });
    expect((await call('/api/sessions/' + session + '/intake-credential', {}, otherToken)).status).toBe(404);
    // Humans/agents cannot mint.
    expect((await call('/api/sessions/' + session + '/intake-credential', {}, 'hubt_nope')).status).toBeGreaterThanOrEqual(401);

    const res = await call('/api/sessions/' + session + '/intake-credential', {});
    expect(res.status, await res.clone().text()).toBe(200);
    const cred: any = await res.json();
    expect(cred.access_token).toMatch(/^hubt_/);
    expect(cred.intake_task_id).toBe(task);
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      const row = h.sql.exec("SELECT scopes FROM credentials WHERE kind='agent' AND revoked_at IS NULL").toArray()[0];
      expect(JSON.parse(row.scopes)).toEqual(['bus:read', 'worker:own-intake']);
    });
    // Minted: no longer listed.
    expect(await (await call('/api/session-intake/credentials')).json()).toEqual({ sessions: [] });

    const api = (p: string, b?: any, tok = cred.access_token) => call(p, b, tok);
    const targetNative = uuid(), targetSession = targetNative;
    const linkAt = new Date().toISOString();
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
        VALUES(?,'unknown',? ,?,'native-host','shell','interactive','running',?,?,?)`, targetSession, 'target-' + targetSession,
        JSON.stringify({ provenance:'observed_native', observation_only:true, workspace:'/home/u/git/drain-x' }),
        JSON.stringify({ native_id:targetNative, tag:'another-drain', workspace:'/home/u/git/drain-x', provenance:'observed_native' }), linkAt, linkAt);
    });
    const linked = await api('/api/sessions/' + targetNative.slice(0, 8) + '/link', { key: 'worker-link-' + uuid(), intake_task_id: task });
    expect(linked.status, await linked.clone().text()).toBe(200);
    expect((await linked.json() as any).link.source).toBe('scoped_worker');
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      expect(JSON.parse(h.sql.exec('SELECT request_json FROM session_control_sessions WHERE id=?', targetSession).one().request_json).intake_task_id).toBe(task);
    });
    // The scoped credential cannot link a different intake or a same-host session from another workspace.
    expect((await api('/api/sessions/' + targetSession + '/link', { key: 'wrong-task-' + uuid(), intake_task_id: sibling })).status).toBe(403);
    const foreignSession = uuid();
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
        VALUES(?,'unknown',? ,?,'native-host','shell','interactive','running',?,?,?)`, foreignSession, 'foreign-' + foreignSession,
        JSON.stringify({ provenance:'observed_native', observation_only:true, workspace:'/home/u/git/other-repo' }),
        JSON.stringify({ native_id:uuid(), tag:'foreign-drain', workspace:'/home/u/git/other-repo', provenance:'observed_native' }), linkAt, linkAt);
    });
    expect((await api('/api/sessions/' + foreignSession + '/link', { key: 'wrong-workspace-' + uuid(), intake_task_id: task })).status).toBe(403);
    // A host credential without the intake-scoped worker capability still cannot link it.
    expect((await call('/api/sessions/' + foreignSession + '/link', { key: 'host-link-' + uuid(), intake_task_id: task })).status).toBe(403);
    // Own intake: read and append a DONE-style note.
    const shown = await api('/api/intake/tasks/' + task);
    expect(shown.status, await shown.clone().text()).toBe(200);
    const body: any = await shown.json();
    const version = body.task?.version ?? body.snapshot_version ?? 1;
    const note = await api('/api/intake/tasks/' + task + '/events', { key: uuid(), expected_version: version, event: { kind: 'note', text: 'DONE proof' } });
    expect(note.status, await note.clone().text()).toBe(200);
    // Same-project siblings are readable, but not writable by this credential.
    const siblingRead = await api('/api/intake/tasks/' + sibling);
    expect(siblingRead.status).toBe(200);
    const siblingWrite = await api('/api/intake/tasks/' + sibling + '/events', { key: uuid(), expected_version: 1, event: { kind: 'note', text: 'x' } });
    expect(siblingWrite.status).toBe(403);
    // A different project is outside the capability.
    const cross = await api('/api/intake/tasks/' + foreign);
    expect(cross.status).toBe(403);
    expect(await cross.json()).toEqual({ error: 'cross_intake_forbidden' });
    const crossWrite = await api('/api/intake/tasks/' + foreign + '/events', { key: uuid(), expected_version: 1, event: { kind: 'note', text: 'x' } });
    expect(crossWrite.status).toBe(403);
    // Non-note kinds (e.g. decisions) stay forbidden on the own intake.
    const decision = await api('/api/intake/tasks/' + task + '/events', { key: uuid(), expected_version: version + 1, event: { kind: 'decision', text: 'no' } });
    expect([400, 403]).toContain(decision.status);
    // Admin/operator surfaces are out of scope.
    for (const path of ['/api/fleet', '/api/credentials', '/api/machines', '/api/session-intake/credentials', '/api/session-messaging/credentials', '/api/engines', '/api/quota']) {
      expect([401, 403, 404], path).toContain((await api(path)).status);
    }
    expect((await api('/api/sessions/' + session + '/intake-credential', {})).status).toBeGreaterThanOrEqual(403);
    expect((await api('/api/intake/tasks')).status).toBe(403); // listing every intake is not a worker capability

    // Refresh keeps the family alive while the session runs...
    const refresh = (t: string) => stub.fetch('https://hub.test/api/token/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: t }) });
    const refreshed = await refresh(cred.refresh_token);
    expect(refreshed.status, await refreshed.clone().text()).toBe(200);
    const next: any = await refreshed.json();
    expect((await api('/api/intake/tasks/' + task, undefined, next.access_token)).status).toBe(200);

    // ...and dies with it: stopped session => access and refresh fail.
    // Production stop paths bump updated_at; the watermarked sweep keys on it.
    await runInDurableObject<Hub, any>(stub, (h: any) => { h.sql.exec("UPDATE session_control_sessions SET state='stopped', updated_at=? WHERE id=?", new Date().toISOString(), session); });
    expect((await api('/api/intake/tasks/' + task, undefined, next.access_token)).status).toBe(403);
    expect((await refresh(next.refresh_token)).status).toBe(400);
    // The sweep revokes the family explicitly.
    await call('/api/session-intake/credentials');
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      expect(h.sql.exec("SELECT count(*) AS n FROM credentials WHERE kind='agent' AND revoked_at IS NULL").one().n).toBe(0);
    });
  }));

  it('does not mint for a closed intake and a restarted session gets a fresh family', () => setup(async ({ stub, task, session, native, call, link }: any) => {
    await link(task, native);
    await runInDurableObject<Hub, any>(stub, (h: any) => {
      const t = JSON.parse(h.sql.exec('SELECT data FROM intake_tasks WHERE id=?', task).one().data);
      h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify({ ...t, status: 'done' }), task);
    });
    expect((await call('/api/sessions/' + session + '/intake-credential', {})).status).toBe(409);
    expect(await (await call('/api/session-intake/credentials')).json()).toEqual({ sessions: [] });
  }));
});

