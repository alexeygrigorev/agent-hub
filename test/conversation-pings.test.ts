import { beforeEach, describe, expect, it } from 'vitest';
import { SELF, env, runInDurableObject } from 'cloudflare:test';
import { Hub } from '../src/conversation-hub';
import { createSessionCookie } from '../src/auth/session';
import { ChecksService, validateCheck, type ActionDeps } from '../src/checks';
import { ConversationsService } from '../src/conversations';
import { AUTOMATION_SENDER, type ConversationCheckPort } from '../src/conversation-automation';

const actor = 'pings:recipient', csrf = 'local-pings-csrf', template = 'Follow-up {check}: {detail}';
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const inside = <T>(fn: (hub: Hub, state: DurableObjectState) => T | Promise<T>) => runInDurableObject(stub(), fn);
async function sql(q: string, ...args: (string | number | null)[]): Promise<any[]> {
  let rows: any[] = []; await inside(h => { rows = h.sql.exec(q, ...args).toArray(); }); return rows;
}
async function cookie(sub = 'pings-owner') {
  return (await createSessionCookie(env as any, { sub, email: 'admin@fake.local', isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
}
async function req(path: string, method = 'GET', body?: unknown, auth?: string, useCsrf = true) {
  const headers: Record<string, string> = {};
  if (auth?.startsWith('hubt_')) headers.Authorization = 'Bearer ' + auth;
  else if (auth) headers.Cookie = auth;
  if (useCsrf) headers['X-CSRF-Token'] = csrf;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return SELF.fetch('https://hub.test' + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
}
async function ok(response: Response, status = 200): Promise<any> { expect(response.status).toBe(status); return response.json(); }
async function identity(auth: string) { await ok(await req('/api/conversations/participants', 'GET', undefined, auth)); }
async function pair(owner: string, target = actor) {
  await sql("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('pings-machine','Local fixture','local',?)", new Date().toISOString());
  const code = await ok(await req('/api/device/code', 'POST', { client_kind: 'agent', name: target, actor: target, requested_scopes: ['bus:read', 'bus:send'] }));
  await ok(await req('/api/device/approve/' + code.user_code, 'POST', { machine_id: 'pings-machine', expected_actor: target }, owner));
  return (await ok(await req('/api/device/token', 'POST', { grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: code.device_code }))).access_token as string;
}
async function group(owner: string, target = actor) {
  return (await ok(await req('/api/conversations', 'POST', { kind: 'group', title: 'Local ping contract', members: [target], idempotency_key: crypto.randomUUID() }, owner), 201)).id as string;
}
async function send(cid: string, auth: string, body = 'Original unread', key = crypto.randomUUID()) {
  return ok(await req(`/api/conversations/${cid}/messages`, 'POST', { body, idempotency_key: key }, auth), 201);
}
async function age(cid: string) { await sql('UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ?', new Date(Date.now() - 7200000).toISOString(), cid); }
async function grant(cid: string, owner: string, target = actor, check = 'followup', body = template, on = 'fail') {
  return ok(await req(`/api/conversations/${cid}/automation`, 'PUT', { check, target_actor: target, body, on, expires_at: Date.now() + 600000 }, owner));
}
function check(cid: string, grantId: string, target = actor, name = 'followup', body = template, on = 'fail'): any {
  return { name, kind: 'conversation_backlog', interval_s: 300, params: { actor: target, max_age_s: 3600, conversation_id: cid }, action: { type: 'message', to: target, body, on, conversation_id: cid, grant_id: grantId } };
}
async function config(owner: string, checks: unknown[]) { return ok(await req('/api/checks', 'PUT', { checks }, owner)); }
async function run(owner: string, name = 'followup') { return (await ok(await req(`/api/checks/${name}/run`, 'POST', {}, owner))).results[0]; }
async function page(cid: string, auth: string) { return ok(await req(`/api/conversations/${cid}/messages`, 'GET', undefined, auth)); }
async function evaluate(cid?: string, target = actor) {
  let result: unknown; await inside(h => { result = h.conversationsService.evaluateScheduledBacklog({ actor: target, checkName: 'followup', maxAgeS: 3600, conversationId: cid }); }); return result as { status: string; detail: string };
}
async function hook(change: (port: ConversationCheckPort, hub: Hub, state: DurableObjectState) => ConversationCheckPort) {
  await inside((h, state) => {
    const base = (Reflect.get(h, 'extraCheckDeps') as () => Partial<ActionDeps>).bind(h);
    Reflect.set(h, 'extraCheckDeps', (): Partial<ActionDeps> => { const deps = base(); return { ...deps, conversation: change(deps.conversation!, h, state) }; });
  });
}
async function setup(target = actor) {
  const owner = await cookie(); await identity(owner);
  const token = target.startsWith('human:') ? await cookie(target.slice(6)) : await pair(owner, target);
  if (target.startsWith('human:')) await identity(token);
  const cid = await group(owner, target); await send(cid, owner); await age(cid);
  const g = await grant(cid, owner, target); const definition = check(cid, g.grant_id, target);
  await config(owner, [definition]); return { owner, token, cid, g, definition };
}
beforeEach(async () => {
  await inside(h => { Reflect.deleteProperty(h, 'extraCheckDeps'); });
  for (const table of ['conversation_automation_receipts', 'conversation_automation_grants', 'conversation_messages', 'conversation_members', 'conversations', 'hub_check_slots', 'hub_check_runs', 'hub_checks', 'device_pairings', 'credentials', 'agents', 'machines', 'users']) await sql('DELETE FROM ' + table);
  await sql('UPDATE conversation_storage_usage SET conversation_count=0,message_count=0,message_bytes=0,removed_member_count=0');
});

describe('M5 public Worker authority, retained unread and bridge contract', () => {
  it('preserves target watermark and retained unread; emits a distinct truthful sender and one stable receipt', async () => {
    const { owner, token, cid } = await setup(); const before = await page(cid, token);
    expect(before.my_read_seq).toBe(0); expect((await evaluate(cid)).status).toBe('fail');
    const first = await run(owner); expect(first.action).toMatch(/^conversation_message .*Hub stored; native delivery unconfirmed/);
    const after = await page(cid, token); expect(after.my_read_seq).toBe(before.my_read_seq);
    expect(after.messages).toHaveLength(2); expect(after.messages[0]).toEqual(before.messages[0]);
    expect(after.messages[1]).toMatchObject({ sender_actor: AUTOMATION_SENDER, sender_kind: 'system', kind: 'note' });
    expect((await evaluate(cid)).status).toBe('fail'); expect((await run(owner)).action).toBe(first.action);
    expect((await page(cid, token)).messages).toHaveLength(2);
    console.log('M5_BRIDGE_SNAPSHOT=' + JSON.stringify(after));
  });
  it('does not let an outside admin impersonate a human or acquire a grant', async () => {
    const owner = await cookie(), victim = await cookie('pings-victim'), outsider = await cookie('pings-outsider');
    await identity(owner); await identity(victim); await identity(outsider);
    const cid = await group(owner, 'human:pings-victim'); await send(cid, owner); await age(cid);
    expect((await req('/api/conversations/' + cid, 'GET', undefined, outsider)).status).toBe(403);
    expect((await req(`/api/conversations/${cid}/automation`, 'PUT', { check: 'followup', target_actor: 'human:pings-victim', body: template, on: 'fail', expires_at: Date.now() + 600000 }, outsider)).status).toBe(403);
    await config(outsider, [check(cid, 'ungranted', 'human:pings-victim')]);
    expect((await run(outsider)).status).toBe('error'); expect((await page(cid, victim)).messages).toHaveLength(1);
  });
  it('requires owner session, CSRF and canonical target at grant issuance', async () => {
    const { owner, token, cid } = await setup();
    const body = { check: 'second', target_actor: actor, body: template, on: 'fail', expires_at: Date.now() + 600000 };
    expect((await req(`/api/conversations/${cid}/automation`, 'PUT', body, owner, false)).status).toBe(403);
    expect((await req(`/api/conversations/${cid}/automation`, 'PUT', body, token)).status).toBe(403);
    expect((await req(`/api/conversations/${cid}/automation`, 'PUT', { ...body, target_actor: 'recipient' }, owner)).status).toBe(403);
    expect((await req(`/api/conversations/${cid}/automation`, 'PUT', { ...body, expires_at: Date.now() + 86400000 }, owner)).status).toBe(400);
    await inside(h => expect(() => h.conversationsService.configureAutomation({ caller: { actor: 'human:pings-owner', kind: 'human' }, authType: 'session', csrfVerified: true }, cid, body)).toThrow());
  });
  it('keeps admin, CSRF, ordinary-agent, read-only and sender override protections', async () => {
    const { owner, token, cid, definition } = await setup();
    expect((await req('/api/checks', 'PUT', { checks: [definition] }, token)).status).toBe(403);
    expect((await req('/api/checks', 'PUT', { checks: [definition] }, owner, false)).status).toBe(403);
    expect((await req('/api/checks/followup/run', 'POST', {}, token)).status).toBe(403);
    expect((await req('/api/liveness')).status).toBe(401);
    expect((await req(`/api/conversations/${cid}/messages`, 'POST', { sender_actor: AUTOMATION_SENDER, body: 'spoof', idempotency_key: 'spoof' }, owner)).status).toBe(400);
    await sql("UPDATE credentials SET scopes='[\"bus:read\"]' WHERE kind='agent'");
    expect((await req(`/api/conversations/${cid}/messages`, 'POST', { body: 'forbidden', idempotency_key: 'readonly' }, token)).status).toBe(403);
    for (const target of ['system:conversation-checks', 'root', 'role:root', 'human:fake']) {
      expect((await req('/api/device/code', 'POST', { client_kind: 'agent', name: target, actor: target, requested_scopes: ['bus:read', 'bus:send'] })).status).toBe(400);
    }
  });
  for (const mutation of ['remove', 'archive', 'revoke', 'expire', 'machine', 'registry', 'kind', 'scope', 'owner-disable', 'grant-revoke', 'grant-expire']) {
    for (const timing of ['before', 'after-evaluation']) it(`fails closed for ${mutation} ${timing}`, async () => {
      const { owner, cid, token } = await setup();
      const mutate = (h: Hub) => {
        if (mutation === 'remove') h.sql.exec('UPDATE conversation_members SET removed_at=? WHERE conversation_id=? AND actor=?', new Date().toISOString(), cid, actor);
        if (mutation === 'archive') h.sql.exec('UPDATE conversations SET archived_at=? WHERE id=?', new Date().toISOString(), cid);
        if (mutation === 'revoke') h.sql.exec("UPDATE credentials SET revoked_at=? WHERE kind='agent'", new Date().toISOString());
        if (mutation === 'expire') h.sql.exec("UPDATE credentials SET expires_at=? WHERE kind='agent'", new Date(Date.now() - 1000).toISOString());
        if (mutation === 'machine') h.sql.exec("DELETE FROM machines WHERE id='pings-machine'");
        if (mutation === 'registry') h.sql.exec("DELETE FROM agents WHERE tag='recipient'");
        if (mutation === 'kind') h.sql.exec("UPDATE credentials SET kind='host' WHERE kind='agent'");
        if (mutation === 'scope') h.sql.exec("UPDATE credentials SET scopes='[\"health:report\"]' WHERE kind='agent'");
        if (mutation === 'owner-disable') h.sql.exec("UPDATE users SET is_admin=0 WHERE sub='pings-owner'");
        if (mutation === 'grant-revoke') h.sql.exec('UPDATE conversation_automation_grants SET revoked_at=1');
        if (mutation === 'grant-expire') h.sql.exec('UPDATE conversation_automation_grants SET expires_at=0');
      };
      if (timing === 'before') await inside(mutate);
      else await hook((port, h) => ({ ...port, evaluateBacklog: q => { const result = port.evaluateBacklog(q); expect(result.status).toBe('fail'); mutate(h); return result; } }));
      // Owner disable also prevents the HTTP admin route; run the genuine RPC path
      // for this one case to prove the grant's own effect-time guard independently.
      let result: any;
      if (mutation === 'owner-disable' && timing === 'before') await inside(async h => { result = (await h.runChecks('followup'))[0]; });
      else result = await run(owner);
      expect(result.action ?? '').not.toMatch(/^conversation_message /);
      expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
      expect((await sql('SELECT last_read_seq FROM conversation_members WHERE conversation_id=? AND actor=?', cid, actor))[0].last_read_seq).toBe(0);
    });
  }
  for (const timing of ['before', 'after-evaluation']) it('refuses disabled human target ' + timing, async () => {
    const { owner, cid } = await setup('human:pings-victim');
    if (timing === 'before') await sql("UPDATE users SET is_admin=0 WHERE sub='pings-victim'");
    else await hook((port, h) => ({ ...port, evaluateBacklog: q => { const result = port.evaluateBacklog(q); h.sql.exec("UPDATE users SET is_admin=0 WHERE sub='pings-victim'"); return result; } }));
    expect((await run(owner)).action ?? '').not.toMatch(/^conversation_message /);
    expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
  });
  it('binds grant version and template; replacement/re-add cannot revive a revoked grant', async () => {
    const { owner, cid, definition, g } = await setup();
    await config(owner, [{ ...definition, action: { ...definition.action, body: 'unapproved' } }]);
    expect((await run(owner)).action).toContain('automation_grant_mismatch');
    await ok(await req(`/api/conversations/${cid}/automation`, 'DELETE', { check: 'followup' }, owner));
    const replacement = await grant(cid, owner); expect(replacement.grant_id).not.toBe(g.grant_id);
    await config(owner, [check(cid, replacement.grant_id)]);
    // The failed slot retries its original grant and original template, not replacement authority.
    expect((await run(owner)).action).toContain('automation_grant_mismatch');
    expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
  });
  it('preserves unread rows and counters when original message capacity is full', async () => {
    const { owner, cid, token } = await setup();
    await hook((port, h, state) => ({ ...port, sendFollowUp: intent => {
      const service = new ConversationsService(h.sql, fn => state.storage.transactionSync(fn), { limits: { maxMessagesPerConversation: 1 } });
      return service.postScheduledFollowUp(intent);
    } }));
    const before = await page(cid, token); const usage = (await sql('SELECT * FROM conversation_storage_usage'))[0];
    const result = await run(owner); expect(result.status).toBe('error'); expect(result.action).toContain('conversation_message_limit_exceeded');
    expect(await page(cid, token)).toEqual(before); expect((await sql('SELECT * FROM conversation_storage_usage'))[0]).toEqual(usage);
    expect(await sql('SELECT * FROM conversation_automation_receipts')).toHaveLength(0);
  });
  it('fails closed at retained receipt capacity without pruning original unread history', async () => {
    const { owner, cid, token } = await setup(); const before = await page(cid, token);
    await sql(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<4096)
      INSERT INTO conversation_automation_receipts(effect_key,request_json,message_id,conversation_id,created_at) SELECT 'fixture-'||i,'{}','fixture-'||i,?,0 FROM n`, cid);
    const result = await run(owner); expect(result.status).toBe('error'); expect(result.action).toContain('automation_capacity');
    expect(await page(cid, token)).toEqual(before);
  });
  it('routine follow-up uses its explicit grant once per interval and survives configuration replacement', async () => {
    const { owner, token, cid } = await setup(); const g = await grant(cid, owner, actor, 'routine', template, 'always');
    const definition = { name: 'routine', kind: 'routine', interval_s: 300, params: {}, action: { type: 'message', to: actor, body: template, on: 'always', conversation_id: cid, grant_id: g.grant_id } };
    await config(owner, [definition]); const first = await run(owner, 'routine'); expect(first.status).toBe('pass');
    await config(owner, [{ ...definition, interval_s: 600 }]); expect((await run(owner, 'routine')).action).toBe(first.action);
    expect((await page(cid, token)).my_read_seq).toBe(0);
    await inside(async h => {
      const future = Date.now() + 600001; const checks = new ChecksService(h.sql, () => future);
      const deps: ActionDeps = { liveness: h.livenessService, notify: async () => {}, message: async () => '', launch: async () => '', fetch: async () => new Response(null), evaluateQuota: () => [], conversation: {
        evaluateBacklog: q => h.conversationsService.evaluateScheduledBacklog(q), sendFollowUp: i => h.conversationsService.postScheduledFollowUp(i),
      } };
      const next = await checks.runDue(deps); expect(next).toHaveLength(1); expect(next[0].action).not.toBe(first.action);
      const rows = h.sql.exec('SELECT * FROM conversation_messages WHERE conversation_id=?', cid).toArray(); expect(rows).toHaveLength(3);
      expect(rows[1].idempotency_key).not.toBe(rows[2].idempotency_key);
    });
  });
  it('permanently revokes the old grant when membership is removed and restored', async () => {
    const { owner, cid } = await setup();
    await ok(await req(`/api/conversations/${cid}/members/${actor}`, 'DELETE', undefined, owner));
    await ok(await req(`/api/conversations/${cid}/members`, 'POST', { actor }, owner), 201);
    expect((await run(owner)).status).toBe('error');
    expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
  });
  it('keeps current/read/self-sent/gaps and bus ACK separate from conversation backlog', async () => {
    const { owner, token, cid } = await setup(); expect((await evaluate(cid)).status).toBe('fail');
    await ok(await req(`/api/conversations/${cid}/read`, 'POST', { seq: 1 }, token)); expect((await evaluate(cid)).status).toBe('pass');
    await send(cid, token, 'self'); await age(cid); expect((await evaluate(cid)).status).toBe('pass');
    await send(cid, owner, 'prefix'); await send(cid, owner, 'interior'); await send(cid, owner, 'retained'); await age(cid);
    await sql('DELETE FROM conversation_messages WHERE conversation_id=? AND seq IN (1,4)', cid);
    expect((await evaluate(cid)).status).toBe('fail'); expect((await page(cid, token)).my_read_seq).toBe(2);
    await sql("INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,acked_at) VALUES ('bus','hub',?,'note','bus','bus','bus',?,?,?)", actor, Date.now() - 7200000, Date.now() + 86400000, Date.now());
    expect((await evaluate(cid)).status).toBe('fail');
  });
  it('scans only explicit per-CID grants and fails closed above fifty', async () => {
    const { owner, cid } = await setup(); expect((await evaluate()).status).toBe('fail');
    for (let n = 0; n < 50; n++) { const next = await group(owner); await grant(next, owner); }
    await inside(h => expect(() => h.conversationsService.evaluateScheduledBacklog({ actor, checkName: 'followup', maxAgeS: 3600 })).toThrow());
    expect((await evaluate(cid)).status).toBe('fail');
    const unauthorized = await group(owner); await send(unauthorized, owner); await age(unauthorized);
    await inside(h => expect(() => h.conversationsService.evaluateScheduledBacklog({ actor, checkName: 'followup', maxAgeS: 3600, conversationId: unauthorized })).toThrow());
  }, 20000);
  it('cannot promote bare target after a denied configuration', async () => {
    const { owner, cid, g } = await setup();
    await config(owner, [check(cid, g.grant_id, 'recipient')]);
    expect((await run(owner)).status).toBe('error'); expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
  });
});

describe('M5 durable intent, scheduler reservations and uncertain dispatch', () => {
  it('recovers response loss before reevaluation/detail/config/restart drift, preserving first payload and key', async () => {
    const { owner, token, cid, definition } = await setup();
    await hook(port => ({ ...port, sendFollowUp: async intent => { port.sendFollowUp(intent); throw new Error('injected response loss'); } }));
    const lost = await run(owner); expect(lost.status).toBe('error'); expect(lost.action).toContain('injected response loss');
    const first = (await page(cid, token)).messages.at(-1); expect(first.body).toContain('1 overdue');
    await ok(await req(`/api/conversations/${cid}/read`, 'POST', { seq: 2 }, token));
    await config(owner, [{ ...definition, action: { ...definition.action, body: 'changed configuration' } }]);
    await inside(h => { Reflect.deleteProperty(h, 'extraCheckDeps'); h.checksService = new ChecksService(h.sql); h.checksService.init(); });
    const replay = await run(owner); expect(replay.action).toContain(first.id); expect(replay.status).toBe('fail');
    const after = await page(cid, token); expect(after.messages).toHaveLength(2); expect(after.messages.at(-1)).toEqual(first);
  });
  it('recovers a stored receipt after pruning and grant revocation without creating another effect', async () => {
    const { owner, cid } = await setup();
    await hook(port => ({ ...port, sendFollowUp: async intent => { port.sendFollowUp(intent); throw new Error('injected loss'); } }));
    await run(owner); const receipt = (await sql('SELECT * FROM conversation_automation_receipts'))[0];
    await sql('DELETE FROM conversation_messages WHERE id=?', receipt.message_id);
    await ok(await req(`/api/conversations/${cid}/automation`, 'DELETE', { check: 'followup' }, owner));
    await inside(h => { Reflect.deleteProperty(h, 'extraCheckDeps'); });
    expect((await run(owner)).action).toContain(receipt.message_id);
    expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
  });
  it('claims each later due row conditionally across overlapping snapshots and configuration replacement', async () => {
    const { owner, definition } = await setup();
    await config(owner, [{ name: 'a-first', kind: 'routine', interval_s: 300, action: { type: 'message', to: 'local', body: 'pause' } }, definition]);
    await inside(async h => {
      let release!: () => void, started!: () => void;
      const entered = new Promise<void>(resolve => { started = resolve; });
      const gate = new Promise<void>(resolve => { release = resolve; });
      let evaluations = 0, sends = 0;
      const deps: ActionDeps = {
        liveness: h.livenessService, notify: async () => {}, launch: async () => 'unused', fetch: async () => new Response(null), evaluateQuota: () => [],
        message: async () => { started(); await gate; return 'local'; },
        conversation: { evaluateBacklog: q => { evaluations++; return h.conversationsService.evaluateScheduledBacklog(q); }, sendFollowUp: intent => { sends++; return h.conversationsService.postScheduledFollowUp(intent); } },
      };
      const first = h.checksService.runDue(deps); await entered;
      const second = await h.checksService.runDue(deps); expect(second.map(r => r.name)).toEqual(['followup']);
      h.checksService.replace([validate(definition)]);
      release(); await first;
      expect(evaluations).toBe(1); expect(sends).toBe(1);
      expect(h.sql.exec("SELECT * FROM hub_check_runs WHERE name='followup'").toArray()).toHaveLength(1);
    });
  });
  it('resumes a pre-intent crash after its reservation expires', async () => {
    const { cid } = await setup();
    await inside(async h => {
      const clock = { t: Date.now() }; const checks = new ChecksService(h.sql, () => clock.t);
      let calls = 0;
      const deps: ActionDeps = { liveness: h.livenessService, notify: async () => {}, message: async () => '', launch: async () => '', evaluateQuota: () => [],
        fetch: async () => new Response(null), conversation: { evaluateBacklog: q => h.conversationsService.evaluateScheduledBacklog(q), sendFollowUp: i => { calls++; return h.conversationsService.postScheduledFollowUp(i); } } };
      // A durable evaluating reservation with no intent models a process dying
      // before evaluation finished; no external effect could yet have been issued.
      const definition = checks.list()[0];
      const n = Math.floor(clock.t / 300000);
      h.sql.exec("INSERT INTO hub_check_slots VALUES ('followup', ?, ?, ?, NULL, NULL, NULL, 'evaluating', 'dead-runner', ?)", `followup-${n}`, (n+1)*300000, JSON.stringify({ ...definition }), clock.t + 30000);
      expect(await checks.runDue(deps)).toEqual([]); clock.t += 30001;
      const restarted = new ChecksService(h.sql, () => clock.t); const result = await restarted.runDue(deps);
      expect(result).toHaveLength(1); expect(calls).toBe(1);
      expect(await restarted.runDue(deps)).toEqual([]);
      expect(h.sql.exec('SELECT * FROM conversation_messages WHERE conversation_id=?', cid).toArray()).toHaveLength(2);
    });
  });
  it('fences an evaluator whose lease expires while a replacement runner owns the slot', async () => {
    const { owner, cid } = await setup(); const g = await grant(cid, owner, actor, 'followup', template, 'always');
    await config(owner, [{ name: 'followup', kind: 'http', interval_s: 300, params: { url: 'https://local.invalid/' }, action: { type: 'message', to: actor, body: template, on: 'always', conversation_id: cid, grant_id: g.grant_id } }]);
    await inside(async h => {
      const clock = { t: Date.now() }, checks = new ChecksService(h.sql, () => clock.t);
      let enter!: () => void, release!: () => void, evaluations = 0, sends = 0;
      const entered = new Promise<void>(r => { enter = r; }), gate = new Promise<void>(r => { release = r; });
      const deps: ActionDeps = { liveness: h.livenessService, notify: async () => {}, message: async () => '', launch: async () => '', evaluateQuota: () => [],
        fetch: async () => { if (++evaluations === 1) { enter(); await gate; } return new Response(null, { status: 200 }); },
        conversation: { evaluateBacklog: q => h.conversationsService.evaluateScheduledBacklog(q), sendFollowUp: i => { sends++; return h.conversationsService.postScheduledFollowUp(i); } } };
      const expired = checks.runDue(deps); await entered; clock.t += 30001;
      expect(await new ChecksService(h.sql, () => clock.t).runDue(deps)).toHaveLength(1);
      release(); expect(await expired).toEqual([]); expect(sends).toBe(1);
      expect(h.sql.exec('SELECT * FROM conversation_messages WHERE conversation_id=?', cid).toArray()).toHaveLength(2);
    });
  });
  it('holds unresolved slots across interval changes instead of issuing a new grant/body', async () => {
    const { owner, cid, definition } = await setup();
    await hook(port => ({ ...port, sendFollowUp: async () => { throw new Error('dispatch unknown'); } }));
    expect((await run(owner)).status).toBe('error'); const first = (await sql('SELECT intent_json FROM hub_check_slots'))[0].intent_json;
    await config(owner, [{ ...definition, interval_s: 600, action: { ...definition.action, body: 'changed' } }]);
    expect((await run(owner)).status).toBe('error'); expect((await sql('SELECT intent_json FROM hub_check_slots'))[0].intent_json).toBe(first);
    expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
  });
  for (const failure of ['missing', 'invalid', 'throwing']) it('fails closed for ' + failure + ' conversation dependency', async () => {
    const { owner, cid } = await setup();
    await inside(h => {
      if (failure === 'missing') Reflect.set(h, 'extraCheckDeps', () => ({}));
    });
    if (failure === 'throwing') await hook(port => ({ ...port, evaluateBacklog: () => { throw new Error('port failed'); } }));
    if (failure === 'invalid') await hook(port => ({ ...port, evaluateBacklog: () => JSON.parse('{"status":"unknown"}') }));
    const result = await run(owner); expect(result.status).toBe('error'); expect(result.action).toBeNull();
    expect((await sql('SELECT * FROM conversation_messages WHERE conversation_id=?', cid))).toHaveLength(1);
  });
});
function validate(c: unknown) { const result = validateCheck(c); if ('error' in result) throw new Error(result.error); return result.row; }
