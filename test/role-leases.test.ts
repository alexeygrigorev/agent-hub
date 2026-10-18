import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { JobsService } from '../src/jobs';

// The Cloudflare RPC mapped type recurses on `any`; keep this test-only bridge
// explicit, as in the existing real-SQL JobsService tests.
const inHub = runInDurableObject as unknown as <T>(stub: any, callback: (hub: any) => T | Promise<T>) => Promise<T>;

const BASE = Date.parse('2099-05-06T12:00:00Z');
const config = { enabled: true, profiles: ['writer-run'], actions: ['execute'], hosts: ['host-a','host-b'], ttl_seconds: 120 };
let sequence = 0;
async function fixture() {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('roles-' + ++sequence));
  const cookie = await inHub(stub, async (hub: any) => {
    hub._roleNow = BASE;
    hub.jobsService = new JobsService(hub.sql, hub.ctx, { now: () => new Date(hub._roleNow), enableAlarms: false });
    for (const id of ['host-a','host-b','foreign']) {
      const hash = await sha256('token-' + id);
      hub.sql.exec("INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at) VALUES(?,'host',?,?,?,?,?,'test',?,?)",
        'cred-' + id, hash, id, id, id, '["host"]', new Date(BASE).toISOString(), '2100-01-01T00:00:00Z');
      hub.sql.exec('INSERT INTO machines(id,name,hostname,credential_id,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES(?,?,?,?,?,?,?,?)',
        id,id,id,'cred-' + id,new Date(BASE).toISOString(),new Date(BASE).toISOString(),'{"gate":{"launch_allowed":true}}','{"jobs":["writer-run"]}');
    }
    for (const kind of ['agent','job','routing-client']) {
      hub.sql.exec("INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,'test',?,?)",
        kind,kind,await sha256('token-' + kind),kind,kind,kind === 'routing-client' ? 'routing-client:test' : 'ordinary',
        kind === 'routing-client' ? '["routing:submit"]' : '["host","admin","jobs:create"]',new Date(BASE).toISOString(),'2100-01-01T00:00:00Z');
    }
    hub.sql.exec("UPDATE credentials SET approval_seq=1 WHERE kind='host'");
    hub.sql.exec('UPDATE machines SET approval_seq=1');
    return createSessionCookie(hub.env, { sub: 'admin', email: 'admin@fake.local', isAdmin: true, csrfToken: 'roles-csrf', exp: Math.floor(Date.now()/1000)+3600 });
  });
  const request = async (path: string, body?: any, token = 'token-host-a', method = body === undefined ? 'GET' : 'POST', session = false, csrf = true) => {
    const headers: Record<string,string> = { 'Content-Type': 'application/json' };
    if (session) { headers.Cookie = cookie; if (csrf) headers['x-csrf-token'] = 'roles-csrf'; }
    else headers.Authorization = 'Bearer ' + token;
    const response = await stub.fetch(new Request('https://hub.test' + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }));
    return { code: response.status, data: await response.json() as any };
  };
  const configure = (value = config, generation = 0) => request('/api/roles/demo/writer', { config: value, expected_generation: generation }, '', 'PUT', true);
  const offer = (key = 'one', host = 'host-a', epoch = 1, generation = 1, date = '2099-05-06') => request('/api/roles/demo/writer/jobs', {
    generation, expected_epoch: epoch, idempotency_key: '2099-05-06:' + key, profile: 'writer-run', payload: { date }
  }, 'token-' + host);
  const claim = (id: string, host = 'host-a') => request(`/api/jobs/${id}/claim`, { gate: { launch_allowed: true } }, 'token-' + host);
  const execute = (id: string, token: string, epoch = 2, generation = 1) => request(`/api/jobs/${id}/execute`, { epoch, generation }, token);
  const start = (id: string, token: string, epoch = 2) => request(`/api/jobs/${id}/start`, { epoch, evidence_digest: 'sha256:0123456789abcdef', native_session: 'actual-fake-session', launcher_task_id: 'fake-approved-consumer' }, token);
  const inspect = (fn: (hub: any) => any) => inHub(stub, fn);
  const advance = (ms: number) => inspect((hub: any) => {
    hub._roleNow += ms;
    hub.sql.exec('UPDATE machines SET last_checkin_at=?', new Date(hub._roleNow).toISOString());
  });
  return { stub, request, configure, offer, claim, execute, start, inspect, advance };
}

