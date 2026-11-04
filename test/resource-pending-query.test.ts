import {registerFixtureStatements,fixtureStatements} from './helpers/fixture-statements';
import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ResourceManagement } from '../src/resource-management';
import { freshCpu } from './cpu-fixture';

const inDO: any = runInDurableObject;
const activeFilter = "AND l.state IN ('offered','starting','running','uncertain')";
// The reference keeps eligibility but defeats its index constraint. Removing
// eligibility entirely would put retained terminal rows into the raw sentinel
// and legitimately make the bounded service refuse an incomplete snapshot.
const referenceFilter = "AND CASE WHEN l.state IN ('offered','starting','running','uncertain') THEN 1 ELSE 0 END=1";

it('bounds reservation reads by active launches while preserving ramp, host and exclusion semantics', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub, async (h: any) => {
    registerFixtureStatements(h);
    await h.rowBudget.begin(true);
    const sql = h.sql, now = Date.now(), at = new Date(now).toISOString();
    const original = new ResourceManagement(sql, () => now);
    const cutoff = now - original.config().ramp_seconds * 1000;
    const before = new Date(cutoff - 1).toISOString(), boundary = new Date(cutoff).toISOString();
    for (const machine of ['pending-host', 'other-host']) {
      fixtureStatements(sql).exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES(?,?,?,?,?,?,?)',
        machine, machine, machine, at, at, JSON.stringify({mem_avail_mb:64000,cpu_usage:freshCpu(at,64)}), JSON.stringify({cpu_count:64}));
    }
    const request = JSON.stringify({project:'pending',task_kind:'goal',prompt:'retained history '.repeat(500)});
    const insert = (id: string, state: string, created = at, machine = 'pending-host') => fixtureStatements(sql).exec(`
      INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      VALUES(?,'fixture','agent','fixture',?,?,?,'codex','fixture','headless','/fixture','fixture',?,?,?)`,
      id, id, request, machine, state, created, at);
    h.ctx.storage.transactionSync(() => {
      for (let i=0;i<2000;i++) insert('history-'+i, ['stopped','failed','exited','queued','cancelled'][i%5]);
      for (const state of ['offered','starting','uncertain','running']) insert('active-'+state,state);
      insert('old-running','running',before);
      insert('boundary-running','running',boundary);
      insert('old-created-fresh-ramp','running',before);
      insert('fresh-created-old-ramp','running');
      insert('foreign','offered',at,'other-host');
      fixtureStatements(sql).exec('INSERT INTO resource_ramps(launch_id,started_at) VALUES(?,?),(?,?)',
        'old-created-fresh-ramp',at,'fresh-created-old-ramp',before);
    });
    const work: {query: string; reads: number}[] = [], referenceWork: {query:string;reads:number}[]=[];
    let referenceRewrites=0;
    const measured = (baseline: boolean) => new Proxy(sql, { get(target,key) {
      if (key !== 'exec') return Reflect.get(target,key);
      return (query: string, ...args: any[]) => {
        const pending = query.includes('FROM fleet_launches l') && query.includes('resource_ramps') && query.includes('AS request_json');
        if(baseline&&pending){expect(query).toContain(activeFilter);referenceRewrites++;}
        const executed=baseline&&pending?query.replace(activeFilter,referenceFilter):query;
        const cursor = target.exec(executed,...args);
        if (!pending) return cursor;
        return new Proxy(cursor,{get(c,property) {
          if (property === 'toArray') return () => { const rows=c.toArray(); (baseline?referenceWork:work).push({query:executed,reads:c.rowsRead}); return rows; };
          const value=Reflect.get(c,property,c); return typeof value === 'function' ? value.bind(c) : value;
        }});
      };
    }});
    const baseline = new ResourceManagement(measured(true), () => now);
    const current = new ResourceManagement(measured(false), () => now);
    const input = {project:'pending',workspace:'/fixture',task_kind:'goal'};
    const memory = current.headroom('pending-host',input,'codex');
    expect(memory).toEqual(baseline.headroom('pending-host',input,'codex'));
    // Intake 63a9ee3a: all 2000 terminal/queued rows remain visible to the
    // non-sargable reference scan, but never become active reservations.

    expect(baseline.headroom('pending-host',input,'codex').reserved_mb).toBe(6*current.estimate('pending','codex','goal'));
    // Four active states, the exact boundary, and an old launch with a fresh
    // ramp count. Old ramps, foreign hosts and terminal/queued history do not.
    expect(memory.reserved_mb).toBe(6*current.estimate('pending','codex','goal'));
    expect(current.cpuHeadroom('pending-host',input)).toEqual(baseline.cpuHeadroom('pending-host',input));
    expect(current.cpuHeadroom('pending-host',input,'active-offered')).toEqual(baseline.cpuHeadroom('pending-host',input,'active-offered'));
    // Memory visibility seeks each exact active state using the deployed
    // index; bound the aggregate row work rather than a particular call count.
    expect(referenceRewrites).toBeGreaterThan(0);
    expect(referenceWork.reduce((total,query)=>total+query.reads,0)).toBeGreaterThan(2000);
    expect(work.length).toBeGreaterThan(0);
    expect(work.reduce((total, query) => total + query.reads, 0)).toBeLessThan(200);
    for (const query of work) expect(query.reads,query.query).toBeLessThan(100);
    fixtureStatements(sql).exec("UPDATE fleet_launches SET state='stopped' WHERE id='active-offered'");
    expect(current.headroom('pending-host',input,'codex').reserved_mb).toBe(5*current.estimate('pending','codex','goal'));
    fixtureStatements(sql).exec("UPDATE resource_ramps SET started_at=? WHERE launch_id='old-created-fresh-ramp'",before);
    expect(current.headroom('pending-host',input,'codex').reserved_mb).toBe(4*current.estimate('pending','codex','goal'));
    expect(h.rowBudget.status()).toMatchObject({limit_total:20000,stopped:false});
  });
});
