import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ResourceManagement } from '../src/resource-management';
import { freshCpu } from './cpu-fixture';

const inDO: any = runInDurableObject;
const activeFilter = "AND l.state IN ('offered','starting','running','uncertain')";

it('bounds reservation reads by active launches while preserving ramp, host and exclusion semantics', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub, (h: any) => {
    const sql = h.sql, now = Date.now(), at = new Date(now).toISOString();
    const original = new ResourceManagement(sql, () => now);
    const cutoff = now - original.config().ramp_seconds * 1000;
    const before = new Date(cutoff - 1).toISOString(), boundary = new Date(cutoff).toISOString();
    for (const machine of ['pending-host', 'other-host']) {
      sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES(?,?,?,?,?,?,?)',
        machine, machine, machine, at, at, JSON.stringify({mem_avail_mb:64000,cpu_usage:freshCpu(at,64)}), JSON.stringify({cpu_count:64}));
    }
    const request = JSON.stringify({project:'pending',task_kind:'goal',prompt:'retained history '.repeat(500)});
    const insert = (id: string, state: string, created = at, machine = 'pending-host') => sql.exec(`
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
      sql.exec('INSERT INTO resource_ramps(launch_id,started_at) VALUES(?,?),(?,?)',
        'old-created-fresh-ramp',at,'fresh-created-old-ramp',before);
    });
    const work: {query: string; reads: number}[] = [];
    const measured = (baseline: boolean) => new Proxy(sql, { get(target,key) {
      if (key !== 'exec') return Reflect.get(target,key);
      return (query: string, ...args: any[]) => {
        const pending = query.includes('FROM fleet_launches l') && query.includes('resource_ramps') && query.includes('AS request_json');
        const cursor = target.exec(baseline && pending ? query.replace(activeFilter,'') : query,...args);
        if (!pending || baseline) return cursor;
        return new Proxy(cursor,{get(c,property) {
          if (property === 'toArray') return () => { const rows=c.toArray(); work.push({query,reads:c.rowsRead}); return rows; };
          const value=Reflect.get(c,property,c); return typeof value === 'function' ? value.bind(c) : value;
        }});
      };
    }});
    const baseline = new ResourceManagement(measured(true), () => now);
    const current = new ResourceManagement(measured(false), () => now);
    const input = {project:'pending',workspace:'/fixture',task_kind:'goal'};
    const memory = current.headroom('pending-host',input,'codex');
    expect(memory).toEqual(baseline.headroom('pending-host',input,'codex'));
    // Four active states, the exact boundary, and an old launch with a fresh
    // ramp count. Old ramps, foreign hosts and terminal/queued history do not.
    expect(memory.reserved_mb).toBe(6*current.estimate('pending','codex','goal'));
    expect(current.cpuHeadroom('pending-host',input)).toEqual(baseline.cpuHeadroom('pending-host',input));
    expect(current.cpuHeadroom('pending-host',input,'active-offered')).toEqual(baseline.cpuHeadroom('pending-host',input,'active-offered'));
    expect(work).toHaveLength(3);
    for (const query of work) expect(query.reads,query.query).toBeLessThan(100);
    sql.exec("UPDATE fleet_launches SET state='stopped' WHERE id='active-offered'");
    expect(current.headroom('pending-host',input,'codex').reserved_mb).toBe(5*current.estimate('pending','codex','goal'));
    sql.exec("UPDATE resource_ramps SET started_at=? WHERE launch_id='old-created-fresh-ramp'",before);
    expect(current.headroom('pending-host',input,'codex').reserved_mb).toBe(4*current.estimate('pending','codex','goal'));
  });
});
