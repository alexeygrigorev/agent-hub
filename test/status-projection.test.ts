import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { buildSessionInputs } from '../src/session_lifecycle_registry';

const inDO: any = runInDurableObject;
const fixture = (fn: (h: any) => void) => inDO((env as any).HUB.get((env as any).HUB.idFromName('projection-' + crypto.randomUUID())), fn);

it('keeps projection and byte totals exact through writes, rollback and cold migration', async () => fixture(h => {
  const task = { id: 'task', project: 'perf', status: 'open', title: 'Synthetic', version: 1, history: [
    {kind: 'intake', text: null, source: {reference: 'voice'}},
    {kind: 'note', text: 'Private retained note'},
    {kind: 'blocker', id: 'blocked', text: 'Waiting'},
    {kind: 'link', hub_session_id: 'session'},
  ] };
  const put = (value: any) => h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', 'task', JSON.stringify(value));
  const projected = () => h.intakeService.statusTaskSummaries({admin: true})[0];
  const verify = () => {
    const usage = h.intakeService.storageUsage();
    const bytes = Number(h.sql.exec('SELECT COALESCE(SUM(length(CAST(data AS BLOB))+length(CAST(id AS BLOB))),0) AS n FROM intake_tasks').one().n);
    expect(usage.bytes.open_tasks + usage.bytes.archived_or_terminal_tasks).toBe(bytes);
    const history = h.sql.exec("SELECT COALESCE(SUM(length(CAST(json_extract(data,'$.history') AS BLOB))),0) AS n FROM intake_tasks").one().n;
    expect(usage.bytes.events_within_tasks).toBe(history);
    for (const kind of ['receipts', 'grants', 'rules']) {
      const row = h.sql.exec(`SELECT COALESCE(SUM(length(CAST(data AS BLOB))+length(CAST(id AS BLOB))),0) AS n, COUNT(*) AS count FROM intake_${kind}`).one();
      expect(usage.bytes[kind]).toBe(row.n);
      expect(usage.counts[kind]).toBe(row.count);
    }
    return usage;
  };
  put(task);
  expect(projected().history.some((e: any) => e.kind === 'note')).toBe(false);
  expect(h.intakeService.statusContinuation(projected())).toBe('uncertain');
  verify();
  for (const kind of ['receipts', 'grants', 'rules']) {
    h.sql.exec(`INSERT INTO intake_${kind}(id,data) VALUES ('fixture',?)`, JSON.stringify({id: 'fixture', text: 'unicode é😀'}));
    verify();
    h.sql.exec(`UPDATE intake_${kind} SET data=? WHERE id='fixture'`, JSON.stringify({id: 'fixture', text: 'replaced'}));
    verify();
    h.sql.exec(`DELETE FROM intake_${kind} WHERE id='fixture'`);
    verify();
  }
  const transcribed = {...task, history: [...task.history, {kind: 'intake', text: 'Transcribed private body'.repeat(200), transcription: 'provided', source: {reference: 'voice'}}]};
  put(transcribed);
  expect(h.intakeService.statusContinuation(projected())).toBe('blocked');
  expect(JSON.stringify(projected())).not.toContain('Transcribed private body');
  const before = verify();
  expect(() => h.ctx.storage.transactionSync(() => { put({...transcribed, status: 'done'}); throw new Error('rollback'); })).toThrow('rollback');
  expect(verify()).toEqual(before);
  put({...transcribed, status: 'done', archived_at: 123});
  expect(verify().bytes.open_tasks).toBe(0);
  expect(verify().counts.audit).toBe(2);
  // Simulate a pre-migration database. Only derived data is removed, from
  // this isolated disposable test DO; all canonical ledger rows survive.
  h.sql.exec('DROP TABLE intake_task_projection');
  h.sql.exec('DROP TABLE intake_storage_totals');
  h.intakeService.init();
  const migrated = verify();
  h.intakeService.init();
  expect(verify()).toEqual(migrated);
  expect(projected().status).toBe('done');
  h.sql.exec("DELETE FROM intake_tasks WHERE id='task'");
  expect(h.intakeService.statusTaskSummaries({admin: true})).toEqual([]);
  expect(verify().counts.tasks).toBe(0);
}));

it('bounded lifecycle joins preserve duplicate-native session and launch precedence', async () => fixture(h => {
  const now = new Date().toISOString();
  // Insertion order deliberately differs from id/index order.
  for (const id of ['z-first', 'a-second']) {
    h.sql.exec('INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
      id, 'perf:owner', id, '{}', 'machine', 'codex', 'headless', 'running', '{"native_id":"native"}', now, now);
    h.sql.exec('INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
      id, 'perf:owner', 'agent', 'Synthetic', id, '{}', 'machine', 'codex', 'default', 'headless', '/synthetic', 'Synthetic', 'running', 'native', now, now);
  }
  const full = buildSessionInputs(h.sql);
  const bounded = buildSessionInputs(h.sql, new Set(['a-second']));
  expect(bounded).toEqual(full);
  expect(bounded.refs.get('machine/native')?.session_id).toBe('z-first');
  expect(bounded.refs.get('machine/native')?.fleet_launch_id).toBe('z-first');
}));
