import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ChecksService } from '../src/checks';
import { OpsLiveness } from '../src/ops-liveness';
import { migrateAgentRegistry } from '../src/agent_registry';
import { RowBudget } from '../src/row-budget';

const fixture = (fn: (raw: any,state: any) => Promise<void>) => (runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('existing-bootstrap-' + crypto.randomUUID())),
  async (_hub: any,state: any) => fn(state.storage.sql,state));
const ledger = () => { let value: any; return { async get<T>() { return value as T|undefined; }, async put<T>(_key: string,next: T) { value=structuredClone(next); } }; };
function observe(raw: any) {
  const calls: {query: string;cursor: any}[]=[];
  const sql = new Proxy(raw,{get(target,key) {
    if(key==='exec')return(query: string,...bindings: any[])=>{const cursor=target.exec(query,...bindings);calls.push({query,cursor});return cursor;};
    const value=target[key];return typeof value==='function'?value.bind(target):value;
  }});
  return {sql,calls};
}
function seedAgents(raw: any,count: number) {
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
    INSERT INTO agents(project,tag) SELECT 'fixture','agent-'||i FROM n`,count);
}

describe('bootstrap reuses deployed indexes on large retained sources',()=>{
  it('checks and ops initialize without building missing indexes over messages/intakes/sessions',()=>fixture(async(raw,state)=>{
    const now=Date.now(),at=new Date(now).toISOString();
    for(const name of ['messages_check_pending_created','messages_check_actor_pending_created','ops_intake_open_ac','ops_active_sessions','ops_ended_intake_sessions']) raw.exec('DROP INDEX IF EXISTS '+name);
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
      INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
      SELECT 'bootstrap-msg-'||i,'test','reader','note','t','x','bootstrap-msg-'||i,?,? FROM n`,now-600000,now+3600000);
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
      SELECT 'bootstrap-session-'||i,'test','bootstrap-session-'||i,'{}','headless','failed',?,? FROM n`,at,at);
    const observation=observe(raw),budget=new RowBudget(ledger());await budget.begin(true);
    const sql=budget.wrap(observation.sql);
    new ChecksService(sql,()=>now).init();
    new OpsLiveness(sql,{},()=>now).init();
    expect(budget.status().stopped).toBe(false);
    expect(observation.calls.some(call=>/CREATE INDEX.*(?:messages_check_|ops_intake_open_ac|ops_active_sessions|ops_ended_intake_sessions)/.test(call.query))).toBe(false);
    await budget.finish();
  }));

  it('observes new terminal work during a bounded bootstrap grace instead of remaining unknown forever',()=>fixture(async(raw,_state)=>{
    let now=Date.now();const initial=now,at=new Date(now-3600000).toISOString();let kicks=0;
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
      SELECT 'ended-bootstrap-'||i,'test','ended-bootstrap-'||i,'{}','headless','failed',?,? FROM n`,at,at);
    raw.exec('INSERT INTO intake_task_projection(id,data,links) VALUES(?,?,?)','observed-backlog',
      JSON.stringify({id:'observed-backlog',status:'open',acceptance_criteria:'AC',created_at:now-86400000}),'[]');
    const service=new OpsLiveness(raw,{kick:async()=>{kicks++;return{task_id:'observed-backlog',reason:'queued'};}},()=>now);
    service.init();raw.exec('UPDATE ops_work_observation SET started_at=?,last_terminal_at=? WHERE id=1',now,now);
    expect(service.backlog().truncated).toBe(true);
    expect((await (service as any).idleTick(now,new Map())).state).toBe('observation_incomplete');
    now+=30*60000;
    expect(service.backlog().truncated).toBeUndefined();
    expect((await (service as any).idleTick(now,new Map())).state).toBe('kicked');
    expect(kicks).toBe(1);
    now+=60000;
    raw.exec("UPDATE session_control_sessions SET request_json=?,updated_at=? WHERE id='ended-bootstrap-1'",
      JSON.stringify({intake_task_id:'observed-backlog'}),new Date(now).toISOString());
    expect(Math.abs(service.backlog().last_work_at!-now)).toBeLessThanOrEqual(1);
    expect(now).toBeGreaterThan(initial);
  }));

  it('uses deployed collision guards as prior validation evidence without a new registry index',()=>fixture(async(raw,state)=>{
    seedAgents(raw,3190);
    raw.exec("DELETE FROM hub_init_stamps WHERE key='agents_projectless_collision_validation_v1'");
    raw.exec('DROP INDEX IF EXISTS idx_agents_projectless_tag');
    const observation=observe(raw),budget=new RowBudget(ledger());await budget.begin(true);
    migrateAgentRegistry(budget.wrap(observation.sql),(fn:any)=>state.storage.transactionSync(fn));
    expect(budget.status().stopped).toBe(false);
    expect(observation.calls.some(call=>/CREATE INDEX.*idx_agents_projectless_tag/.test(call.query))).toBe(false);
    expect(observation.calls.some(call=>/SELECT project,tag FROM agents ORDER BY rowid/.test(call.query))).toBe(false);
    await budget.finish();
  }));

  it('bounds one-time registry validation when old guards are absent and rejects an incomplete source',()=>fixture(async(raw,state)=>{
    seedAgents(raw,5001);
    raw.exec("DELETE FROM hub_init_stamps WHERE key='agents_projectless_collision_validation_v1'");
    raw.exec('DROP TRIGGER IF EXISTS agents_no_legacy_collision_insert');
    raw.exec('DROP TRIGGER IF EXISTS agents_no_legacy_collision_update');
    const observation=observe(raw),budget=new RowBudget(ledger());await budget.begin(true);
    expect(()=>migrateAgentRegistry(budget.wrap(observation.sql),(fn:any)=>state.storage.transactionSync(fn))).toThrow('agent_registry_validation_incomplete');
    const probe=observation.calls.find(call=>/SELECT project,tag FROM agents ORDER BY rowid/.test(call.query))!;
    expect(Number(probe.cursor.rowsRead)).toBeLessThanOrEqual(5001);
    expect(raw.exec("SELECT 1 FROM hub_init_stamps WHERE key='agents_projectless_collision_validation_v1'").toArray()).toHaveLength(0);
    await budget.finish();
  }));
});
