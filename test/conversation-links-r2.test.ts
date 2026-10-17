import { RETENTION_CLEANUP_CURSOR_SQL } from '../src/bus';
import { ConversationsService, authenticateConversationRequest } from '../src/conversations';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { Hub, BaseHub } from '../src/conversation-hub';
import type { Env } from '../src/env';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const testEnv = env as unknown as Env;

function notePeak(_label: string): void {
  // Scratch/cgroup peaks are measured by the serial test runner around this file.
}

async function cookieFor(
  sub: string,
  email: string,
  csrf: string,
): Promise<string> {
  const header = await createSessionCookie(testEnv, {
    sub,
    email,
    isAdmin: true,
    csrfToken: csrf,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return header.split(';')[0];
}

async function withHub(
  fn: (hub: Hub, state: DurableObjectState) => Promise<void>,
): Promise<void> {
  const stub = testEnv.HUB.get(
    testEnv.HUB.idFromName('links-' + crypto.randomUUID()),
  );
  await runInDurableObject(
    stub,
    async (_base: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);
      const nowIso = new Date().toISOString();
      hub.sql.exec(
        "INSERT OR IGNORE INTO machines (id, name, hostname, registered_at) VALUES ('machine-A', 'A', 'host-a', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT OR IGNORE INTO machines (id, name, hostname, registered_at) VALUES ('machine-B', 'B', 'host-b', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('worker-1', 'projA', 'implementer', 'machine-A', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('worker-2', 'projB', 'reviewer', 'machine-B', ?)",
        nowIso,
      );
      await fn(hub, state);
    },
  );
}

function jsonReq(
  path: string,
  opts: {
    method?: string;
    cookie?: string;
    csrf?: string;
    token?: string;
    body?: unknown;
  } = {},
): Request {
  const headers: Record<string, string> = {};
  if (opts.cookie) headers.Cookie = opts.cookie;
  if (opts.csrf) headers['X-CSRF-Token'] = opts.csrf;
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  return new Request('https://hub.test' + path, {
    method: opts.method ?? (opts.body !== undefined ? 'POST' : 'GET'),
    headers,
    body,
  });
}

async function seedAgentToken(
  sql: any,
  opts: {
    id: string;
    token: string;
    actor: string;
    machineId: string;
    scopes: string[];
    expiresAt?: string;
    revokedAt?: string | null;
  },
): Promise<string> {
  const nowIso = new Date().toISOString();
  sql.exec(
    `INSERT INTO credentials
     (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at, revoked_at)
     VALUES (?, 'agent', ?, ?, ?, ?, ?, ?, 'alice', ?, ?, ?)`,
    opts.id,
    await sha256(opts.token),
    'fam-' + opts.id,
    opts.actor,
    opts.actor,
    opts.machineId,
    JSON.stringify(opts.scopes),
    nowIso,
    opts.expiresAt ?? new Date(Date.now() + 86400000).toISOString(),
    opts.revokedAt ?? null,
  );
  return opts.token;
}

function insertJob(
  sql: any,
  opts: { id: string; actor: string; state?: string; launchId?: string | null },
): void {
  const at = new Date().toISOString();
  sql.exec(
    `INSERT INTO routing_jobs
     (id, actor, request_key, request_json, launch_id, state, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    opts.id,
    opts.actor,
    'key-' + opts.id,
    JSON.stringify({
      workspace: 'tasks',
      tier: 'daily',
      model: null,
      needs: [],
      tasks: [{ prompt: 'Read only', approved_action: 'Read only' }],
      preferred_runner: null,
      preferred_machine: null,
    }),
    opts.launchId ?? null,
    opts.state ?? 'queued',
    at,
    at,
  );
}

function insertLaunch(
  sql: any,
  opts: { id: string; actor: string; state?: string },
): void {
  const at = new Date().toISOString();
  sql.exec(
    `INSERT INTO fleet_launches
     (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at)
     VALUES (?, ?, 'agent', NULL, 'summary', ?, '{}', 'machine-A', 'zcodex', 'acct', NULL, 'headless', 'tasks', 'prompt', ?, ?, ?)`,
    opts.id,
    opts.actor,
    'launch-' + opts.id,
    opts.state ?? 'offered',
    at,
    at,
  );
}

async function createGroup(
  hub: Hub,
  cookie: string,
  csrf: string,
  title: string,
  members: string[],
): Promise<any> {
  const res = await hub.fetch(
    jsonReq('/api/conversations', {
      method: 'POST',
      cookie,
      csrf,
      body: { kind: 'group', title, members },
    }),
  );
  expect(res.status, await res.clone().text()).toBe(201);
  return res.json();
}

// Exact repair regressions for the independently retained M4 F1–F4 findings.
async function attach(hub: Hub, cid: string, auth: {cookie?: string; csrf?: string; token?: string}, ref: string, kind = 'routing_job', key?: string): Promise<Response> {
  return hub.fetch(jsonReq(`/api/conversations/${cid}/links`, { ...auth,
    body: {kind, ref, ...(key ? {idempotency_key: key} : {})} }));
}
async function opaque(res: Response): Promise<void> {
  expect(res.status).toBe(404);
  expect(await res.json()).toEqual({error: 'not_found'});
}
const unavailable = {available: false, canonical_status: null, terminal: false, complete: false};

describe('M4 r2 retained adversarial HTTP regressions', () => {
  it('F1 checks foreign job and admin launch authority before duplicate refs and conflicts', async () => {
    await withHub(async hub => {
      const csrf = 'r2-acl'; const cookie = await cookieFor('alice', 'admin@fake.local', csrf);
      const admin = {cookie, csrf};
      const conv = await createGroup(hub, cookie, csrf, 'shared', ['projA:worker-1', 'projB:worker-2']);
      const worker = {token: await seedAgentToken(hub.sql, {id:'r2-w1', token:'r2-w1-token', actor:'projA:worker-1', machineId:'machine-A', scopes:['bus:send','bus:read','routing:submit']})};
      const ordinary = {token: await seedAgentToken(hub.sql, {id:'r2-w2', token:'r2-w2-token', actor:'projB:worker-2', machineId:'machine-B', scopes:['bus:send','bus:read']})};
      const foreign = crypto.randomUUID(), launch = crypto.randomUUID(), own = crypto.randomUUID();
      insertJob(hub.sql, {id:foreign, actor:'routing-client:private'});
      insertJob(hub.sql, {id:own, actor:'projA:worker-1'});
      insertLaunch(hub.sql, {id:launch, actor:'routing-client:private'});
      expect((await attach(hub, conv.id, worker, own, 'routing_job', 'existing-key')).status).toBe(201);
      for (const [ref,kind] of [[foreign,'routing_job'],[launch,'fleet_launch']]) {
        await opaque(await attach(hub, conv.id, worker, ref, kind));
        expect((await attach(hub, conv.id, admin, ref, kind)).status).toBe(201);
        for (const auth of [worker,ordinary]) {
          await opaque(await attach(hub, conv.id, auth, ref, kind));
          await opaque(await attach(hub, conv.id, auth, ref, kind, 'fresh'));
          await opaque(await attach(hub, conv.id, auth, ref, kind, 'existing-key'));
          await opaque(await hub.fetch(jsonReq(`/api/conversations/links?kind=${kind}&ref=${ref}&limit=1`, auth)));
        }
      }
      const listed = await hub.fetch(jsonReq(`/api/conversations/${conv.id}/links`, ordinary));
      expect(listed.status).toBe(200);
      const body = await listed.json() as any;
      for (const row of body.links) expect(row.target).toEqual(unavailable);
      expect(JSON.stringify(body)).not.toMatch(/routing-client:private|prompt|acct|host-a/);
      const counts = ['routing_jobs','fleet_launches','conversations','conversation_members'].map(t => Number(hub.sql.exec(`SELECT COUNT(*) n FROM ${t}`).toArray()[0].n));
      await opaque(await attach(hub, conv.id, worker, crypto.randomUUID(), 'routing_job', 'existing-key'));
      expect(['routing_jobs','fleet_launches','conversations','conversation_members'].map(t => Number(hub.sql.exec(`SELECT COUNT(*) n FROM ${t}`).toArray()[0].n))).toEqual(counts);
    });
  });

  it('F1 rechecks deleted/recreated targets, reduced scopes and live credentials on every replay', async () => {
    await withHub(async hub => {
      const csrf='r2-replay', cookie=await cookieFor('alice','admin@fake.local',csrf);
      const conv=await createGroup(hub,cookie,csrf,'replay',['projA:worker-1']);
      const auth={token:await seedAgentToken(hub.sql,{id:'r2-live',token:'r2-live-token',actor:'projA:worker-1',machineId:'machine-A',scopes:['bus:send','bus:read','routing:submit']})};
      const ref=crypto.randomUUID();insertJob(hub.sql,{id:ref,actor:'projA:worker-1'});
      const first=await attach(hub,conv.id,auth,ref,'routing_job','retry');
      const row=await first.json() as any;expect(first.status).toBe(201);
      const replay=async()=>{for(const key of ['retry',undefined]) await opaque(await attach(hub,conv.id,auth,ref,'routing_job',key));};
      hub.sql.exec('DELETE FROM routing_jobs WHERE id = ?',ref);await replay();
      insertJob(hub.sql,{id:ref,actor:'foreign:private',state:'accepted'});await replay();
      const get=await hub.fetch(jsonReq(`/api/conversations/${conv.id}/links/${row.id}`,auth));
      expect((await get.json() as any).target).toEqual(unavailable);
      hub.sql.exec("UPDATE routing_jobs SET actor = 'projA:worker-1' WHERE id = ?",ref);
      hub.sql.exec("UPDATE credentials SET scopes = ? WHERE id = 'r2-live'",JSON.stringify(['bus:send','bus:read']));await replay();
      hub.sql.exec("UPDATE credentials SET scopes = ? WHERE id = 'r2-live'",JSON.stringify(['bus:send','bus:read','routing:submit']));
      expect((await attach(hub,conv.id,auth,ref,'routing_job','retry')).status).toBe(200);
      for(const update of ["revoked_at = '2020-01-01T00:00:00Z'", "revoked_at = NULL, expires_at = '2020-01-01T00:00:00Z'"]) {
        hub.sql.exec(`UPDATE credentials SET ${update} WHERE id = 'r2-live'`);
        expect((await attach(hub,conv.id,auth,ref,'routing_job','retry')).status).toBe(401);
      }
      // Historical unavailable associations remain removable by active members;
      // detach never grants target access or mutates its canonical record.
      const removed=await hub.fetch(jsonReq(`/api/conversations/${conv.id}/links/${row.id}`,{method:'DELETE',cookie,csrf}));
      expect(removed.status).toBe(200);
      expect(hub.sql.exec('SELECT state FROM routing_jobs WHERE id = ?',ref).toArray()[0].state).toBe('accepted');
    });
  });

  it('F3 preserves canonical dispositions with exited/failed launches and reports live review evidence', async () => {
    await withHub(async hub => {
      const csrf='r2-status',cookie=await cookieFor('alice','admin@fake.local',csrf),auth={cookie,csrf};
      const conv=await createGroup(hub,cookie,csrf,'status',[]);
      const ref=crypto.randomUUID(),launch=crypto.randomUUID();
      insertLaunch(hub.sql,{id:launch,actor:'x',state:'exited'});
      insertJob(hub.sql,{id:ref,actor:'x',state:'queued',launchId:launch});
      const ids: Record<string,string> = {};
      for (const kind of ['routing_job','review','outcome']) {
        const res=await attach(hub,conv.id,auth,ref,kind);expect(res.status).toBe(201);
        ids[kind]=(await res.json() as any).id;
      }
      for (const launchState of ['exited','failed']) for (const state of ['completed-awaiting-review','cancelled','accepted','done','failed','waiting-on-human','stuck','unrecognized-private-value']) {
        hub.sql.exec('UPDATE routing_jobs SET state = ? WHERE id = ?',state,ref);
        hub.sql.exec('UPDATE fleet_launches SET state = ? WHERE id = ?',launchState,launch);
        for (const kind of Object.keys(ids)) {
          const target={available:true,canonical_status:state==='unrecognized-private-value'?null:state,
            terminal: ['accepted','failed','cancelled'].includes(state)||(state==='done'&&kind!=='review'),
            complete:state==='accepted'||(state==='done'&&kind!=='review')};
          const got=await hub.fetch(jsonReq(`/api/conversations/${conv.id}/links/${ids[kind]}`,{cookie}));
          expect((await got.json() as any).target).toEqual(target);
          const retry=await attach(hub,conv.id,auth,ref,kind);
          expect(retry.status).toBe(200);expect((await retry.json() as any).target).toEqual(target);
          const rev=await hub.fetch(jsonReq(`/api/conversations/links?kind=${kind}&ref=${ref}`,{cookie}));
          expect((await rev.json() as any).target).toEqual(target);
        }
      }
      hub.sql.exec('UPDATE routing_jobs SET launch_id = NULL WHERE id = ?',ref);
      for(const state of ['cancelled','accepted','done']) {
        hub.sql.exec('UPDATE routing_jobs SET state = ? WHERE id = ?',state,ref);
        const got=await hub.fetch(jsonReq(`/api/conversations/${conv.id}/links/${ids.review}`,{cookie}));
        expect((await got.json() as any).target).toEqual({available:true,canonical_status:state,terminal:state!=='done',complete:state==='accepted'});
      }
      // Only active dispatch states may use the existing fleet fallback.
      hub.sql.exec('UPDATE routing_jobs SET launch_id = ?, state = ? WHERE id = ?',launch,'running',ref);
      for(const [state,status] of [['running','running'],['uncertain','stuck'],['exited','done'],['failed','failed']]) {
        hub.sql.exec('UPDATE fleet_launches SET state = ? WHERE id = ?',state,launch);
        const got=await hub.fetch(jsonReq(`/api/conversations/${conv.id}/links/${ids.outcome}`,{cookie}));
        expect((await got.json() as any).target.canonical_status).toBe(status);
      }
    });
  });

  it('F4 applies current membership and expiry before LIMIT, with complete keyset pages and no foreign cursor', async () => {
    await withHub(async hub => {
      const csrf='r2-pages',cookie=await cookieFor('alice','admin@fake.local',csrf),auth={cookie,csrf};
      const ref=crypto.randomUUID();insertJob(hub.sql,{id:ref,actor:'x'});
      const active: string[]=[];const hidden: string[]=[];
      for(let i=0;i<7;i++) {
        const conv=await createGroup(hub,cookie,csrf,'page-'+i,[]);
        expect((await attach(hub,conv.id,auth,ref)).status).toBe(201);
        hub.sql.exec('UPDATE conversation_links SET created_at = ? WHERE conversation_id = ?',`2026-10-01T00:00:0${i}.000Z`,conv.id);
        if(i<3)active.unshift(conv.id); else hidden.push(conv.id);
        if(i===3)hub.sql.exec("UPDATE conversation_members SET removed_at = '2020-01-01' WHERE conversation_id = ?",conv.id);
        if(i===4)hub.sql.exec("UPDATE conversation_members SET kind = 'agent' WHERE conversation_id = ?",conv.id);
        if(i>=5)hub.sql.exec("UPDATE conversations SET archived_at = '2000-01-01T00:00:00Z' WHERE id = ?",conv.id);
      }
      const observed: string[]=[];let cursor: string|null=null;
      for(let page=0;page<3;page++) {
        const res=await hub.fetch(jsonReq(`/api/conversations/links?kind=routing_job&ref=${ref}&limit=1${cursor?'&cursor='+encodeURIComponent(cursor):''}`,{cookie}));
        expect(res.status).toBe(200);const body=await res.json() as any;
        expect(body.links).toHaveLength(1);observed.push(body.links[0].conversation_id);
        for(const cid of hidden)expect(JSON.stringify(body)).not.toContain(cid);
        cursor=body.next_cursor;
        if(page<2)expect(cursor).toBeTruthy();else expect(cursor).toBeNull();
      }
      expect(observed).toEqual(active);
      hub.sql.exec("UPDATE conversation_members SET removed_at = '2020-01-01' WHERE conversation_id = ?",active[0]);
      const res=await hub.fetch(jsonReq(`/api/conversations/links?kind=routing_job&ref=${ref}&limit=1`,{cookie}));
      expect((await res.json() as any).links[0].conversation_id).toBe(active[1]);
    });
  });

  it('serializes concurrent idempotent attachments and counts no canonical creations', async () => {
    await withHub(async hub => {
      const csrf='r2-race',cookie=await cookieFor('alice','admin@fake.local',csrf),auth={cookie,csrf};
      const conv=await createGroup(hub,cookie,csrf,'race',[]),ref=crypto.randomUUID();
      insertJob(hub.sql,{id:ref,actor:'x'});
      const before=['routing_jobs','fleet_launches','conversations','conversation_members'].map(t=>hub.sql.exec(`SELECT COUNT(*) n FROM ${t}`).toArray()[0].n);
      const responses=await Promise.all(Array.from({length:6},()=>attach(hub,conv.id,auth,ref,'routing_job','race')));
      expect(responses.map(r=>r.status).sort()).toEqual([200,200,200,200,200,201]);
      const bodies=await Promise.all(responses.map(r=>r.json())) as any[];
      expect(new Set(bodies.map(r=>r.id)).size).toBe(1);
      expect(hub.sql.exec('SELECT COUNT(*) n FROM conversation_links').toArray()[0].n).toBe(1);
      expect(['routing_jobs','fleet_launches','conversations','conversation_members'].map(t=>hub.sql.exec(`SELECT COUNT(*) n FROM ${t}`).toArray()[0].n)).toEqual(before);
    });
  });
});

describe('M4 r2 standalone SQLite lifecycle', () => {
  it('F2 initializes the complete standalone HTTP API and purges every child within limit=1', async () => {
    await withHub(async (hub,state) => {
      // Remove only conversation tables from this test's isolated DO: prove fresh init.
      for(const row of hub.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'conversation%'").toArray()) {
        hub.sql.exec(`DROP TABLE ${row.name}`);
      }
      const svc=new ConversationsService(hub.sql,fn=>state.storage.transactionSync(fn));svc.init();
      expect(svc.linksHandler).toBeTruthy();
      expect(hub.sql.exec("SELECT name FROM sqlite_master WHERE name = 'conversation_links'").toArray()).toHaveLength(1);
      const csrf='r2-standalone',cookie=await cookieFor('alice','admin@fake.local',csrf);
      const http=async(req:Request)=>svc.handle(req,await authenticateConversationRequest(testEnv,hub.sql,req));
      const made=await http(jsonReq('/api/conversations',{cookie,csrf,body:{kind:'group',title:'standalone'}}));
      expect(made.status).toBe(201);const conv=await made.json() as any;
      const ref=crypto.randomUUID();insertJob(hub.sql,{id:ref,actor:'x'});
      const linked=await http(jsonReq(`/api/conversations/${conv.id}/links`,{cookie,csrf,body:{kind:'routing_job',ref}}));
      expect(linked.status).toBe(201);const link=await linked.json() as any;
      for(const path of [`/api/conversations/${conv.id}/links`,`/api/conversations/${conv.id}/links/${link.id}`,`/api/conversations/links?kind=routing_job&ref=${ref}`]) {
        const response=await http(jsonReq(path,{cookie}));expect(response.status).toBe(200);
      }
      expect((await http(jsonReq(`/api/conversations/${conv.id}/messages`,{cookie,csrf,body:{body:'retained child',idempotency_key:'child'}}))).status).toBe(201);
      hub.sql.exec("UPDATE conversations SET archived_at = '2000-01-01T00:00:00Z' WHERE id = ?",conv.id);
      let deleted=0,links=0;
      for(let i=0;i<20;i++) {
        const result=svc.retentionCleanup(Date.now(),1);
        expect(result.rowsDeleted).toBeLessThanOrEqual(1);
        expect(result.rowsDeleted).toBe(result.conversationsDeleted+result.messagesDeleted+result.membersDeleted+Number(result.linksDeleted));
        deleted+=result.rowsDeleted;links+=Number(result.linksDeleted);
        const orphan=hub.sql.exec('SELECT id FROM conversation_links WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_links.conversation_id)').toArray();
        expect(orphan).toHaveLength(0);
        if(!result.hasMore)break;
      }
      expect(deleted).toBe(4);expect(links).toBe(1);
      expect(hub.sql.exec('SELECT id FROM conversations WHERE id = ?',conv.id).toArray()).toHaveLength(0);
      expect(svc.getStorageUsage()).toEqual({conversation_count:0,message_count:0,message_bytes:0,removed_member_count:0});
    });
  });

  it('F2 startup reconciles orphan links with one bounded fair pass and drains the remainder', async () => {
    await withHub(async(hub,state)=>{
      // More than the startup budget: init must neither skip nor sweep unbounded rows.
      for(let i=0;i<105;i++) hub.sql.exec(`INSERT INTO conversation_links
        (id,conversation_id,kind,ref,created_by,created_at) VALUES (?, 'missing-parent', 'routing_job', ?, 'human:alice', '2000-01-01')`,
        `orphan-${String(i).padStart(3,'0')}`,crypto.randomUUID());
      const svc=new ConversationsService(hub.sql,fn=>state.storage.transactionSync(fn));
      svc.init();
      expect(hub.sql.exec('SELECT COUNT(*) n FROM conversation_links').toArray()[0].n).toBe(5);
      expect(svc.hasExpiredRows()).toBe(true);
      for(let i=0;i<5;i++) {
        const result=svc.retentionCleanup(Date.now(),1);
        expect(result.rowsDeleted).toBe(1);expect(result.linksDeleted).toBe(1);
        expect(result.hasMore).toBe(i<4);
      }
      svc.init();expect(hub.sql.exec('SELECT COUNT(*) n FROM conversation_links').toArray()[0].n).toBe(0);
    });
  });

  it('F2 rolls back standalone link deletion, counters and fairness cursor atomically', async () => {
    await withHub(async(hub,state)=>{
      const csrf='r2-rollback',cookie=await cookieFor('alice','admin@fake.local',csrf);
      const conv=await createGroup(hub,cookie,csrf,'rollback',[]),ref=crypto.randomUUID();insertJob(hub.sql,{id:ref,actor:'x'});
      expect((await attach(hub,conv.id,{cookie,csrf},ref)).status).toBe(201);
      hub.sql.exec("UPDATE conversations SET archived_at = '2000-01-01' WHERE id = ?",conv.id);
      hub.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
      const snapshot=()=>Object.fromEntries(['conversations','conversation_members','conversation_links','conversation_storage_usage','retention_cleanup_cursor'].map(t=>[t,hub.sql.exec(`SELECT * FROM ${t} ORDER BY rowid`).toArray()]));
      const before=snapshot();let sawDelete=false;
      const proxy={exec(q:string,...args:any[]){
        const result=hub.sql.exec(q,...args);
        if(q.includes('DELETE FROM conversation_links')) {sawDelete=true;throw new Error('injected after link deletion');}
        return result;
      }};
      const svc=new ConversationsService(proxy as any,fn=>state.storage.transactionSync(fn));
      expect(()=>svc.retentionCleanup(Date.now(),20)).toThrow('injected after link deletion');
      expect(sawDelete).toBe(true);expect(snapshot()).toEqual(before);
      const fresh=new ConversationsService(hub.sql,fn=>state.storage.transactionSync(fn));fresh.init();
      const result=fresh.retentionCleanup(Date.now(),20);expect(result.linksDeleted).toBe(1);expect(result.conversationsDeleted).toBe(1);
      expect(result.rowsDeleted).toBe(3);
    });
  });

  it('gives orphan links fair turns beside persistent expired messages at limit=1',async()=>{
    await withHub(async(hub,state)=>{
      const csrf='r2-fair',cookie=await cookieFor('alice','admin@fake.local',csrf);
      const conv=await createGroup(hub,cookie,csrf,'fair',[]);
      for(let i=0;i<6;i++) expect((await hub.fetch(jsonReq(`/api/conversations/${conv.id}/messages`,{cookie,csrf,body:{body:'old',idempotency_key:'old-'+i}}))).status).toBe(201);
      hub.sql.exec("UPDATE conversation_messages SET created_at = '2000-01-01'");
      hub.sql.exec("INSERT INTO conversation_links (id,conversation_id,kind,ref,created_by,created_at) VALUES ('orphan','missing','routing_job',?,'human:alice','2000-01-01')",crypto.randomUUID());
      const svc=new ConversationsService(hub.sql,fn=>state.storage.transactionSync(fn));
      let links=0,messages=0;
      for(let i=0;i<4;i++) {const r=svc.retentionCleanup(Date.now(),1);expect(r.rowsDeleted).toBe(1);links+=Number(r.linksDeleted);messages+=r.messagesDeleted;}
      expect(links).toBe(1);expect(messages).toBeGreaterThan(0);
    });
  });
});

describe('M4 r2 authority at the HTTP effect', () => {
  it('revalidates authority after body read on replay, using current binding/project/machine', async () => {
    await withHub(async hub => {
      const csrf='r2-effect',cookie=await cookieFor('alice','admin@fake.local',csrf);
      const conv=await createGroup(hub,cookie,csrf,'effect',['projA:worker-1']);
      const token=await seedAgentToken(hub.sql,{id:'r2-effect',token:'r2-effect-token',actor:'projA:worker-1',machineId:'machine-A',scopes:['bus:send','bus:read','routing:submit']});
      const ref=crypto.randomUUID();insertJob(hub.sql,{id:ref,actor:'projA:worker-1'});
      const first=await attach(hub,conv.id,{token},ref,'routing_job','effect');expect(first.status).toBe(201);
      const link=await first.json() as any;
      const attempts: Array<{change:()=>void; restore:()=>void; status:number}> = [
        {change:()=>hub.sql.exec("UPDATE routing_jobs SET actor = 'foreign:private' WHERE id = ?",ref),restore:()=>hub.sql.exec("UPDATE routing_jobs SET actor = 'projA:worker-1' WHERE id = ?",ref),status:404},
        {change:()=>hub.sql.exec("UPDATE credentials SET scopes = ? WHERE id = 'r2-effect'",JSON.stringify(['bus:send','bus:read'])),restore:()=>hub.sql.exec("UPDATE credentials SET scopes = ? WHERE id = 'r2-effect'",JSON.stringify(['bus:send','bus:read','routing:submit'])),status:404},
        {change:()=>hub.sql.exec("UPDATE credentials SET revoked_at = '2020-01-01' WHERE id = 'r2-effect'"),restore:()=>hub.sql.exec("UPDATE credentials SET revoked_at = NULL WHERE id = 'r2-effect'"),status:401},
        {change:()=>hub.sql.exec("UPDATE agents SET project = 'renamed' WHERE tag = 'worker-1'"),restore:()=>hub.sql.exec("UPDATE agents SET project = 'projA' WHERE tag = 'worker-1'"),status:403},
        {change:()=>hub.sql.exec("UPDATE agents SET machine_id = 'machine-B' WHERE tag = 'worker-1'"),restore:()=>{hub.sql.exec("UPDATE agents SET machine_id = 'machine-A' WHERE tag = 'worker-1'");hub.sql.exec("UPDATE credentials SET revoked_at = NULL WHERE id = 'r2-effect'");},status:401},
        {change:()=>hub.sql.exec("UPDATE credentials SET machine_id = 'machine-B' WHERE id = 'r2-effect'"),restore:()=>hub.sql.exec("UPDATE credentials SET machine_id = 'machine-A' WHERE id = 'r2-effect'"),status:403},
      ];
      for(const scenario of attempts) {
        let changed=false;
        const stream=new ReadableStream<Uint8Array>({pull(controller){
          scenario.change();changed=true;
          controller.enqueue(new TextEncoder().encode(JSON.stringify({kind:'routing_job',ref,idempotency_key:'effect'})));
          controller.close();
        }},{highWaterMark:0});
        const req=new Request(`https://hub.test/api/conversations/${conv.id}/links`,{method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},body:stream});
        const res=await hub.fetch(req);
        expect(changed, 'scenario '+attempts.indexOf(scenario)+' response '+await res.clone().text()).toBe(true);expect(res.status).toBe(scenario.status);
        if(scenario.status===404)expect(await res.json()).toEqual({error:'not_found'});
        expect(hub.sql.exec('SELECT COUNT(*) n FROM conversation_links').toArray()[0].n).toBe(1);
        scenario.restore();
      }
      // Mutable actor/subject labels cannot override the immutable canonical binding.
      hub.sql.exec("UPDATE credentials SET actor = 'projB:worker-2', subject = 'projB:worker-2' WHERE id = 'r2-effect'");
      expect((await attach(hub,conv.id,{token},ref,'routing_job','effect')).status).toBe(200);
      for(const extra of [{actor:'human:alice'},{from_actor:'human:alice'},{auth:{isAdmin:true}},{verdict:'accepted'},{status:'accepted'},{url:'https://private.invalid'}]) {
        const res=await hub.fetch(jsonReq(`/api/conversations/${conv.id}/links`,{token,body:{kind:'review',ref,...extra}}));
        expect(res.status).toBe(400);expect(await res.json()).toMatchObject({error:'from_actor' in extra?'sender_override_forbidden':'unknown_fields'});
      }
      expect(hub.sql.exec('SELECT state FROM routing_jobs WHERE id = ?',ref).toArray()[0].state).toBe('queued');
      hub.sql.exec("UPDATE conversation_members SET removed_at = '2020-01-01' WHERE conversation_id = ? AND actor = 'projA:worker-1'",conv.id);
      for(const [path,method] of [[`/api/conversations/${conv.id}/links`,'GET'],[`/api/conversations/${conv.id}/links/${link.id}`,'GET'],[`/api/conversations/${conv.id}/links/${link.id}`,'DELETE']]) {
        expect((await hub.fetch(jsonReq(path,{token,method}))).status).toBe(403);
      }
      expect((await attach(hub,conv.id,{token},ref,'routing_job','effect')).status).toBe(403);
      const reversed=await hub.fetch(jsonReq(`/api/conversations/links?kind=routing_job&ref=${ref}&limit=1`,{token}));
      expect(await reversed.json()).toMatchObject({links:[],next_cursor:null});
      hub.sql.exec("UPDATE users SET is_admin = 0 WHERE sub = 'alice'");
      expect((await attach(hub,conv.id,{cookie,csrf},ref,'routing_job','disabled')).status).toBe(403);
      for(const [path,method] of [[`/api/conversations/${conv.id}/links`,'GET'],[`/api/conversations/${conv.id}/links/${link.id}`,'GET'],[`/api/conversations/${conv.id}/links/${link.id}`,'DELETE'],[`/api/conversations/links?kind=routing_job&ref=${ref}`,'GET']]) {
        expect((await hub.fetch(jsonReq(path,{cookie,csrf,method}))).status).toBe(403);
      }
    });
  });

  it('keeps global link bounds across conversations and forward pages bounded', async()=>{
    await withHub(async hub=>{
      const csrf='r2-global',cookie=await cookieFor('alice','admin@fake.local',csrf),auth={cookie,csrf};
      const a=await createGroup(hub,cookie,csrf,'a',[]),b=await createGroup(hub,cookie,csrf,'b',[]);
      hub.linksService.limits.maxTotalLinks=2;
      const refs=Array.from({length:3},()=>crypto.randomUUID());for(const id of refs)insertJob(hub.sql,{id,actor:'x'});
      for(const ref of refs.slice(0,2))expect((await attach(hub,a.id,auth,ref)).status).toBe(201);
      const over=await attach(hub,b.id,auth,refs[2]);expect(over.status).toBe(409);
      expect(await over.json()).toMatchObject({error:'storage_limit_exceeded',reason:'total_link_limit_exceeded'});
      expect(hub.sql.exec('SELECT COUNT(*) n FROM conversation_links').toArray()[0].n).toBe(2);
      const first=await hub.fetch(jsonReq(`/api/conversations/${a.id}/links?limit=1`,{cookie}));const page=await first.json() as any;
      expect(page.links).toHaveLength(1);expect(page.next_cursor).toBeTruthy();
      const second=await hub.fetch(jsonReq(`/api/conversations/${a.id}/links?limit=1&cursor=${encodeURIComponent(page.next_cursor)}`,{cookie}));const last=await second.json() as any;
      expect(last.links).toHaveLength(1);expect(last.next_cursor).toBeNull();expect(last.links[0].id).not.toBe(page.links[0].id);
    });
  });
});
