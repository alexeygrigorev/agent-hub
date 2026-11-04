import { describe,expect,it } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { reservedLaunchPage,parseReservedLaunchCursor } from '../src/account-reserved-launches';
import { SILENT_START_REASON } from '../src/silent-start';
import { RowsMeter } from '../src/rows-meter';
import { reconcileVerifiedNoStarts } from '../src/account_capacity';

const NOW=Date.parse('2026-11-03T21:00:00Z'),AT=new Date(NOW).toISOString();
const fixture=(fn:(hub:any,raw:any)=>Promise<void>)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('fleet-existing-'+crypto.randomUUID())),async(hub:any,state:any)=>fn(hub,state.storage.sql));
function measured(raw:any) {
  let cursors:any[]=[];const queries:string[]=[];
  const native=new Proxy(raw,{get(target,key){
    if(key==='exec')return(q:string,...b:any[])=>{queries.push(q);const c=target.exec(q,...b);cursors.push(c);return c;};
    const v=target[key];return typeof v==='function'?v.bind(target):v;
  }});
  return {sql:new RowsMeter(()=>NOW).wrap(native),reset(){cursors=[];queries.length=0;},rows(){return cursors.reduce((n,c)=>n+Number(c.rowsRead),0);},queries};
}
function launch(raw:any,id:string,state:string,reason:string|null=null,updated=AT) {
  raw.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,exit_reason,verification_status,finished_at)
    VALUES(?,'test','human','test',?,'{}','host','codex','account','headless','/repo','x',?,?,?,?,?,?)`,
    id,id,state,updated,updated,reason,'unverified',AT);
}

describe('Fleet recovery and paging use deployed indexes',()=>{
  it('reconciliation advances bounded raw state pages past sparse retained failures',()=>fixture(async(_hub,raw)=>{
    for(let i=0;i<1000;i++)launch(raw,'old-'+i,'failed','unrelated');
    launch(raw,'recover-me','failed',SILENT_START_REASON);
    const m=measured(raw),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    let found=false;
    for(let page=0;page<42;page++) {
      m.reset();
      const rows=(fleet as any).reconciliationCandidatesUncached('host',false);
      expect(m.rows()).toBeLessThan(100);
      if(rows.some((row:any)=>row.id==='recover-me')) {found=true;break;}
    }
    expect(found).toBe(true);
    expect(m.queries.some(q=>q.includes('ORDER BY rowid LIMIT 26'))).toBe(true);
  }));

  it('reserved pages seek account/state/rowid before filtering old reservations',()=>fixture(async(_hub,raw)=>{
    const old=new Date(NOW-86400000).toISOString();
    for(let i=0;i<1000;i++)launch(raw,'stale-'+i,'offered',null,old);
    launch(raw,'eligible','offered');
    const context={live_ids:['host'],reservation_cutoff:new Date(NOW-900000).toISOString(),complete_catalogs:new Map<string,number>()};
    const m=measured(raw);let cursor=null;let found=false;
    for(let page=0;page<12;page++) {
      m.reset();const result=reservedLaunchPage(m.sql,'account',context,100,cursor);
      expect(m.rows()).toBeLessThan(230);
      if(result.rows.some(row=>row.id==='eligible'))found=true;
      if(!result.next_cursor)break;
      expect(result.next_cursor).toMatch(/^v2:/);
      cursor=parseReservedLaunchCursor(result.next_cursor);
    }
    expect(found).toBe(true);
    expect(m.queries.some(q=>q.includes('ORDER BY rowid LIMIT ?'))).toBe(true);
  }));

  it('no-start reconciliation advances bounded state pages and preserves positive receipt fences',()=>fixture(async(_hub,raw)=>{
    for(let i=0;i<1000;i++)launch(raw,'no-start-history-'+i,'running');
    launch(raw,'receipt-proven','running');
    const diagnostic={reason:'launcher_memory_reserve',message:'Host memory headroom after launcher reservations is below the configured floor.',
      error_type:'ValueError',stage:'dispatch_headless',exit_code:1};
    raw.exec('UPDATE fleet_launches SET launch_error_json=? WHERE id=?',JSON.stringify(diagnostic),'receipt-proven');
    const m=measured(raw);let settled=false;
    for(let page=0;page<42;page++) {
      m.reset();const changes=reconcileVerifiedNoStarts(m.sql as any,NOW);
      expect(m.rows()).toBeLessThan(150);
      if(changes.some(row=>row.id==='receipt-proven')) {settled=true;break;}
      expect(m.queries.some(query=>/^UPDATE|^INSERT|^DELETE/.test(query))).toBe(false);
    }
    expect(settled).toBe(true);
    expect(raw.exec("SELECT state,verification_status FROM fleet_launches WHERE id='receipt-proven'").one())
      .toMatchObject({state:'failed',verification_status:'not_started'});
  }));

  it('account capacity bounds indexed active prefixes before foreign-host filtering',()=>fixture(async(_hub,raw)=>{
    raw.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES('host','host','host',?,?,?)",AT,AT,JSON.stringify({engines:['codex']}));
    // Same account and same machine: a machine-only plan reads all retained
    // terminal history even when there are just two live natives to count.
    for(let i=0;i<2100;i++) {
      raw.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account)
        VALUES('host',?,?,'codex','exited',?,'account')`,'old-'+i,'old-'+i,AT);
      launch(raw,'terminal-'+i,'stopped');
    }
    for(const phase of ['running','unknown']) raw.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account)
      VALUES('host',?,?,'codex',?,?,'account')`,phase,phase,phase,AT);
    const m=measured(raw),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    const definition={label:'account',provider:'codex',max_concurrency:10,reserve_percent:15,enabled:1};
    // Skip unrelated maintenance only in this component query-cost test;
    // the real account-status implementation and all accounting SQL run.
    (fleet as any).reconcileCapacity=()=>{};
    m.reset();const known=fleet.accountStatus(definition);
    expect(known).toMatchObject({active:2,available_slots:8,capacity_observation_limited:false});
    expect(m.rows()).toBeLessThan(150);
    console.log(JSON.stringify({evidence:'route_service_native_cost',operation:'FleetService.accountStatus',case:'2100terminal history per table,2live natives',rows_read:m.rows(),excludes:'outer authentication, KV ledger,maintenance and guard migration preflight'}));
    for(const query of m.queries.filter(q=>q.includes('INDEXED BY fleet_session_account_phase')||q.includes('INDEXED BY fleet_launch_account_state'))) {
      const detail=raw.exec('EXPLAIN QUERY PLAN '+query,'account').toArray().map((r:any)=>r.detail).join(' | ');
      expect(detail).toMatch(/SEARCH.*USING INDEX fleet_(session_account_phase|launch_account_state)/);
      expect(detail).not.toMatch(/TEMP B-TREE|^SCAN /);
    }
    // Raw foreign-host occupancy must refuse, rather than scanning until it
    // finds enough eligible local rows or declaring the local account free.
    for(let i=0;i<257;i++) raw.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account)
      VALUES('foreign',?,?,'codex','running',?,'account')`,'foreign-'+i,'foreign-'+i,AT);
    m.reset();const limited=fleet.accountStatus(definition);
    expect(limited).toMatchObject({status:'blocked',available_slots:0,placeable_slots:0,native_live:null,
      capacity_observation_limited:true,active_is_lower_bound:true});
    expect(limited.placement_reasons).toContain('capacity_observation_limited');
    expect(m.rows()).toBeLessThan(700);
    expect(m.queries.some(q=>q.includes('COUNT(*)'))).toBe(false);
    raw.exec("DELETE FROM fleet_sessions WHERE machine_id='foreign'");
    for(let i=0;i<257;i++)launch(raw,'stale-offer-'+i,'offered',null,new Date(NOW-86400000).toISOString());
    m.reset();const stalePrefix=fleet.accountStatus(definition);
    expect(stalePrefix).toMatchObject({status:'blocked',available_slots:0,capacity_observation_limited:true});
    expect(m.rows()).toBeLessThan(700);
    expect(m.queries.some(q=>q.includes('WHERE l.id=?'))).toBe(false);
  }));

  it('maps legacy three-part session cursors to the existing primary-key order',()=>fixture(async(_hub,raw)=>{
    for(let i=0;i<1200;i++)raw.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace)
      VALUES('host',?,?,'exited',?,'/repo')`,'native-'+String(i).padStart(4,'0'),'tag-'+(1200-i),AT);
    const m=measured(raw),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    const encoded=btoa(JSON.stringify(['host','old-tag','native-0500'])).replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_');
    const response=await fleet.handle(new Request('https://hub/api/fleet?sections=sessions&sessions_limit=10&sessions_cursor='+encoded),
      {caller:{kind:'human',actor:'test',isAdmin:true},scopes:['admin']} as any);
    const body:any=await response.json();
    expect(body.sessions[0].native_id).toBe('native-0501');
    expect(body.sessions_order).toEqual(['machine_id','native_id']);
    expect(body.sessions_total).toBeNull();
    expect(m.rows()).toBeLessThan(100);
    const cursor=JSON.parse(atob(body.sessions_cursor_next.replace(/-/g,'+').replace(/_/g,'/')));
    expect(cursor).toHaveLength(2);
  }));
});
