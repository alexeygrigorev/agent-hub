import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, adoptObservedNativeSessions } from '../src/session_control';
import { refreshObservedHostFamily, expireUnclaimedStops, visibleSessionPrefixMatches } from '../src/session_stop';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'stop-test', kind: 'human' }, scopes: ['admin'] };
const host = { caller: { machineId: 'stop-host', kind: 'host', actor: 'machine:stop-host', tokenId: 'stop-host-credential' }, scopes: ['host'] };
const nativeId = '61bba72f-e254-48d4-8166-0e11a16e99a5';
const native = (id = nativeId) => ({native_id:id, tag:'stop-test', workspace:'/srv/stop-test', engine:'shell', phase:'running', created_at_ms:1792000000000});
function req(path: string, data?: unknown) { return new Request('https://hub.test' + path, {method:data === undefined ? 'GET':'POST', ...(data === undefined ? {} : {body:JSON.stringify(data)})}); }
async function fixture(fn: (service: SessionControlService, sql: any, advance: (ms:number)=>void) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('stop-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    let now = Date.now();
    const service = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    service.init();
    const at = new Date(now).toISOString(), far = new Date(now+86400000).toISOString();
    hub.sql.exec(`INSERT INTO credentials (id,kind,token_hash,refresh_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at)
      VALUES ('stop-host-credential','host','stop-hash','stop-refresh','current-family','test','stop-host','["host"]','test',?,?)`,at,far);
    hub.sql.exec(`INSERT INTO machines (id,name,hostname,credential_id,registered_at) VALUES ('stop-host','test','test','stop-host-credential',?)`,at);
    await fn(service,hub.sql,ms=>{now+=ms;});
  });
}

describe('stop delivery for adopted sessions', () => {
  it('refreshes an adopted row family from a fresh authoritative native observation, then claims stop', async () => {
    await fixture(async (s,sql) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'old-family');
      const queued = await s.handle(req('/api/sessions/'+nativeId+'/stop',{key:'adopted-stop'}),admin);
      expect(queued.status).toBe(201);
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'current-family');
      const offers = await (await s.handle(req('/api/session-commands/offers'),host)).json() as any;
      expect(offers.offers).toHaveLength(1);
      const offer = offers.offers[0];
      const claimed = await s.handle(req('/api/session-commands/'+offer.command_id+'/claim', {correlation:offer.correlation, attempt_id:crypto.randomUUID()}),host);
      expect(claimed.status).toBe(200);
      expect((await claimed.json() as any).claim_token).toBeTruthy();
      const row = sql.exec('SELECT state FROM session_control_sessions WHERE id = ?',nativeId).one();
      expect(row.state).toBe('running');
    });
  });
  it('does not let twenty old-family commands hide a current adopted stop', async () => {
    await fixture(async (s,sql,advance) => {
      for (let i=0; i<21; i++) {
        const id = crypto.randomUUID();
        adoptObservedNativeSessions(sql,'stop-host',[{...native(id),tag:'old-'+i}],Date.now(),'old-family');
        await s.handle(req('/api/sessions/'+id+'/stop',{key:'old-stop-'+i}),admin);
        advance(10);
      }
      adoptObservedNativeSessions(sql,'stop-host',[{...native(),name:'owned-name'}],Date.now(),'current-family');
      expect((await s.handle(req('/api/sessions/owned-name/stop',{key:'current-stop'}),admin)).status).toBe(201);
      const offers = await (await s.handle(req('/api/session-commands/offers'),host)).json() as any;
      expect(offers.offers).toHaveLength(1);
      expect(offers.offers[0].session_id).toBe(nativeId);
    });
  });
  it('requires a positive exit receipt for direct adopted terminal observations', async () => {
    await fixture(async (_s,sql) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'current-family');
      for (const confirmed_exit of [undefined, false]) {
        adoptObservedNativeSessions(sql,'stop-host',[{...native(),phase:'exited',confirmed_exit}],Date.now(),'current-family');
        expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?',nativeId).one().state).toBe('uncertain');
      }
      adoptObservedNativeSessions(sql,'stop-host',[{...native(),phase:'exited',confirmed_exit:true}],Date.now(),'current-family');
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?',nativeId).one().state).toBe('stopped');
    });
  });
  it('marks stopped only after the exact bound host stop acknowledgement', async () => {
    await fixture(async (s,sql) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'current-family');
      await s.handle(req('/api/sessions/'+nativeId+'/stop',{key:'verified-stop'}),admin);
      const listing = await (await s.handle(req('/api/session-commands/offers'),host)).json() as any;
      const offer = listing.offers[0];
      const claim = await (await s.handle(req('/api/session-commands/'+offer.command_id+'/claim',
        {correlation:offer.correlation,attempt_id:crypto.randomUUID()}),host)).json() as any;
      const payload = {correlation:offer.correlation,claim_token:claim.claim_token,outcome:'delivered',confirmed_exit:true,
        native:{native_id:nativeId,tag:'stop-test',workspace:'/srv/stop-test'}};
      const bad = await s.handle(req('/api/session-commands/'+offer.command_id+'/ack',
        {...payload,native:{...payload.native,tag:'foreign'}}),host);
      expect(bad.status).toBe(409);
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?',nativeId).one().state).toBe('running');
      expect((await s.handle(req('/api/session-commands/'+offer.command_id+'/ack',payload),host)).status).toBe(200);
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?',nativeId).one().state).toBe('stopped');
    });
  });
  it.each([null, 'deadbeef-aaaa-4aaa-8aaa-aaaaaaaaaaaa'])('settles a linked unknown Fleet native, preserving conflicting custody (%s)', async (fleetNative) => {
    await fixture(async (s,sql) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'current-family');
      const launchId = crypto.randomUUID(), at = new Date().toISOString();
      sql.exec(`INSERT INTO fleet_launches
        (id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
        VALUES (?,'stop-test','human','stop fixture',?,'{}','stop-host','shell','stop-account','headless','/srv/stop-test','','uncertain',?,?,?)`,
        launchId,launchId,fleetNative,at,at);
      sql.exec('UPDATE session_control_sessions SET fleet_launch_id = ? WHERE id = ?',launchId,nativeId);
      await s.handle(req('/api/sessions/'+nativeId+'/stop',{key:'linked-stop'}),admin);
      const offers = await (await s.handle(req('/api/session-commands/offers'),host)).json() as any;
      const offer = offers.offers[0];
      const claim = await (await s.handle(req('/api/session-commands/'+offer.command_id+'/claim',
        {correlation:offer.correlation,attempt_id:crypto.randomUUID()}),host)).json() as any;
      const ack = await s.handle(req('/api/session-commands/'+offer.command_id+'/ack',
        {correlation:offer.correlation,claim_token:claim.claim_token,outcome:'delivered',confirmed_exit:true,
          native:{native_id:nativeId,tag:'stop-test',workspace:'/srv/stop-test'}}),host);
      expect(ack.status).toBe(200);
      const launch = sql.exec('SELECT state,native_id,verification_status FROM fleet_launches WHERE id = ?',launchId).one();
      expect(launch.state).toBe(fleetNative === null ? 'exited' : 'uncertain');
      expect(launch.native_id).toBe(fleetNative);
      if (fleetNative === null) expect(launch.verification_status).toBe('verified');
      else expect(launch.verification_status).not.toBe('verified');
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?',nativeId).one().state).toBe('stopped');
    });
  });
  it('resolves unique UUID prefixes, and reports all visible ambiguous matches', async () => {
    await fixture(async (s,sql) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'current-family');
      expect((await s.handle(req('/api/sessions/61bba72f/stop',{key:'short-stop'}),admin)).status).toBe(201);
      const secondId = '61bba72f-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
      adoptObservedNativeSessions(sql,'stop-host',[{...native(secondId),tag:'second'}],Date.now(),'current-family');
      const response = await s.handle(req('/api/sessions/61bba72f/stop',{key:'ambiguous-stop'}),admin);
      expect(response.status).toBe(409);
      const body = await response.json() as any;
      expect(body.error).toBe('ambiguous_session_prefix');
      expect(body.matches.map((row:any)=>row.id).sort()).toEqual([nativeId,secondId].sort());
    });
  });
  it('fails an unclaimed stop within the delivery budget without falsely marking the session stopped', async () => {
    await fixture(async (s,sql,advance) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'current-family');
      await s.handle(req('/api/sessions/'+nativeId+'/stop',{key:'offline-stop'}),admin);
      advance(9000);
      const body = await (await s.handle(req('/api/sessions/'+nativeId+'/commands'),admin)).json() as any;
      expect(body.commands[0].state).toBe('failed');
      expect(body.commands[0].ack.reason).toBe('stop_host_unreachable');
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?',nativeId).one().state).toBe('running');
      const offers = await (await s.handle(req('/api/session-commands/offers'),host)).json() as any;
      expect(offers.offers).toHaveLength(0);
    });
  });
});


