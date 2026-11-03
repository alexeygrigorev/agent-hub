import { describe,expect,it } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { WorkerAccess,revokeNativeWorkerCredentials,NATIVE_WORKER_PROJECT,workerAccessContext,workerRefreshContext } from '../src/worker-access';
import { initSessionMessaging,pollSessionCredentials } from '../src/session-messaging';
import { RowsMeter } from '../src/rows-meter';

const NOW=Date.now(),iso=new Date(NOW).toISOString();
const forbidden=['credentials_family','worker_access_session_family','idx_agents_project_machine'];
const fixture=(fn:(hub:any,raw:any)=>void|Promise<void>)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('native-cost-'+crypto.randomUUID())),async(hub:any,state:any)=>{
    for(const name of forbidden)state.storage.sql.exec('DROP INDEX IF EXISTS '+name);
    await fn(hub,state.storage.sql);
  });
function session(raw:any,id:string,state='running') {
  raw.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES(?,'test',?,'{}','host','codex','interactive',?,?,?,?)`,id,id,state,JSON.stringify({native_id:'n-'+id,tag:'tag-'+id,workspace:'/repo'}),iso,iso);
}
function credential(raw:any,id:string,family:string,actor:string,revoked=false,registryActor:string|null=actor) {
  raw.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,registry_actor,revoked_at)
    VALUES(?,'agent',?,?,?,?,?,?,'["worker:own-intake"]','test',?,?,?,?)`,id,'hash-'+id,'refresh-'+id,family,actor,'host',actor,iso,new Date(NOW+3600000).toISOString(),registryActor,revoked?iso:null);
}
function access(raw:any,id:string) {
  raw.exec('INSERT INTO worker_access VALUES(?,?,?,?,?,?)','family-'+id,id,id,'task','/repo',NOW+3600000);
}
function unrelatedHistory(raw:any) {
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
    INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,registry_actor,revoked_at)
    SELECT 'old-'||i,'agent','oldhash-'||i,'oldrefresh-'||i,'oldfamily-'||i,'other:'||i,'other','other:'||i,'[]','test',?,?, 'other:'||i,? FROM n`,iso,iso,iso);
}

describe('production-schema credential polling bounds',()=>{
  it('finds a late needy native fairly without scanning credential history or creating large indexes',()=>fixture(async(_hub,raw)=>{
    unrelatedHistory(raw);
    raw.exec("INSERT INTO intake_tasks(id,data) VALUES('task',?)",JSON.stringify({id:'task',project:'demo',status:'open',history:[],acceptance_criteria:'synthetic'}));
    for(let i=0;i<140;i++) {
      const id='covered-'+i;session(raw,id);access(raw,id);
      credential(raw,'credential-'+id,'family-'+id,NATIVE_WORKER_PROJECT+':native-'+id);
    }
    session(raw,'needy');
    raw.exec("INSERT INTO intake_session_links VALUES('needy','task','host_census_verified')");
    const meter=new RowsMeter(()=>NOW),sql=meter.wrap(raw),worker=new WorkerAccess(sql,fn=>fn(),()=>true);
    worker.init();initSessionMessaging(sql);
    const found:any[]=[];
    for(let i=0;i<3;i++) {
      const response=await meter.run('poll-'+i,()=>worker.listNative({kind:'host',machineId:'host'} as any,['host']));
      expect(response.status).toBe(200);
      found.push(...(await response.json() as any).sessions);
      expect(meter.snapshot()['poll-'+i].rows_read).toBeLessThan(800);
    }
    expect(found.map(row=>row.session_id)).toContain('needy');
    const indexes=raw.exec(`SELECT name FROM sqlite_master WHERE type='index' AND name IN (${forbidden.map(()=>'?').join(',')})`,...forbidden).toArray();
    expect(indexes).toEqual([]);
  }));

  it('bounds the running-state page before machine filtering on the actual deployed index layout',()=>fixture(async(_hub,raw)=>{
    raw.exec('DROP INDEX IF EXISTS session_control_machine_state');
    raw.exec('CREATE INDEX session_control_machine_state ON session_control_sessions(machine_id,state,created_at)');
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      SELECT 'busy-'||i,'test','busy-'||i,'{}','host','codex','interactive','running',?, ?,? FROM n`,JSON.stringify({native_id:'unlinked',workspace:'/repo'}),iso,iso);
    session(raw,'late');raw.exec("INSERT INTO intake_tasks(id,data) VALUES('task',?)",JSON.stringify({id:'task',project:'demo',status:'open'}));
    raw.exec("INSERT INTO intake_session_links VALUES('late','task','host_census_verified')");
    const meter=new RowsMeter(()=>NOW),worker=new WorkerAccess(meter.wrap(raw),fn=>fn(),()=>true);worker.init();
    let found=false;
    for(let i=0;i<34;i++) {
      const response=await meter.run('page-'+i,()=>worker.listNative({kind:'host',machineId:'host'} as any,['host']));
      expect(response.status).toBe(200);
      found ||= (await response.json() as any).sessions.some((row:any)=>row.session_id==='late');
      expect(meter.snapshot()['page-'+i].rows_read).toBeLessThan(200);
    }
    expect(found).toBe(true);
    const plan=raw.exec("EXPLAIN QUERY PLAN SELECT rowid,id,machine_id FROM session_control_sessions INDEXED BY session_control_state WHERE state='running' AND rowid>? ORDER BY rowid LIMIT 65",0).toArray().map((r:any)=>r.detail).join(' ');
    expect(plan).toContain('session_control_state');
    expect(plan).not.toMatch(/TEMP B-TREE|SCAN session_control_sessions/);
  }));

  it('revokes all native-family members by authoritative IDs and preserves unrelated credentials',()=>fixture((_hub,raw)=>{
    unrelatedHistory(raw);session(raw,'ended','stopped');access(raw,'ended');
    const actor=NATIVE_WORKER_PROJECT+':native-ended';
    credential(raw,'live-one','family-ended',actor);credential(raw,'live-two','family-ended',actor);
    credential(raw,'other-family','unrelated',actor);
    const meter=new RowsMeter(()=>NOW),sql=meter.wrap(raw);
    meter.runSync('revoke',()=>revokeNativeWorkerCredentials(sql,'ended',NOW));
    expect(raw.exec("SELECT id FROM credentials WHERE id IN ('live-one','live-two') AND revoked_at IS NULL").toArray()).toEqual([]);
    expect(raw.exec("SELECT revoked_at FROM credentials WHERE id='other-family'").one().revoked_at).toBeNull();
    expect(meter.snapshot().revoke.rows_read).toBeLessThan(30);
  }));

  it('rotates legacy unbound-registry native grants behind an authoritative family fence',()=>fixture(async(_hub,raw)=>{
    const id=crypto.randomUUID(),actor=NATIVE_WORKER_PROJECT+':native-'+id;
    session(raw,id);access(raw,id);
    // Deployed binding triggers normalize unresolved NULL inserts to an
    // empty registry binding. Insert that legacy state directly; immutable
    // binding guards remain installed and unchanged.
    credential(raw,'legacy','family-'+id,actor,false,'');
    raw.exec("INSERT INTO agents(tag,project,machine_id,registered_at) VALUES(?,?,'host',?)",'native-'+id,NATIVE_WORKER_PROJECT,iso);
    raw.exec("INSERT INTO intake_tasks(id,data) VALUES('task',?)",JSON.stringify({id:'task',project:'demo',status:'open'}));
    raw.exec("INSERT INTO intake_session_links VALUES(?,'task','host_census_verified')",id);
    const worker=new WorkerAccess(raw,fn=>fn(),()=>true);worker.init();
    expect(workerAccessContext(raw,'legacy',NOW)).not.toBeNull();
    const response=await worker.issueNative(new Request('https://hub.test/api/sessions/'+id+'/intake-credential',{method:'POST'}),{kind:'host',machineId:'host'} as any,['host']);
    expect(response.status).toBe(200);
    const fresh:any=raw.exec('SELECT id FROM credentials WHERE registry_actor=?',actor).one();
    expect(workerAccessContext(raw,'legacy')).toBeNull();
    expect(workerAccessContext(raw,fresh.id)).not.toBeNull();
    expect(workerRefreshContext(raw,fresh.id,Date.now(),true)).not.toBeNull();
    revokeNativeWorkerCredentials(raw,id,Date.now());
    expect(workerAccessContext(raw,fresh.id)).toBeNull();
    expect(workerRefreshContext(raw,'legacy',Date.now(),true)).toBeNull();
  }));

  it('refuses unknown native-family coverage without silently skipping revocation or minting',()=>fixture(async(_hub,raw)=>{
    session(raw,'overflow');access(raw,'overflow');
    const actor=NATIVE_WORKER_PROJECT+':native-overflow';
    for(let i=0;i<129;i++)credential(raw,'overflow-'+i,'family-overflow',actor);
    const meter=new RowsMeter(()=>NOW),worker=new WorkerAccess(meter.wrap(raw),fn=>fn(),()=>true);worker.init();
    const response=worker.listNative({kind:'host',machineId:'host'} as any,['host']);
    expect(response.status).toBe(503);
    expect((await response.json() as any).error).toBe('native_credential_observation_limit');
    expect(raw.exec("SELECT COUNT(*) AS n FROM credentials WHERE family_id='family-overflow' AND revoked_at IS NULL").one().n).toBe(129);
  }));

  it('messaging polls reuse deployed keys and ignore unrelated registry/credential history',()=>fixture((_hub,raw)=>{
    unrelatedHistory(raw);
    for(let i=0;i<1500;i++)raw.exec("INSERT INTO agents(tag,project,machine_id,registered_at) VALUES(?,'other','other',?)",'history-'+i,iso);
    session(raw,'messaging');raw.exec("INSERT INTO agents(tag,project,machine_id,registered_at) VALUES('messaging','session','host',?)",iso);
    const meter=new RowsMeter(()=>NOW),sql=meter.wrap(raw);initSessionMessaging(sql);
    const first=meter.runSync('messaging',()=>pollSessionCredentials(sql,'host',NOW));
    expect(first.map(row=>row.session_id)).toEqual(['messaging']);
    expect(meter.snapshot().messaging.rows_read).toBeLessThan(40);
  }));
});
