import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { MeteredDb } from '../src/db.ts';
import { PICKER_ALERT_SCHEMA, PickerAlerts, PICKER_ALERT_RECIPIENT } from '../src/picker-alerts.ts';

function fixture() {
  const sqlite = new DatabaseSync(':memory:');
  const db = new MeteredDb({ exec(query: string, ...args: unknown[]) {
    const statement = sqlite.prepare(query);
    if (statement.columns().length) {
      const rows = statement.all(...args as any[]);
      return { toArray: () => rows, rowsRead: rows.length, rowsWritten: 0 };
    }
    const result = statement.run(...args as any[]);
    return { toArray: () => [], rowsRead: 0, rowsWritten: Number(result.changes) };
  } } as any);
  const state = { storage: { transactionSync<T>(fn: () => T): T {
    sqlite.exec('BEGIN');
    try {
      const result = fn();
      sqlite.exec('COMMIT');
      return result;
    } catch (error) {
      sqlite.exec('ROLLBACK');
      throw error;
    }
  } } } as DurableObjectState;
  for (const statement of PICKER_ALERT_SCHEMA) db.run(statement);
  const reopen = () => new PickerAlerts(db, state);
  return { sqlite, db, collector: reopen(), reopen };
}

function pick(operation: string, models = ['model-a'], project = 'project-a', attempt: 0 | 1 = 0) {
  const selected = { policy_id: 'policy-0', policy_version: 1, machine_id: 'host-1', attempt,
    model: models[0], engine: 'codex', effort: 'medium' };
  const pool = models.map((model, index) => ({ policy_id: 'policy-' + index, model,
    engine: 'codex', effort: 'medium', weight: 1, share: 1 / models.length }));
  return { project_id: project, operation_id: operation, attempt, committed_at: 100,
    picker: { ...selected, pool, history: [selected], pool_degraded: new Set(models).size < 3 } };
}

test('global receipt order and cumulative occurrences survive reopening and replay', async t => {
  const f = fixture();
  t.after(() => f.sqlite.close());
  const a = pick('op-a'), b = pick('op-b', ['model-a', 'model-b'], 'project-b');
  // Producer clock order cannot reorder accepted committed picks.
  b.committed_at = 1;
  assert.equal(f.collector.record(a, 100).receipt_seq, 1);
  assert.equal(f.collector.record(b, 100).receipt_seq, 2);
  assert.equal(f.collector.record(pick('op-c'), 100).occurrence_watermark, 1);
  const before = f.collector.counters();
  assert.equal(f.reopen().record(a, 200).replay, true);
  assert.deepEqual(f.collector.counters(), before);
  f.collector.record(pick('op-d'), 100);
  assert.equal((await f.reopen().offers(100)).length, 1);
  f.collector.record(pick('healthy', ['model-a', 'model-b', 'model-c']), 100);
  assert.equal(f.collector.counters().consecutive_degraded, 0);
  for (const id of ['next-a', 'next-b', 'next-c']) f.reopen().record(pick(id), 100);
  assert.deepEqual(f.collector.counters(), { total_picks: 8, degraded_picks: 7,
    consecutive_degraded: 3, occurrence_watermark: 2, receipt_watermark: 8 });
  assert.equal(f.sqlite.prepare('SELECT COUNT(*) AS n FROM picker_alert_deliveries').get()!.n, 2);
});

test('identity separates attempts and projects while changed immutable facts conflict', t => {
  const f = fixture();
  t.after(() => f.sqlite.close());
  f.collector.record(pick('same'));
  f.collector.record(pick('same', ['model-a'], 'project-b'));
  f.collector.record(pick('same', ['model-a'], 'project-a', 1));
  const before = f.collector.counters();
  const changed = pick('same');
  changed.committed_at++;
  assert.throws(() => f.collector.record(changed), /picker_receipt_conflict/);
  assert.throws(() => f.collector.record(pick('same', ['model-a', 'model-b'])), /picker_receipt_conflict/);
  assert.deepEqual(f.collector.counters(), before);
});

