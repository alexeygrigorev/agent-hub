import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const source = { type: 'unknown', reference: null, timestamp: null };
let cookie: string;
let seq = 0;
async function call(path: string, payload?: any, extra: any = {}) {
  return SELF.fetch('https://hub.test/api/intake' + path, { method: payload ? 'POST' : 'GET',
    headers: { Cookie: cookie, 'x-csrf-token': 'csrf', ...extra },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
}
async function ok(path: string, payload?: any): Promise<any> {
  const res = await call(path, payload); const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200); return data;
}
async function sessionsCall(payload: any): Promise<{ status: number; data: any }> {
  const res = await SELF.fetch('https://hub.test/api/sessions', { method: 'POST',
    headers: { Cookie: cookie, 'x-csrf-token': 'csrf' }, body: JSON.stringify(payload) });
  return { status: res.status, data: await res.json() as any };
}
async function sessionCount(): Promise<number> {
  const res = await SELF.fetch('https://hub.test/api/sessions?limit=100',
    { headers: { Cookie: cookie, 'x-csrf-token': 'csrf' } });
  return ((await res.json()) as any).sessions.length;
}
const create = (keySuffix = '', extra: any = {}) =>
  ok('/tasks', { key: 'ac-create-' + ++seq + keySuffix, project: 'demo', title: 'Synthetic task',
    text: 'Synthetic request', source, ...extra });
const assignPayload = (version: number, keySuffix = '') => ({ key: 'ac-assign-' + ++seq + keySuffix,
  expected_version: version, session: { workspace: 'demo', mode: 'headless', brief: 'Synthetic assigned goal' },
  provenance: source });

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    for (const t of ['tasks','grants','rules','receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sql.exec('DELETE FROM credentials'); h.sql.exec('DELETE FROM agents');
    h.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('fixture', 'admin@fake.local', 1, '2026-01-01', '2026-01-01')");
  });
});

describe('intake acceptance-criteria start gate', () => {
  it('rejects assign when acceptance criteria are missing', async () => {
    const { task } = await create();
    expect(task.payload_data.acceptance_criteria).toBeNull();
    const before = await sessionCount();
    const res = await call('/tasks/' + task.id + '/assign', assignPayload(1));
    const data = await res.json() as any;
    expect(res.status, JSON.stringify(data)).toBe(400);
    expect(data.error).toBe('acceptance_criteria_required');
    expect(await sessionCount()).toBe(before);
  });

  it('rejects assign when acceptance criteria are whitespace-only', async () => {
    const { task } = await create('', { acceptance_criteria: '   \n  ' });
    const res = await call('/tasks/' + task.id + '/assign', assignPayload(1));
    const data = await res.json() as any;
    expect(res.status, JSON.stringify(data)).toBe(400);
    expect(data.error).toBe('acceptance_criteria_required');
  });

  it('accepts assign once criteria are set; the gate alone does not block start', async () => {
    const { task } = await create();
    const updated = await ok('/tasks/' + task.id, { key: 'ac-set-' + ++seq, expected_version: 1,
      acceptance_criteria: 'Done means a posted review with at most two findings.' });
    expect(updated.task.payload_data.acceptance_criteria).toContain('posted review');
    const assigned = await ok('/tasks/' + task.id + '/assign', assignPayload(2));
    expect(assigned.assigned_session.hub_session_id).toMatch(/^[0-9a-f-]{36}$/i);
    // Clearing the criteria afterwards blocks the next fresh assignment attempt.
    await ok('/tasks/' + task.id, { key: 'ac-clear-' + ++seq, expected_version: 3, acceptance_criteria: null });
    const cleared = await call('/tasks/' + task.id + '/assign', assignPayload(4, '-cleared'));
    const clearedData = await cleared.json() as any;
    expect(cleared.status, JSON.stringify(clearedData)).toBe(400);
    expect(clearedData.error).toBe('acceptance_criteria_required');
  });

  it('rejects sessions_start linked to a task with empty criteria, allows linked and unlinked starts with criteria', async () => {
    const empty = (await create()).task;
    const full = (await create('', { acceptance_criteria: 'Tests pass; review posted.' })).task;
    const linkedEmpty = await sessionsCall({ key: 'ac-start-' + ++seq, workspace: 'demo', brief: 'Synthetic direct goal', intake_task_id: empty.id });
    expect(linkedEmpty.status, JSON.stringify(linkedEmpty.data)).toBe(400);
    expect(linkedEmpty.data.error).toBe('acceptance_criteria_required');
    const linkedFull = await sessionsCall({ key: 'ac-start-' + ++seq, workspace: 'demo', brief: 'Synthetic direct goal', intake_task_id: full.id });
    expect([201, 409], JSON.stringify(linkedFull.data)).toContain(linkedFull.status);
    expect(linkedFull.data.error ?? null).not.toBe('acceptance_criteria_required');
    const unlinked = await sessionsCall({ key: 'ac-start-' + ++seq, workspace: 'demo', brief: 'Synthetic direct goal' });
    expect([201, 409], JSON.stringify(unlinked.data)).toContain(unlinked.status);
    const unknown = await sessionsCall({ key: 'ac-start-' + ++seq, workspace: 'demo', brief: 'Synthetic direct goal', intake_task_id: 'missing-task' });
    expect(unknown.status, JSON.stringify(unknown.data)).toBe(404);
    expect(unknown.data.error).toBe('unknown_intake_task');
  });
});
