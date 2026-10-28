import { describe, it, expect } from 'vitest';
import { env, SELF, runInDurableObject, createExecutionContext, waitOnExecutionContext, createScheduledController } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { SCHEMA_SQL } from '../src/schema';
import { BUS_SCHEMA_SQL } from '../src/bus';
import { migrateAgentRegistry } from '../src/agent_registry';
import { Hub, BaseHub } from '../src/conversation-hub';
import worker from '../src/worker';
import type { Env } from '../src/env';
const bindings = env as unknown as Env;
const stub = () => bindings.HUB.get(bindings.HUB.idFromName('hub'));
const csrf = 'local-f7-csrf';
async function admin(sub = 'local-owner', email = 'admin@fake.local') {
  return (await createSessionCookie(bindings, { sub, email, isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
}
async function req(path: string, method = 'GET', body?: any, auth?: string, csrfValue: string | null = csrf) {
  const headers: Record<string,string> = {};
  if (auth?.startsWith('hubt_')) headers.Authorization = 'Bearer '+auth;
  else if (auth) { headers.Cookie = auth; if (csrfValue !== null) headers['X-CSRF-Token'] = csrfValue; }
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return SELF.fetch('https://hub.test'+path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
}
async function rows(query: string, ...args: any[]) {
  let result: any[] = [];
  await runInDurableObject(stub() as any, (hub: Hub) => { result = hub.sql.exec(query,...args).toArray(); });
  return result;
}
async function credential(actor: string, kind = 'agent', scopes = ['bus:send','bus:read'], extra: any = {}) {
  const token = 'hubt_'+crypto.randomUUID().replaceAll('-',''); const hash = await sha256(token);
  await rows(`INSERT INTO credentials (id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at,revoked_at,machine_id)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, token,kind,hash,token,actor,actor,JSON.stringify(scopes),'local-owner',new Date().toISOString(),extra.expires ?? new Date(Date.now()+3600000).toISOString(),extra.revoked ?? null,extra.machine ?? null);
  return token;
}
async function register(project: string | null, tag: string, cookie: string, extra: any = {}) {
  return req('/api/agents','POST',{tag, ...(project ? {project}: {}),...extra},cookie);
}
async function conversation(token: string, members: string[] = []) {
  const res = await req('/api/conversations','POST',{kind:'group',title:'Local F7',members,idempotency_key:crypto.randomUUID()},token);
  expect(res.status).toBe(201); return ((await res.json()) as any).conversation.id;
}

describe('F7 actual Worker -> bound ConversationHub / existing authority', () => {
  it('uses the real schema, admin-only registry, scoped credentials and qualified delete', async () => {
    const cookie = await admin();
    expect((await req('/api/agents')).status).toBe(401);
    expect((await req('/api/agents','POST',{tag:'worker',project:'pA'},cookie,null)).status).toBe(403);
    expect((await register('pA','worker',cookie)).status).toBe(201);
    expect((await register('pB','worker',cookie)).status).toBe(201);
    expect((await register('pA','worker',cookie)).status).toBe(409);
    expect((await register(null,'worker',cookie)).status).toBe(409);
    const a = await credential('pA:worker'); const b = await credential('pB:worker');
    for (const token of [a,b,await credential('machine-local','host',['host']),await credential('job-local','job',['bus:send'])]) {
      expect((await req('/api/agents','POST',{tag:'worker',project:'pB'},token)).status).toBe(403);
      expect((await req('/api/agents/pB:worker','DELETE',undefined,token)).status).toBe(403);
      expect((await req('/api/agents','GET',undefined,token)).status).toBe(403);
    }
    expect((await req('/api/agents/worker','DELETE',undefined,cookie)).status).toBe(409);
    expect((await req('/api/agents/pA%3Aworker','DELETE',undefined,cookie)).status).toBe(200);
    expect(await rows('SELECT project FROM agents WHERE tag = ?', 'worker')).toEqual([{project:'pB'}]);
    expect((await req('/api/agents/worker','DELETE',undefined,cookie)).status).toBe(200);
    expect((await register(null,'legacy',cookie)).status).toBe(201);
    expect((await req('/api/agents/legacy','DELETE',undefined,cookie)).status).toBe(200);
    for (const actor of ['pA%ZZworker','pA%3Aworker%3Aextra','pA%3A','a'.repeat(129),'pA%2Fworker'])
      expect((await req('/api/agents/'+actor,'DELETE',undefined,cookie)).status).toBe(400);
    expect((await req('/api/agents/unknown:worker','DELETE',undefined,cookie)).status).toBe(404);
    expect((await register('role','invalid',cookie)).status).toBe(400);
    expect((await register('pA','rootbad',cookie,{role:'root'})).status).toBe(400);
    expect((await register('pA','bad-machine',cookie,{machine_id:'unknown'})).status).toBe(400);
    expect((await register('pA','x'.repeat(65),cookie)).status).toBe(400);
    const listing = await req('/api/agents','GET',undefined,cookie);
    expect(listing.headers.get('Cache-Control')).toBe('private, no-store');
    expect(JSON.stringify(await listing.json())).not.toContain('token_hash');
  });

  it('pairs qualified agents through code/approve/token and keeps refresh bound', async () => {
    const cookie = await admin();
    for (const project of ['deviceA','deviceB']) {
      expect((await register(project,'paired',cookie)).status).toBe(201);
      const code = await req('/api/device/code','POST',{client_kind:'agent',name:'local-paired',actor:project+':paired',requested_scopes:['bus:send','bus:read']});
      expect(code.status).toBe(200); const payload = await code.json() as any;
      expect((await req('/api/device/approve/'+payload.user_code,'POST',{},cookie)).status).toBe(200);
      const issue = await req('/api/device/token','POST',{grant_type:'urn:ietf:params:oauth:grant-type:device_code',device_code:payload.device_code});
      expect(issue.status).toBe(200); const tokens = await issue.json() as any;
      const id = await conversation(tokens.access_token);
      expect((await req('/api/conversations/'+id,'GET',undefined,tokens.access_token)).status).toBe(200);
      const refresh = await req('/api/token/refresh','POST',{grant_type:'refresh_token',refresh_token:tokens.refresh_token});
      expect(refresh.status).toBe(200); const refreshed = await refresh.json() as any;
      expect((await req('/api/conversations/'+id,'GET',undefined,refreshed.access_token)).status).toBe(200);
      expect((await rows('SELECT registry_actor FROM credentials WHERE token_hash = ?',await sha256(refreshed.access_token)))[0].registry_actor).toBe(project+':paired');
    }
    const ambiguous = await req('/api/device/code','POST',{client_kind:'agent',name:'paired',actor:'paired',requested_scopes:['bus:read']});
    const payload = await ambiguous.json() as any;
    expect((await req('/api/device/approve/'+payload.user_code,'POST',{},cookie)).status).toBe(409);
    expect((await rows('SELECT status FROM device_pairings WHERE user_code = ?',payload.user_code))[0].status).toBe('pending');
  });

  it('isolates same-tag conversations, private membership, scopes and native bindings', async () => {
    const cookie = await admin();
    for (const project of ['chatA','chatB']) expect((await register(project,'shared',cookie)).status).toBe(201);
    const a = await credential('chatA:shared'), b = await credential('chatB:shared');
    const ca = await conversation(a), cb = await conversation(b);
    for (const [own,other,token] of [[ca,cb,a],[cb,ca,b]]) {
      expect((await req('/api/conversations/'+other,'GET',undefined,token)).status).toBe(403);
      expect((await req('/api/conversations/'+other+'/messages','POST',{body:'local-private',idempotency_key:'x'},token)).status).toBe(403);
      const send = await req('/api/conversations/'+own+'/messages','POST',{body:'local-private',idempotency_key:'x'},token);
      expect(send.status).toBe(201);
      expect((await send.json() as any).message.sender_actor).toBe(token===a?'chatA:shared':'chatB:shared');
    }
    expect((await req('/api/conversations/'+ca,'GET',undefined,cookie)).status).toBe(403);
    const ambiguous = await credential('shared');
    expect((await req('/api/conversations','GET',undefined,ambiguous)).status).toBe(403);
    const unknown = await credential('unknown:shared');
    expect((await req('/api/conversations','GET',undefined,unknown)).status).toBe(403);
    const readOnly = await credential('chatA:shared','agent',['bus:read']);
    expect((await req('/api/conversations/'+ca,'GET',undefined,readOnly)).status).toBe(200);
    expect((await req('/api/conversations/'+ca+'/messages','POST',{body:'no',idempotency_key:'read-only'},readOnly)).status).toBe(403);
    for (const token of [await credential('chatA:shared','host',['host']), await credential('chatA:shared','job',['bus:send']), await credential('chatA:shared','agent',['routing:submit']), await credential('chatA:shared','agent',['bus:read'],{revoked:'2026-01-01'}),await credential('chatA:shared','agent',['bus:read'],{expires:'2026-01-01'})])
      expect((await req('/api/conversations/'+ca,'GET',undefined,token)).status).not.toBe(200);
    const member = (await rows('SELECT actor FROM conversation_members WHERE conversation_id = ?',ca))[0];
    expect(member.actor).toBe('chatA:shared');
    // Registration metadata is local fixture data, never evidence of physical enrollment.
    const detail = await (await req('/api/conversations/'+ca,'GET',undefined,a)).json() as any;
    expect(JSON.stringify(detail)).not.toContain('chatB:shared');
    expect(JSON.stringify(detail)).not.toContain('token_hash');
  });

  it('pins bare credential identity across metadata moves and freezes historical bus endpoints', async () => {
    const cookie = await admin();
    expect((await register('oldProject','movable',cookie)).status).toBe(201);
    const token = await credential('movable');
    expect((await rows('SELECT registry_actor FROM credentials WHERE id = ?',token))[0].registry_actor).toBe('oldProject:movable');
    const ca = await conversation(token);
    await rows('UPDATE agents SET project = ? WHERE project = ? AND tag = ?','newProject','oldProject','movable');
    const newToken = await credential('newProject:movable'); const cb = await conversation(newToken);
    expect((await req('/api/conversations/'+cb,'GET',undefined,token)).status).toBe(403);
    expect((await req('/api/conversations/'+ca,'GET',undefined,newToken)).status).toBe(403);
    await expect(rows('UPDATE credentials SET registry_actor = ? WHERE id = ?','newProject:movable',token)).rejects.toThrow('immutable_agent_binding');
    expect((await register(null,'projectless',cookie)).status).toBe(201);
    const bare = await credential('projectless');
    await rows('UPDATE agents SET project = ? WHERE tag = ?','newProject','projectless');
    expect((await req('/api/conversations','GET',undefined,bare)).status).not.toBe(200);
    // Send before a collision, then add the second project through the real registry.
    expect((await register('busA','bus-worker',cookie)).status).toBe(201);
    const ba = await credential('busA:bus-worker');
    const send = await req('/api/bus/send','POST',{from_actor:'admin',to_actor:'bus-worker',kind:'note',body:'local bus private',idempotency_key:'before-collision'},cookie);
    expect(send.status).toBe(200); const mid = (await send.json() as any).id;
    expect((await rows('SELECT to_actor FROM messages WHERE id = ?',mid))[0].to_actor).toBe('busA:bus-worker');
    expect((await register('busB','bus-worker',cookie)).status).toBe(201);
    const bb = await credential('busB:bus-worker');
    expect((await req('/api/bus/inbox?actor=bus-worker','GET',undefined,ba)).status).toBe(403);
    expect(JSON.stringify(await (await req('/api/bus/messages','GET',undefined,bb)).json())).not.toContain(mid);
    expect(JSON.stringify(await (await req('/api/bus/messages','GET',undefined,ba)).json())).toContain(mid);
    expect((await req('/api/agents/busA:bus-worker','DELETE',undefined,cookie)).status).toBe(200);
    expect(JSON.stringify(await (await req('/api/bus/messages','GET',undefined,bb)).json())).not.toContain(mid);
    expect((await req('/api/bus/messages/'+mid,'GET',undefined,bb)).status).not.toBe(200);
  });

  it('does not transfer projectless bus history to a later qualified registration', async () => {
    const cookie = await admin();
    expect((await register(null,'bare-history',cookie)).status).toBe(201);
    const bare = await credential('bare-history');
    const sent = await req('/api/bus/send','POST',{from_actor:'admin',to_actor:'bare-history',kind:'note',body:'local prior owner',idempotency_key:'bare-history'},cookie);
    expect(sent.status).toBe(200); const id = (await sent.json() as any).id;
    expect((await req('/api/bus/messages/'+id,'GET',undefined,bare)).status).toBe(200);
    expect((await req('/api/agents/bare-history','DELETE',undefined,cookie)).status).toBe(200);
    expect((await register('laterProject','bare-history',cookie)).status).toBe(201);
    const later = await credential('laterProject:bare-history');
    expect((await req('/api/bus/messages/'+id,'GET',undefined,later)).status).toBe(403);
    expect((await req('/api/bus/messages/'+id+'/ack','POST',{},later)).status).toBe(403);
    expect(JSON.stringify(await (await req('/api/bus/messages','GET',undefined,later)).json())).not.toContain(id);
  });

  it('keeps claims/hosts fenced by exact project and ambiguous health unknown', async () => {
    const cookie = await admin();
    for (const machine of ['hostA','hostB']) await rows('INSERT INTO machines(id,name,hostname,registered_at) VALUES (?,?,?,?)',machine,machine,'local',new Date().toISOString());
    expect((await register('claimsA','claimant',cookie,{machine_id:'hostA',role:'implementer'})).status).toBe(201);
    expect((await register('claimsB','claimant',cookie,{machine_id:'hostB',role:'implementer'})).status).toBe(201);
    const a = await credential('claimsA:claimant','agent',['bus:send','bus:read'],{machine:'hostA'}), b = await credential('claimsB:claimant','agent',['bus:send','bus:read'],{machine:'hostB'});
    const claim = {project:'claimsA',scope:'src/local',holder:'claimsA:claimant',ttl_ms:60000};
    expect((await req('/api/bus/claims','POST',claim,b)).status).toBe(403);
    expect((await req('/api/bus/claims','POST',{...claim,holder:'claimant'},a)).status).toBe(403);
    const owned = await req('/api/bus/claims','POST',claim,a); expect(owned.status).toBe(200);
    const id = (await owned.json() as any).id;
    expect((await req('/api/bus/claims/'+id+'/release','POST',{},b)).status).toBe(403);
    const host = await credential('local-host','host',['host','bus:read'],{machine:'hostA'});
    expect((await req('/api/bus/inbox?actor=claimsA:claimant','GET',undefined,host)).status).toBe(200);
    expect((await req('/api/bus/inbox?actor=claimsB:claimant','GET',undefined,host)).status).toBe(403);
    expect((await req('/api/bus/inbox?actor=claimant','GET',undefined,host)).status).toBe(403);
    const actors = await (await req('/api/bus/actors','GET',undefined,host)).json() as string[];
    expect(actors).toContain('claimsA:claimant'); expect(actors).not.toContain('claimsB:claimant'); expect(actors).not.toContain('claimant');
    await rows("UPDATE agents SET state='healthy' WHERE project='claimsA' AND tag='claimant'");
    await rows("UPDATE agents SET state='retired' WHERE project='claimsB' AND tag='claimant'");
    await runInDurableObject(stub() as any, (hub: Hub) => {
      expect(hub.getEffectiveHealth('claimant')).toEqual({state:'unknown'});
      expect(hub.getEffectiveHealth('claimsA:claimant')).toEqual({state:'healthy'});
      expect(hub.getEffectiveHealth('claimsB:claimant')).toEqual({state:'retired'});
    });
    expect((await req('/api/bus/claims/'+id+'/release','POST',{},a)).status).toBe(200);
  });

  it('preserves inherited bound RPC/cron/alarm and late-slot refusal deterministically', async () => {
    await runInDurableObject(stub() as any, (hub: Hub) => { hub.jobsService.setClock(()=>new Date('2026-10-17T12:01:00Z')); });
    const tick = await (stub() as any).scheduledTick(); expect(tick.status).toBe('ok');
    expect(Array.isArray(await (stub() as any).runChecks())).toBe(true);
    for (const cron of ['*/5 * * * *','*/30 * * * *']) {
      const ctx = createExecutionContext(); await worker.scheduled(createScheduledController({cron}) as unknown as ScheduledEvent,bindings,ctx); await waitOnExecutionContext(ctx);
    }
    await runInDurableObject(stub() as any, async (hub: Hub,state) => {
      const base = new BaseHub(state,bindings); base.jobsService.setClock(()=>new Date('2026-10-17T13:26:00Z'));
      hub.jobsService.setClock(()=>new Date('2026-10-17T13:26:00Z'));
      expect((await hub.scheduledTick()).status).toBe((await base.scheduledTick()).status);
      expect((await hub.scheduledTick()).status).toBe('refused');
      await hub.alarm(); hub.jobsService.setClock(()=>new Date());
    });
    expect((await req('/api/conversations-other')).status).toBe(404);
  });
});

describe('F7 complete old-database startup through SELF', () => {
  async function installOldFixture(collision = false) {
    const token = 'hubt_local_old_database'; const hash = await sha256(token);
    let before: any;
    await runInDurableObject(stub() as any, (hub: Hub) => {
      hub.sql.exec('DROP TRIGGER messages_bind_agents');
      hub.sql.exec('DROP TABLE messages');
      hub.sql.exec(SCHEMA_SQL.slice(SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS messages ('), SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS claims (')));
      hub.sql.exec('DROP TABLE agents');
      hub.sql.exec(SCHEMA_SQL.slice(SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS agents ('),SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS messages')).replace('tag TEXT NOT NULL','tag TEXT PRIMARY KEY'));
      hub.sql.exec('DROP TABLE credentials');
      hub.sql.exec(SCHEMA_SQL.slice(SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS credentials ('),SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS device_pairings')));
      hub.sql.exec("INSERT INTO agents(tag,project,role,machine_id,registered_at,state,health_state,health_at,last_seen_at,updated_at) VALUES ('old-worker','oldA','implementer','local-old-machine','2020-01-01','unknown','unknown','2020-01-02','2020-01-03','2020-01-04')");
      hub.sql.exec("INSERT OR REPLACE INTO machines(id,name,hostname,registered_at,credential_id) VALUES ('local-old-machine','local','local','2020-01-01','local-old-id')");
      hub.sql.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,machine_id,scopes,created_by_sub,created_at,expires_at) VALUES ('local-old-id','agent',?,'local-family','old-worker','old-worker','local-old-machine','["bus:send","bus:read"]','local-owner','2020-01-01',?)`,hash,new Date(Date.now()+3600000).toISOString());
      hub.sql.exec("INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at) VALUES ('old-message','old-worker','old-worker','note','old-thread','local-private','old-key',?,?)",Date.now(),Date.now()+3600000);
      if(collision) hub.sql.exec("INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at) VALUES ('collision-message','oldA:old-worker','old-worker','note','old-thread','local-private','old-key',?,?)",Date.now(),Date.now()+3600000);
      before = {
        agent: hub.sql.exec('SELECT * FROM agents').toArray()[0],
        credential: hub.sql.exec('SELECT * FROM credentials').toArray()[0],
        message: hub.sql.exec('SELECT * FROM messages WHERE id = ?', 'old-message').toArray()[0],
      };
    });
    return {token,before};
  }
  it('preserves every old credential field/reference and serves two projects after startup', async () => {
    const {token,before} = await installOldFixture();
    await runInDurableObject(stub() as any, (hub: Hub,state) => { new Hub(state,bindings); });
    expect((await rows('SELECT * FROM agents'))[0]).toEqual(before.agent);
    const migrated = (await rows('SELECT * FROM credentials'))[0];
    expect(migrated.registry_actor).toBe('oldA:old-worker'); delete migrated.registry_actor;
    expect(migrated).toEqual(before.credential);
    expect((await rows('SELECT credential_id FROM machines WHERE id = ?', 'local-old-machine'))[0].credential_id).toBe('local-old-id');
    const message = (await rows('SELECT * FROM messages WHERE id = ?', 'old-message'))[0];
    // Later migrations add nullable delivery columns; every legacy field must survive.
    expect(message).toMatchObject({...before.message,registry_unbound_from:1,registry_unbound_to:1});
    const cookie = await admin();
    expect((await register('oldB','old-worker',cookie)).status).toBe(201);
    expect((await register('oldB','old-worker',cookie)).status).toBe(409);
    expect(JSON.stringify(await (await req('/api/bus/messages','GET',undefined,token)).json())).not.toContain('old-message');
    const other = await credential('oldB:old-worker');
    expect(JSON.stringify(await (await req('/api/bus/messages','GET',undefined,other)).json())).not.toContain('old-message');
    const id = await conversation(token);
    expect((await req('/api/conversations/'+id,'GET',undefined,other)).status).toBe(403);
    await runInDurableObject(stub() as any, (hub: Hub,state) => { new Hub(state,bindings); });
    expect((await req('/api/conversations/'+id,'GET',undefined,token)).status).toBe(200);
  });
  it('rolls the entire startup back on injected credential migration failure, leaving legacy keys and data usable', async () => {
    const {before} = await installOldFixture(true);
    await rows("CREATE TRIGGER fail_registry_binding BEFORE UPDATE ON credentials BEGIN SELECT RAISE(ABORT, 'injected_binding_failure'); END");
    await runInDurableObject(stub() as any, (hub: Hub,state) => {
      expect(()=>new Hub(state,bindings)).toThrow();
      expect(hub.sql.exec('PRAGMA table_info(agents)').toArray().find((r:any)=>r.name==='tag')?.pk).toBe(1);
      expect(hub.sql.exec('PRAGMA table_info(credentials)').toArray().some((r:any)=>r.name==='registry_actor')).toBe(false);
      expect(hub.sql.exec('SELECT * FROM agents').toArray()[0]).toEqual(before.agent);
      expect(hub.sql.exec('SELECT * FROM credentials').toArray()[0]).toEqual(before.credential);
      expect(hub.sql.exec('SELECT * FROM messages WHERE id = ?', 'old-message').toArray()[0]).toEqual(before.message);
      expect(hub.sql.exec('SELECT COUNT(*) as n FROM messages').toArray()[0]).toEqual({n:2});
      hub.sql.exec("INSERT INTO agents(tag,project) VALUES ('still-usable','oldA')");
      hub.sql.exec('DROP TRIGGER fail_registry_binding');
      new Hub(state,bindings);
      expect(hub.sql.exec('SELECT COUNT(*) as n FROM messages').toArray()[0]).toEqual({n:2});
    });
  });
});

