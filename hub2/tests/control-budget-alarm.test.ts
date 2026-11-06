import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { ControlDO } from '../src/control-do';
import { METRICS } from '../src/budget-do';
import { SQL_PROBE_HEADER, SQL_RECEIPT_HEADER } from '../src/sql-receipt';

function sqliteState() {
  const database = new DatabaseSync(':memory:');
  const ctx: any = {
    storage: {
      sql: { exec(query: string, ...args: unknown[]) {
        const statement = database.prepare(query);
        let rows: unknown[] = [], rowsRead = 0, rowsWritten = 0;
        if (statement.columns().length) { rows = statement.all(...args as any); rowsRead = rows.length; }
        else { const result = statement.run(...args as any); rowsWritten = Number(result.changes); }
        return { toArray: () => rows, rowsRead, rowsWritten };
      }},
      transactionSync<T>(callback: () => T): T {
        database.exec('BEGIN');
        try { const result = callback(); database.exec('COMMIT'); return result; }
        catch (error) { database.exec('ROLLBACK'); throw error; }
      },
      async setAlarm(at: number) { ctx.alarm = at; },
      async deleteAlarm() { ctx.alarm = null; },
      async getAlarm() { return ctx.alarm; },
      alarm: null as number | null,
    },
    blockConcurrencyWhile(callback: () => unknown) { return Promise.resolve(callback()); },
  };
  return { ctx, database };
}

function receipt(request: Request, body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: {
    [SQL_RECEIPT_HEADER]: JSON.stringify({ v: 1, valid: true, reads: 0, writes: 0,
      operation_id: request.headers.get(SQL_PROBE_HEADER), do_requests: 1, do_gb_ms: 1 }),
  }});
}

function fakeBudget(mode: { deny: boolean; retryAt: number; paths: string[] }) {
  const units: Record<string, number> = Object.fromEntries(METRICS.map(metric => [metric, 1]));
  Object.assign(units, { worker_requests: 1, worker_cpu_ms: 500, do_requests: 20, do_gb_ms: 1000,
    sqlite_rows_read: 4096, sqlite_rows_written: 4096, sqlite_byte_days: 1,
    kv_reads: 1, kv_writes: 1, kv_deletes: 1, kv_lists: 1, kv_byte_days: 1 });
  const envelopes = JSON.stringify({ version: 1,
    normal: { units, max_do_calls: 8, max_wall_ms: 10_000, provenance: 'Control alarm unit-test envelope only' },
    safety: { units, max_do_calls: 8, max_wall_ms: 10_000, provenance: 'Control alarm unit-test safety envelope only' },
  });
  return {
    HUB2_BUDGET_ENVELOPES: envelopes,
    BUDGET: { idFromName: (name: string) => name, get: () => ({ fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init), path = new URL(request.url).pathname;
      mode.paths.push(path);
      if (path === '/_internal/reserve' && mode.deny)
        return receipt(request, { allowed: false, retry_at: mode.retryAt }, 429);
      return receipt(request, path === '/_internal/reserve' ? { allowed: true } : { settled: true });
    }})},
  } as any;
}

test('Control alarm persists a budget retry and stops fanout before the mailbox effect', async () => {
  const state = sqliteState();
  const now = new Date();
  const mode = { deny: true, retryAt: Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1), paths: [] as string[] };
  let mailboxCalls = 0;
  const env = fakeBudget(mode);
  env.MAILBOXES = { idFromName: (name: string) => name, get: () => ({ fetch: async (request: Request) => {
    mailboxCalls++;
    return receipt(request, { next: null, resumed: 0 });
  }})};
  const control = new ControlDO(state.ctx, env);
  await (control as any).ready;
  state.database.prepare('INSERT INTO mailbox_wakes(owner,revision,after_id,next_attempt,attempt) VALUES(?,?,?,?,?)')
    .run('agent:writer', 1, '', Date.now() - 1, 3);

  await control.alarm();
  const row = state.database.prepare('SELECT attempt,next_attempt FROM mailbox_wakes WHERE owner=?').get('agent:writer') as any;
  assert.equal(mailboxCalls, 0);
  assert.equal(row.attempt, 4);
  assert.equal(row.next_attempt, mode.retryAt, JSON.stringify({ row, retryAt: mode.retryAt, paths: mode.paths }));
  assert.deepEqual(mode.paths, ['/_internal/reserve']);

  mode.deny = false;
  state.database.prepare('UPDATE mailbox_wakes SET next_attempt=? WHERE owner=?').run(Date.now() - 1, 'agent:writer');
  await control.alarm();
  assert.equal(mailboxCalls, 1);
  assert.equal(state.database.prepare('SELECT count(*) AS count FROM mailbox_wakes').get()?.count, 0);
  assert.deepEqual(mode.paths.slice(1), ['/_internal/reserve', '/_internal/settle']);
});
