import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { IntakeService } from '../src/intake';
import { SESSION_CONTROL_SCHEMA } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
const LAUNCH = /^[a-f0-9-]{36}$/;
const uuid = () => crypto.randomUUID();

async function setup(fn: (s: FleetService, intake: IntakeService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-finish-' + uuid()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const s = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    s.init();
    const intake = new IntakeService(hub.sql, (cb) => state.storage.transactionSync(cb));
    intake.init();
    for (const stmt of SESSION_CONTROL_SCHEMA.split(';')) if (stmt.trim()) hub.sql.exec(stmt);
    await fn(s, intake, hub.sql);
  });
}

function seedIntake(sql: any, taskId: string) {
  const task = {
    id: taskId, project: 'agent-hub', status: 'open', version: 1,
    created_at: now, updated_at: now, title: 't', current_state: null,
    next_step: null, owner: null, uncertainty: null, acceptance_criteria: 'ac',
    scope: null, history: [{
      id: uuid(), kind: 'intake', actor: 'tester', captured_at: now,
      text: 'seed', authority: 'untrusted_task_data',
    }],
  };
  sql.exec('INSERT INTO intake_tasks(id, data) VALUES(?, ?)', taskId, JSON.stringify(task));
  sql.exec("INSERT OR IGNORE INTO intake_meta(id, revision) VALUES(1, 0)");
}

function seedLaunch(sql: any, launchId: string, sessionId: string, taskId: string) {
  sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json,
    machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'tester', 'agent', NULL, 's', ?, '{}', 'hetzner', 'opencode', 'shared-codex', NULL,
    'headless', 'demo', 'prompt', 'running', 'native-1', ?, ?, ?)`,
    launchId, 'key-' + launchId, 'hub-' + launchId, iso(), iso());
  sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id,
    machine_id, engine, mode, state, created_at, updated_at)
    VALUES (?, 'tester', ?, ?, ?, 'hetzner', 'opencode', 'headless', 'running', ?, ?)`,
    sessionId, 'sk-' + sessionId,
    JSON.stringify({ intake_task_id: taskId, brief: 'b' }),
    launchId, iso(), iso());
}

function history(sql: any, taskId: string): any[] {
  const row: any = sql.exec('SELECT data FROM intake_tasks WHERE id = ?', taskId).toArray()[0];
  return JSON.parse(row.data).history;
}

