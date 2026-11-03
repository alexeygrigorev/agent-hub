import { freshFixtureBudgetDay, fixtureSql } from './helpers/fixture-budget';
import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';

const inDO: any = runInDurableObject;
const fixture = (fn: (h: any) => void | Promise<void>) => inDO((env as any).HUB.get((env as any).HUB.idFromName('status-bounded-' + crypto.randomUUID())), fn);

// Intake 3bd2521a: every /api/status call used to SELECT the whole
// intake_task_projection and JSON.parse each body (no LIMIT), blocking the
// single-threaded DO for seconds per call (measured 7-12s on prod). The
// contract now: a write-maintained light summary feeds selection/counts, and
// full projected bodies are read back only for the bounded id selection.
const LEGACY_UNBOUNDED = 'SELECT data FROM intake_task_projection';
const LEGACY_UNBOUNDED_TASKS = 'SELECT data FROM intake_tasks ORDER BY id';

async function cookieFor(): Promise<{ cookie: string; csrf: string }> {
  const csrf = 'csrf-3bd2521a';
  const raw = await createSessionCookie(env as any, { sub: 'status-bounded', email: 'admin@fake.local', isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600 });
  return { cookie: raw.split(';')[0], csrf };
}

it('status reads stay bounded to the capped selection and keep their output shape', async () => fixture(async h => {
  const now = Date.now();
  const statuses = ['open', 'in_progress', 'blocked', 'pending_approval', 'done', 'cancelled'];
  await freshFixtureBudgetDay(h);
  const seedSql=fixtureSql(h);
  h.ctx.storage.transactionSync(() => {
    for (let i = 0; i < 900; i++) {
      const task = {
        id: `t${String(i).padStart(4, '0')}`, project: 'bounded', status: statuses[i % statuses.length],
        title: `Task ${i}`, version: 3, created_at: now - 86400000, updated_at: now - i * 1000,
        owner: i % 2 ? 'someone' : null,
        history: [
          { kind: 'intake', text: 'Original request body. '.repeat(400), source: { reference: null } },
          ...Array.from({ length: 12 }, (_, j) => ({ kind: 'note', text: `Retained note ${j}. `.repeat(30), captured_at: now, id: `t${i}-${j}` })),
          ...(i % 3 === 0 ? [{ kind: 'blocker', id: `blk-${i}`, text: 'Blocked on review' }] : []),
        ],
      };
      seedSql.exec('INSERT INTO intake_tasks(id,data) VALUES (?,?)', task.id, JSON.stringify(task));
    }
  });

  const queries: string[] = [];
  const rawSql = h.ctx.storage.sql;
  const origExec = rawSql.exec.bind(rawSql);
  rawSql.exec = (q: string, ...args: any[]) => { queries.push(String(q)); return origExec(q, ...args); };

  const { cookie, csrf } = await cookieFor();
  const post1 = await h.fetch(new Request('https://hub.test/api/status', { method: 'POST', headers: { Cookie: cookie, 'x-csrf-token': csrf }, body: '{}' }));
  expect(post1.status).toBe(200);
  const body1 = await post1.json();

  // Contract: no statement may scan task bodies without an id-bound WHERE.
  for (const q of queries) expect(q.includes(LEGACY_UNBOUNDED)).toBe(false);
  expect(queries.some(q => q.includes('FROM intake_status_summary'))).toBe(true);
  expect(queries.some(q => q.includes('FROM intake_task_projection WHERE id IN'))).toBe(true);

  // First 256 lexicographic ids survive the cap; counts reflect that cap.
  expect(body1.tasks.length).toBe(256);
  expect(body1.summary.truncated).toBe(true);
  expect(body1.summary.by_status).toEqual({ open: 43, in_progress: 43, blocked: 43, pending_approval: 43, done: 42, cancelled: 42 });
  expect(body1.categories.diff.state).toBe('first_baseline');
  expect(body1.categories.diff.added.length).toBe(256);
  // Open non-terminal count covers the WHOLE authorized population, not the cap.
  expect(body1.open_non_terminal_count).toBe(600);
  // Retained private note bodies stay out of the capped hydration too.
  expect(JSON.stringify(body1)).not.toContain('Retained note 5');

  const post2 = await h.fetch(new Request('https://hub.test/api/status', { method: 'POST', headers: { Cookie: cookie, 'x-csrf-token': csrf }, body: '{}' }));
  expect(post2.status).toBe(200);
  const body2 = await post2.json();
  expect(body2.categories.diff.state).toBe('available');
  expect(body2.categories.diff.added).toEqual([]);
  expect(body2.categories.diff.removed).toEqual([]);
  expect(body2.categories.diff.changed).toEqual([]);

  const get1 = await h.fetch(new Request("https://hub.test/api/status", { method: "GET", headers: { Cookie: cookie } }));
  expect(get1.status).toBe(200);
  const page = await get1.json();
  expect(page.tasks.length).toBe(25);
  expect(page.pagination.total).toBe(900);
  expect(page.pagination.next).toBe('t0024');
  expect(page.tasks[0].id).toBe('t0000');
  expect(page.open_non_terminal_count).toBe(600);

  // Authorized scoping still works on the light summaries.
  const granted = h.intakeService.statusTaskSummaries({ admin: false, grants: [{ scope: 'task', target: 't0000', access: 'read' }] } as any);
  expect(granted.map((t: any) => t.id)).toEqual(['t0000']);
  expect(granted[0]).toEqual({ id: 't0000', project: 'bounded', status: 'open', title: 'Task 0', updated_at: now, created_at: now - 86400000, version: 3 });

  // Intake writes (prune before write) also use intake_status_summary and
  // never scan all intake_tasks bodies.
  queries.length = 0;
  for (let w = 0; w < 3; w++) {
    const ev = await h.fetch(new Request('https://hub.test/api/intake/tasks/t0000/events', {
      method: 'POST',
      headers: { Cookie: cookie, 'x-csrf-token': csrf, 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: `prune-bounded-${w}`, expected_version: 3 + w, note: { text: `note ${w}` } }),
    }));
    expect(ev.status).toBe(200);
  }
  for (const q of queries) {
    expect(q.includes(LEGACY_UNBOUNDED)).toBe(false);
    expect(q.includes(LEGACY_UNBOUNDED_TASKS)).toBe(false);
  }
  expect(Number(h.sql.exec("SELECT COUNT(*) AS n FROM intake_status_summary WHERE archived_at IS NULL AND status IN ('done','cancelled')").one().n)).toBe(0);

  // Loose wall-time fence: the bounded path must stay far below the legacy
  // multi-second projection (generous headroom keeps CI variance out).
  const times: number[] = [];
  for (let i = 0; i < 8; i++) {
    const start = performance.now();
    const r = await h.fetch(new Request('https://hub.test/api/status', { method: 'POST', headers: { Cookie: cookie, 'x-csrf-token': csrf }, body: '{}' }));
    expect(r.status).toBe(200);
    await r.text();
    times.push(performance.now() - start);
  }
  times.sort((a, b) => a - b);
  expect(times[Math.floor(times.length / 2)]).toBeLessThan(2500);
}));