test('canonical repository project keys preserve their slash and bounded operation identity', t => {
  const f = fixture();
  t.after(() => f.sqlite.close());
  const facts = pick('start:1234', ['model-a'], 'DataTalksClub/agent-hub');
  assert.equal(f.collector.record(facts).replay, false);
  assert.equal(f.collector.record(facts).replay, true);
  const stored = f.sqlite.prepare('SELECT project_id FROM picker_receipts').get()!;
  assert.equal(stored.project_id, 'DataTalksClub/agent-hub');
  assert.throws(() => f.collector.record({ ...facts, operation_id: 'start/1234' }), /invalid_picker_receipt/);
  for (const project_id of ['x'.repeat(181), 'org/repo\n', ' org/repo']) {
    assert.throws(() => f.collector.record({ ...facts, project_id }), /invalid_picker_receipt/);
  }
  assert.equal(f.collector.counters().total_picks, 1);
});

test('transaction failure rolls back receipt, occurrence, counters, and replay state', t => {
  const f = fixture();
  t.after(() => f.sqlite.close());
  f.collector.record(pick('a'));
  f.collector.record(pick('b'));
  f.sqlite.exec("CREATE TRIGGER fail_alert BEFORE INSERT ON picker_alert_deliveries BEGIN SELECT RAISE(ABORT,'injected'); END");
  assert.throws(() => f.collector.record(pick('c')), /injected/);
  assert.equal(f.collector.counters().total_picks, 2);
  assert.equal(f.collector.counters().occurrence_watermark, 0);
  f.sqlite.exec('DROP TRIGGER fail_alert');
  const recovered = f.collector.record(pick('c'));
  assert.equal(recovered.replay, false);
  assert.equal(recovered.occurrence_watermark, 1);
});

test('facts are bounded and whitelisted and degradation counts distinct models', t => {
  const f = fixture();
  t.after(() => f.sqlite.close());
  const facts = pick('a', ['model-a', 'model-a', 'model-b']);
  (facts as any).token = 'private-marker';
  (facts.picker as any).raw_quota = 'private-marker';
  (facts.picker.pool[0] as any).secret = 'private-marker';
  f.collector.record(facts);
  const stored = f.sqlite.prepare('SELECT facts_json FROM picker_receipts').get()!.facts_json as string;
  assert.equal(stored.includes('private-marker'), false);
  assert.equal(f.collector.counters().degraded_picks, 1);
  const oversized = pick('b');
  oversized.picker.pool[0].model = 'a'.repeat(1000);
  assert.throws(() => f.collector.record(oversized), /invalid_picker_receipt/);
  const wrongFlag = pick('c');
  wrongFlag.picker.pool_degraded = false;
  assert.throws(() => f.collector.record(wrongFlag), /invalid_picker_receipt/);
  assert.throws(() => f.collector.record({ ...pick('d'), attempt: 1 }), /invalid_picker_receipt/);
  assert.throws(() => f.collector.record({ ...pick('d'), project_id: '' }), /invalid_picker_receipt/);
  assert.equal(f.collector.counters().total_picks, 1);
});

