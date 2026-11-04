import {describe,it,expect,vi} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {fixtureSql} from './helpers/fixture-budget';
import {sha256} from '../src/auth/crypto';
import {freshCpu} from './cpu-fixture';

const ID='11111111-1111-4111-8111-111111111111';
describe('terminal Fleet claim replay bounds',()=>{
 it('rejects terminal replays before maintenance or admission while retaining live offered gates',()=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('claim-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
   const seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString(),fleet=h.fleetService;
   const insert=(id:string)=>seed.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,claim_key)
    VALUES(?,'fixture','agent','retained',?,'{}','host','codex','account','headless','/repo','','exited',?,?,'previous-claim')`,id,id,at,at);
   for(let i=0;i<2100;i++)insert('retained-'+i);insert(ID);
   await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
   const spies=[vi.spyOn(fleet.resources,'cpuHeadroom'),vi.spyOn(fleet,'accountStatus'),vi.spyOn(fleet,'observationProvenance'),
    vi.spyOn(fleet.admission,'heldReason'),vi.spyOn(fleet.admission,'retireInvalidOffer'),vi.spyOn(fleet,'reconcileCapacity'),vi.spyOn(fleet,'drainCapacityQueue')];
   const auth={caller:{kind:'host',machineId:'host'},scopes:['host']};
   const claim=(body:any={},who:any=auth)=>fleet.handle(new Request('https://hub/api/fleet/launches/'+ID+'/claim',
    {method:'POST',body:JSON.stringify(body)}),who);
   try{
    await h.meter.run('terminal-claims',async()=>{
     for(let i=0;i<30;i++){
      const result=await claim(i%2?{claim_key:'previous-claim'}:{});
      expect(result.status).toBe(409);expect(await result.json()).toEqual({error:'already_claimed'});
     }
    });
    const work=h.meter.snapshot()['terminal-claims'];
    expect(work.calls).toBe(30);expect(work.rows_read).toBeLessThan(4);expect(work.rows_written).toBe(0);
    for(const spy of spies)expect(spy).not.toHaveBeenCalled();
    expect((await claim({claim_key:'invalid key'})).status).toBe(400);
    expect((await claim({}, {caller:{kind:'host',machineId:'foreign'},scopes:['host']})).status).toBe(404);
    expect((await claim({}, {caller:{kind:'agent',actor:'fixture'},scopes:['fleet:launch']})).status).toBe(403);
    expect(seed.exec('SELECT state,claim_key,launch_error_json FROM fleet_launches WHERE id=?',ID).one()).toEqual({state:'exited',claim_key:'previous-claim',launch_error_json:null});
    seed.exec("UPDATE fleet_launches SET state='offered' WHERE id=?",ID);
    const live=await claim();
    // The real resource gate still executes and refuses this host's missing
    // CPU evidence. No mocks grant admission or skip production checks.
    expect(live.status).toBe(409);expect(spies[0]).toHaveBeenCalledTimes(1);
    expect(seed.exec('SELECT state FROM fleet_launches WHERE id=?',ID).one().state).toBe('offered');
    expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
   }finally{for(const spy of spies)spy.mockRestore();}
  }));
 it('keeps settled Offers clean after terminal HTTP replay but invalidates them after a real successful claim',()=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('claim-offers-causal-'+crypto.randomUUID())),async(h:any,state:any)=>{
   const seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString(),future=new Date(now+86400000).toISOString();
   const token='hubt_claim_offers_causal_fixture',hash=await sha256(token),fleet=h.fleetService;
   seed.exec(`INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities,credential_id)
    VALUES('claim-host','claim-host','claim-host',?,?,?,0,?,'claim-host-token')`,at,at,
    JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,load:[1],cpu_usage:freshCpu(at),gate:{launch_allowed:true,at}}),
    JSON.stringify({engines:['codex'],fleet_modes:['headless'],workspaces:['demo'],cpu_count:8}));
   seed.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,last_used_at)
    VALUES('claim-host-token','host',?,'claim-family','host:claim-host','claim-host','host:claim-host','["host"]','fixture',?,?,?)`,hash,at,future,at);
   seed.exec("INSERT INTO fleet_accounts VALUES('claim-account','codex',1,15,1)");
   seed.exec("INSERT INTO fleet_routes VALUES('claim-host','codex','claim-account',NULL)");
   seed.exec(`INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json)
    VALUES('claim-host','1.0',?,?,?,?,'ok',NULL,?)`,at,now,at,now,
    JSON.stringify({codex:{status:'ok',observed_at:at,details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:future}}}}));
   seed.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
    VALUES(?,'fixture','agent','terminal','terminal','{}','claim-host','codex','claim-account','headless','demo','','stopped',?,?)`,ID,at,at);
   await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
   // Freeze only the periodic maintenance clock for this causal test. Real
   // authentication, quota, CPU and memory checks retain their production SQL.
   const originalNow=fleet.now;fleet.now=()=>now;
   const maintenance=vi.spyOn(fleet,'reconcileCapacity');
   const http=(path:string,body?:any)=>h.fetch(new Request('https://hub'+path,{method:body===undefined?'GET':'POST',
    headers:{Authorization:'Bearer '+token},...(body===undefined?{}:{body:JSON.stringify(body)})}));
   try{
    const launch=fleet.reserve({workspace:'demo',prompt:'Real causal claim',machine:'claim-host',idempotency_key:'causal-claim'},
     {caller:{kind:'human',isAdmin:true,actor:'fixture-operator'},scopes:['admin']});
    expect(launch.status,JSON.stringify(launch.value)).toBe(201);
    const settled=await http('/api/fleet/offers');expect(settled.status,await settled.clone().text()).toBe(200);
    expect((await settled.json()).offers.some((offer:any)=>offer.id===launch.value.id)).toBe(true);
    maintenance.mockClear();
    const replay=await http('/api/fleet/launches/'+ID+'/claim',{});
    expect(replay.status).toBe(409);expect(await replay.json()).toEqual({error:'already_claimed'});
    const afterReplay=await http('/api/fleet/offers');expect(afterReplay.status).toBe(200);await afterReplay.json();
    expect(maintenance).not.toHaveBeenCalled();
    const claimed=await http('/api/fleet/launches/'+launch.value.id+'/claim',{gate_allowed:true,profile_supported:true,
     observed_at:at,account:'claim-account',model:launch.value.selection.model});
    expect(claimed.status,await claimed.clone().text()).toBe(200);expect(await claimed.json()).toMatchObject({state:'starting'});
    // Admission itself performs real maintenance. Clear that observation so
    // the assertion below proves the NEXT Offers request noticed the mutation.
    maintenance.mockClear();
    const afterClaim=await http('/api/fleet/offers');expect(afterClaim.status).toBe(200);await afterClaim.json();
    expect(maintenance).toHaveBeenCalled();
    expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
   }finally{maintenance.mockRestore();fleet.now=originalNow;}
  }));

});
