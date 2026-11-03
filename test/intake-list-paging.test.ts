/** Intake 08658ee8: stable paged listing (list_token), server-side search (q),
 * and unique short-id prefix show. Targeted coverage for the three ACs. */
import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const source = { type: 'unknown', reference: null, timestamp: null };
let cookie: string;
let seq = 0;
async function call(path: string, payload?: any) {
  return SELF.fetch('https://hub.test/api/intake' + path, { method: payload ? 'POST' : 'GET',
    headers: { Cookie: cookie, 'x-csrf-token': 'csrf' },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
}
async function ok(path: string, payload?: any): Promise<any> {
  const res = await call(path, payload); const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200); return data;
}
const create = (project: string, title: string, text: string) => ok('/tasks',
  { key: 'tok-' + ++seq, project, title, text, source,
    acceptance_criteria: 'AC for ' + title });

beforeEach(async () => {
  seq = 0;
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    h.sql.exec('DROP TRIGGER IF EXISTS intake_note_archive_no_delete');
    h.sql.exec('DELETE FROM intake_note_archive');
    h.sql.exec("CREATE TRIGGER intake_note_archive_no_delete BEFORE DELETE ON intake_note_archive BEGIN SELECT RAISE(ABORT,'append_only_note_archive'); END");
    for (const t of ['tasks', 'grants', 'rules', 'receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sql.exec('DELETE FROM credentials'); h.sql.exec('DELETE FROM agents');
    h.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('fixture', 'admin@fake.local', 1, '2026-01-01', '2026-01-01')");
  });
});

describe('intake 08658ee8 stable listing and search', () => {
  it('pins a walk with list_token so creates mid-walk do not expire the cursor', async () => {
    const a = await create('demo', 'Alpha task', 'alpha original text');
    const b = await create('demo', 'Beta task', 'beta original text');
    const c = await create('demo', 'Gamma task', 'gamma original text');
    const first = await ok('/tasks?all=true&limit=1&list_token=new');
    expect(first.list_token).toMatch(/^[0-9a-f-]{36}$/);
    expect(first.items).toHaveLength(1);
    // Churn mid-walk: a new intake must not invalidate the pinned cursor.
    const d = await create('demo', 'Delta task', 'delta original text');
    expect(d.task.id).not.toBeUndefined();
    const seen = first.items.map((t: any) => t.id);
    let after = first.next;
    expect(after).not.toBeNull();
    for (let i = 0; i < 5 && after; i++) {
      const page = await ok(`/tasks?all=true&limit=1&list_token=${first.list_token}&after=${after}`);
      expect(page.list_token).toBe(first.list_token);
      seen.push(...page.items.map((t: any) => t.id));
      after = page.next;
    }
    expect(after).toBeNull();
    // Pinned set is exactly the three intakes from pin time (delta excluded).
    expect(seen.sort()).toEqual([a.task.id, b.task.id, c.task.id].sort());
  });

  it('rejects bad tokens and restarts once on expiry', async () => {
    const bad = await call('/tasks?all=true&list_token=nope');
    expect(bad.status).toBe(400);
    const unknown = await call('/tasks?all=true&list_token=' + '00000000-0000-4000-8000-000000000000');
    expect(unknown.status).toBe(410);
    expect(((await unknown.json()) as any).error).toBe('list_token_expired');
  });

  it('filters server-side by q across title, project, text and context', async () => {
    await create('demo', 'Fix login bug', 'users cannot sign in');
    await create('demo', 'Fix logout bug', 'session ends slowly');
    await create('other', 'Unrelated work', 'nothing relevant here');
    const login = await ok('/tasks?all=true&q=' + encodeURIComponent('login'));
    expect(login.items.map((t: any) => t.payload_data.title)).toEqual(['Fix login bug']);
    const project = await ok('/tasks?all=true&q=' + encodeURIComponent('other'));
    expect(project.items.map((t: any) => t.project)).toEqual(['other']);
    const text = await ok('/tasks?all=true&q=' + encodeURIComponent('sign in'));
    expect(text.items.map((t: any) => t.payload_data.title)).toEqual(['Fix login bug']);
    const empty = await ok('/tasks?all=true&q=' + encodeURIComponent('  '));
    expect(empty.total).toBe(3);
    const bad = await call('/tasks?all=true&q=' + encodeURIComponent('x'.repeat(129)));
    expect(bad.status).toBe(400);
    expect(((await bad.json()) as any).error).toBe('invalid_search');
    // Pinned search walk carries the filter through every page.
    const pinned = await ok('/tasks?all=true&limit=1&list_token=new&q=' + encodeURIComponent('fix'));
    expect(pinned.total).toBe(2);
    expect(pinned.list_token).toMatch(/^[0-9a-f-]{36}$/);
    const second = await ok(`/tasks?all=true&limit=1&list_token=${pinned.list_token}&after=${pinned.next}&q=${encodeURIComponent('fix')}`);
    expect(second.items).toHaveLength(1);
  });

  it('shows a task by unique short-id prefix', async () => {
    const created = await create('demo', 'Prefix target', 'prefix body');
    const id: string = created.task.id;
    const prefix = id.slice(0, 8);
    const shown = await ok('/tasks/' + prefix);
    expect(shown.task.id).toBe(id);
    const history = await ok('/tasks/' + prefix + '/history');
    expect(history.task_id ?? history.task?.id ?? id).toBeDefined();
    const missing = await call('/tasks/00000000');
    expect(missing.status).toBe(404);
  });
});
