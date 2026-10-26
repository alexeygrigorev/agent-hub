import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';
import { FleetService } from '../src/fleet';
import { freshCpu } from './cpu-fixture';

const inDO: any = runInDurableObject;
const sessionId = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;

it('keeps status and host handlers below 100ms p95 with retained production-scale history', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('hub'));
  const cookie = (await createSessionCookie(env as any, { sub: 'perf-operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'perf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  const token = 'hubt_perf_synthetic_host';
  const hash = await sha256(token);
  await inDO(stub, (h: any) => {
    const now = new Date().toISOString();
    h.ctx.storage.transactionSync(() => {
      h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('perf-host','perf-host','perf-host',?,?,0)", now, now);
      h.sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        'perf-credential', 'host', hash, 'perf-family', 'host:perf-host', 'perf-host', 'host:perf-host', '["host"]', 'perf-operator', now, new Date(Date.now() + 86400000).toISOString());
      h.sql.exec("UPDATE machines SET credential_id='perf-credential' WHERE id='perf-host'");
      for (let i = 0; i < 500; i++) {
        const id = `perf-task-${String(i).padStart(4, '0')}`;
        const history = Array.from({length: 40}, (_, j) => ({ kind: 'note', text: 'Synthetic retained history. '.repeat(20), captured_at: Date.now(), id: `${id}-${j}` }));
        h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES (?,?)', id, JSON.stringify({ id, project: 'perf', title: id, status: 'open', version: 1, created_at: Date.now(), updated_at: Date.now(), history: [{kind: 'intake', text: 'Original request. '.repeat(480), source: {reference: null}}, ...history, {kind: 'link', hub_session_id: sessionId(i)}] }));
      }
      for (let i = 0; i < 2000; i++) {
        h.sql.exec('INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
          sessionId(i), 'perf:owner', `perf-${i}`, '{}', 'perf-host', 'codex', 'headless', i < 100 ? 'running' : 'stopped', JSON.stringify({native_id: `native-${i}`}), now, now);
        h.sql.exec('INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace) VALUES (?,?,?,?,?,?,?,?)',
          'perf-host', `native-${i}`, `perf-${i}`, 'codex', i < 100 ? 'running' : 'stopped', 'working', now, '/synthetic');
        h.sql.exec('INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          sessionId(i + 10000), 'perf:owner', 'agent', 'Synthetic', `perf-launch-${i}`, '{}', 'perf-host', 'codex', 'default', 'headless', '/synthetic', 'Synthetic', 'stopped', `native-${i}`, now, now);
        for (let j = 0; j < 3; j++) {
          const at = new Date(Date.now() - (3-j)*1000).toISOString();
          h.sql.exec('INSERT INTO status_observations(id,machine_id,hub_session_id,kind,observed_at,payload_json,reporter,received_at) VALUES (?,?,?,?,?,?,?,?)',
            `obs-${i}-${j}`, 'perf-host', sessionId(i), 'session', at, '{"state":"working"}', 'perf-host', at);
          h.sql.exec('INSERT INTO session_lifecycle_decisions(session_key,decision_id,kind,actor,reason,created_at) VALUES (?,?,?,?,?,?)',
            `perf-host/native-${i}`, `decision-${i}-${j}`, 'resume_on_reset', 'perf:owner', 'Synthetic', at);
        }
      }
    });
  });
  // Time the real DO fetch/auth/handler and serialization over local RPC.
  // Public TLS/network latency is measured separately against production.
  const cases: [string, string, boolean, any, number][] = [
    ['GET', '/api/status', false, undefined, 200],
    ['POST', '/api/status', false, {}, 200],
    ['GET', '/api/sessions?limit=1', false, undefined, 200],
    ['POST', '/api/host/poll', true, {type: 'heartbeat'}, 200],
    ['POST', '/api/machines/register', true, {hostname: 'perf-host', os: 'linux', network: 'synthetic', roles: [], capabilities: {}}, 200],
    ['GET', '/api/fleet/offers', true, undefined, 200],
    // Real retained launch: replay claims must promptly refuse a terminal row.
    ['POST', `/api/fleet/launches/${sessionId(11999)}/claim`, true, {}, 409],
  ];
  for (const [method, path, host, payload, status] of cases) {
    const times: number[] = [];
    for (let i = 0; i < 20; i++) {
      const start = performance.now();
      const response = await stub.fetch('https://hub.test' + path, { method, headers: host ? {Authorization: 'Bearer ' + token} : {Cookie: cookie, 'x-csrf-token': 'perf'}, ...(payload ? {body: JSON.stringify(payload)} : {}) });
      const body: any = await response.json();
      times.push(performance.now() - start);
      expect(response.status, JSON.stringify(body).slice(0, 200)).toBe(status);
      if (path === '/api/status') {
        expect(body.tasks).toHaveLength(method === 'GET' ? 25 : 256);
        expect(body.tasks[0].hub_sessions[0].lifecycle.session_key).toBe('perf-host/native-0');
        expect(body.agents.running).toBe(100);
        expect(body.tasks[0].hub_sessions[0].host_observation.state).toBe('working');
        expect(body.tasks[0].hub_sessions[0].lifecycle.decision.kind).toBe('resume_on_reset');
      }
    }
    times.sort((a, b) => a - b);
    console.log(`${method} ${path} p50=${times[9]}ms p95=${times[18]}ms`);
    // Clock must advance across real I/O; a frozen Worker clock cannot pass.
    expect(times[18]).toBeGreaterThan(0);
    expect.soft(times[18], `${method} ${path}`).toBeLessThan(100);
  }
  // Exercise actual admission/transition work as well as terminal replay.
  // An independent healthy host avoids altering the retained workload above.
  const claimToken = 'hubt_perf_synthetic_claim_host';
  const claimHash = await sha256(claimToken);
  await inDO(stub, (h: any) => {
    const now = new Date().toISOString();
    h.sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES (?,?,?,?,?,?,0,?)',
      'claim-host', 'claim-host', 'claim-host', now, now,
      JSON.stringify({mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(now), gate: {launch_allowed: true, at: now}}),
      JSON.stringify({engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8}));
    h.sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
      'claim-credential', 'host', claimHash, 'claim-family', 'host:claim-host', 'claim-host', 'host:claim-host', '["host"]', 'perf-operator', now, new Date(Date.now() + 86400000).toISOString());
    h.sql.exec("UPDATE machines SET credential_id='claim-credential' WHERE id='claim-host'");
    h.sql.exec("INSERT INTO fleet_accounts VALUES ('claim-account','codex',1,15,1)");
    h.sql.exec("INSERT INTO fleet_routes VALUES ('claim-host','codex','claim-account',NULL)");
    h.sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES (?,?,?,?,?,?,?,?,?)',
      'claim-host', '1.0', now, Date.now(), now, Date.now(), 'ok', null,
      JSON.stringify({codex: {status: 'ok', observed_at: now, details: {limit_reached: false}, windows: {weekly: {present: true, percent_remaining: 80, reset_at: new Date(Date.now() + 86400000).toISOString()}}}}));
  });
  const admittedTimes: number[] = [];
  const offeredTimes: number[] = [];
  for (let i = 0; i < 20; i++) {
    const launch = await inDO(stub, (h: any) => {
      const service = new FleetService(h.sql, (cb) => h.ctx.storage.transactionSync(cb));
      const result = service.reserve({workspace: 'demo', prompt: 'Synthetic performance claim', machine: 'claim-host', idempotency_key: `perf-claim-${i}`},
        {caller: {isAdmin: true, actor: 'perf-operator', kind: 'human'}, scopes: ['admin']});
      expect(result.status).toBe(201);
      return result.value;
    });
    let start = performance.now();
    const offers = await stub.fetch('https://hub.test/api/fleet/offers', {headers: {Authorization: 'Bearer ' + claimToken}});
    const offerBody: any = await offers.json();
    offeredTimes.push(performance.now() - start);
    expect(offers.status).toBe(200);
    expect(JSON.stringify(offerBody)).toContain(launch.id);
    start = performance.now();
    const claim = await stub.fetch(`https://hub.test/api/fleet/launches/${launch.id}/claim`, {
      method: 'POST', headers: {Authorization: 'Bearer ' + claimToken},
      body: JSON.stringify({gate_allowed: true, profile_supported: true, observed_at: new Date().toISOString(), account: 'claim-account', model: launch.selection.model}),
    });
    const claimBody: any = await claim.json();
    admittedTimes.push(performance.now() - start);
    expect(claim.status, JSON.stringify(claimBody)).toBe(200);
    expect(claimBody.state).toBe('starting');
    await inDO(stub, (h: any) => { h.sql.exec("UPDATE fleet_launches SET state='stopped' WHERE id=?", launch.id); });
  }
  for (const [name, times] of [['offers with live work', offeredTimes], ['successful claim', admittedTimes]] as const) {
    times.sort((a, b) => a - b);
    console.log(`${name} p50=${times[9]}ms p95=${times[18]}ms`);
    expect(times[18]).toBeGreaterThan(0);
    expect.soft(times[18], name).toBeLessThan(100);
  }
  // A busy retained queue must not make every host poll re-plan its backlog.
  await inDO(stub, (h: any) => h.ctx.storage.transactionSync(() => {
    h.sql.exec("UPDATE machines SET last_heartbeat=json_set(last_heartbeat,'$.mem_avail_mb',0) WHERE id='claim-host'");
    for (let i = 0; i < 200; i++) {
      h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        SELECT ?,actor,requester_kind,summary,?,request_json,machine_id,engine,account,mode,workspace,prompt,'queued',created_at,updated_at
        FROM fleet_launches WHERE account='claim-account' AND state='stopped' LIMIT 1`, sessionId(20000+i), `perf-parked-${i}`);
    }
  }));
  await inDO(stub, (h: any) => h.ctx.storage.transactionSync(() => {
    for (let i=0;i<200;i++) h.sql.exec(`INSERT INTO routing_jobs(id,actor,request_key,request_json,state,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?)`,
      sessionId(30000+i),'perf:owner',`routing-backlog-${i}`,JSON.stringify({workspace:'demo',needs:['unavailable'],tasks:[],access:{mode:'read'}}),new Date().toISOString(),new Date().toISOString());
  }));
  const backlogTimes: number[] = [];
  for (let i = 0; i < 20; i++) {
    const start = performance.now();
    const response = await stub.fetch('https://hub.test/api/fleet/offers', {headers: {Authorization: 'Bearer ' + claimToken}});
    await response.json();
    backlogTimes.push(performance.now() - start);
    expect(response.status).toBe(200);
  }
  backlogTimes.sort((a, b) => a - b);
  console.log(`offers with blocked backlog p50=${backlogTimes[9]}ms p95=${backlogTimes[18]}ms`);
  expect(backlogTimes[18]).toBeGreaterThan(0);
  expect(backlogTimes[18]).toBeLessThan(100);
  // Competing requests exercise DO scheduling while both queues are retained.
  const mixed = [
    ['status','/api/status', {method:'POST',headers:{Cookie:cookie,'x-csrf-token':'perf'},body:'{}'}],
    ['sessions','/api/sessions?limit=1',{headers:{Cookie:cookie}}],
    ['offers','/api/fleet/offers',{headers:{Authorization:'Bearer '+claimToken}}],
    ['claim',`/api/fleet/launches/${sessionId(11999)}/claim`,{method:'POST',headers:{Authorization:'Bearer '+token},body:'{}'}],
  ] as const;
  const mixedTimes: number[][] = mixed.map(()=>[]);
  for(let i=0;i<20;i++) await Promise.all(mixed.map(async ([name,path,options],j)=>{
    const start=performance.now();
    const response=await stub.fetch('https://hub.test'+path,options);
    await response.json();
    mixedTimes[j].push(performance.now()-start);
    expect(response.status,name).toBe(name==='claim'?409:200);
  }));
  for(const [j,[name]] of mixed.entries()) {
    const times=mixedTimes[j].sort((a,b)=>a-b);
    console.log(`concurrent ${name} p50=${times[9]}ms p95=${times[18]}ms`);
    expect.soft(times[18],`concurrent ${name}`).toBeLessThan(100);
  }
}, 120000);
