import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { buildSessionInputs, initLifecycleRequestProjection } from '../src/session_lifecycle_registry';

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


it('bounded observations follow canonical aliases without crossing machines', async () => fixture(h => {
  const now = new Date().toISOString();
  for (const [id, machine] of [['z-first', 'machine'], ['a-second', 'machine'], ['other', 'other-machine']]) {
    h.sql.exec('INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
      id, 'perf:owner', id, '{}', machine, 'codex', 'headless', 'running', '{"native_id":"native"}', now, now);
  }
  for (const [machine, payload] of [['machine', '{"waiting_confirmation":true}'], ['other-machine', '{"awaiting_alexey":true}']]) {
    h.sql.exec('INSERT INTO session_lifecycle_observations VALUES(?,?,?,?,?)', `${machine}/native`, machine, 'native', now, payload);
  }
  const all = buildSessionInputs(h.sql);
  const selected = buildSessionInputs(h.sql, new Set(['a-second']));
  expect(selected.inputs).toEqual(all.inputs.filter(input => input.machine_id === 'machine'));
  expect(selected.refs.get('machine/native')).toEqual(all.refs.get('machine/native'));
  expect(selected.refs.get('machine/native')?.session_id).toBe('z-first');
  expect(selected.inputs[0]).toMatchObject({waiting_confirmation: true});
  expect(selected.inputs[0]).not.toHaveProperty('awaiting_alexey');
  expect(buildSessionInputs(h.sql, new Set())).toEqual({inputs: [], refs: new Map()});
  h.sql.exec("UPDATE session_lifecycle_observations SET payload_json='not-json' WHERE machine_id='machine'");
  expect(buildSessionInputs(h.sql, new Set(['a-second'])).inputs[0]).not.toHaveProperty('waiting_confirmation');
  const withoutObservationStore = { exec(query: string, ...args: any[]) {
    if (query.includes('FROM session_lifecycle_observations')) throw new Error('pre-init observation store');
    return h.sql.exec(query, ...args);
  }} as any;
  expect(buildSessionInputs(withoutObservationStore, new Set(['a-second'])))
    .toEqual(buildSessionInputs(h.sql, new Set(['a-second'])));
}));


it('maintains lifecycle request facts through edits, rollback and migration', async () => fixture(h => {
  const now = new Date().toISOString();
  h.sql.exec('INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
    'request-facts', 'perf:owner', 'request-facts', JSON.stringify({cwd:'/first',workspace:'/work',brief:'private brief',prompt:'private prompt'}),
    'machine','codex','headless','running','{"native_id":"native"}',now,now);
  const input = () => buildSessionInputs(h.sql,new Set(['request-facts']));
  const original = input();
  expect(original.refs.get('machine/native')).toMatchObject({cwd:'/first',has_brief:true});
  const compact = h.sql.exec("SELECT request_json FROM lifecycle_request_projection WHERE id='request-facts'").one().request_json;
  expect(compact).not.toContain('private');
  const change = () => h.sql.exec("UPDATE session_control_sessions SET request_json=? WHERE id='request-facts'",'{"cwd":"/second","brief":false}');
  expect(() => h.ctx.storage.transactionSync(() => {change();expect(input().refs.get('machine/native')?.cwd).toBe('/second');throw new Error('rollback');})).toThrow('rollback');
  expect(input()).toEqual(original);
  change();
  expect(input().refs.get('machine/native')).toMatchObject({cwd:'/second',has_brief:false});
  // Legacy duplicate keys retain JSON.parse last-key semantics.
  h.sql.exec("UPDATE session_control_sessions SET request_json=? WHERE id='request-facts'",'{"cwd":"/old","cwd":"/last","brief":true}');
  expect(input().refs.get('machine/native')?.cwd).toBe('/last');
  for(const raw of ['not-json','null','[]','false','{"cwd":{"nested":true}}','{"brief":false,"brief":"yes"}','{"brief":"yes","brief":false}']) {
    h.sql.exec("UPDATE session_control_sessions SET request_json=? WHERE id='request-facts'",raw);
    const outcome=()=>{try{return input();}catch(error){return {error:String(error)};}};
    const projected=outcome();
    h.sql.exec("DELETE FROM lifecycle_request_projection WHERE id='request-facts'");
    expect(outcome()).toEqual(projected);
    initLifecycleRequestProjection(h.sql);
    expect(outcome()).toEqual(projected);
  }
  h.sql.exec("UPDATE session_control_sessions SET id='renamed' WHERE id='request-facts'");
  expect(h.sql.exec("SELECT id FROM lifecycle_request_projection").toArray()).toEqual([{id:'renamed'}]);
  h.sql.exec("UPDATE session_control_sessions SET id='request-facts' WHERE id='renamed'");
  h.sql.exec('DROP TABLE lifecycle_request_projection');
  const fallback = input();
  initLifecycleRequestProjection(h.sql);
  expect(input()).toEqual(fallback);
  h.sql.exec("DELETE FROM session_control_sessions WHERE id='request-facts'");
  expect(h.sql.exec('SELECT * FROM lifecycle_request_projection').toArray()).toEqual([]);
}));