describe('F7 migration on actual Durable Object SQLite', () => {
  it('preserves both same-tag project rows through full Hub startup without replacement inserts', async () => {
    const isolated = bindings.HUB.get(bindings.HUB.idFromName('m3-f7-'+crypto.randomUUID()));
    await runInDurableObject(isolated as any, (hub: Hub,state) => {
      hub.sql.exec('DROP TABLE agents');
      for (const statement of BUS_SCHEMA_SQL.split(';')) if(statement.trim())hub.sql.exec(statement);
      hub.sql.exec("INSERT INTO agents(tag,project,role,machine_id,registered_at) VALUES ('same-m3','m3A','implementer','local-A','2020-01-01')");
      hub.sql.exec("INSERT INTO agents(tag,project,role,machine_id,registered_at) VALUES ('same-m3','m3B','reviewer','local-B','2020-01-02')");
      const before = hub.sql.exec('SELECT tag,project,role,machine_id,registered_at FROM agents ORDER BY project').toArray();
      new Hub(state,bindings);
      expect(hub.sql.exec('SELECT tag,project,role,machine_id,registered_at FROM agents ORDER BY project').toArray()).toEqual(before);
      expect(()=>hub.sql.exec("INSERT INTO agents(tag,project) VALUES ('same-m3','m3B')")).toThrow();
      expect(hub.busService.validateRecipient('same-m3')).toBe(false);
      expect(hub.busService.validateRecipient('m3A:same-m3')).toBe(true);
    });
  });
});
