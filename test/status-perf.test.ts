import { freshFixtureBudgetDay, fixtureSql } from './helpers/fixture-budget';
import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';
import { FleetService } from '../src/fleet';
import { normalizeRoute } from '../src/rows-meter';
import { freshCpu } from './cpu-fixture';

const inDO: any = runInDurableObject;
const sessionId = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;

// Wall-clock samples remain useful diagnostics, but host scheduling under load
// makes them unsuitable as a CI gate. The gate below uses deterministic SQL
// calls and rows-read work from the same retained production-scale fixture.
const SAMPLES = 100;
const quantile = (samples: number[], percent: number) => samples.slice().sort((a,b)=>a-b)[Math.ceil(samples.length*percent/100)-1];
const p95 = (samples: number[]) => quantile(samples,95);
const p50 = (samples: number[]) => quantile(samples,50);
function report(name: string, samples: number[]) {
  console.log(`latency diagnostic ${name} n=${samples.length} p50=${p50(samples)}ms p95=${p95(samples)}ms max=${Math.max(...samples)}ms first20_p95=${p95(samples.slice(0,20))}ms`);
}
async function rowMeterSnapshot(stub: any) {
  return inDO(stub, (h: any) => h.meter.snapshot());
}
function routeWork(before: any, after: any, method: string, path: string) {
  const scope = `route:${normalizeRoute(method, new URL(path, 'https://hub.test').pathname)}`;
  const start = before[scope] ?? { calls: 0, rows_read: 0, rows_written: 0, max_read: 0 };
  const end = after[scope] ?? { calls: 0, rows_read: 0, rows_written: 0, max_read: 0 };
  return { calls: end.calls - start.calls, rows_read: end.rows_read - start.rows_read, rows_written: end.rows_written - start.rows_written };
}
it('bounds status and host query work with retained production-scale history', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('hub'));
  const cookie = (await createSessionCookie(env as any, { sub: 'perf-operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'perf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  const token = 'hubt_perf_synthetic_host';
  const hash = await sha256(token);
  await inDO(stub, async (h: any, state:any) => {
    await freshFixtureBudgetDay(h);
    const sql=fixtureSql(h,state);
    const now = new Date().toISOString();
    h.ctx.storage.transactionSync(() => {
      sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('perf-host','perf-host','perf-host',?,?,0)", now, now);
      sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        'perf-credential', 'host', hash, 'perf-family', 'host:perf-host', 'perf-host', 'host:perf-host', '["host"]', 'perf-operator', now, new Date(Date.now() + 86400000).toISOString());
      sql.exec("UPDATE machines SET credential_id='perf-credential' WHERE id='perf-host'");
      sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
        'perf-owner','agent','synthetic-owner-hash','perf-owner-family','perf-owner','perf:owner','[]','perf-operator',now,new Date(Date.now()+86400000).toISOString());
      for (let i = 0; i < 500; i++) {
        const id = `perf-task-${String(i).padStart(4, '0')}`;
        const history = Array.from({length: 40}, (_, j) => ({ kind: 'note', text: 'Synthetic retained history. '.repeat(20), captured_at: Date.now(), id: `${id}-${j}` }));
        sql.exec('INSERT INTO intake_tasks(id,data) VALUES (?,?)', id, JSON.stringify({ id, project: 'perf', title: id, status: 'open', version: 1, created_at: Date.now(), updated_at: Date.now(), history: [{kind: 'intake', text: 'Original request. '.repeat(480), source: {reference: null}}, ...history, ...(i % 2 === 0 ? [{kind: 'link', hub_session_id: sessionId(i + 500)}] : []), {kind: 'link', hub_session_id: sessionId(i)}] }));
      }
      for (let i = 0; i < 2000; i++) {
        sql.exec('INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
          sessionId(i), 'perf:owner', `perf-${i}`, JSON.stringify({prompt:'Retained synthetic prompt. '.repeat(320),intake_task_id:`perf-task-${String(i%500).padStart(4,'0')}`}), 'perf-host', 'codex', 'headless', i < 100 ? 'running' : 'stopped', JSON.stringify({native_id: `native-${i}`,tag:`perf-${i}`,workspace:'/synthetic'}), now, now);
        sql.exec('INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace) VALUES (?,?,?,?,?,?,?,?)',
          'perf-host', `native-${i}`, `perf-${i}`, 'codex', i < 100 ? 'running' : i < 156 ? 'exited' : 'stopped', 'working', new Date(Date.now()-3600000).toISOString(), '/synthetic');
        sql.exec('INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          sessionId(i + 10000), 'perf:owner', 'agent', 'Synthetic', `perf-launch-${i}`, JSON.stringify({prompt:'Retained synthetic prompt. '.repeat(320),intake_task_id:`perf-task-${String(i%500).padStart(4,'0')}`}), 'perf-host', 'codex', 'default', 'headless', '/synthetic', 'Synthetic', i < 100 ? 'running' : i < 156 ? 'uncertain' : 'stopped', `native-${i}`, now, now);
        for (let j = 0; j < 3; j++) {
          const at = new Date(Date.now() - (3-j)*1000).toISOString();
          sql.exec('INSERT INTO status_observations(id,machine_id,hub_session_id,kind,observed_at,payload_json,reporter,received_at) VALUES (?,?,?,?,?,?,?,?)',
            `obs-${i}-${j}`, 'perf-host', sessionId(i), 'session', at, '{"state":"working"}', 'perf-host', at);
          sql.exec('INSERT INTO session_lifecycle_decisions(session_key,decision_id,kind,actor,reason,created_at) VALUES (?,?,?,?,?,?)',
            `perf-host/native-${i}`, `decision-${i}-${j}`, 'resume_on_reset', 'perf:owner', 'Synthetic', at);
        }
      }
      sql.exec("UPDATE session_control_sessions SET owner_token_id='perf-owner',owner_family='perf-owner-family',session_type='headless' WHERE actor='perf:owner'");
      for(let i=0;i<850;i++) sql.exec('INSERT INTO session_lifecycle_observations VALUES(?,?,?,?,?)',
        `perf-host/native-${i}`,'perf-host',`native-${i}`,now,'{"waiting_input":false,"report_written":false}');
      sql.exec('INSERT INTO fleet_catalog_seen VALUES (?,?)','perf-host',now);
      // Keep 56 genuine reconciliation rows after excluding unchanged live
      // natives, with production-sized bindings to exercise the batched lookup.
      for(let i=100;i<156;i++) sql.exec('UPDATE session_control_sessions SET fleet_launch_id=?,binding_json=? WHERE id=?',
        sessionId(i+10000),JSON.stringify({native_id:`native-${i}`,tag:`perf-${i}`,workspace:'/synthetic',detail:'x'.repeat(200)}),sessionId(i));
      // Retention permits 200 disk observations per host / 1000 total.
      // Trend reads must not repeatedly materialize top-consumer histories.
      for (let host = 0; host < 5; host++) {
        const machine = host === 0 ? 'perf-host' : `history-host-${host}`;
        if (host) sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES(?,?,?,?,?,0)', machine, machine, machine, now, now);
        sql.exec('INSERT INTO host_health_observations VALUES(?,?,?,?,?,?)', machine, now, Date.now(), now,
          JSON.stringify({memory:{total_bytes:64000000000,available_bytes:32000000000,used_bytes:32000000000,percent:50,status:'ok'},services:[]}), machine);
        for (let i = 0; i < 200; i++) {
          const at = Date.now() - (199-i)*1800000;
          const used = 40000000000 + (i < 100 ? i : i-100)*1000000;
          const filesystems = ['/', '/data', '/backup'].map(mount => ({mount_point:mount,device_id:mount,is_primary:true,total_bytes:100000000000,
            used_bytes:used,free_bytes:100000000000-used,percent:used/1000000000,status:'ok',
            top_consumers:Array.from({length:20},(_,j)=>({path:`/synthetic/${j}/`+'x'.repeat(230),bytes:1000000}))}));
          sql.exec('INSERT INTO disk_observations VALUES(?,?,?,?,?,?,?)', `${machine}-${i}`,machine,new Date(at).toISOString(),at,now,JSON.stringify({filesystems}),machine);
        }
      }
    });
  });
  // Match production Server-Timing hub;dur: DO fetch through response headers,
  // including queueing/auth/handler JSON serialization, excluding client body
  // transfer and parsing. Every body is still consumed and validated below.
  const cases: [string, string, boolean, any, number][] = [
    ['GET', '/api/status', false, undefined, 200],
    ['POST', '/api/status', false, {}, 200],
    ['GET', '/api/sessions?limit=1', false, undefined, 200],
    ['POST', '/api/host/poll', true, {type: 'heartbeat'}, 200],
    ['POST', '/api/machines/register', true, {hostname: 'perf-host', os: 'linux', network: 'synthetic', roles: [], capabilities: {}}, 200],
    ['GET', '/api/fleet/offers', true, undefined, 200],
    ['GET', '/api/fleet/resources/watchdog', true, undefined, 503],
    ['POST', '/api/fleet/resources/report', true, {samples:[],reaps:[]}, 200],
    // Real retained launch: replay claims must promptly refuse a terminal row.
    ['POST', `/api/fleet/launches/${sessionId(11999)}/claim`, true, {}, 409],
  ];
  const reconciledIds=new Set<string>();
  const workBefore = await rowMeterSnapshot(stub);
  for (const [method, path, host, payload, status] of cases) {
    const times: number[] = [];
    const clientTimes: number[] = [];
    for (let i = 0; i < SAMPLES; i++) {
      // Each independent latency sample has the unchanged strict daily guard.
      await inDO(stub, (h:any) => freshFixtureBudgetDay(h));
      const start = performance.now();
      const response = await stub.fetch('https://hub.test' + path, { method, headers: host ? {Authorization: 'Bearer ' + token} : {Cookie: cookie, 'x-csrf-token': 'perf'}, ...(payload ? {body: JSON.stringify(payload)} : {}) });
      times.push(performance.now() - start);
      const body: any = await response.json();
      clientTimes.push(performance.now() - start);
      expect(response.status, path+' '+JSON.stringify(body).slice(0,200)).toBe(status);
      if(path==='/api/fleet/resources/watchdog') {
        //100controls +156Fleetidentities exceed the strict128identity closure.
        //This retained-load benchmark must refuse unknown destructive authority.
        expect(body).toMatchObject({error:'resource_catalog_observation_limit',observation_pending:true});
        expect(body.sessions).toBeUndefined();
      }
      if(path === '/api/fleet/offers') {
        expect(body.reconciliation.length).toBeGreaterThan(0);expect(body.reconciliation.length).toBeLessThanOrEqual(25);
        expect(body.reconciliation_observation_pending).toBe(true);expect(body.observation_pending).toBe(true);
        expect(body.reconciliation_cursors).toHaveProperty('uncertain');
        for(const item of body.reconciliation)reconciledIds.add(item.id);
        expect(body.reconciliation[0].binding.detail).toHaveLength(200);
      }
      if (path === '/api/status') {
        expect(response.headers.get('X-Hub-Status-Projection')).toBe('resource-disk-v6');
        expect(body.tasks).toHaveLength(method === 'GET' ? 25 : 256);
        expect(body.tasks[0].hub_sessions[0].lifecycle.session_key).toBe('perf-host/native-0');
        expect(body.agents.running).toBe(100);
        expect(body.tasks[0].hub_sessions[0].host_observation.state).toBe('working');
        expect(body.tasks[0].hub_sessions[0].lifecycle.decision.kind).toBe('resume_on_reset');
      }
    }
    report(`DO fetch/headers ${method} ${path}`, times);
    report(`client body-inclusive ${method} ${path}`, clientTimes);
  }
  // Managed sessions share creator credentials. A census write invalidates
  // lifecycle facts; cold status must not re-query that creator per session.
  const coldPost:number[]=[];
  for(let i=0;i<SAMPLES;i++) {
    await inDO(stub,(h:any)=>freshFixtureBudgetDay(h));
    await inDO(stub,(h:any)=>h.sql.exec("UPDATE fleet_sessions SET last_activity_at=? WHERE machine_id='perf-host' AND native_id='native-0'",new Date(Date.now()+i).toISOString()).toArray());
    const start=performance.now();
    const response=await stub.fetch('https://hub.test/api/status',{method:'POST',headers:{Cookie:cookie,'x-csrf-token':'perf'},body:'{}'});
    coldPost.push(performance.now()-start);
    expect(response.status).toBe(200);await response.json();
  }
  report("DO fetch/headers POST status after native write", coldPost);
  expect(reconciledIds.size).toBe(56);
  // A real host census changes activity before watchdog. Warm-only reads
  // would miss expensive canonical projection rebuilds after that write.
  const invalidatedWatchdog:number[]=[];
  for(let i=0;i<SAMPLES;i++) {
    await inDO(stub,(h:any)=>freshFixtureBudgetDay(h));
    await inDO(stub,(h:any)=>h.sql.exec("UPDATE fleet_sessions SET last_activity_at=? WHERE machine_id='perf-host' AND native_id='native-0'",new Date(Date.now()+i).toISOString()).toArray());
    const start=performance.now();
    const response=await stub.fetch('https://hub.test/api/fleet/resources/watchdog',{headers:{Authorization:'Bearer '+token}});
    invalidatedWatchdog.push(performance.now()-start);
    expect(response.status).toBe(503);expect(await response.json()).toMatchObject({error:'resource_catalog_observation_limit',observation_pending:true});
  }
  report("DO fetch/headers bounded watchdog refusal after native write", invalidatedWatchdog);
  await inDO(stub,(h:any)=>expect(h.rowBudget.status()).toMatchObject({limit_total:20000,stopped:false}));
  // Exercise actual admission/transition work as well as terminal replay.
  // An independent healthy host avoids altering the retained workload above.
  const claimToken = 'hubt_perf_synthetic_claim_host';
  const claimHash = await sha256(claimToken);
  await inDO(stub, (h: any, state:any) => {
    const sql=fixtureSql(h,state);
    const now = new Date().toISOString();
    sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES (?,?,?,?,?,?,0,?)',
      'claim-host', 'claim-host', 'claim-host', now, now,
      JSON.stringify({mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(now), gate: {launch_allowed: true, at: now}}),
      JSON.stringify({engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8}));
    sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
      'claim-credential', 'host', claimHash, 'claim-family', 'host:claim-host', 'claim-host', 'host:claim-host', '["host"]', 'perf-operator', now, new Date(Date.now() + 86400000).toISOString());
    sql.exec("UPDATE machines SET credential_id='claim-credential' WHERE id='claim-host'");
    sql.exec("INSERT INTO fleet_accounts VALUES ('claim-account','codex',1,15,1)");
    sql.exec("INSERT INTO fleet_routes VALUES ('claim-host','codex','claim-account',NULL)");
    sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES (?,?,?,?,?,?,?,?,?)',
      'claim-host', '1.0', now, Date.now(), now, Date.now(), 'ok', null,
      JSON.stringify({codex: {status: 'ok', observed_at: now, details: {limit_reached: false}, windows: {weekly: {present: true, percent_remaining: 80, reset_at: new Date(Date.now() + 86400000).toISOString()}}}}));
  });
  const admittedTimes: number[] = [];
  const offeredTimes: number[] = [];
  for (let i = 0; i < SAMPLES; i++) {
    // Each reserve/offers/claim/stop flow is one independent latency sample.
    // Business state and every measured service/HTTP operation stay real.
    await inDO(stub,(h:any)=>freshFixtureBudgetDay(h));
    const launch = await inDO(stub, (h: any) => {
      const service = new FleetService(h.sql, (cb) => h.ctx.storage.transactionSync(cb));
      const result = service.reserve({workspace: 'demo', prompt: 'Synthetic performance claim', machine: 'claim-host', idempotency_key: `perf-claim-${i}`},
        {caller: {isAdmin: true, actor: 'perf-operator', kind: 'human'}, scopes: ['admin']});
      expect(result.status).toBe(201);
      return result.value;
    });
    let start = performance.now();
    const offers = await stub.fetch('https://hub.test/api/fleet/offers', {headers: {Authorization: 'Bearer ' + claimToken}});
    offeredTimes.push(performance.now() - start);
    const offerBody: any = await offers.json();
    expect(offers.status).toBe(200);
    expect(JSON.stringify(offerBody)).toContain(launch.id);
    start = performance.now();
    const claim = await stub.fetch(`https://hub.test/api/fleet/launches/${launch.id}/claim`, {
      method: 'POST', headers: {Authorization: 'Bearer ' + claimToken},
      body: JSON.stringify({gate_allowed: true, profile_supported: true, observed_at: new Date().toISOString(), account: 'claim-account', model: launch.selection.model}),
    });
    admittedTimes.push(performance.now() - start);
    const claimBody: any = await claim.json();
    expect(claim.status, JSON.stringify(claimBody)).toBe(200);
    expect(claimBody.state).toBe('starting');
    await inDO(stub, (h: any) => { h.sql.exec("UPDATE fleet_launches SET state='stopped' WHERE id=?", launch.id); });
  }
  for (const [name, times] of [['offers with live work', offeredTimes], ['successful claim', admittedTimes]] as const) {
    report(`DO fetch/headers ${name}`, times);
  }
  // A busy retained queue must not make every host poll re-plan its backlog.
  await inDO(stub,(h:any)=>freshFixtureBudgetDay(h));
  await inDO(stub, (h: any) => h.ctx.storage.transactionSync(() => {
    // Real placement reads measured history for every pending commitment.
    // Empty history and unroutable workloads hide multiplicative SQL reads.
    const at = new Date().toISOString();
    for (let i = 0; i < 2000; i++) {
      fixtureSql(h).exec(`INSERT INTO session_resource_usage(session_id,machine_id,native_id,project,engine,kind,started_at,finished_at,state,peak_rss_mb,observed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)`, `usage-${i}`, 'claim-host', `usage-${i}`, `project-${i % 20}`, 'codex', 'goal', at, at, 'stopped', 64 + i % 64, at);
      fixtureSql(h).exec('INSERT INTO session_cpu_usage VALUES(?,?,?)', `usage-${i}`, 0.01, at);
    }
    for (let i = 0; i < 36; i++) {
      fixtureSql(h).exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        SELECT ?,actor,requester_kind,summary,?,request_json,machine_id,engine,account,mode,workspace,prompt,'uncertain',created_at,updated_at
        FROM fleet_launches WHERE account='claim-account' AND state='stopped' LIMIT 1`, sessionId(40000+i), `perf-pending-${i}`);
    }
    for (let i = 0; i < 20; i++) {
      fixtureSql(h).exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        SELECT ?,actor,requester_kind,summary,?,request_json,machine_id,engine,account,mode,workspace,prompt,'offered',created_at,updated_at
        FROM fleet_launches WHERE account='claim-account' AND state='stopped' LIMIT 1`, sessionId(41000+i), `perf-offered-${i}`);
    }
    for (let i = 0; i < 6; i++) {
      fixtureSql(h).exec("INSERT INTO fleet_accounts VALUES (?,'codex',1,15,1)", `history-account-${i}`);
      fixtureSql(h).exec("INSERT INTO fleet_routes VALUES ('claim-host','codex',?,NULL)", `history-account-${i}`);
    }
    // Live account-tagged native history must reach the orphan correlation:
    // terminal-only history and an early memory refusal hide its scan cost.
    fixtureSql(h).exec("UPDATE fleet_accounts SET max_concurrency=1");
    fixtureSql(h).exec("UPDATE machines SET capabilities=json_set(capabilities,'$.cpu_count',1024),last_heartbeat=? WHERE id='claim-host'",
      JSON.stringify({mem_avail_mb:640000,disk_free_gb:1000,cpu_usage:freshCpu(at,1024),gate:{launch_allowed:true,at}}));
    const accounts = ['claim-account',...Array.from({length:6},(_,i)=>`history-account-${i}`)];
    for (const account of accounts) fixtureSql(h).exec("INSERT INTO fleet_subscriptions VALUES('claim-host',?,'codex','synthetic',?,?)",account,at,
      JSON.stringify({status:'ok',observed_at:at,details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:new Date(Date.now()+86400000).toISOString()}}}));
    for (let i=0;i<200;i++) {
      const account=accounts[i%accounts.length];
      fixtureSql(h).exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,account,last_activity_at) VALUES('claim-host',?,?,'codex',?,'working',?,'demo',?,?)",
        `active-${i}`,`active-${i}`,i<64?'running':'exited',at,account,at);
      if (i<36) fixtureSql(h).exec("UPDATE fleet_launches SET state='running',native_id=?,account=? WHERE id=?",`active-${i}`,account,sessionId(40000+i));
    }
    // Retain200native rows, with64actualactive observations. The36bound
    // launches +20offers bring the raw active source to120 and deduplicated
    // physical population to84: both remain below the128observation ceiling.
    // This high-core fixture uses a96-session host policy, reduced from500.
    fixtureSql(h).exec("INSERT INTO resource_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
      JSON.stringify({host_max_sessions:96}));
    // Use the production-configured service and its real cache graph. A
    // second bare service discarded those caches between this census proof
    // and planning; both observations still share the existing strict day.
    const diagnostic=(phase:string,fn:()=>any)=>{
      const scope='test:status-perf:'+phase,before=h.rowBudget.status();
      try { return h.meter.runSync(scope,fn); }
      finally {
        console.log(JSON.stringify({evidence:'status_perf_component_cost',phase,before,after:h.rowBudget.status(),
          native:h.meter.snapshot()[scope],statements:h.meter.statementTop(1500).filter((statement:any)=>statement.scope===scope)
            .sort((a:any,b:any)=>b.rows_read-a.rows_read).slice(0,10),
          excludes:'outer authentication and fixture seeding; guard ledger reported separately'}));
      }
    };
    const knownHost:any=diagnostic('known-host',()=>h.fleetService.resources.cpuHeadroom('claim-host'));
    expect(knownHost).toMatchObject({host_session_count:84,host_count_observation_limited:false});
    const fixturePlan:any=diagnostic('account-plan',()=>h.fleetService.plan({workspace:'demo',prompt:'Synthetic',machine:'claim-host'}));
    expect(fixturePlan.rejected.length===7 && fixturePlan.rejected.every((r:any)=>r.reason==='account_concurrency')).toBe(true);
    for (let i = 0; i < 200; i++) {
      fixtureSql(h).exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        SELECT ?,actor,requester_kind,summary,?,request_json,machine_id,engine,account,mode,workspace,prompt,'queued',created_at,updated_at
        FROM fleet_launches WHERE account='claim-account' AND state='stopped' LIMIT 1`, sessionId(20000+i), `perf-parked-${i}`);
    }
  }));
  await inDO(stub, (h: any) => h.ctx.storage.transactionSync(() => {
    for (let i=0;i<200;i++) fixtureSql(h).exec(`INSERT INTO routing_jobs(id,actor,request_key,request_json,state,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?)`,
      sessionId(30000+i),'perf:owner',`routing-backlog-${i}`,JSON.stringify({workspace:'demo',needs:['unavailable'],tasks:[],access:{mode:'read'}}),new Date().toISOString(),new Date().toISOString());
  }));
  const backlogTimes: number[] = [];
  for (let i = 0; i < SAMPLES; i++) {
    await inDO(stub,(h:any)=>freshFixtureBudgetDay(h));
    const start = performance.now();
    const response = await stub.fetch('https://hub.test/api/fleet/offers', {headers: {Authorization: 'Bearer ' + claimToken}});
    backlogTimes.push(performance.now() - start);
    const body: any = await response.json();
    expect(response.status).toBe(200);
    expect(body.offers).toHaveLength(20);
  }
  console.log('blocked offers ordered samples ms', JSON.stringify(backlogTimes));
  report("DO fetch/headers offers with blocked backlog", backlogTimes);
  // Competing requests exercise DO scheduling while both queues are retained.
  const mixed = [
    ['status','/api/status', {method:'POST',headers:{Cookie:cookie,'x-csrf-token':'perf'},body:'{}'}],
    ['sessions','/api/sessions?limit=1',{headers:{Cookie:cookie}}],
    ['offers','/api/fleet/offers',{headers:{Authorization:'Bearer '+claimToken}}],
    ['claim',`/api/fleet/launches/${sessionId(11999)}/claim`,{method:'POST',headers:{Authorization:'Bearer '+token},body:'{}'}],
  ] as const;
  const mixedTimes: number[][] = mixed.map(()=>[]);
  const cohortClientTimes: number[] = [];
  for(let i=0;i<SAMPLES;i++) {
    // One shared strict day covers the entire concurrent request cohort.
    await inDO(stub,(h:any)=>freshFixtureBudgetDay(h));
    const beforeCohort=await inDO(stub,(h:any)=>({ledger:h.rowBudget.status(),meter:h.meter.snapshot(),statements:h.meter.statementTop(1500)}));
    const cohortStart=performance.now();
    const responses=await Promise.all(mixed.map(async ([,path,options],j)=>{
      const start=performance.now();
      const response=await stub.fetch('https://hub.test'+path,options);
      mixedTimes[j].push(performance.now()-start);
      return response;
    }));
    // Client parsing must not delay timing callbacks for sibling requests.
    // Complete body validation for all four before issuing the next cohort.
    const bodies=await Promise.all(responses.map(response=>response.json())) as any[];
    if(responses.some((response,j)=>response.status!==(mixed[j][0]==='claim'?409:200))) {
      const after=await inDO(stub,(h:any)=>({ledger:h.rowBudget.status(),meter:h.meter.snapshot(),statements:h.meter.statementTop(1500)}));
      const beforeStatements=new Map(beforeCohort.statements.map((statement:any)=>[statement.scope+'\n'+statement.sql,statement]));
      const statements=after.statements.map((statement:any)=>{
        const prior:any=beforeStatements.get(statement.scope+'\n'+statement.sql);
        return {...statement,rows_read:statement.rows_read-(prior?.rows_read??0),rows_written:statement.rows_written-(prior?.rows_written??0),calls:statement.calls-(prior?.calls??0)};
      }).filter((statement:any)=>statement.calls>0).sort((a:any,b:any)=>b.rows_read-a.rows_read).slice(0,30);
      console.log(JSON.stringify({evidence:'status_perf_failed_cohort',cohort:i,before:beforeCohort.ledger,after:after.ledger,
        responses:responses.map((response,j)=>({name:mixed[j][0],status:response.status,error:bodies[j]?.error??null,reason:bodies[j]?.reason??null})),
        statements}));
    }
    for(const [j,response] of responses.entries())expect(response.status,mixed[j][0]).toBe(mixed[j][0]==='claim'?409:200);
    cohortClientTimes.push(performance.now()-cohortStart);
  }
  report("concurrent full cohort including client bodies", cohortClientTimes);
  for(const [j,[name]] of mixed.entries()) {
    const times=mixedTimes[j];
    report(`DO fetch/headers concurrent ${name}`, times);
  }
  const workAfter = await rowMeterSnapshot(stub);
  // Average per-request ceilings leave modest headroom over measured SQL
  // work for this fixture while failing on query fan-out or history scans.
  // GET /api/sessions is deliberately tight: losing the partial live index
  // makes its bounded active count walk all 2,000 retained sessions.
  const workRoutes: [string, string, number, number, number][] = [
    ['GET', '/api/status', SAMPLES * 2, 100, 1300],
    ['POST', '/api/status', SAMPLES * 3, 210, 5000],
    ['GET', '/api/sessions?limit=1', SAMPLES * 2, 28, 350],
    ['POST', '/api/host/poll', SAMPLES, 6, 8],
    ['POST', '/api/machines/register', SAMPLES, 7, 8],
    ['GET', '/api/fleet/offers', SAMPLES * 4, 95, 900],
    ['GET', '/api/fleet/resources/watchdog', SAMPLES * 2, 13, 5000],
    ['POST', '/api/fleet/resources/report', SAMPLES, 4, 2300],
    ['POST', `/api/fleet/launches/${sessionId(11999)}/claim`, SAMPLES * 3, 18, 260],
  ];
  const routeResults=workRoutes.map(([method,path,requests,maxCallsPerRequest,maxRowsPerRequest])=>({
    route:`${method} ${new URL(path,'https://hub.test').pathname}`,
    scope:`route:${normalizeRoute(method,new URL(path,'https://hub.test').pathname)}`,
    requests,maxCallsPerRequest,maxRowsPerRequest,work:routeWork(workBefore,workAfter,method,path),
  }));
  // Emit every route before any assertion stops the test, including call
  // breakdowns. A single failure must not hide the next route's evidence.
  for(const {route,scope,requests,maxCallsPerRequest,maxRowsPerRequest,work} of routeResults) {
    const rowsLimited=work.rows_read>requests*maxRowsPerRequest;
    if(rowsLimited || work.calls>requests*maxCallsPerRequest) {
      const statements=await inDO(stub,(h:any)=>h.meter.statementTop(1500)
        .filter((statement:any)=>statement.scope===scope)
        .sort((a:any,b:any)=>rowsLimited?b.rows_read-a.rows_read:b.calls-a.calls));
      console.log(JSON.stringify({evidence:'route_work_limit_exceeded',route,requests,work,
        limits:{calls:requests*maxCallsPerRequest,rows_read:requests*maxRowsPerRequest},statements}));
    }
    console.log(`route work ${route} requests=${requests} sql_calls=${work.calls} sql_calls_per_request=${(work.calls / requests).toFixed(1)} rows_read=${work.rows_read} rows_read_per_request=${(work.rows_read / requests).toFixed(1)}`);
  }
  for(const {route,requests,maxCallsPerRequest,maxRowsPerRequest,work} of routeResults) {
    expect(work.calls, `${route} should execute SQL`).toBeGreaterThan(0);
    expect(work.calls, `${route} SQL calls`).toBeLessThanOrEqual(requests * maxCallsPerRequest);
    expect(work.rows_read, `${route} rows read`).toBeLessThanOrEqual(requests * maxRowsPerRequest);
  }
}, 120000);
