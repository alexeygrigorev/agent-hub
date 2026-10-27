import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { IntakeService } from '../src/intake';
import { SESSION_CONTROL_SCHEMA } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const host = (id = 'hetzner') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const stamp = new Date().toISOString();
async function fixture(fn: (service: FleetService, sql: any, launch: string, task: string, session: string) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('terminal-enrichment-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb));
    service.init();
    new IntakeService(hub.sql, cb => state.storage.transactionSync(cb)).init();
    for (const statement of SESSION_CONTROL_SCHEMA.split(';')) if (statement.trim()) hub.sql.exec(statement);
    const launch = crypto.randomUUID(), task = crypto.randomUUID(), session = crypto.randomUUID();
    hub.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', task, JSON.stringify({ id: task, project: 'agent-hub',
      title: 'Report stop result', status: 'open', version: 1, created_at: Date.now(), updated_at: Date.now(),
      acceptance_criteria: 'Preserve stopped lifecycle and append failed partial-work diagnostics.', history: [] }));
    hub.sql.exec(`INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,
      machine_id,engine,account,mode,workspace,prompt,state,native_id,launcher_task_id,
      verification_status,finished_at,created_at,updated_at)
      VALUES (?,'tester','agent','audit',?,'{}','hetzner','opencode','go','headless','/demo','audit',
      'exited','native-1',?,'verified',?,?,?)`, launch, launch, 'hub-' + launch, stamp, stamp, stamp);
    hub.sql.exec(`INSERT INTO session_control_sessions (id,actor,request_key,request_json,fleet_launch_id,
      machine_id,engine,mode,state,created_at,updated_at) VALUES (?,'tester',?,?,?,'hetzner','opencode',
      'headless','stopped',?,?)`, session, session, JSON.stringify({ intake_task_id: task }), launch, stamp, stamp);
    await fn(service, hub.sql, launch, task, session);
  });
}
function payload(launch: string) {
  return { state: 'failed', confirmed_exit: true, native_id: 'native-1', launcher_task_id: 'hub-' + launch,
    report_status: 'failed', report: 'Failed with partial work; no final JSON.', artifacts: [],
    diagnostic: { reason: 'engine_exit_code', message: 'Exited without result.', error_type: 'RuntimeError', stage: 'runtime', exit_code: 0 },
    runtime: { exit_code: 0, exit_signal: null, wall_time_ms: 61558, log_tail: 'Completed part of the audit.',
      engine_outcome: null, observed_commits: [], dirty_paths: [], outcome: 'failed-with-partial-work' } };
}
function post(service: FleetService, launch: string, value: any, caller = host()) {
  return service.handle(new Request('https://hub.test/api/fleet/launches/' + launch + '/result',
    { method: 'POST', body: JSON.stringify(value) }), caller);
}

describe('existing exact terminal replay contract for stopped headless work', () => {
  it('reproduces the failed/exited conflict and enriches without lifecycle or duplicate history effects', () => fixture(async (service, sql, launch, task, session) => {
    const failed = payload(launch);
    const before = sql.exec('SELECT * FROM fleet_launches WHERE id=?', launch).one();
    const conflict = await post(service, launch, failed);
    expect(conflict.status).toBe(409);
    expect(await conflict.json()).toEqual({ error: 'invalid_transition' });
    const { diagnostic: _diagnostic, ...enrichment } = { ...failed, state: 'exited' };
    expect((await post(service, launch, enrichment)).status).toBe(200);
    expect((await post(service, launch, enrichment)).status).toBe(200); // Lost reply replay.
    const after = sql.exec('SELECT * FROM fleet_launches WHERE id=?', launch).one();
    for (const key of ['state', 'finished_at', 'verification_status', 'native_id', 'launcher_task_id', 'exit_reason']) {
      expect(after[key]).toEqual(before[key]);
    }
    expect(JSON.parse(after.runtime_json)).toEqual(failed.runtime);
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?', session).one().state).toBe('stopped');
    const data = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', task).one().data);
    expect(data.status).toBe('open');
    const reports = data.history.filter((entry: any) => entry.actor === 'hub:service:fleet-finish');
    expect(reports).toHaveLength(1);
    expect(reports[0].text).toContain(session);
    expect(reports[0].text).toContain('(exited/failed, launch ' + launch + ')');
    expect(reports[0].text).toContain('Failed with partial work');
  }));

  it('refuses foreign host/native/task receipts and routed tasks before enrichment', () => fixture(async (service, sql, launch, task) => {
    const { diagnostic: _diagnostic, ...enrichment } = { ...payload(launch), state: 'exited' };
    for (const [change, status] of [[{ native_id: 'foreign' }, 409], [{ launcher_task_id: 'foreign-task' }, 409], [{ confirmed_exit: false }, 400]] as const) {
      expect((await post(service, launch, { ...enrichment, ...change })).status).toBe(status);
    }
    expect((await post(service, launch, enrichment, host('foreign'))).status).toBe(404);
    sql.exec(`INSERT INTO routing_jobs (id,actor,request_key,request_json,launch_id,state,created_at,updated_at)
      VALUES ('routing','tester','routing','{}',?,'running',?,?)`, launch, stamp, stamp);
    expect((await post(service, launch, enrichment)).status).toBe(409);
    expect(sql.exec('SELECT runtime_json FROM fleet_launches WHERE id=?', launch).one().runtime_json).toBeNull();
    expect(JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', task).one().data).history).toEqual([]);
  }));
});
