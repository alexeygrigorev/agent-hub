/** Lock-free intake summary fast path (intake ef429827).
 *
 * Covers: complete machine-readable counts on a 600+ task fixture with no
 * histories in the body, the incomplete-backfill 503 gate, and a bounded
 * storage-read budget for the uncontended read (no write transaction, no
 * full-table scan). Fixtures live in a dedicated project and are removed
 * afterwards so other suites never observe them.
 */
import { advanceIntakeExport } from '../src/intake-export';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const PROJECT = 'perf-probe-ef429827';
const SEED = 600;
let cookie: string;
let baseTotal = 0;

async function summary(project?: string) {
  const res = await SELF.fetch(
    'https://hub.test/api/intake/summary' + (project ? '?project=' + project : ''),
    { headers: { Cookie: cookie, 'x-csrf-token': 'csrf' } });
  return { status: res.status, body: await res.json() as any };
}

function taskData(i: number) {
  const statuses = ['open', 'open', 'open', 'in_progress', 'blocked', 'done', 'cancelled'];
  const status = statuses[i % statuses.length];
  return JSON.stringify({
    id: `perf-${i}`, project: PROJECT, status, version: 1,
    title: `perf task ${i}`, owner: null, current_state: 's', next_step: 'n',
    created_at: 1700000000000 + i, updated_at: 1700001000000 + i,
    archived_at: null, history: [],
  });
}

beforeAll(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local',
    isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  baseTotal = (await summary()).body.total;
  await inDO(stub(), (h: any) => {
    for (let i = 0; i < SEED; i++) {
      h.sql.exec('INSERT OR IGNORE INTO intake_tasks(id,data) VALUES(?,?)', `perf-${i}`, taskData(i));
    }
    let guard = 0;
    while (!advanceIntakeExport(h.sql) && guard++ < 100) { /* drain the cursor */ }
  });
});

afterAll(async () => {
  await inDO(stub(), (h: any) => {
    h.sql.exec('DELETE FROM intake_tasks WHERE id LIKE ?', 'perf-%');
  });
});

describe('intake summary fast path (ef429827)', () => {
  it('returns complete counts with no histories', async () => {
    const after = (await summary()).body;
    expect(after.total - baseTotal).toBe(SEED);
    const sumCounts = Object.values(after.counts as Record<string, number>)
      .reduce((a: number, b: number) => a + (Number(b) || 0), 0);
    expect(sumCounts).toBe(after.total);
    expect(after.freshness).toBe('transactional_current_read');
    expect(after.recent).toHaveLength(5);
    for (const item of after.recent) {
      expect(item.history).toBeUndefined();
      expect(item.payload_data).toBeUndefined();
      expect(typeof item.id).toBe('string');
    }
    expect(JSON.stringify(after)).not.toContain('"history"');
    // Independent cross-check straight from the source tables.
    const counts = await inDO(stub(), (h: any) => ({
      tasks: Number(h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n),
      exported: Number(h.sql.exec("SELECT COUNT(*) AS n FROM intake_export WHERE id LIKE 'perf-%'").toArray()[0].n),
      open: Number(h.sql.exec(
        "SELECT COUNT(*) AS n FROM intake_status_summary WHERE archived_at IS NULL AND status IN ('open','in_progress','blocked','pending_approval')"
      ).toArray()[0].n),
    }));
    expect(counts.exported).toBe(SEED);
    expect(after.total).toBe(counts.tasks);
    expect(after.open_non_terminal_count).toBe(counts.open);
  });

  it('keeps the migrating 503 gate when the backfill is incomplete', async () => {
    await inDO(stub(), (h: any) => {
      h.sql.exec('UPDATE intake_export_migration SET done=0,after_id=NULL WHERE id=1');
    });
    try {
      const res = await summary();
      expect(res.status).toBe(503);
      expect(res.body.error).toBe('intake_export_migrating');
    } finally {
      await inDO(stub(), (h: any) => {
        let guard = 0;
        while (!advanceIntakeExport(h.sql) && guard++ < 200) { /* restore */ }
      });
    }
    const res = await summary();
    expect(res.status).toBe(200);
    expect(res.body.total).toBeGreaterThan(0);
  });

  it('serves the summary with a bounded number of storage reads', async () => {
    const outcome = await inDO(stub(), (h: any) => {
      const svc = h.intakeService;
      const backing = svc.sql;
      let reads = 0;
      const proxy = new Proxy(backing, {
        get(t: any, k: string) {
          if (k === 'exec') return (...a: any[]) => { reads++; return t.exec(...a); };
          const v = t[k];
          return typeof v === 'function' ? v.bind(t) : v;
        },
      });
      svc.sql = proxy;
      try {
        const p = svc.resolvePrincipal({ caller: { kind: 'human', isAdmin: true, actor: 'human:test' }, scopes: ['admin'] });
        const started = Date.now();
        const out = svc.summaryFast(new URL('https://hub.test/api/intake/summary'), p);
        return { ms: Date.now() - started, reads, ok: !!out, revision: out?.snapshot_version, total: out?.total };
      } finally {
        svc.sql = backing;
      }
    });
    expect(outcome.ok).toBe(true);
    expect(outcome.reads).toBeLessThanOrEqual(12);
    expect(outcome.ms).toBeLessThan(5000);
    expect(outcome.total).toBeGreaterThanOrEqual(SEED);
  });
});