describe('stop custody safety', () => {
  it('renews only current enrolled authority and the exact adopted incarnation', async () => {
    await fixture(async (_s,sql) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'old-family');
      const existing = sql.exec('SELECT * FROM session_control_sessions WHERE id = ?',nativeId).one();
      refreshObservedHostFamily(sql,'stop-host',existing,{...native(),created_at_ms:1792000000001},'current-family');
      expect(sql.exec('SELECT host_family FROM session_control_sessions WHERE id = ?',nativeId).one().host_family).toBe('old-family');
      refreshObservedHostFamily(sql,'stop-host',existing,native(),'unrelated-family');
      expect(sql.exec('SELECT host_family FROM session_control_sessions WHERE id = ?',nativeId).one().host_family).toBe('old-family');
      refreshObservedHostFamily(sql,'stop-host',existing,native(),'current-family');
      expect(sql.exec('SELECT host_family FROM session_control_sessions WHERE id = ?',nativeId).one().host_family).toBe('current-family');
      expect(visibleSessionPrefixMatches(sql,'61bba72f',()=>false)).toEqual([]);
      expect(visibleSessionPrefixMatches(sql,'61bba72',()=>true)).toEqual([]);
    });
  });
  it('never expires a stop after a host has acquired execution custody', async () => {
    await fixture(async (s,sql,advance) => {
      adoptObservedNativeSessions(sql,'stop-host',[native()],Date.now(),'current-family');
      await s.handle(req('/api/sessions/'+nativeId+'/stop',{key:'claimed-stop'}),admin);
      const offers = await (await s.handle(req('/api/session-commands/offers'),host)).json() as any;
      const offer = offers.offers[0];
      await s.handle(req('/api/session-commands/'+offer.command_id+'/claim',{correlation:offer.correlation,attempt_id:crypto.randomUUID()}),host);
      advance(9000);
      expireUnclaimedStops(sql,Date.now()+9000,nativeId);
      expect(sql.exec('SELECT state FROM session_control_commands WHERE id = ?',offer.command_id).one().state).toBe('claimed');
    });
  });
});