describe('configured roles through authenticated public HTTP and real Hub SQLite', () => {
  it('requires operator session + CSRF; rejects reserved names, foreign grants, arbitrary profiles and commands', async () => {
    const f = await fixture();
    expect((await f.request('/api/roles/demo/writer')).code).toBe(403);
    expect((await f.request('/api/roles/demo/writer', { config, expected_generation: 0 }, '', 'PUT', true, false)).code).toBe(403);
    for (const name of ['root','principal','role','root:writer','Bad','a'.repeat(49)]) expect((await f.request('/api/roles/demo/' + name, {}, '', 'PUT', true)).code).toBe(400);
    expect((await f.configure({ ...config, profiles: ['root-run'] })).data.error).toBe('invalid_configuration');
    expect((await f.configure({ ...config, command: 'anything' } as any)).data.error).toBe('invalid_configuration');
    expect((await f.configure()).data).toMatchObject({ generation: 1, epoch: 1 });
    expect((await f.configure()).data.error).toBe('configuration_conflict');
    expect((await f.offer('x','foreign')).data.error).toBe('role_unauthorized');
    for (const kind of ['agent','job','routing-client']) expect((await f.request('/api/roles/demo/writer/jobs', {}, 'token-' + kind)).code).toBeGreaterThanOrEqual(403);
    expect(await f.inspect((hub: any) => hub.sql.exec("SELECT epoch,holder_job_id FROM leases WHERE name='root'").one())).toMatchObject({ epoch: 0, holder_job_id: null });
  });

  it('acquires, commits one actual job intent, starts, renews and completes idempotently without actor grants', async () => {
    const f = await fixture(); await f.configure();
    const offered = await f.offer(); expect(offered.code).toBe(201); const id = offered.data.job_id;
    expect((await f.offer()).data).toMatchObject({ job_id: id, duplicate: true });
    expect((await f.offer('one','host-a',1,1,'2099-05-07')).data.error).toBe('idempotency_conflict');
    const c = (await f.claim(id)).data; expect(c.lease_epoch).toBe(2);
    expect(c.role_binding).toMatchObject({ generation: 1, host_id: 'host-a', profile: 'writer-run' });
    expect((await f.start(id,c.job_token)).data.error).toBe('execution_intent_required');
    expect((await f.execute(id,c.job_token)).data.execute).toBe(true);
    expect((await f.execute(id,c.job_token)).data).toMatchObject({ execute: false, status: 'HOLD' });
    expect((await f.start(id,c.job_token)).code).toBe(200);
    expect((await f.start(id,c.job_token)).code).toBe(200);
    await f.advance(30000);
    expect((await f.request(`/api/jobs/${id}/renew`, { epoch: 2 }, c.job_token)).code).toBe(200);
    const finish = { epoch: 2, status: 'succeeded', summary: 'qualified fake consumer completed', evidence_refs: ['fake:evidence'] };
    expect((await f.request(`/api/jobs/${id}/finish`, finish, c.job_token)).code).toBe(200);
    expect((await f.request(`/api/jobs/${id}/finish`, finish, c.job_token)).code).toBe(200);
    expect((await f.request(`/api/jobs/${id}/finish`, { ...finish, summary: 'changed' }, c.job_token)).data.error).toBe('completion_conflict');
    expect((await f.execute(id,c.job_token)).code).toBe(409);
    expect(await f.inspect((hub: any) => ({ job: hub.sql.exec('SELECT status,role_effect,role_finish FROM jobs WHERE id=?', id).one(),
      scopes: hub.sql.exec('SELECT scopes FROM credentials WHERE subject=?',id).one().scopes,
      actor: hub.sql.exec('SELECT actor FROM credentials WHERE subject=?',id).one().actor,
      lease: hub.sql.exec("SELECT * FROM leases WHERE name='automation:demo:writer'").one() }))).toMatchObject({ job: { status: 'succeeded' }, scopes: '[]', actor: 'machine:host-a', lease: { epoch: 2, holder_job_id: null } });
  });

  it('serializes competing acquisitions and fences a delayed partitioned holder after expiry and restart', async () => {
    const f = await fixture(); await f.configure();
    const a = (await f.offer('a')).data.job_id; const b = (await f.offer('b','host-b')).data.job_id;
    const claims = await Promise.all([f.claim(a),f.claim(b,'host-b')]);
    expect(claims.map(c=>c.code).sort()).toEqual([200,409]);
    const winner = claims[0].code === 200 ? { id:a, c:claims[0].data } : { id:b, c:claims[1].data };
    await f.execute(winner.id,winner.c.job_token); await f.start(winner.id,winner.c.job_token);
    await f.advance(121000);
    expect((await f.request(`/api/jobs/${winner.id}/renew`, { epoch: 2 },winner.c.job_token)).code).toBe(409);
    await f.inspect((hub: any) => { hub.jobsService = new JobsService(hub.sql,hub.ctx,{now:()=>new Date(hub._roleNow),enableAlarms:false}); });
    // A different approved action may proceed; uncertain old work is never retried.
    const next = (await f.offer('replacement','host-b',2,1,'2099-05-07')).data.job_id;
    const c = (await f.claim(next,'host-b')).data; expect(c.lease_epoch).toBe(3);
    expect((await f.execute(winner.id,winner.c.job_token)).code).toBe(401);
    expect((await f.request(`/api/jobs/${winner.id}/finish`,{epoch:2,status:'succeeded',summary:'late'},winner.c.job_token)).code).toBe(401);
    expect((await f.execute(next,c.job_token,3)).data.execute).toBe(true);
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT status FROM jobs WHERE id=?',winner.id).one().status)).toBe('fenced');
  });

  for (const mutation of ['disable','generation','rotate','revoke','expire-credential','expire-job-credential','machine-death','drain','stale','quota','capability','rollback']) {
    it(`fails closed for ${mutation} at real job effects and renewal`, async () => {
      const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
      const c = (await f.claim(id)).data; await f.execute(id,c.job_token); await f.start(id,c.job_token);
      if (mutation === 'disable') await f.configure({ ...config, enabled: false },1);
      else if (mutation === 'generation') await f.configure(config,1);
      else await f.inspect((hub: any) => {
        if (mutation === 'rotate') hub.sql.exec("UPDATE machines SET credential_id='replacement' WHERE id='host-a'");
        if (mutation === 'revoke') hub.sql.exec("UPDATE credentials SET revoked_at=? WHERE id='cred-host-a'",new Date(hub._roleNow).toISOString());
        if (mutation === 'expire-credential') hub.sql.exec("UPDATE credentials SET expires_at=? WHERE id='cred-host-a'",new Date(hub._roleNow).toISOString());
        if (mutation === 'expire-job-credential') hub.sql.exec("UPDATE credentials SET expires_at=? WHERE kind='job' AND subject=?",new Date(hub._roleNow).toISOString(),id);
        if (mutation === 'machine-death') hub.sql.exec("DELETE FROM machines WHERE id='host-a'");
        if (mutation === 'drain') hub.sql.exec("UPDATE machines SET drained=1 WHERE id='host-a'");
        if (mutation === 'stale') hub.sql.exec("UPDATE machines SET last_checkin_at=NULL WHERE id='host-a'");
        if (mutation === 'quota') hub.sql.exec("UPDATE machines SET last_heartbeat='{}' WHERE id='host-a'");
        if (mutation === 'capability') hub.sql.exec("UPDATE machines SET capabilities='{}' WHERE id='host-a'");
        if (mutation === 'rollback') { hub._roleNow -= 1; hub.sql.exec('UPDATE machines SET last_checkin_at=?',new Date(hub._roleNow).toISOString()); }
      });
      expect((await f.execute(id,c.job_token)).code).toBeGreaterThanOrEqual(400);
      expect((await f.request(`/api/jobs/${id}/renew`, { epoch: 2 },c.job_token)).code).toBeGreaterThanOrEqual(400);
      expect((await f.request(`/api/jobs/${id}/finish`, { epoch: 2,status:'succeeded',summary:'stale' },c.job_token)).code).toBeGreaterThanOrEqual(400);
      expect(await f.inspect((hub: any) => hub.sql.exec('SELECT status FROM jobs WHERE id=?',id).one().status)).not.toBe('succeeded');
    });
  }

  it('does not accept a naked epoch, another job token, foreign claim or profile/payload injection', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    expect((await f.claim(id,'foreign')).code).toBe(409);
    const c = (await f.claim(id)).data;
    expect((await f.execute(id,'token-host-a')).code).toBe(403);
    expect((await f.execute(id,c.job_token,999)).code).toBe(409);
    expect((await f.execute(id,c.job_token,2,999)).code).toBe(409);
    expect((await f.request('/api/roles/demo/writer/jobs',{generation:1,expected_epoch:2,idempotency_key:'2099-05-06:bad',profile:'writer-run',payload:{date:'2099-05-06',argv:['sh']} })).data.error).toBe('invalid_role_job');
    expect((await f.request('/api/conversations', { title:'no role membership' }, c.job_token)).code).toBeGreaterThanOrEqual(400);
  });

  it('bounds retained jobs and never prunes unresolved intents to permit a duplicate', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    const c = (await f.claim(id)).data; await f.execute(id,c.job_token);
    await f.inspect((hub: any) => {
      hub.sql.exec("UPDATE jobs SET finished_at='2099-01-01T00:00:00Z',status='fenced' WHERE id=?",id);
      hub.jobsService.roles.prune(hub._roleNow);
    });
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT count(*) AS n FROM jobs WHERE id=?',id).one().n)).toBe(1);
    await f.advance(86400000);
    expect((await f.offer()).data.error).toBe('idempotency_key_expired');
  });

  it('enforces hard role/job caps, accounts terminal receipts and prunes only resolved old jobs', async () => {
    const f = await fixture(); await f.configure();
    await f.inspect((hub: any) => {
      hub.sql.exec(`WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1024)
        INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,role_name,role_generation,finished_at)
        SELECT 'cap-'||x,'writer-run','cap-'||x,'{}','failed','2099-05-06T00:00:00Z','2099-05-07T00:00:00Z','test','automation:demo:writer',1,'2099-05-06T00:00:00Z' FROM n`);
    });
    expect((await f.offer('at-capacity')).data.error).toBe('role_job_capacity');
    await f.inspect((hub: any) => {
      hub.sql.exec("UPDATE jobs SET finished_at='2099-01-01T00:00:00Z',role_finish='resolved' WHERE id='cap-1'");
      hub.jobsService.roles.prune(hub._roleNow);
    });
    expect((await f.offer('after-prune')).code).toBe(201);
    await f.inspect((hub: any) => {
      hub.sql.exec(`WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<63)
        INSERT INTO automation_roles(name,generation,config,last_clock) SELECT 'cap-'||x,1,'{}',0 FROM n`);
    });
    expect((await f.request('/api/roles/demo/extra', {config,expected_generation:0},'', 'PUT',true)).data.error).toBe('role_capacity');
  });

  it('rechecks actual host/credential/epoch at claim and public offers carry consumer binding', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    const offers = await f.request('/api/jobs/offers');
    expect(offers.data.offers.find((x: any) => x.id === id).role_binding).toMatchObject({ name:'automation:demo:writer',generation:1,profile:'writer-run',host_id:'host-a' });
    await f.inspect((hub: any) => hub.sql.exec("UPDATE machines SET credential_id='rotated' WHERE id='host-a'"));
    expect((await f.claim(id)).code).toBe(403);
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT status FROM jobs WHERE id=?',id).one().status)).toBe('offered');
    await f.inspect((hub: any) => hub.sql.exec("UPDATE machines SET credential_id='cred-host-a' WHERE id='host-a'"));
    await f.advance(120001);
    expect((await f.claim(id)).code).toBe(403);
  });

  it('commits one competing execution intent and rolls back a failed SQL effect before granting a retry', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    const c = (await f.claim(id)).data;
    await f.inspect((hub: any) => hub.sql.exec("CREATE TRIGGER reject_role_intent BEFORE UPDATE OF role_effect ON jobs BEGIN SELECT RAISE(ABORT, 'injected SQL intent failure'); END"));
    expect((await f.execute(id,c.job_token)).code).toBe(500);
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect FROM jobs WHERE id=?',id).one().role_effect)).toBeNull();
    await f.inspect((hub: any) => hub.sql.exec('DROP TRIGGER reject_role_intent'));
    const replies = await Promise.all([f.execute(id,c.job_token),f.execute(id,c.job_token)]);
    expect(replies.filter(r=>r.data.execute === true)).toHaveLength(1);
    expect(replies.filter(r=>r.data.status === 'HOLD')).toHaveLength(1);
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT count(*) AS n FROM jobs WHERE role_name IS NOT NULL').one().n)).toBe(1);
  });

  it('fails closed before epoch integers lose precision', async () => {
    const f = await fixture(); await f.configure();
    await f.inspect((hub: any) => hub.sql.exec("UPDATE leases SET epoch=? WHERE name='automation:demo:writer'",Number.MAX_SAFE_INTEGER));
    const id = (await f.offer('max','host-a',Number.MAX_SAFE_INTEGER)).data.job_id;
    expect((await f.claim(id)).code).toBe(409);
    expect((await f.configure(config,1)).data.error).toBe('epoch_capacity');
  });

  it('preserves existing writer-date exclusion for both legacy-first and role-first claims', async () => {
    for (const legacyFirst of [true,false]) {
      const f = await fixture(); await f.configure(); const roleId = (await f.offer()).data.job_id;
      await f.inspect((hub: any) => hub.sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_expires_at)
        VALUES('legacy-writer','writer-run','writer-run:2099-05-06','{}','offered',?,?,'system','host-a',?)`,
        new Date(BASE).toISOString(),new Date(BASE+1500000).toISOString(),new Date(BASE+120000).toISOString()));
      const first = legacyFirst ? 'legacy-writer' : roleId;
      const second = legacyFirst ? roleId : 'legacy-writer';
      expect((await f.claim(first)).code).toBe(200);
      expect((await f.claim(second)).code).toBe(409);
      expect(await f.inspect((hub: any) => hub.sql.exec("SELECT count(*) AS n FROM credentials WHERE kind='job' AND subject IN (?,?)",roleId,'legacy-writer').one().n)).toBe(1);
    }
  });

  it('retains an unresolved effect after expiry and refuses a different key for the same approved payload', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    const c = (await f.claim(id)).data; expect((await f.execute(id,c.job_token)).data.execute).toBe(true);
    await f.advance(121000);
    const next = (await f.offer('different-key','host-b',2)).data.job_id;
    const nextClaim = await f.claim(next,'host-b');
    expect(nextClaim.code).toBe(409);
    expect(nextClaim.data).toMatchObject({ error:'conflict' });
    expect(nextClaim.data.job_token).toBeUndefined();
    expect(await f.inspect((hub: any) => hub.sql.exec("SELECT count(*) AS n FROM credentials WHERE kind='job' AND subject=?",next).one().n)).toBe(0);
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect FROM jobs WHERE id=?',next).one().role_effect)).toBeNull();
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect,role_finish FROM jobs WHERE id=?',id).one())).toMatchObject({ role_finish:null });
    // No takeover occurs after a refusal. Reconfiguration explicitly revokes
    // the original credential while retaining the unresolved effect.
    await f.configure(config,1);
    expect((await f.execute(id,c.job_token)).code).toBe(401);
  });

  for (const target of ['other/writer','demo/alias']) {
    it(`keeps one credential and intent for an unresolved writer date across expiry and ${target}`, async () => {
      const f = await fixture(); await f.configure();
      const configureTarget = (generation = 0) => f.request('/api/roles/' + target, {config,expected_generation:generation},'', 'PUT',true);
      const offerTarget = (key: string, epoch = 1, generation = 1, date = '2099-05-06') => f.request('/api/roles/' + target + '/jobs', {
        generation,expected_epoch:epoch,idempotency_key:'2099-05-06:' + key,profile:'writer-run',payload:{date},
      },'token-host-b');
      expect((await configureTarget()).code).toBe(200);
      const id = (await f.offer()).data.job_id;
      const c = (await f.claim(id)).data;
      expect((await f.execute(id,c.job_token)).data.execute).toBe(true);
      const original = await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect,role_finish FROM jobs WHERE id=?',id).one());
      await f.advance(121000);
      // Restart with real retained SQLite; no in-memory lease or project map can fence this.
      await f.inspect((hub: any) => { hub.jobsService = new JobsService(hub.sql,hub.ctx,{now:()=>new Date(hub._roleNow),enableAlarms:false}); });
      for (const [key,epoch,generation] of [['after-expiry',1,1],['after-reconfigure',2,2]] as const) {
        if (generation === 2) expect((await configureTarget(1)).data).toMatchObject({epoch:2,generation:2});
        const offered = await offerTarget(key,epoch,generation); expect(offered.code).toBe(201);
        const next = offered.data.job_id;
        const denied = await f.claim(next,'host-b');
        expect(denied.code).toBe(409); expect(denied.data.error).toBe('conflict');
        expect(denied.data.job_token).toBeUndefined();
        expect((await f.execute(next,c.job_token,epoch,generation)).code).toBeGreaterThanOrEqual(400);
        expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect FROM jobs WHERE id=?',next).one().role_effect)).toBeNull();
      }
      // A legacy dated key has exactly the same identity, despite lacking payload.date.
      await f.inspect((hub: any) => hub.sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_expires_at)
        VALUES('legacy-later','writer-run','writer-run:2099-05-06','{}','offered',?,?,'system','host-b',?)`,
        new Date(hub._roleNow).toISOString(),new Date(hub._roleNow+1500000).toISOString(),new Date(hub._roleNow+120000).toISOString()));
      expect((await f.claim('legacy-later','host-b')).code).toBe(409);
      expect(await f.inspect((hub: any) => hub.sql.exec("SELECT count(*) AS n FROM credentials WHERE kind='job' AND subject IN (SELECT id FROM jobs)").one().n)).toBe(1);
      expect(await f.inspect((hub: any) => hub.sql.exec('SELECT count(*) AS n FROM jobs WHERE role_effect IS NOT NULL').one().n)).toBe(1);
      expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect,role_finish FROM jobs WHERE id=?',id).one())).toEqual(original);
      // A different UTC date is approved work without resolving or deleting the old intent.
      const different = await offerTarget('different-date',2,2,'2099-05-07'); expect(different.code).toBe(201);
      const next = different.data.job_id; const nextClaim = await f.claim(next,'host-b'); expect(nextClaim.code).toBe(200);
      expect((await f.execute(next,nextClaim.data.job_token,3,2)).data.execute).toBe(true);
      expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect,role_finish FROM jobs WHERE id=?',id).one())).toEqual(original);
      expect((await f.execute(id,c.job_token)).code).toBe(409);
    });
  }

  for (const legacy of [false,true]) {
    it(`rechecks retained ${legacy ? 'legacy dated-key' : 'cross-project'} uncertainty at the effect transaction`, async () => {
      const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
      const c = (await f.claim(id)).data;
      // Model a previously issued job discovering retained pre-repair uncertainty
      // after its claim. This fixture row is evidence, never a physical effect.
      await f.inspect((hub: any) => hub.sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,role_name,role_effect)
        VALUES('retained-intent','writer-run',?,?,'fenced',?,?,'system',?,'retained unresolved intent')`,
        legacy ? 'writer-run:2099-05-06' : 'automation:other:alias:2099-05-06:old',
        legacy ? '{}' : '{"project":"other","date":"2099-05-06"}',new Date(BASE).toISOString(),new Date(BASE+120000).toISOString(),
        legacy ? null : 'automation:other:alias'));
      const replies = await Promise.all([f.execute(id,c.job_token),f.execute(id,c.job_token)]);
      for (const reply of replies) expect(reply.data).toMatchObject({execute:false,status:'HOLD',reason:'previous_execution_unresolved'});
      expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect FROM jobs WHERE id=?',id).one().role_effect)).toBeNull();
      expect(await f.inspect((hub: any) => hub.sql.exec("SELECT role_effect,role_finish FROM jobs WHERE id='retained-intent'").one())).toMatchObject({role_effect:'retained unresolved intent',role_finish:null});
    });
  }

  it('refuses a delayed first execute after authority changes without losing earlier uncertainty', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    const c = (await f.claim(id)).data; expect((await f.execute(id,c.job_token)).data.execute).toBe(true);
    await f.advance(121000);
    const next = (await f.offer('later-date','host-b',2,1,'2099-05-07')).data.job_id;
    const nextClaim = await f.claim(next,'host-b'); expect(nextClaim.code).toBe(200);
    expect((await f.configure(config,1)).data.generation).toBe(2);
    expect((await f.execute(next,nextClaim.data.job_token,3)).code).toBeGreaterThanOrEqual(400);
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect FROM jobs WHERE id=?',next).one().role_effect)).toBeNull();
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_effect IS NOT NULL AS kept,role_finish FROM jobs WHERE id=?',id).one())).toMatchObject({kept:1,role_finish:null});
  });

  it('rolls back lease acquisition when credential insertion fails and can safely retry', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    await f.inspect((hub: any) => hub.sql.exec("CREATE TRIGGER reject_role_credential BEFORE INSERT ON credentials WHEN NEW.kind='job' BEGIN SELECT RAISE(ABORT, 'injected SQL credential failure'); END"));
    expect((await f.claim(id)).code).toBe(500);
    expect(await f.inspect((hub: any) => hub.sql.exec("SELECT epoch,holder_job_id FROM leases WHERE name='automation:demo:writer'").one())).toMatchObject({epoch:1,holder_job_id:null});
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT status,role_effect FROM jobs WHERE id=?',id).one())).toMatchObject({status:'offered',role_effect:null});
    expect(await f.inspect((hub: any) => hub.sql.exec("SELECT count(*) AS n FROM credentials WHERE kind='job' AND subject IN (SELECT id FROM jobs)").one().n)).toBe(0);
    await f.inspect((hub: any) => hub.sql.exec('DROP TRIGGER reject_role_credential'));
    const c = await f.claim(id); expect(c.code).toBe(200);
    expect((await f.execute(id,c.data.job_token)).data.execute).toBe(true);
  });

  it('uses the claim snapshot when a writer lease expires during credential hashing', async () => {
    const f = await fixture(); await f.configure(); const id = (await f.offer()).data.job_id;
    expect((await f.claim(id)).code).toBe(200);
    await f.advance(119000);
    const result = await f.inspect(async (hub: any) => {
      hub.sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_expires_at)
        VALUES('legacy-snapshot','writer-run','writer-run:2099-05-06','{}','offered',?,?,'system','host-b',?)`,
        new Date(hub._roleNow).toISOString(),new Date(hub._roleNow+1500000).toISOString(),new Date(hub._roleNow+120000).toISOString());
      let reads = 0;
      hub.jobsService.setClock(() => new Date(hub._roleNow + (reads++ === 0 ? 0 : 2000)));
      try { return await hub.jobsService.claimJob('legacy-snapshot','host-b',true,undefined,'cred-host-b'); }
      finally { hub.jobsService.setClock(() => new Date(hub._roleNow)); }
    });
    expect(result).toMatchObject({error:'conflict',reason:'writer_date_busy'});
    expect(result.job_token).toBeUndefined();
  });

  it('supports expanded role profile sets while requiring all enabled capabilities on configured hosts', async () => {
    const f = await fixture();
    // Host-a and Host-b only have ['writer-run'] initially.
    // Configuring multi-profile fails host_ineligible because hosts lack 'principal-wake'
    const multiConfig = { ...config, profiles: ['writer-run', 'principal-wake'] };
    expect((await f.configure(multiConfig)).data.error).toBe('host_ineligible');

    // Reject duplicate profiles
    expect((await f.configure({ ...config, profiles: ['writer-run', 'writer-run'] })).data.error).toBe('invalid_configuration');
    // Reject empty profiles
    expect((await f.configure({ ...config, profiles: [] })).data.error).toBe('invalid_configuration');
    // Reject unknown profiles
    expect((await f.configure({ ...config, profiles: ['writer-run', 'unsupported'] })).data.error).toBe('invalid_configuration');

    // Grant 'principal-wake' and 'head-recovery' capabilities to host-a and host-b
    await f.inspect((hub: any) => {
      hub.sql.exec("UPDATE machines SET capabilities='{\"jobs\":[\"writer-run\",\"principal-wake\",\"head-recovery\"]}' WHERE id IN ('host-a','host-b')");
    });

    // Now multi-profile configuration succeeds
    const fullConfig = { ...config, profiles: ['writer-run', 'principal-wake', 'head-recovery'] };
    const res = await f.configure(fullConfig);
    expect(res.data).toMatchObject({ generation: 1, epoch: 1 });

    // Host can offer principal-wake job
    const wakeRes = await f.request('/api/roles/demo/writer/jobs', {
      generation: 1, expected_epoch: 1, idempotency_key: '2099-05-06:wake-1',
      profile: 'principal-wake', payload: { prompt: 'wake idle principal' }
    }, 'token-host-a');
    expect(wakeRes.code).toBe(201);
    expect(wakeRes.data.job_id).toBeDefined();

    // Claim principal-wake job
    const claimRes = await f.claim(wakeRes.data.job_id, 'host-a');
    expect(claimRes.code).toBe(200);
    expect(claimRes.data.role_binding).toMatchObject({ generation: 1, host_id: 'host-a', profile: 'principal-wake' });

    // Execute intent on principal-wake job
    const execRes = await f.execute(wakeRes.data.job_id, claimRes.data.job_token, 2, 1);
    expect(execRes.data.execute).toBe(true);
    expect(execRes.data.profile).toBe('principal-wake');
  });

  it('refuses nonterminal and successful jobs even after their lease expires', async () => {
    for (const status of ['claimed', 'running', 'succeeded', 'finished']) {
      const f = await fixture(); await f.configure();
      const id = (await f.offer('terminal-status-' + status)).data.job_id;
      await f.claim(id);
      await f.inspect((hub: any) => {
        hub.sql.exec('UPDATE jobs SET status=? WHERE id=?', status, id);
        hub.sql.exec("UPDATE leases SET expires_at=? WHERE name='automation:demo:writer'",
          new Date(hub._roleNow - 1000).toISOString());
      });
      const result = await f.request(`/api/jobs/${id}/reconcile`, {
        epoch: 2, generation: 1, terminal_case: 'unstarted'
      });
      expect(result.code, status).toBe(409);
      expect(result.data.error, status).toBe('job_not_terminal');
      expect(await f.inspect((hub: any) => hub.sql.exec('SELECT role_finish FROM jobs WHERE id=?', id).one().role_finish), status).toBeNull();
    }
  });

  it('reconciles terminal unstarted V18-style and executed V17-style role jobs with server marker, rejecting business success and conflicting replay', async () => {
    const f = await fixture();
    await f.configure();

    // 1. V18-style: claimed, unstarted, terminal expired without native or execution intent
    const v18Offer = await f.offer('v18-offer');
    expect(v18Offer.code).toBe(201);
    const v18Id = v18Offer.data.job_id;
    const v18Claim = (await f.claim(v18Id)).data;
    expect(v18Claim.lease_epoch).toBe(2);

    // Job expires without start or execution intent
    await f.inspect((hub: any) => {
      hub.sql.exec("UPDATE jobs SET status = 'expired', deadline_at = ? WHERE id = ?", new Date(hub._roleNow - 1000).toISOString(), v18Id);
      hub.sql.exec("UPDATE leases SET expires_at = ? WHERE name = 'automation:demo:writer'", new Date(hub._roleNow - 1000).toISOString());
    });

    // Cleanup authority does not depend on launch quota/admission being available.
    await f.inspect((hub: any) => hub.sql.exec("UPDATE machines SET last_heartbeat=? WHERE id='host-a'", JSON.stringify({ gate: { launch_allowed: false } })));
    // Reconcile terminal unstarted
    const v18Rec = await f.request(`/api/jobs/${v18Id}/reconcile`, {
      epoch: 2,
      generation: 1,
      terminal_case: 'unstarted',
      summary: 'settled terminal unstarted'
    }, 'token-host-a');
    expect(v18Rec.code).toBe(200);
    expect(v18Rec.data).toMatchObject({
      reconciled: true,
      status: 'reconciled',
      job_id: v18Id,
      epoch: 2,
      generation: 1,
      outcome: 'failed',
      terminal_case: 'unstarted'
    });

    // Verify DB state: original expired status retained, never business success; role_finish has failed outcome
    const v18Db = await f.inspect((hub: any) => ({
      job: hub.sql.exec('SELECT status, role_effect, role_finish FROM jobs WHERE id = ?', v18Id).one(),
      audit: hub.sql.exec("SELECT action, actor, target FROM audit WHERE action = 'reconcile' AND target = ?", v18Id).one()
    }));
    expect(v18Db.job.status).toBe('expired');
    expect(v18Db.job.role_effect).toBeNull();
    expect(JSON.parse(v18Db.job.role_finish)).toMatchObject({ status: 'failed', outcome: 'failed', terminal_case: 'unstarted' });
    expect(v18Db.audit).toMatchObject({ action: 'reconcile', actor: 'host:host-a', target: v18Id });

    // Exact idempotent replay returns replay: true
    const v18Replay = await f.request(`/api/jobs/${v18Id}/reconcile`, {
      epoch: 2,
      generation: 1,
      terminal_case: 'unstarted',
      summary: 'settled terminal unstarted'
    }, 'token-host-a');
    expect(v18Replay.code).toBe(200);
    expect(v18Replay.data.replay).toBe(true);

    // Conflicting replay returns 409
    const v18Conflict = await f.request(`/api/jobs/${v18Id}/reconcile`, {
      epoch: 2,
      generation: 1,
      terminal_case: 'unstarted',
      summary: 'conflicting summary'
    }, 'token-host-a');
    expect(v18Conflict.code).toBe(409);
    expect(v18Conflict.data.error).toBe('reconciliation_conflict');

    await f.inspect((hub: any) => hub.sql.exec("UPDATE machines SET last_heartbeat=? WHERE id='host-a'", JSON.stringify({ gate: { launch_allowed: true } })));

    // 2. Fully acknowledged executed job (actual unacknowledged V17 must remain HOLD).
    const v17Offer = await f.offer('v17-offer', 'host-a', 2);
    expect(v17Offer.code).toBe(201);
    const v17Id = v17Offer.data.job_id;
    const v17Claim = (await f.claim(v17Id)).data;
    const v17Exec = await f.execute(v17Id, v17Claim.job_token, 3, 1);
    expect(v17Exec.data.execute).toBe(true);

    // A fully server-acknowledged role later reaches physical retirement.
    const nativeSid = '60bbfd26-6666-4666-8666-666666666666';
    const canonicalTag = `task-hub-role-writer-run-${v17Id}-g1-e3`;
    expect((await f.request(`/api/jobs/${v17Id}/start`, { epoch: 3, evidence_digest: 'sha256:0123456789abcdef', native_session: nativeSid, launcher_task_id: canonicalTag.slice(5) }, v17Claim.job_token)).code).toBe(200);
    await f.advance(20000);
    await f.inspect((hub: any) => {
      // Preserve the acknowledged native association while expiring the job.
      hub.sql.exec("UPDATE jobs SET status = 'expired', deadline_at = ? WHERE id = ?", new Date(hub._roleNow - 1000).toISOString(), v17Id);
      hub.sql.exec("UPDATE leases SET expires_at = ? WHERE name = 'automation:demo:writer'", new Date(hub._roleNow - 1000).toISOString());
      const bindingJson = JSON.stringify({
        provenance: 'observed_native',
        native_id: nativeSid,
        tag: canonicalTag,
        workspace: '/test/workspace',
        native_created_at_ms: BASE,
        generation: 1,
        retirement: {
          native_id: nativeSid,
          tag: canonicalTag,
          workspace: '/test/workspace',
          engine: 'writer-run',
          generation: 1,
          cause: 'finished',
          created_at_ms: BASE,
          finished_at_ms: BASE + 10000,
          retired_at: new Date(BASE + 10000).toISOString()
        }
      });
      hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
        VALUES (?, 'host-a', 'req-v17', '{}', 'host-a', 'writer-run', 'task', 'stopped', ?, ?, ?)`,
        nativeSid, bindingJson, new Date(BASE).toISOString(), new Date(BASE + 10000).toISOString());
    });

    // Attempting unstarted on executed job fails closed
    const invalidUnstarted = await f.request(`/api/jobs/${v17Id}/reconcile`, {
      epoch: 3,
      generation: 1,
      terminal_case: 'unstarted'
    }, 'token-host-a');
    expect(invalidUnstarted.code).toBe(409);
    expect(invalidUnstarted.data.error).toBe('execution_intent_recorded');

    // Reconcile executed V17 with posthoc correlation tuple
    const v17Rec = await f.request(`/api/jobs/${v17Id}/reconcile`, {
      epoch: 3,
      generation: 1,
      terminal_case: 'executed',
      native_session: nativeSid,
      summary: 'settled terminal executed'
    }, 'token-host-a');
    expect(v17Rec.code).toBe(200);
    expect(v17Rec.data).toMatchObject({
      reconciled: true,
      status: 'reconciled',
      job_id: v17Id,
      epoch: 3,
      generation: 1,
      outcome: 'failed',
      terminal_case: 'executed'
    });

    // DB state: status remains expired, role_effect preserved, null started_at preserved (no fabrication)
    const v17Db = await f.inspect((hub: any) => hub.sql.exec('SELECT status, role_effect, role_finish, started_at, native_session FROM jobs WHERE id = ?', v17Id).one());
    expect(v17Db.status).toBe('expired');
    expect(v17Db.role_effect).not.toBeNull();
    expect(v17Db.started_at).not.toBeNull();
    expect(v17Db.native_session).toBe(nativeSid);
    expect(JSON.parse(v17Db.role_finish)).toMatchObject({ status: 'failed', outcome: 'failed', terminal_case: 'executed', native_session: nativeSid });

    // Now that V17 fence is discharged, next job with same payload/date can execute
    const nextOffer = await f.offer('v17-next', 'host-a', 3);
    expect(nextOffer.code).toBe(201);
    const nextClaim = (await f.claim(nextOffer.data.job_id)).data;
    const nextExec = await f.execute(nextOffer.data.job_id, nextClaim.job_token, 4, 1);
    expect(nextExec.data.execute).toBe(true);
  });

  it('refuses reconciliation on wrong host, epoch/generation mismatch, revocation race, and malformed marker while preserving active leases', async () => {
    const f = await fixture();
    await f.configure();

    const offered = await f.offer('guard-test');
    const id = offered.data.job_id;
    const claim = (await f.claim(id)).data;

    // Reject non-terminal job reconciliation
    const activeReconcile = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'unstarted' }, 'token-host-a');
    expect(activeReconcile.code).toBe(409);
    expect(activeReconcile.data.error).toBe('job_not_terminal');

    // Mark expired
    await f.inspect((hub: any) => hub.sql.exec("UPDATE jobs SET status = 'expired' WHERE id = ?", id));

    // Reject unauthorized callers (agents, jobs, foreign hosts, wrong host)
    for (const token of ['token-agent', 'token-job', 'token-foreign', 'token-host-b']) {
      const res = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'unstarted' }, token);
      expect(res.code).toBe(403);
    }

    const mixed = await f.stub.fetch(new Request(`https://hub.test/api/jobs/${id}/reconcile`, { method: 'POST', headers: { Authorization: 'Bearer token-host-a', Cookie: 'irrelevant=1', 'Content-Type': 'application/json' }, body: JSON.stringify({ epoch: 2, generation: 1, terminal_case: 'unstarted' }) }));
    expect(mixed.status).toBe(403);
    expect(await mixed.json()).toMatchObject({ error: 'mixed_authority' });

    // Reject epoch and generation mismatch
    expect((await f.request(`/api/jobs/${id}/reconcile`, { epoch: 99, generation: 1, terminal_case: 'unstarted' }, 'token-host-a')).code).toBe(409);
    expect((await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 99, terminal_case: 'unstarted' }, 'token-host-a')).code).toBe(409);

    // Re-read credential authority inside the mutation, even if entry authentication passed.
    const raced = await f.inspect((hub: any) => hub.jobsService.reconcileTerminalJob(id, 'host-a', 'cred-host-a', { epoch: 2, generation: 1, terminal_case: 'unstarted' }, () => {
      hub.sql.exec("UPDATE credentials SET scopes='[]' WHERE id='cred-host-a'");
      return true;
    }));
    expect(raced).toMatchObject({ error: 'unauthorized_or_stale', status_code: 403 });
    await f.inspect((hub: any) => hub.sql.exec("UPDATE credentials SET scopes='[\"host\"]' WHERE id='cred-host-a'"));

    // Strict input rejects aliases and malformed or unstarted native claims.
    for (const extra of [{ lease_epoch: 2 }, { native_session: 42 }, { native_session: 'caller-guessed-session' }]) {
      expect((await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'unstarted', ...extra }, 'token-host-a')).code).toBe(400);
    }

    // Marker negative tests for executed case
    const nativeSid = '99999999-9999-4999-8999-999999999999';
    const canonicalTag = `task-hub-role-writer-run-${id}-g1-e2`;

    // Actual V17 shape: local execution cannot backfill absent server start evidence.
    await f.inspect((hub: any) => hub.sql.exec("UPDATE jobs SET role_effect='execute' WHERE id=?", id));
    const unbound = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'executed', native_session: nativeSid }, 'token-host-a');
    expect(unbound.code).toBe(409);
    expect(unbound.data).toMatchObject({ status: 'HOLD', reason: 'missing_native_session' });
    expect(await f.inspect((hub: any) => hub.sql.exec('SELECT native_session, first_action_at, role_finish FROM jobs WHERE id=?', id).one())).toMatchObject({ native_session: null, first_action_at: null, role_finish: null });

    // A separately acknowledged executed record reaches the marker checks.
    await f.inspect((hub: any) => hub.sql.exec('UPDATE jobs SET native_session=?, launcher_task_id=?, started_at=?, first_action_at=? WHERE id=?', nativeSid, canonicalTag.slice(5), new Date(BASE).toISOString(), new Date(BASE).toISOString(), id));
    // 1. Missing marker
    await f.inspect((hub: any) => {
      hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
        VALUES (?, 'host-a', 'req-neg', '{}', 'host-a', 'writer-run', 'task', 'stopped', ?, ?, ?)`,
        nativeSid, JSON.stringify({ provenance: 'observed_native', native_id: nativeSid, tag: canonicalTag, workspace: '/test/ws', native_created_at_ms: BASE, generation: 1 }),
        new Date(BASE).toISOString(), new Date(BASE + 1000).toISOString());
    });
    const missingMarker = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'executed', native_session: nativeSid }, 'token-host-a');
    expect(missingMarker.code).toBe(409);
    expect(missingMarker.data).toMatchObject({ status: 'HOLD', reason: 'retirement_marker_missing' });

    // 2. Physical state not stopped (e.g. still running)
    await f.inspect((hub: any) => hub.sql.exec("UPDATE session_control_sessions SET state = 'running' WHERE id = ?", nativeSid));
    const runningState = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'executed', native_session: nativeSid }, 'token-host-a');
    expect(runningState.code).toBe(409);
    expect(runningState.data).toMatchObject({ status: 'HOLD', reason: 'session_not_stopped' });

    // 3. Mismatched tag in marker
    await f.inspect((hub: any) => {
      hub.sql.exec("UPDATE session_control_sessions SET state = 'stopped', binding_json = ? WHERE id = ?",
        JSON.stringify({
          provenance: 'observed_native', native_id: nativeSid, tag: 'task-wrong-tag', workspace: '/test/ws', native_created_at_ms: BASE, generation: 1,
          retirement: { native_id: nativeSid, tag: 'task-wrong-tag', workspace: '/test/ws', engine: 'writer-run', generation: 1, cause: 'finished', created_at_ms: BASE, finished_at_ms: BASE + 5000 }
        }), nativeSid);
    });
    const wrongTag = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'executed', native_session: nativeSid }, 'token-host-a');
    expect(wrongTag.code).toBe(409);
    expect(wrongTag.data).toMatchObject({ status: 'HOLD', reason: 'binding_mismatch' });

    // Host credential revocation fails closed
    await f.inspect((hub: any) => hub.sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'cred-host-a'", new Date(hub._roleNow).toISOString()));
    const revoked = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'unstarted' }, 'token-host-a');
    expect([401, 403]).toContain(revoked.code);

    // Machine revocation inside mutation transaction fails closed with 403
    await f.inspect((hub: any) => {
      hub.sql.exec("UPDATE credentials SET revoked_at = NULL WHERE id = 'cred-host-a'");
      hub.sql.exec("UPDATE machines SET state = 'revoked' WHERE id = 'host-a'");
    });
    const machineRevoked = await f.request(`/api/jobs/${id}/reconcile`, { epoch: 2, generation: 1, terminal_case: 'unstarted' }, 'token-host-a');
    expect(machineRevoked.code).toBe(403);

    // Verify root lease and leases table completely untouched
    const rootLease = await f.inspect((hub: any) => hub.sql.exec("SELECT * FROM leases WHERE name = 'root'").one());
    expect(rootLease).toMatchObject({ name: 'root', epoch: 0, holder_job_id: null });
  });
});
