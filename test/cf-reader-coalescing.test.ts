import {describe,it,expect,vi} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {inboxReaderStatus,prepareReaderObservation} from '../src/alert-routing';
const withHub=(fn:(hub:any,state:any)=>Promise<void>|void)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('reader-coalescing-'+crypto.randomUUID())),fn);
function seed(hub:any,state:any,now:number){
  const raw=state.storage.sql,at=new Date(now).toISOString();
  raw.exec("INSERT INTO agents(tag,project,registered_at) VALUES('reader','team',?)",at);
  raw.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
    VALUES('reader-token','agent','reader-token-hash','reader-family','reader','team:reader','["bus:read"]','test',?,?)`,at,new Date(now+7200000).toISOString());
  return{kind:'agent' as const,actor:'team:reader',tokenId:'reader-token'};
}
describe('reader loop write coalescing',()=>{
  it('45s polls retain fresh live proof with zero fast writes and at most one hourly checkpoint',()=>withHub(async(hub,state)=>{
    let now=Date.now();const start=now,caller=seed(hub,state,now),clock=vi.spyOn(Date,'now').mockImplementation(()=>now);
    try{
      hub.busService.inboxPage(caller,'team:reader');
      await hub.meter.run('reader-fast',()=>{for(let i=1;i<80;i++){now=start+i*45000;hub.busService.inboxPage(caller,'team:reader');}});
      expect(hub.meter.snapshot()['reader-fast'].rows_written).toBe(0);
      expect(inboxReaderStatus(hub.sql,'team:reader',now).active_reader).toBe(true);
      // A new SQL identity simulates eviction: only an old durable checkpoint
      // survives, which cannot fabricate a live 15-minute reader loop.
      expect(inboxReaderStatus(state.storage.sql,'team:reader',now).active_reader).toBe(false);
      now=start+3600000;
      await hub.meter.run('reader-hour',()=>hub.busService.inboxPage(caller,'team:reader'));
      expect(hub.meter.snapshot()['reader-hour'].rows_written).toBeGreaterThan(0);
      expect(hub.meter.snapshot()['reader-hour'].rows_written).toBeLessThanOrEqual(4);
      expect(state.storage.sql.exec('SELECT last_read_at FROM bus_reader_activity').one().last_read_at).toBe(now);
      hub.sql.exec('UPDATE credentials SET revoked_at=? WHERE id=?',new Date(now).toISOString(),'reader-token');
      expect(inboxReaderStatus(hub.sql,'team:reader',now).active_reader).toBe(false);
    }finally{clock.mockRestore();}
  }));

  it('failed transactions cannot publish live proof',()=>withHub((hub,state)=>{
    const start=Date.now(),caller=seed(hub,state,start);
    hub.busService.inboxPage(caller,'team:reader');
    const persisted=state.storage.sql.exec('SELECT last_read_at FROM bus_reader_activity').one().last_read_at;
    const later=start+20*60000;
    expect(()=>state.storage.transactionSync(()=>{
      prepareReaderObservation(hub.sql,'team:reader',caller.tokenId,later);
      throw new Error('rollback');
    })).toThrow('rollback');
    expect(inboxReaderStatus(hub.sql,'team:reader',later).active_reader).toBe(false);
    expect(state.storage.sql.exec('SELECT last_read_at FROM bus_reader_activity').one().last_read_at).toBe(persisted);
  }));

  it('an outer Hub transaction rollback clears proof published by a nested inbox call',()=>withHub((hub,state)=>{
    let now=Date.now();const caller=seed(hub,state,now),clock=vi.spyOn(Date,'now').mockImplementation(()=>now);
    try {
      hub.busService.inboxPage(caller,'team:reader');
      const persisted=now;now+=20*60000;
      expect(()=>state.storage.transactionSync(()=>{
        hub.busService.inboxPage(caller,'team:reader');
        throw new Error('outer-rollback');
      })).toThrow('outer-rollback');
      expect(inboxReaderStatus(hub.sql,'team:reader',now).active_reader).toBe(false);
      expect(state.storage.sql.exec('SELECT last_read_at FROM bus_reader_activity').one().last_read_at).toBe(persisted);
    } finally {clock.mockRestore();}
  }));

  it('outer rollback cannot retain an inbox resume offset past a transient read',()=>withHub((hub,state)=>{
    const now=Date.now(),caller=seed(hub,state,now);
    hub.sql.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,recipient_seq)
      VALUES('rolled-back-read','human:test','team:reader','note','rollback-thread','body','rollback-read',?,?,1)`,now,now+3600000);
    expect(()=>state.storage.transactionSync(()=>{
      hub.sql.exec("UPDATE messages SET read_at=? WHERE id='rolled-back-read'",now);
      expect(hub.busService.inboxPage(caller,'team:reader',50,undefined,{unread:true}).rows).toEqual([]);
      throw new Error('outer-read-rollback');
    })).toThrow('outer-read-rollback');
    expect(hub.busService.inboxPage(caller,'team:reader',50,undefined,{unread:true}).rows.map((r:any)=>r.id)).toEqual(['rolled-back-read']);
  }));

  it('an inbox delivery failure cannot fabricate a fresh live read loop',()=>withHub((hub,state)=>{
    let now=Date.now();const caller=seed(hub,state,now),clock=vi.spyOn(Date,'now').mockImplementation(()=>now);
    try{
      hub.busService.inboxPage(caller,'team:reader');
      const persisted=now;now+=20*60000;
      hub.sql.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
        VALUES('failing-delivery','human:test','team:reader','note','failing-delivery','body','failing-delivery',?,?)`,now,now+3600000);
      const stage=vi.spyOn(hub.busService.hooks,'onStage').mockImplementation(()=>{throw new Error('delivery-ledger-failed');});
      expect(()=>hub.busService.inboxPage(caller,'team:reader')).toThrow('delivery-ledger-failed');
      stage.mockRestore();
      expect(inboxReaderStatus(hub.sql,'team:reader',now).active_reader).toBe(false);
      expect(state.storage.sql.exec('SELECT last_read_at FROM bus_reader_activity').one().last_read_at).toBe(persisted);
      expect(state.storage.sql.exec("SELECT delivered_at FROM messages WHERE id='failing-delivery'").one().delivered_at).toBe(null);
    }finally{clock.mockRestore();}
  }));

  it('bounds raw recipient reader history before stale filters and reports overflow unknown',()=>withHub((hub,state)=>{
    const now=Date.now();seed(hub,state,now);const raw=state.storage.sql;
    for(let i=0;i<1200;i++)raw.exec('INSERT INTO bus_reader_activity VALUES(?,?,?)','team:reader','old-'+String(i).padStart(4,'0'),now-86400000);
    let reads=0;
    const sql:any={exec(q:string,...args:any[]){const cursor=raw.exec(q,...args),rows=cursor.toArray();reads+=cursor.rowsRead;return{toArray:()=>rows};}};
    const result=inboxReaderStatus(sql,'team:reader',now);
    expect(result.active_reader).toBe(false);expect(result.observation_pending).toBe(true);
    expect(reads).toBeLessThan(300);
  }));
});
