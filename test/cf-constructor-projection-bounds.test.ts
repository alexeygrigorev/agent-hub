import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {backfillOnce,ProjectionObservationPending} from '../src/sql-probes';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';

const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get(
  (env as any).HUB.idFromName('projection-gate-'+crypto.randomUUID())),async(h:any,state:any)=>{
  await h.rowBudget.begin(true);
  const day=h.rowBudget.status().day,seed=fixtureSql(h,state),meter=new RowsMeter(),sql=meter.wrap(h.sql);
  seed.exec('CREATE TABLE constructor_source(id INTEGER PRIMARY KEY,value TEXT)');
  seed.exec('CREATE TABLE constructor_target(id INTEGER PRIMARY KEY,value TEXT)');
  seed.exec(`CREATE TRIGGER constructor_source_insert AFTER INSERT ON constructor_source BEGIN
    INSERT INTO constructor_target VALUES(NEW.id,NEW.value); END`);
  seed.exec(`CREATE TRIGGER constructor_source_delete AFTER DELETE ON constructor_source BEGIN
    DELETE FROM constructor_target WHERE id=OLD.id; END`);
  fn({seed,meter,sql});
  expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});

describe('constructor projection receipts',()=>{
  it('refuses retained data before running an unbounded callback or publishing a receipt',()=>fixture(({seed,meter,sql}:any)=>{
    for(let i=0;i<2100;i++)seed.exec('INSERT INTO constructor_source VALUES(?,?)',i,'retained');
    let calls=0;
    const callback=()=>{calls++;sql.exec('INSERT OR REPLACE INTO constructor_target SELECT * FROM constructor_source');};
    expect(()=>meter.runSync('constructor',()=>backfillOnce(sql,'test:missing',callback,false,['constructor_source'])))
      .toThrow(ProjectionObservationPending);
    expect(calls).toBe(0);
    expect(meter.snapshot().constructor.rows_read).toBeLessThan(20);
    expect(meter.snapshot().constructor.rows_written).toBe(0);
    expect(()=>meter.runSync('oversized-repair',()=>backfillOnce(sql,'test:missing',callback,true,['constructor_source'])))
      .toThrow(ProjectionObservationPending);
    expect(calls).toBe(0);
    expect(meter.snapshot()['oversized-repair'].rows_read).toBeLessThan(150);
    expect(meter.snapshot()['oversized-repair'].rows_written).toBe(0);
    expect(seed.exec("SELECT key FROM hub_init_stamps WHERE key='test:missing'").toArray()).toEqual([]);
    expect(seed.exec('SELECT value FROM constructor_source WHERE id=2099').one().value).toBe('retained');
  }));

  it('certifies empty initialization, trusts its receipt, and bounds explicit small repair',()=>fixture(({seed,meter,sql}:any)=>{
    let calls=0;
    const callback=()=>{calls++;sql.exec('INSERT OR IGNORE INTO constructor_target SELECT * FROM constructor_source');};
    backfillOnce(sql,'test:empty',callback,false,['constructor_source']);
    expect(calls).toBe(1);
    for(let i=0;i<2100;i++)seed.exec('INSERT INTO constructor_source VALUES(?,?)',i,'later');
    meter.runSync('existing-receipt',()=>backfillOnce(sql,'test:empty',callback,false,['constructor_source']));
    expect(calls).toBe(1);
    expect(meter.snapshot()['existing-receipt'].rows_read).toBeLessThan(10);
    expect(meter.snapshot()['existing-receipt'].rows_written).toBe(0);
    seed.exec('DELETE FROM constructor_source WHERE id>=2');
    // The source's trigger-maintained projection already represents the two
    // retained inputs. Explicit repair can inspect only this bounded source.
    backfillOnce(sql,'test:small-repair',callback,true,['constructor_source']);
    expect(calls).toBe(2);
    expect(seed.exec("SELECT key FROM hub_init_stamps WHERE key='test:small-repair'").toArray()).toHaveLength(1);
  }));
});
