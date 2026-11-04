import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {pollSessionCredentials} from '../src/session-messaging';
import {RowsMeter} from '../src/rows-meter';
const fixture=(fn:(hub:any,raw:any)=>void)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('credential-session-cache-'+crypto.randomUUID())),(hub:any,state:any)=>fn(hub,state.storage.sql));
describe('per-host messaging credential pages',()=>{
  it('bounds 2100 equal-created rows and performs no SQL after a complete unchanged empty cycle',()=>fixture((_hub,raw)=>{
    raw.exec('DROP INDEX IF EXISTS session_control_machine_state');
    raw.exec('CREATE INDEX session_control_machine_state ON session_control_sessions(machine_id,state,created_at)');
    const at=new Date().toISOString(),binding=JSON.stringify({native_id:'native',tag:'tag',workspace:'/repo'});
    for(const host of ['host','foreign'])raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      SELECT ?||i,'test',?||i,'{}',?,'codex','interactive','running',?,?,? FROM n`,host+'-',host+'-',host,binding,at,at);
    let calls=0,generation='initial';
    const observed=new Proxy(raw,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{calls++;return target.exec(query,...args);};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
    const meter=new RowsMeter(),sql=meter.wrap(observed);
    for(let i=0;i<34;i++) {
      expect(meter.runSync('page-'+i,()=>pollSessionCredentials(sql,'host',Date.now(),false,()=>generation))).toEqual([]);
      expect(meter.snapshot()['page-'+i]?.rows_read??0).toBeLessThan(200);
    }
    const finished=calls;
    for(let i=0;i<10;i++)expect(pollSessionCredentials(sql,'host',Date.now(),false,()=>generation)).toEqual([]);
    expect(calls).toBe(finished);
    const first=raw.exec('SELECT rowid AS scan_id FROM session_control_sessions WHERE id=?','host-1').one();
    const plan=raw.exec("EXPLAIN QUERY PLAN SELECT rowid,id FROM session_control_sessions INDEXED BY session_control_machine_state WHERE machine_id=? AND state='running' AND created_at=? AND rowid>? ORDER BY rowid LIMIT 65",'host',at,first.scan_id).toArray().map((row:any)=>row.detail).join(' | ');
    expect(plan).toContain('session_control_machine_state');expect(plan).not.toMatch(/TEMP B-TREE|SCAN session_control_sessions/);
    raw.exec("INSERT INTO agents(tag,project,machine_id) VALUES('host-2100','session','host')");
    generation='new-identity';
    let found=false;
    for(let i=0;i<34;i++)found ||= pollSessionCredentials(sql,'host',Date.now(),false,()=>generation).some(row=>row.session_id==='host-2100');
    expect(found).toBe(true);
  }));
});