describe('fleet finish-report ingestion (intake 88b3260d AC1)', () => {
  it('redacts quoted secrets and cookies at runtime/report/artifact storage boundaries', async () => setup(async (s, _intake, sql) => {
    const launchId=uuid(), sessionId=uuid(), taskId=uuid();
    seedIntake(sql,taskId);seedLaunch(sql,launchId,sessionId,taskId);
    const secret='{"password":"short-test-value"}\nCookie: session=other-short-value';
    const payload={state:'exited',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId,
      report_status:'done',report:secret,artifacts:[{label:secret,text:secret}],
      runtime:{exit_code:0,exit_signal:null,wall_time_ms:1000,log_tail:secret,engine_outcome:'succeeded',observed_commits:[],dirty_paths:[],outcome:'reported'}};
    expect((await s.handle(request('/launches/'+launchId+'/result',payload),host('hetzner'))).status).toBe(200);
    const stored=sql.exec('SELECT runtime_json FROM fleet_launches WHERE id=?',launchId).one().runtime_json;
    for(const value of [stored,JSON.stringify(history(sql,taskId))]) {
      expect(value).not.toContain('short-test-value');expect(value).not.toContain('other-short-value');
    }
  }));
  it('replays an exact diagnostic after a lost response despite journal key order', async () => setup(async (s, _intake, sql) => {
    const launchId=uuid(),sessionId=uuid(),taskId=uuid();seedIntake(sql,taskId);seedLaunch(sql,launchId,sessionId,taskId);
    const diagnostic={reason:'engine_auth_expired',message:'Engine authentication expired.',error_type:'AuthenticationError',stage:'launcher_run'};
    const payload={state:'failed',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId,diagnostic,
      report_status:'failed',report:'Authentication failed before completion',artifacts:[]};
    const path='/launches/'+launchId+'/result';
    expect((await s.handle(request(path,payload),host('hetzner'))).status).toBe(200);
    const reordered=Object.fromEntries(Object.entries(diagnostic).sort(([a],[b])=>a.localeCompare(b)));
    expect((await s.handle(request(path,{...payload,diagnostic:reordered}),host('hetzner'))).status).toBe(200);
    expect((await s.handle(request(path,{...payload,diagnostic:{...diagnostic,stage:'runtime'}}),host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(path,{...payload,native_id:'another'}),host('hetzner'))).status).toBe(409);
    expect(JSON.parse(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id=?',launchId).one().launch_error_json)).toEqual(diagnostic);
    expect(history(sql,taskId).filter(e=>e.actor==='hub:service:fleet-finish')).toHaveLength(1);
  }));
  it('stores sanitized exit evidence on success and failure without changing replay identity', async () => setup(async (s, _intake, sql) => {
    for (const state of ['exited', 'failed']) {
      const launchId = uuid(), sessionId = uuid(), taskId = uuid();
      seedIntake(sql, taskId);
      seedLaunch(sql, launchId, sessionId, taskId);
      const payload = { state, confirmed_exit: true, native_id: 'native-1', launcher_task_id: 'hub-' + launchId,
        runtime: { exit_code: state === 'exited' ? 0 : 17, exit_signal: null, wall_time_ms: 1234,
          log_tail: 'Bearer sensitive-value', engine_outcome: null, observed_commits: [], dirty_paths: [], outcome: 'reported' } };
      expect((await s.handle(request(`/launches/${launchId}/result`, payload), host('hetzner'))).status).toBe(200);
      const stored = JSON.parse(sql.exec('SELECT runtime_json FROM fleet_launches WHERE id=?', launchId).one().runtime_json);
      expect(stored.wall_time_ms).toBe(1234);
      expect(stored.log_tail).not.toContain('sensitive-value');
      expect((await s.handle(request(`/launches/${launchId}/result`, payload), host('hetzner'))).status).toBe(200);
      expect((await s.handle(request(`/launches/${launchId}/result`, { ...payload, native_id: 'other' }), host('hetzner'))).status).toBe(409);
    }
  }));
  it('accepts maximum bounded Unicode evidence after Python-style JSON escaping',async()=>setup(async(s,_intake,sql)=>{
    const launchId=uuid(),sessionId=uuid(),taskId=uuid();seedIntake(sql,taskId);seedLaunch(sql,launchId,sessionId,taskId);
    const payload={state:'exited',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId,
      report_status:'done',report:'work 😀 '.repeat(500),artifacts:Array.from({length:8},()=>({label:'😀'.repeat(60),text:'😀'.repeat(512)})),
      runtime:{exit_code:0,exit_signal:null,wall_time_ms:1000,log_tail:'😀'.repeat(1024),engine_outcome:'succeeded',observed_commits:[],dirty_paths:Array.from({length:16},()=>'😀'.repeat(128)),outcome:'reported'}};
    const wire=JSON.stringify(payload).replace(/[^\x00-\x7f]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
    expect(new TextEncoder().encode(wire).length).toBeGreaterThan(32768);
    expect(new TextEncoder().encode(wire).length).toBeLessThan(131072);
    const res=await s.handle(new Request('https://hub.test/api/fleet/launches/'+launchId+'/result',{method:'POST',body:wire}),host('hetzner'));
    expect(res.status,await res.clone().text()).toBe(200);
    expect(history(sql,taskId).some(e=>e.actor==='hub:service:fleet-finish')).toBe(true);
  }));

  it('appends a Hub-authored report to the linked intake on terminal result', async () => setup(async (s, _intake, sql) => {
    const launchId = uuid(), sessionId = uuid(), taskId = uuid();
    seedIntake(sql, taskId);
    seedLaunch(sql, launchId, sessionId, taskId);
    const res = await s.handle(request(`/launches/${launchId}/result`, {
      state: 'exited', confirmed_exit: true, native_id: 'native-1',
      launcher_task_id: 'hub-' + launchId,
      report_status: 'done', report: 'All checks green, pushed to main.',
      artifacts: [{ label: 'sha', text: 'abc123' }],
    }), host('hetzner'));
    expect(res.status).toBe(200);
    const h = history(sql, taskId);
    const reports = h.filter((e: any) => e.kind === 'report' && e.actor === 'hub:service:fleet-finish');
    expect(reports).toHaveLength(1);
    expect(reports[0].text).toContain(sessionId);
    expect(reports[0].text).toContain('All checks green');
    expect(reports[0].authority).toBe('hub_launch_verified');
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id = ?", sessionId).one().state).toBe('stopped');
    // The intake is never closed by a finish report.
    expect(JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id = ?', taskId).toArray()[0].data).status).toBe('open');
  }));

  it('covers waiting-on-human, stuck and failed worker statuses', async () => setup(async (s, _intake, sql) => {
    for (const workerStatus of ['waiting-on-human', 'stuck', 'failed']) {
      const launchId = uuid(), sessionId = uuid(), taskId = uuid();
      seedIntake(sql, taskId);
      seedLaunch(sql, launchId, sessionId, taskId);
      const fleetState = workerStatus === 'failed' ? 'failed' : 'exited';
      const res = await s.handle(request(`/launches/${launchId}/result`, {
        state: fleetState, confirmed_exit: true, native_id: 'native-1',
        launcher_task_id: 'hub-' + launchId,
        report_status: workerStatus, report: 'status ' + workerStatus,
      }), host('hetzner'));
      expect(res.status).toBe(200);
      const reports = history(sql, taskId).filter((e: any) => e.kind === 'report' && e.actor === 'hub:service:fleet-finish');
      expect(reports).toHaveLength(1);
      expect(reports[0].text).toContain(workerStatus);
    }
  }));

  it('is idempotent: replay finds the existing sourceRef and skips', async () => setup(async (s, _intake, sql) => {
    const launchId = uuid(), sessionId = uuid(), taskId = uuid();
    seedIntake(sql, taskId);
    seedLaunch(sql, launchId, sessionId, taskId);
    const payload = {
      state: 'exited', confirmed_exit: true, native_id: 'native-1',
      launcher_task_id: 'hub-' + launchId, report_status: 'done', report: 'once',
    };
    expect((await s.handle(request(`/launches/${launchId}/result`, payload), host('hetzner'))).status).toBe(200);
    const replay = await s.handle(request(`/launches/${launchId}/result`, payload), host('hetzner'));
    expect(replay.status).toBe(200);
    const reports = history(sql, taskId).filter((e: any) => e.kind === 'report' && e.actor === 'hub:service:fleet-finish');
    expect(reports).toHaveLength(1);
  }));

  it('redacts bearer/gh tokens that slipped through host sanitization', async () => setup(async (s, _intake, sql) => {
    const launchId = uuid(), sessionId = uuid(), taskId = uuid();
    seedIntake(sql, taskId);
    seedLaunch(sql, launchId, sessionId, taskId);
    const res = await s.handle(request(`/launches/${launchId}/result`, {
      state: 'failed', confirmed_exit: true, native_id: 'native-1',
      launcher_task_id: 'hub-' + launchId,
      report_status: 'failed', report: 'tried with ghp_abcdefghij1234567890 and done',
    }), host('hetzner'));
    expect(res.status).toBe(200);
    const reports = history(sql, taskId).filter((e: any) => e.kind === 'report' && e.actor === 'hub:service:fleet-finish');
    expect(reports).toHaveLength(1);
    expect(reports[0].text).not.toContain('ghp_abcdefghij1234567890');
    expect(reports[0].text).toContain('[REDACTED_TOKEN]');
  }));

  it('rejects oversized report payloads and non-terminal report fields', async () => setup(async (s, _intake, sql) => {
    const launchId = uuid(), sessionId = uuid(), taskId = uuid();
    seedIntake(sql, taskId);
    seedLaunch(sql, launchId, sessionId, taskId);
    const big = await s.handle(request(`/launches/${launchId}/result`, {
      state: 'exited', confirmed_exit: true, native_id: 'native-1',
      launcher_task_id: 'hub-' + launchId, report_status: 'done', report: 'x'.repeat(5000),
    }), host('hetzner'));
    expect(big.status).toBe(400);
    const manyArtifacts = await s.handle(request(`/launches/${launchId}/result`, {
      state: 'exited', confirmed_exit: true, native_id: 'native-1',
      launcher_task_id: 'hub-' + launchId, report_status: 'done', report: 'ok',
      artifacts: Array.from({ length: 9 }, (_, i) => ({ label: 'a' + i, text: 't' })),
    }), host('hetzner'));
    expect(manyArtifacts.status).toBe(400);
    const running = await s.handle(request(`/launches/${launchId}/result`, {
      state: 'running', native_id: 'native-1', launcher_task_id: 'hub-' + launchId,
      confirmed_exit: false, report_status: 'done',
    } as any), host('hetzner'));
    expect(running.status).toBe(400);
  }));

  it('truncates long reports and lists artifact labels', async () => setup(async (s, _intake, sql) => {
    const launchId = uuid(), sessionId = uuid(), taskId = uuid();
    seedIntake(sql, taskId);
    seedLaunch(sql, launchId, sessionId, taskId);
    const res = await s.handle(request(`/launches/${launchId}/result`, {
      state: 'exited', confirmed_exit: true, native_id: 'native-1',
      launcher_task_id: 'hub-' + launchId, report_status: 'done', report: 'word '.repeat(600),
      artifacts: [{ label: 'commit-sha', text: 'abc' }, { label: 'run-link', text: 'u' }],
    }), host('hetzner'));
    expect(res.status).toBe(200);
    const reports = history(sql, taskId).filter((e: any) => e.kind === 'report' && e.actor === 'hub:service:fleet-finish');
    expect(reports).toHaveLength(1);
    expect(reports[0].text).toContain('…');
    expect(reports[0].text).toContain('Artifacts: commit-sha, run-link');
  }));

  it('no-ops without an intake link and never closes the intake', async () => setup(async (s, _intake, sql) => {
    const launchId = uuid(), sessionId = uuid();
    seedIntake(sql, uuid());
    sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json,
      machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
      VALUES (?, 'tester', 'agent', NULL, 's', ?, '{}', 'hetzner', 'opencode', 'shared-codex', NULL,
      'headless', 'demo', 'prompt', 'running', 'native-1', ?, ?, ?)`,
      launchId, 'key-' + launchId, 'hub-' + launchId, iso(), iso());
    sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id,
      machine_id, engine, mode, state, created_at, updated_at)
      VALUES (?, 'tester', ?, ?, ?, 'hetzner', 'opencode', 'headless', 'running', ?, ?)`,
      sessionId, 'sk-' + sessionId, JSON.stringify({ brief: 'no intake link' }), launchId, iso(), iso());
    const res = await s.handle(request(`/launches/${launchId}/result`, {
      state: 'exited', confirmed_exit: true, native_id: 'native-1',
      launcher_task_id: 'hub-' + launchId, report_status: 'done', report: 'orphan',
    }), host('hetzner'));
    expect(res.status).toBe(200);
  }));
  it('preserves a prior bare receipt and appends a late worker report exactly once', async () => setup(async (s,_intake,sql)=>{
    const launchId=uuid(),sessionId=uuid(),taskId=uuid();seedIntake(sql,taskId);seedLaunch(sql,launchId,sessionId,taskId);
    const payload={state:'exited',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId};
    expect((await s.handle(request(`/launches/${launchId}/result`,payload),host('hetzner'))).status).toBe(200);
    const first=history(sql,taskId).find(e=>e.actor==='hub:service:fleet-finish');
    expect(first.text).toContain('No worker report file');
    const real={...payload,report_status:'waiting-on-human',report:'Real report arrived'};
    expect((await s.handle(request(`/launches/${launchId}/result`,real),host('hetzner'))).status).toBe(200);
    expect((await s.handle(request(`/launches/${launchId}/result`,{...real,report:'changed replay'}),host('hetzner'))).status).toBe(200);
    const reports=history(sql,taskId).filter(e=>e.actor==='hub:service:fleet-finish');
    expect(reports).toHaveLength(2);expect(reports[0]).toEqual(first);expect(reports[1].text).toContain('Real report arrived');expect(reports[1].text).not.toContain('changed replay');
    expect(JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?',taskId).one().data).status).toBe('open');
  }));
  it('worker-authored source references cannot suppress a canonical finish report',async()=>setup(async(s,_intake,sql)=>{
    const launchId=uuid(),sessionId=uuid(),taskId=uuid();seedIntake(sql,taskId);seedLaunch(sql,launchId,sessionId,taskId);
    const t=JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?',taskId).one().data);
    t.history.push({id:uuid(),kind:'note',actor:'agent:worker',authority:'untrusted_task_data',source:{reference:'fleet-finish:'+launchId},text:'pretend it was already forwarded'});
    sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',JSON.stringify(t),taskId);
    await s.handle(request(`/launches/${launchId}/result`,{state:'exited',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId,report_status:'done',report:'Actual worker report'}),host('hetzner'));
    expect(history(sql,taskId).filter(e=>e.actor==='hub:service:fleet-finish')).toHaveLength(1);
  }));
  it('resolves only unique literal historical intake prefixes',async()=>setup(async(s,_intake,sql)=>{
    const launchId=uuid(),sessionId=uuid(),taskId=uuid();seedIntake(sql,taskId);seedLaunch(sql,launchId,sessionId,taskId.slice(0,8));
    await s.handle(request(`/launches/${launchId}/result`,{state:'exited',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId,report_status:'done',report:'Prefix report'}),host('hetzner'));
    expect(history(sql,taskId).find(e=>e.actor==='hub:service:fleet-finish')?.text).toContain('Prefix report');
  }));
  it('does not resolve an ambiguous historical intake prefix',async()=>setup(async(s,_intake,sql)=>{
    const one='12345678-1111-4111-8111-111111111111',two='12345678-2222-4222-8222-222222222222';
    const launchId=uuid(),sessionId=uuid();seedIntake(sql,one);seedIntake(sql,two);seedLaunch(sql,launchId,sessionId,'12345678');
    await s.handle(request(`/launches/${launchId}/result`,{state:'exited',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId,report_status:'done',report:'Ambiguous link'}),host('hetzner'));
    expect(history(sql,one)).toHaveLength(1);expect(history(sql,two)).toHaveLength(1);
  }));

  it('redacts worker text and artifact-label secrets before clipping while preserving commit evidence',async()=>setup(async(s,_intake,sql)=>{
    const launchId=uuid(),sessionId=uuid(),taskId=uuid(),commit='a'.repeat(40);seedIntake(sql,taskId);seedLaunch(sql,launchId,sessionId,taskId);
    await s.handle(request(`/launches/${launchId}/result`,{state:'failed',confirmed_exit:true,native_id:'native-1',launcher_task_id:'hub-'+launchId,report_status:'failed',report:'commit '+commit+' hubt_private-value',artifacts:[{label:'hubr_private-value',text:'not stored'}]}),host('hetzner'));
    const text=history(sql,taskId).find(e=>e.actor==='hub:service:fleet-finish').text;
    expect(text).toContain(commit);expect(text).toContain(sessionId);expect(text).not.toContain('private-value');
  }));

});