test('leases serialize pollers and fence stale receipts, retries, and acknowledgements', async t => {
  const f = fixture();
  t.after(() => f.sqlite.close());
  for (const id of ['a', 'b', 'c']) f.collector.record(pick(id), 100);
  const polls = await Promise.all([f.collector.offers(100), f.reopen().offers(100)]);
  assert.equal(polls[0].length + polls[1].length, 1);
  const offer = [...polls[0], ...polls[1]][0];
  assert.equal(offer.recipient, PICKER_ALERT_RECIPIENT);
  assert.equal(offer.payload.to, PICKER_ALERT_RECIPIENT);
  await assert.rejects(f.collector.settle(1, '0'.repeat(64), 'ack', 200), /lease_conflict/);
  assert.deepEqual(await f.collector.settle(1, offer.lease_token, 'nack', 200), { replay: false, delivered: false });
  assert.deepEqual(await f.collector.settle(1, offer.lease_token, 'nack', 200), { replay: true, delivered: false });
  assert.equal((await f.collector.offers(15_199)).length, 0);
  const retry = (await f.reopen().offers(15_200))[0];
  assert.equal(retry.attempt, 2);
  await assert.rejects(f.collector.settle(1, offer.lease_token, 'ack', 15_201), /lease_conflict/);
  await assert.rejects(f.collector.settle(1, retry.lease_token, 'ack', retry.lease_until), /lease_conflict/);
  const expired = (await f.reopen().offers(retry.lease_until))[0];
  await f.collector.settle(1, expired.lease_token, 'ack', expired.lease_until - 1);
  assert.deepEqual(await f.reopen().settle(1, expired.lease_token, 'ack', expired.lease_until),
    { replay: true, delivered: true });
  await assert.rejects(f.collector.settle(1, retry.lease_token, 'ack', expired.lease_until), /lease_conflict/);
  assert.equal((await f.collector.offers(expired.lease_until)).length, 0);
  assert.equal(f.collector.counters().occurrence_watermark, 1);
});

test('invalid bounds and lease inputs leave cumulative state and delivery unchanged', async t => {
  const f = fixture();
  t.after(() => f.sqlite.close());
  for (const id of ['a', 'b', 'c']) f.collector.record(pick(id), 100);
  const before = f.collector.counters();
  await assert.rejects(f.collector.offers(100, 11), /invalid_picker_alert_limit/);
  await assert.rejects(f.collector.offers(Number.MAX_SAFE_INTEGER), /invalid_picker_timestamp/);
  await assert.rejects(f.collector.settle(1, null as any, 'ack', 100), /invalid_picker_alert_receipt/);
  assert.throws(() => f.collector.record(pick('d'), -1), /invalid_picker_timestamp/);
  assert.deepEqual(f.collector.counters(), before);
  assert.equal((await f.collector.offers(100))[0].attempt, 1);
});

test('coverage epochs reset only the consecutive streak and preserve cumulative receipts and occurrences', t => {
  const f=fixture();t.after(()=>f.sqlite.close());
  f.collector.beginCoverage('epoch-a',100);
  for(const id of ['a','b','c','d'])f.collector.record(pick(id),100);
  const before=f.collector.counters();
  f.reopen().beginCoverage('epoch-a',200);
  assert.deepEqual(f.collector.counters(),before);
  assert.deepEqual(f.collector.coverage(),{available:true,epoch:'epoch-a',since:100});
  f.reopen().beginCoverage('epoch-b',300);
  assert.deepEqual(f.collector.counters(),{...before,consecutive_degraded:0});
  assert.deepEqual(f.collector.coverage(),{available:true,epoch:'epoch-b',since:300});
  for(const id of ['e','f','g'])f.collector.record(pick(id),300);
  assert.equal(f.collector.counters().occurrence_watermark,2);
});

test('committed draw response, immutable input and collector effects share one transaction',t=>{
  const f=fixture();t.after(()=>f.sqlite.close());
  const facts=pick('a'),response={route:{model:'model-a'},diagnostics:{pool:['synthetic']}};
  f.sqlite.exec("CREATE TRIGGER fail_draw BEFORE INSERT ON picker_draws BEGIN SELECT RAISE(ABORT,'draw-injected'); END");
  assert.throws(()=>f.collector.commitDraw(facts,'digest-a',response,100),/draw-injected/);
  assert.equal(f.collector.counters().total_picks,0);
  assert.equal(f.collector.replayDraw('project-a','a',0,'digest-a'),undefined);
  f.sqlite.exec('DROP TRIGGER fail_draw');
  assert.deepEqual(f.collector.commitDraw(facts,'digest-a',response,100),response);
  assert.deepEqual(f.collector.replayDraw('project-a','a',0,'digest-a'),response);
  assert.throws(()=>f.collector.replayDraw('project-a','a',0,'digest-b'),/picker_draw_conflict/);
  assert.equal(f.collector.counters().total_picks,1);
});
