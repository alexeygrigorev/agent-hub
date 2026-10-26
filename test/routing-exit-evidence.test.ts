import { it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { RoutingService } from '../src/routing';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

it('stores sanitized routed exit evidence only with owning-host confirmed exit', async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('routed-exit-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const tx = <T>(cb: () => T): T => state.storage.transactionSync(cb);
    const fleet = new FleetService(hub.sql, tx); fleet.init();
    const routing = new RoutingService(hub.sql, fleet, Date.now, tx); routing.init();
    const sql = hub.sql, now = new Date().toISOString();
    const launch = crypto.randomUUID(), job = crypto.randomUUID();
    sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      VALUES(?, 'tester', 'agent', 'routed audit', ?, '{}', 'hetzner', 'opencode', 'shared-go', 'headless', 'demo', 'audit', 'running', ?, ?)`, launch, launch, now, now);
    sql.exec(`INSERT INTO routing_jobs(id,actor,request_key,request_json,launch_id,state,created_at,updated_at)
      VALUES(?, 'tester', ?, ?, ?, 'running', ?, ?)`, job, job, JSON.stringify({tier:'daily',tasks:[],needs:[],workspace:'demo'}), launch, now, now);
    const host = (id: string) => ({caller:{kind:'host',machineId:id},scopes:['host']});
    const runtime = {exit_code:3,exit_signal:null,wall_time_ms:1234,log_tail:'Cookie: sid=short-test-value',
      engine_outcome:null,observed_commits:[],dirty_paths:Array.from({length:16},()=>'😀'.repeat(128)),outcome:'failed-with-partial-work'};
    const payload = {status:'failed',report:'😀'.repeat(2000)+'password="short-test-value"',
      artifacts:Array.from({length:8},(_,i)=>({name:'evidence-'+i,text:'😀'.repeat(512)})),confirmed_exit:true,runtime};
    const wire = (data: any) => JSON.stringify(data).replace(/[^\x00-\x7f]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
    expect(new TextEncoder().encode(wire(payload)).length).toBeGreaterThan(32768);
    const req = (data: any) => new Request('https://hub.test/api/tasks/'+job+'/result',{method:'POST',body:wire(data)});
    expect((await routing.handle(req(payload),host('other'))).status).toBe(404);
    expect((await routing.handle(req({...payload,confirmed_exit:false}),host('hetzner'))).status).toBe(400);
    expect((await routing.handle(req({...payload,runtime:{...runtime,unknown:true}}),host('hetzner'))).status).toBe(400);
    expect(sql.exec('SELECT runtime_json FROM fleet_launches WHERE id=?',launch).one().runtime_json).toBeNull();
    const response = await routing.handle(req(payload),host('hetzner'));
    expect(response.status,await response.clone().text()).toBe(200);
    const view: any = await response.json();
    expect(view.runtime.exit_code).toBe(3);
    expect(view.runtime.wall_time_ms).toBe(1234);
    expect(JSON.stringify(view)).not.toContain('short-test-value');
    expect(JSON.parse(String(sql.exec('SELECT runtime_json FROM fleet_launches WHERE id=?',launch).one().runtime_json))).toEqual(view.runtime);
  });
});
