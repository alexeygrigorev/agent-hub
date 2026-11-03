import { describe,expect,it } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { ensureTbmControllerSchema,sweepTerminalSessionRewards,tickTbm } from '../src/tbm-controller';
import { TBM_CONTROL_LIMIT,TbmObservationLimit } from '../src/pacing';
import { RowsMeter } from '../src/rows-meter';
import { pruneUsageQuota,USAGE_RETENTION_MS } from '../src/usage-store';

const NOW=Date.parse('2026-11-03T20:00:00Z'),DAY=86400000;
const fixture=(fn: (hub:any,sql:any)=>void)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('tbm-bounds-'+crypto.randomUUID())),
  async(hub:any,state:any)=>{ensureTbmControllerSchema(state.storage.sql);fn(hub,state.storage.sql);});

function pool(sql:any,id:string,reset=NOW+600000) {
  sql.exec(`INSERT INTO tbm_quota_pools(pool_id,provider,account,total_capacity_tokens,remaining_tokens,
    headroom_percentage,reset_at_ms,last_updated_ms,cycle_ms,capacity_known,remaining_percent,quota_unit)
    VALUES(?,'codex','test',0,0,80,?,?,18000000,0,80,'percent')`,id,reset,NOW);
}

describe('bounded TBM background maintenance',()=>{
  it('installs session indexes after routing created the earlier core schema receipt',async()=>{
    const stub=(env as any).HUB.get((env as any).HUB.idFromName('tbm-index-init-'+crypto.randomUUID()));
    await(runInDurableObject as any)(stub,(_hub:any,state:any)=>{
      const sql=state.storage.sql;
      expect(sql.exec("SELECT value FROM tbm_maintenance_cursor WHERE key='schema_version'").one().value).toBe('2');
      const indexes=sql.exec("SELECT name FROM sqlite_master WHERE type='index' AND name IN ('tbm_terminal_candidates','tbm_session_created','tbm_command_ack')").toArray();
      expect(indexes.map((row:any)=>row.name).sort()).toEqual(['tbm_command_ack','tbm_session_created','tbm_terminal_candidates']);
    });
  });

  it('refuses incomplete session-mix observations before publishing healthy aggregate metrics',()=>fixture((_hub,sql)=>{
    pool(sql,'codex:test:5h');
    const newest=new Date(NOW).toISOString(),older=new Date(NOW-60000).toISOString();
    sql.exec(`WITH RECURSIVE n(i) AS(SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<201)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
      SELECT 'mix-'||i,'test','mix-'||i,'{}','codex',CASE WHEN i=0 THEN 'headless' ELSE 'interactive' END,
        CASE WHEN i=0 THEN 'failed' ELSE 'running' END,CASE WHEN i=0 THEN ? ELSE ? END,? FROM n`,older,newest,newest);
    const meter=new RowsMeter(()=>NOW),tracked=meter.wrap(sql);
    expect(()=>meter.runSync('mix',()=>tickTbm(tracked,NOW))).toThrowError('tbm_observation_limit:session_mix');
    expect(sql.exec('SELECT COUNT(*) AS n FROM tbm_metrics_rollup').one().n).toBe(0);
    const source=meter.statementTop(200).find(row=>row.scope==='mix' && row.sql.startsWith('SELECT engine,state,created_at,updated_at,mode'))!;
    expect(source.max_read).toBeLessThanOrEqual(201);
  }));

  it('pages raw terminal sessions before checking reward existence, independent of rewarded history',()=>fixture((_hub,sql)=>{
    const at=new Date(NOW-2*DAY).toISOString();
    sql.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at)
      SELECT printf('session-%05d',i),'test',printf('request-%05d',i),'{}','a','codex','headless','stopped',?,? FROM n`,at,at);
    sql.exec(`INSERT INTO tbm_reward_events(event_id,timestamp_ms,pool_id,intake_task_id,active_mode,swarm_concurrency,
      useful_work_reward,pacing_error_penalty,floor_breach_penalty,engine_mix_penalty,waste_at_reset_penalty,banked_waste_penalty,
      net_reward,token_efficiency_eta,tokens_known,tokens_burned,session_id,event_kind)
      SELECT 'session:'||id,?,'codex:test','fixture','high',1,0,0,0,0,0,0,0,0,1,1,id,'session' FROM session_control_sessions`,NOW-2*DAY);
    const meter=new RowsMeter(()=>NOW),tracked=meter.wrap(sql);
    const first=meter.runSync('sweep',()=>sweepTerminalSessionRewards(tracked,NOW));
    expect(first.written).toBe(0);
    expect(meter.snapshot().sweep.rows_read).toBeLessThan(500);
    const before=String(sql.exec("SELECT value FROM tbm_maintenance_cursor WHERE key='terminal_session'").one().value);
    sweepTerminalSessionRewards(tracked,NOW);
    expect(String(sql.exec("SELECT value FROM tbm_maintenance_cursor WHERE key='terminal_session'").one().value)>before).toBe(true);
  }));

  it('refuses an incomplete control registry before updating controller outputs',()=>fixture((_hub,sql)=>{
    for(let i=0;i<=TBM_CONTROL_LIMIT;i++)pool(sql,'codex:test:'+i);
    const meter=new RowsMeter(()=>NOW),tracked=meter.wrap(sql);
    expect(()=>meter.runSync('overflow',()=>tickTbm(tracked,NOW))).toThrow(TbmObservationLimit);
    expect(sql.exec('SELECT COUNT(*) AS n FROM tbm_controller_state').one().n).toBe(0);
    expect(meter.snapshot().overflow.max_read).toBeLessThanOrEqual(2*(TBM_CONTROL_LIMIT+1));
  }));

  it('does no SQL on an idle TBM tick but still processes a future reset without a source write',()=>fixture((hub,sql)=>{
    pool(hub.sql,'codex:test:5h');
    hub.runTbmTick(NOW);
    hub.meter.runSync('idle_tbm',()=>hub.runTbmTick(NOW+300000));
    expect(hub.meter.snapshot().idle_tbm?.calls??0).toBe(0);
    const reset=hub.runTbmTick(NOW+660000);
    expect(reset.resets_processed).toBe(1);
    expect(sql.exec("SELECT rewarded FROM tbm_pool_resets WHERE pool_id='codex:test:5h'").one().rewarded).toBe(1);
    hub.meter.runSync('after_reset',()=>hub.runTbmTick(NOW+DAY));
    expect(hub.meter.snapshot().after_reset?.calls??0).toBe(0);
  }));

  it('keeps quota journal retention deletes bounded across a large old backlog',()=>fixture((_hub,sql)=>{
    sql.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
      INSERT INTO usage_quota_history(account,engine,provider,observed_at_ms,quota_json)
      SELECT 'a','codex','codex',?+i,'{}' FROM n`,NOW-USAGE_RETENTION_MS-DAY);
    const meter=new RowsMeter(()=>NOW),tracked=meter.wrap(sql);
    meter.runSync('retention',()=>pruneUsageQuota(tracked,NOW));
    expect(sql.exec('SELECT COUNT(*) AS n FROM usage_quota_history').one().n).toBe(1900);
    expect(meter.snapshot().retention.rows_read).toBeLessThan(500);
    expect(meter.snapshot().retention.rows_written).toBeLessThan(500);
  }));
});
