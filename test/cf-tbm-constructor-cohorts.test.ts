import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {ensureTbmControllerSchema,nextTbmDue,tickTbm} from '../src/tbm-controller';
import {TbmObservationLimit} from '../src/pacing';
import {fixtureSql} from './helpers/fixture-budget';
const NOW=Date.now(),DAY=86400000;
const optional=['tbm_resets_unrewarded_due','tbm_pools_reset_due','tbm_tranches_pool','tbm_tranches_unknown_active'];
const fixture=(fn:(x:any)=>void|Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('tbm-cohorts-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const seed=fixtureSql(h,state);for(const name of optional)seed.exec('DROP INDEX IF EXISTS '+name);
 const pool=(id:string,reset=NOW+DAY)=>seed.exec(`INSERT INTO tbm_quota_pools(pool_id,provider,account,total_capacity_tokens,remaining_tokens,headroom_percentage,reset_at_ms,last_updated_ms,cycle_ms,capacity_known,quota_unit)
  VALUES(?,'claude','fixture',1000000,600000,60,?,?,18000000,1,'tokens')`,id,reset,NOW);
 const reset=(i:number)=>seed.exec(`INSERT INTO tbm_pool_resets(pool_id,reset_at_ms,provider,account,total_capacity_tokens,remaining_tokens,headroom_percentage,capacity_known,hard_floor_percent,captured_at_ms,rewarded)
  VALUES(?,?,'claude','fixture',1000000,600000,60,1,5,?,0)`,'fixture-pool-'+i%20,NOW-(141-i)*1000,NOW);
 const tranche=(i:number,expires=NOW+2*DAY)=>seed.exec(`INSERT INTO tbm_banked_tranches(tranche_id,pool_id,provider,allocated_tokens,remaining_tokens,granted_at_ms,expires_at_ms,status,expired_unused_tokens,updated_at_ms,quantity_known,account)
  VALUES(?,?,'claude',1000,800,?,?,'active',0,?,1,'fixture')`,'fixture-tranche-'+String(i).padStart(4,'0'),'fixture-pool-'+i%20,NOW-DAY,expires,NOW);
 for(let i=0;i<20;i++)pool('fixture-pool-'+i);for(let i=0;i<141;i++)reset(i);for(let i=0;i<16;i++)tranche(i);
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
 await fn({h,state,seed,pool,reset,tranche});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
 expect(seed.exec("SELECT name FROM sqlite_master WHERE type='index' AND name IN (?,?,?,?)",...optional).toArray()).toEqual([]);
});
describe('TBM control observes complete bounded cohorts without historical constructor indexes',()=>{
 it('initializes141/20/16 retained sources without optional index DDL or a false index-ready receipt',()=>fixture(({h,seed}:any)=>{
  seed.exec("DELETE FROM tbm_maintenance_cursor WHERE key IN ('structure_version','schema_version')");
  h.meter.runSync('tbm-retained-core',()=>ensureTbmControllerSchema(h.sql));
  const statements=h.meter.statementTop(1500).filter((row:any)=>row.scope==='tbm-retained-core');
  expect(statements.some((row:any)=>optional.some(name=>row.sql.includes(name)))).toBe(false);
  expect(statements.some((row:any)=>/FROM tbm_(?:quota_pools|pool_resets|banked_tranches)\b/.test(row.sql))).toBe(false);
  expect(seed.exec("SELECT value FROM tbm_maintenance_cursor WHERE key='structure_version'").one().value).toBe('2');
  expect(seed.exec("SELECT value FROM tbm_maintenance_cursor WHERE key='schema_version'").toArray()).toHaveLength(0);
 }));
 it('preserves FIFO100 reset processing, future deadlines and shared tranche/reset rollup truth',()=>fixture(({h,seed}:any)=>{
  seed.exec("UPDATE tbm_banked_tranches SET expires_at_ms=? WHERE tranche_id='fixture-tranche-0000'",NOW-1);
  expect(h.meter.runSync('tbm-known-due',()=>nextTbmDue(h.sql,NOW))).toBe(NOW+300000);
  const result=h.meter.runSync('tbm-known-tick',()=>tickTbm(h.sql,NOW));
  expect(result.resets_processed).toBe(100);expect(result.pools_updated).toBe(20);expect(result.tranches_updated).toBe(16);
  const rows=seed.exec('SELECT reset_at_ms,rewarded FROM tbm_pool_resets ORDER BY reset_at_ms,pool_id').toArray();
  expect(rows.slice(0,100).every((row:any)=>row.rewarded===1)).toBe(true);expect(rows.slice(100).every((row:any)=>row.rewarded===0)).toBe(true);
  const event=seed.exec('SELECT tokens_burned,net_reward,event_kind FROM tbm_reward_events WHERE event_id=?','reset:fixture-pool-0:'+(NOW-141000)).one();
  expect(event.tokens_burned).toBe(550000);expect(event.net_reward).toBeCloseTo(-151.25);expect(event.event_kind).toBe('reset');
  const rollup=seed.exec("SELECT leftover_quota_at_reset,banked_tokens_burned,banked_tokens_expired_unused FROM tbm_metrics_rollup WHERE rollup_id='fixture-pool-0:1h'").one();
  expect(rollup).toMatchObject({leftover_quota_at_reset:8*600000,banked_tokens_burned:200,banked_tokens_expired_unused:800});
  expect(h.meter.runSync('tbm-unprocessed-due',()=>nextTbmDue(h.sql,NOW))).toBe(NOW+300000);
  seed.exec('UPDATE tbm_pool_resets SET rewarded=1');
  expect(h.meter.runSync('tbm-future-due',()=>nextTbmDue(h.sql,NOW))).toBe(NOW+DAY);
  const statements=h.meter.statementTop(1500).filter((row:any)=>row.scope==='tbm-known-tick');
  expect(statements.filter((row:any)=>row.sql.startsWith('SELECT * FROM tbm_banked_tranches ORDER BY tranche_id'))).toHaveLength(1);
  expect(statements.some((row:any)=>/INDEXED BY tbm_tranches_pool|CREATE INDEX.*tbm_(?:resets_unrewarded_due|pools_reset_due|tranches_pool|tranches_unknown_active)/.test(row.sql))).toBe(false);
 }));
 it('preserves SQLite BINARY pool order at the100th equal-time reset boundary',()=>fixture(({h,seed}:any)=>{
  for(const id of ['A','a'])seed.exec(`INSERT INTO tbm_pool_resets(pool_id,reset_at_ms,provider,account,total_capacity_tokens,remaining_tokens,headroom_percentage,capacity_known,hard_floor_percent,captured_at_ms,rewarded)
    VALUES(?,?,'claude','fixture',1000000,600000,60,1,5,?,0)`,id,NOW-42500,NOW);
  expect(tickTbm(h.sql,NOW).resets_processed).toBe(100);
  expect(seed.exec('SELECT pool_id FROM tbm_pool_resets WHERE reset_at_ms=? AND rewarded=1',NOW-42500).toArray().map((row:any)=>row.pool_id)).toEqual(['A']);
 }));
 it('never invents a reset penalty when a real capture INSERT is ignored',()=>fixture(({h,seed,pool}:any)=>{
  pool('ignored-capture',NOW-1);
  seed.exec("CREATE TRIGGER ignore_reset_capture BEFORE INSERT ON tbm_pool_resets WHEN NEW.pool_id='ignored-capture' BEGIN SELECT RAISE(IGNORE); END");
  const result=tickTbm(h.sql,NOW);expect(result.resets_processed).toBe(100);
  expect(seed.exec("SELECT 1 FROM tbm_pool_resets WHERE pool_id='ignored-capture'").toArray()).toEqual([]);
  expect(seed.exec("SELECT 1 FROM tbm_reward_events WHERE pool_id='ignored-capture' AND event_kind='reset'").toArray()).toEqual([]);
  expect(seed.exec("SELECT leftover_quota_at_reset FROM tbm_metrics_rollup WHERE pool_id='ignored-capture'").toArray().every((row:any)=>row.leftover_quota_at_reset===0)).toBe(true);
 }));
 it('rolls up actual stored tranche facts when an expiry UPDATE is ignored',()=>fixture(({h,seed}:any)=>{
  seed.exec("UPDATE tbm_banked_tranches SET expires_at_ms=? WHERE tranche_id='fixture-tranche-0000'",NOW-1);
  seed.exec("CREATE TRIGGER ignore_tranche_expiry BEFORE UPDATE OF status ON tbm_banked_tranches WHEN OLD.tranche_id='fixture-tranche-0000' BEGIN SELECT RAISE(IGNORE); END");
  tickTbm(h.sql,NOW);
  expect(seed.exec("SELECT remaining_tokens,expired_unused_tokens,status FROM tbm_banked_tranches WHERE tranche_id='fixture-tranche-0000'").one()).toMatchObject({remaining_tokens:800,expired_unused_tokens:0,status:'active'});
  expect(seed.exec("SELECT banked_tokens_burned,banked_tokens_expired_unused FROM tbm_metrics_rollup WHERE rollup_id='fixture-pool-0:1h'").one()).toMatchObject({banked_tokens_burned:200,banked_tokens_expired_unused:0});
 }));
 for(const source of ['resets','tranches'])it('refuses513 complete-source overflow before any control effects: '+source,()=>fixture(({h,seed,reset,tranche}:any)=>{
  if(source==='resets')for(let i=141;i<513;i++)reset(i);else for(let i=16;i<513;i++)tranche(i);
  expect(()=>h.meter.runSync('tbm-overflow',()=>tickTbm(h.sql,NOW))).toThrow(TbmObservationLimit);
  expect(h.meter.snapshot()['tbm-overflow'].rows_written).toBe(0);expect(h.meter.snapshot()['tbm-overflow'].rows_read).toBeLessThan(800);
  expect(()=>nextTbmDue(h.sql,NOW)).toThrow(TbmObservationLimit);
  expect(seed.exec('SELECT 1 FROM tbm_reward_events LIMIT 1').toArray()).toEqual([]);
  expect(seed.exec('SELECT 1 FROM tbm_controller_state LIMIT 1').toArray()).toEqual([]);
 }));
 it('keeps the128-active-tranche ceiling before reset capture or pacing effects',()=>fixture(({h,seed,pool,tranche}:any)=>{
  for(let i=16;i<129;i++)tranche(i);pool('expired-new-pool',NOW-1);
  expect(()=>h.meter.runSync('tbm-active-overflow',()=>tickTbm(h.sql,NOW))).toThrow(TbmObservationLimit);
  expect(h.meter.snapshot()['tbm-active-overflow'].rows_written).toBe(0);
  expect(seed.exec("SELECT 1 FROM tbm_pool_resets WHERE pool_id='expired-new-pool'").toArray()).toEqual([]);
  expect(seed.exec('SELECT 1 FROM tbm_controller_state LIMIT 1').toArray()).toEqual([]);
 }));
 it('refuses prospective reset-capture growth before writing the extra reset',()=>fixture(({h,seed,pool,reset}:any)=>{
  for(let i=141;i<512;i++)reset(i);pool('expired-new-pool',NOW-1);
  expect(()=>h.meter.runSync('tbm-capture-overflow',()=>tickTbm(h.sql,NOW))).toThrow(TbmObservationLimit);
  expect(h.meter.snapshot()['tbm-capture-overflow'].rows_written).toBe(0);
  expect(seed.exec("SELECT 1 FROM tbm_pool_resets WHERE pool_id='expired-new-pool'").toArray()).toEqual([]);
 }));
});
