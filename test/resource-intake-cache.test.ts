import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ResourceManagement } from '../src/resource-management';

const done = [{ kind: 'note', text: 'DONE abc1234\nlive check: synthetic cache fixture\nresult: all synthetic criteria proven' }];
const inDO: any = runInDurableObject;

async function fixture(check: (r: ResourceManagement, sql: any, state: any, scans: () => number) => void) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub, (hub: any, state: any) => {
    let scans = 0;
    const sql = hub.sql;
    const measured = { exec(query: string, ...args: any[]) {
      if (query.startsWith('SELECT id, data FROM intake_tasks')) scans++;
      return sql.exec(query, ...args);
    }} as any;
    const r = new ResourceManagement(measured);
    r.initCatalogCache();
    const at = new Date().toISOString();
    sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('cache-host','fixture','fixture',?)", at);
    sql.exec('INSERT INTO intake_tasks VALUES(?,?)', 'cache-task', JSON.stringify({
      id: 'cache-task', status: 'open', history: [{kind: 'note', text: 'retained history '.repeat(4000)}],
    }));
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES('cache-session','fixture','cache-request',?,'cache-host','codex','headless','running',?,?,?)`,
      JSON.stringify({intake_task_id: 'cache-task'}), JSON.stringify({native_id: 'cache-native', tag: 'fixture', workspace: '/fixture'}), at, at);
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,last_activity_at,workspace)
      VALUES('cache-host','cache-native','fixture','codex','running',?,?,'/fixture')`, at, at);
    check(r, sql, state, () => scans);
  });
}

it('keeps census activity fresh without rereading retained intake bodies and invalidates closure writes', async () => fixture((r, sql, _state, scans) => {
  const read = () => r.sessions('cache-host')[0];
  expect(read().linked_intake_open).toBe(true);
  expect(scans()).toBe(1);
  sql.exec("UPDATE fleet_sessions SET last_activity_at='2026-10-28T12:34:56Z'");
  expect(read()).toMatchObject({linked_intake_open: true, last_activity_at: '2026-10-28T12:34:56Z'});
  expect(scans()).toBe(1);
  sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?)) WHERE id='cache-task'", JSON.stringify(done));
  expect(read().linked_intake_open).toBe(false);
  expect(scans()).toBe(2);
  sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json('[]')) WHERE id='cache-task'");
  expect(read().linked_intake_open).toBe(true);
  expect(scans()).toBe(3);
}));

it('discards a closure snapshot read in a rolled-back transaction before another census write', async () => fixture((r, sql, state, scans) => {
  expect(r.sessions('cache-host')[0].linked_intake_open).toBe(true);
  expect(() => state.storage.transactionSync(() => {
    sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?))", JSON.stringify(done));
    expect(r.sessions('cache-host')[0].linked_intake_open).toBe(false);
    throw Error('rollback closure');
  })).toThrow('rollback closure');
  sql.exec("UPDATE fleet_sessions SET last_activity_at='2026-10-28T12:34:56Z'");
  expect(r.sessions('cache-host')[0].linked_intake_open).toBe(true);
  expect(scans()).toBe(3);
}));

it('rechecks malformed link ledgers and repaired authority after a native-only invalidation', async () => fixture((r, sql) => {
  sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?))", JSON.stringify(done));
  expect(r.sessions('cache-host')[0].linked_intake_open).toBe(false);
  sql.exec("INSERT INTO intake_task_projection VALUES('invalid','{}','invalid')");
  expect(r.sessions('cache-host')[0].linked_intake_open).toBe(true);
  sql.exec("DELETE FROM intake_task_projection WHERE id='invalid'");
  sql.exec("UPDATE fleet_sessions SET last_activity_at='2026-10-28T12:34:56Z'");
  expect(r.sessions('cache-host')[0].linked_intake_open).toBe(false);
}));

it('bypasses snapshot retention for oversized input and missing structural generations', async () => fixture((r, sql, _state, scans) => {
  sql.exec('INSERT INTO intake_task_projection VALUES(?,?,?)', 'large', '{}', JSON.stringify([{kind: 'link', pad: 'x'.repeat(2100000)}]));
  r.sessions('cache-host');
  sql.exec("UPDATE fleet_sessions SET last_activity_at='2026-10-28T12:34:56Z'");
  r.sessions('cache-host');
  expect(scans()).toBe(2);
  sql.exec("DELETE FROM intake_task_projection WHERE id='large'");
  sql.exec('DELETE FROM resource_catalog_revision');
  r.sessions('cache-host'); r.sessions('cache-host');
  expect(scans()).toBe(4);
}));

it('refreshes closure links after launch requests, binding identity and hostname aliases change', async () => fixture((r, sql) => {
  sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?))", JSON.stringify(done));
  const read=()=>r.sessions('cache-host').find(s=>s.session_id==='cache-session')!;
  expect(read().linked_intake_open).toBe(false);
  sql.exec('INSERT INTO intake_tasks VALUES(?,?)','linked',JSON.stringify({id:'linked',status:'open',history:[]}));
  const at=new Date().toISOString();
  sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
    VALUES('cache-launch','fixture','agent','fixture','cache-launch','{}','cache-host','codex','default','headless','/fixture','fixture','running',?,?)`,at,at);
  sql.exec("UPDATE session_control_sessions SET fleet_launch_id='cache-launch' WHERE id='cache-session'");
  expect(read().linked_intake_open).toBe(false);
  sql.exec("UPDATE fleet_launches SET request_json=? WHERE id='cache-launch'",JSON.stringify({intake_task_id:'linked'}));
  expect(read().linked_intake_open).toBe(true);
  sql.exec("UPDATE fleet_launches SET request_json='{}' WHERE id='cache-launch'");
  expect(read().linked_intake_open).toBe(false);
  sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?)) WHERE id='linked'",
    JSON.stringify([{kind:'link',native_id:'replacement',host:'cache-host'}]));
  expect(read().linked_intake_open).toBe(false);
  sql.exec("UPDATE session_control_sessions SET binding_json=json_set(binding_json,'$.native_id','replacement') WHERE id='cache-session'");
  expect(read().linked_intake_open).toBe(true);
  sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?)) WHERE id='linked'",
    JSON.stringify([{kind:'link',native_id:'replacement',host:'cache-alias'}]));
  sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('cache-alias','fixture','different',?)",at);
  expect(read().linked_intake_open).toBe(false);
  sql.exec("UPDATE machines SET hostname='FIXTURE' WHERE id='cache-alias'");
  expect(read().linked_intake_open).toBe(true);
}));
