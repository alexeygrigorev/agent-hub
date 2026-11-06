import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MeteredDb } from '../src/db';

type Cursor = { toArray(): unknown[]; rowsRead?: number; rowsWritten?: number };
type Adapter = { exec(query: string, ...bindings: unknown[]): Cursor };

function snapshotAfterFailure(adapter: Adapter, run: (db: MeteredDb) => unknown) {
  const db = new MeteredDb(adapter as never);
  assert.throws(() => run(db));
  return db.snapshot();
}

test('missing cursor counters invalidate the budget measurement instead of certifying zero rows', () => {
  const db = new MeteredDb({
    exec: () => ({ toArray: () => [{ value: 1 }] }),
  } as never);

  assert.deepEqual(db.all('SELECT 1 AS value'), [{ value: 1 }]);
  const counts = db.snapshot();
  assert.equal(counts.measurement_valid, false);
  assert.equal(counts.measurement_errors, 1);
  assert.equal(counts.sql_calls, 1);
  assert.equal(counts.rows_read, 0);
  assert.equal(counts.rows_written, 0);
  assert.equal(counts.read_cache.entries, 0);
});

test('all marks a throwing cursor materialization as unknown-cost and clears cached rows', () => {
  const counts = snapshotAfterFailure({
    exec: () => ({ rowsRead: 7, rowsWritten: 0, toArray: () => { throw new Error('cursor materialization failed'); } }),
  }, db => db.all('SELECT value FROM settings'));

  assert.equal(counts.measurement_valid, false);
  assert.equal(counts.measurement_errors, 1);
  assert.equal(counts.sql_calls, 1);
  assert.equal(counts.rows_read, 0);
  assert.equal(counts.rows_written, 0);
  assert.equal(counts.read_cache.entries, 0);
});

test('run marks a throwing SQL execution as unknown-cost and clears cached rows', () => {
  const counts = snapshotAfterFailure({
    exec: () => { throw new Error('SQL execution failed'); },
  }, db => db.run('UPDATE settings SET value = ?', 'x'));

  assert.equal(counts.measurement_valid, false);
  assert.equal(counts.measurement_errors, 1);
  assert.equal(counts.sql_calls, 1);
  assert.equal(counts.rows_read, 0);
  assert.equal(counts.rows_written, 0);
  assert.equal(counts.read_cache.entries, 0);
});

test('a throwing cursor counter getter invalidates measurement even when rows materialize', () => {
  const cursor = {
    toArray: () => [{ value: 1 }],
    get rowsRead(): number { throw new Error('rowsRead unavailable'); },
    rowsWritten: 0,
  };
  const db = new MeteredDb({ exec: () => cursor } as never);

  assert.deepEqual(db.all('SELECT 1 AS value'), [{ value: 1 }]);
  const counts = db.snapshot();
  assert.equal(counts.measurement_valid, false);
  assert.equal(counts.measurement_errors, 1);
  assert.equal(counts.sql_calls, 1);
  assert.equal(counts.read_cache.entries, 0);
});