it('summary table tracks every write path, rebuilds after loss and survives rollback', async () => fixture(h => {
  const put = (value: any) => h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', value.id, JSON.stringify(value));
  const rows = () => h.sql.exec('SELECT id,project,status,title,updated_at,created_at,archived_at,version FROM intake_status_summary ORDER BY id').toArray().map((r: any) => {
    const t: any = { id: r.id };
    for (const k of ['project', 'status', 'title', 'updated_at', 'created_at', 'archived_at', 'version']) if (r[k] !== null) t[k] = r[k];
    return t;
  });
  put({ id: 'a', project: 'p', status: 'open', title: 'A', version: 1, created_at: 5, updated_at: 6, history: [{ kind: 'intake', text: 'x', source: {} }] });
  put({ id: 'b', project: 'q', status: 'done', title: 'B', version: 2, created_at: 7, updated_at: 8, history: [] });
  expect(rows()).toEqual([
    { id: 'a', project: 'p', status: 'open', title: 'A', updated_at: 6, created_at: 5, version: 1 },
    { id: 'b', project: 'q', status: 'done', title: 'B', updated_at: 8, created_at: 7, version: 2 },
  ]);
  put({ id: 'a', project: 'p', status: 'in_progress', title: 'A2', version: 2, created_at: 5, updated_at: 9, history: [] });
  expect(rows().find((r: any) => r.id === 'a')).toEqual({ id: 'a', project: 'p', status: 'in_progress', title: 'A2', updated_at: 9, created_at: 5, version: 2 });
  // Raw UPDATE/DELETE outside put() stay tracked (trigger-maintained).
  h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify({ id: 'b', project: 'q', status: 'done', title: 'B', version: 3, created_at: 7, updated_at: 10, archived_at: 11, history: [] }), 'b');
  expect(rows().find((r: any) => r.id === 'b')).toEqual({ id: 'b', project: 'q', status: 'done', title: 'B', updated_at: 10, created_at: 7, archived_at: 11, version: 3 });
  h.sql.exec('DELETE FROM intake_tasks WHERE id=?', 'b');
  expect(rows().map((r: any) => r.id)).toEqual(['a']);
  // Transactional rollback reverts summary writes with the task write.
  const before = rows();
  expect(() => h.ctx.storage.transactionSync(() => { put({ id: 'c', project: 'p', status: 'open', title: 'C', version: 1, created_at: 1, updated_at: 2, history: [] }); throw new Error('rollback'); })).toThrow('rollback');
  expect(rows()).toEqual(before);
  // Loss of the derived table rebuilds it from the canonical ledger once.
  h.sql.exec('DROP TABLE intake_status_summary');
  h.intakeService.init();
  expect(rows()).toEqual([...before]);
  h.intakeService.init();
  expect(rows()).toEqual([...before]);
  // Counts equal, so no rescan; a real drift is repaired anyway.
  h.sql.exec(`DELETE FROM intake_status_summary WHERE id='a'`);
  h.intakeService.init();
  expect(rows()).toEqual([...before]);
}));
