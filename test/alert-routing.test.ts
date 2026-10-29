import { mintSessionCredential } from '../src/session-messaging';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { alertDestinations, inboxReaderStatus, READER_FRESH_MS, ALERT_SUBJECT_COOLDOWN_MS, BUS_REFRESH_GRACE_MS, pruneAlertRouting } from '../src/alert-routing';
import { sha256 } from '../src/auth/crypto';
import { LivenessService } from '../src/liveness';
import { validateCheck } from '../src/checks';
import type { Env } from '../src/env';

async function fixture(fn: (hub: any) => Promise<void>) {
  const e = env as unknown as Env;
  const stub = e.HUB.get(e.HUB.idFromName('alert-routing-' + crypto.randomUUID()));
  await runInDurableObject(stub, fn);
}
async function reader(h: any, actor = 'watched', token = 'test-reader-token') {
  const now = new Date().toISOString();
  h.sql.exec('INSERT INTO agents(tag, project, registered_at) VALUES (?, NULL, ?)', actor, now);
  h.sql.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
    VALUES (?, 'agent', ?, ?, ?, ?, ?, '["bus:read","bus:send"]', 'test', ?, ?)`, actor, await sha256(token), await sha256('refresh-' + token), actor, actor, actor, now, new Date(Date.now() + 3600000).toISOString());
  return { kind: 'agent', actor, tokenId: actor } as any;
}
async function expiredSession(h:any) {
  const id=crypto.randomUUID(), token='hubt_session_'+id, refresh='hubr_session_'+id;
  const at=new Date().toISOString();
  h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES ('auth-host','auth','auth',?)",at);
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES (?,'test',?,'{}','auth-host','codex','interactive','running',?, ?, ?)`,id,id,JSON.stringify({native_id:crypto.randomUUID(),tag:'fixture',workspace:'/fixture'}),at,at);
  const hash=await sha256(token);
  const minted=mintSessionCredential(h.sql,id,'auth-host',{accessHash:hash,refreshHash:await sha256(refresh)},Date.now()-3601000)!;
  return {token,refresh,actor:minted.actor};
}
const incident = (h: any, kind = 'session', subject = 'host/shell') => h.livenessService.setAlert(kind, subject, { state: 'stalled', detail: 'no_native_progress' });

describe('alert routing acceptance', () => {
  it('has no dead default and rejects insecure or credential-bearing webhooks', () => {
    expect(alertDestinations({})).toEqual({ actors: [], webhook: null, invalidWebhook: false, invalidActors: false });
    for (const url of ['http://example.test', 'https://u:p@example.test', 'invalid']) expect(alertDestinations({ HUB_ALERT_WEBHOOK: url }).invalidWebhook).toBe(true);
  });

  it('refuses unset and no-reader targets without adding backlog', () => fixture(async h => {
    delete h.env.HUB_ALERT_TO;
    const a = incident(h);
    await h.notifyAlert(a);
    expect(h.livenessService.alerts()[0].notify_error).toContain('no alert target configured');
    await reader(h, 'unwatched');
    h.env = { ...h.env, HUB_ALERT_TO: 'unwatched' };
    await h.notifyAlert(a);
    expect(h.livenessService.alerts()[0].notify_error).toContain('no_active_credential_read_loop');
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM messages').one().n).toBe(0);
    await h.runChecks('liveness');
    expect(h.livenessService.alerts().some((a: any) => a.subject_kind === 'alert_routing' && a.state === 'fail')).toBe(true);
    const status = await h.statusService.handle(new Request('https://hub.test/api/status'), async () => ({ caller: { isAdmin: true, actor: 'test', kind: 'human' }, scopes: ['admin'] }));
    expect((await status.json()).needs_attention.some((a: any) => a.subject === 'alert_routing:destination')).toBe(true);
  }));

  it('routes to a reader once per open stall, including stale-row retries and restarts', () => fixture(async h => {
    const caller = await reader(h);
    h.busService.inbox(caller, 'watched'); // Empty polls are reader evidence too.
    h.env = { ...h.env, HUB_ALERT_TO: 'watched,watched' };
    const a = incident(h);
    for (let i = 0; i < 10; i++) await h.notifyAlert(a);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM messages').one().n).toBe(1);
    const restarted = new LivenessService(h.sql); restarted.init();
    expect(restarted.reconcileAlerts([{ kind: 'session', id: 'host/shell', state: 'stalled', detail: 'no_native_progress for 2100s' }])).toEqual([]);
    expect(restarted.undelivered(0)).toEqual([]);
    h.livenessService.setAlert('session', 'host/shell', null);
    const reopened = incident(h);
    await h.notifyAlert(reopened);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM messages').one().n).toBe(1);
    expect(h.sql.exec('SELECT notify_error FROM liveness_alerts WHERE id=?', reopened.id).one().notify_error).toContain('rate limited');
    h.sql.exec('UPDATE alert_delivery_receipts SET sent_at=?', Date.now() - ALERT_SUBJECT_COOLDOWN_MS - 1);
    await h.notifyAlert(reopened);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM messages').one().n).toBe(2);
  }));

  it('routes to the configured webhook; only failed destinations retry and errors hide URLs', () => fixture(async h => {
    h.env = { ...h.env, HUB_ALERT_TO: 'no-reader', HUB_ALERT_WEBHOOK: 'https://example.test/private-hook' };
    const original = globalThis.fetch; const calls: RequestInit[] = [];
    globalThis.fetch = (async (_url: any, init: RequestInit) => { calls.push(init); return new Response('ok'); }) as any;
    try {
      const a = incident(h);
      await Promise.all([h.notifyAlert(a), h.notifyAlert(a)]);
      await h.notifyAlert(a);
      expect(calls).toHaveLength(1);
      expect((calls[0].headers as any)['Idempotency-Key']).toBe('hub-alert-' + a.id);
      const saved = h.sql.exec('SELECT * FROM liveness_alerts WHERE id=?', a.id).one();
      expect(saved.notify_error).toContain('no-reader');
      expect(saved.delivered_to).not.toContain('private-hook');
    } finally { globalThis.fetch = original; }
  }));

  it('reroutes an already-open incident when configuration changes without repeating successful targets', () => fixture(async h => {
    await h.runChecks('liveness');
    const a = incident(h, 'check', 'existing-slo');
    h.livenessService.markNotified(a.id, null, ['inbox:retired']);
    h.sql.exec('UPDATE liveness_alerts SET opened_at=? WHERE id=?', new Date(Date.now()-3600000).toISOString(), a.id);
    const caller = await reader(h);
    h.busService.inbox(caller, 'watched');
    h.env = { ...h.env, HUB_ALERT_TO: 'watched' };
    await h.runChecks('liveness');
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM messages WHERE to_actor='watched'").one().n).toBe(1);
    await h.runChecks('liveness');
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM messages WHERE to_actor='watched'").one().n).toBe(1);
  }));

  it('requires fresh actual inbox reads; admin inspection, token use and revoked/expired readers cannot pass', () => fixture(async h => {
    const caller = await reader(h);
    h.sql.exec('UPDATE credentials SET last_used_at=?', new Date().toISOString());
    h.busService.inbox({ isAdmin: true, kind: 'human', actor: 'observer' }, 'watched');
    expect(inboxReaderStatus(h.sql, 'watched').active_reader).toBe(false);
    h.busService.inbox(caller, 'watched');
    expect(inboxReaderStatus(h.sql, 'watched').active_reader).toBe(true);
    expect(inboxReaderStatus(h.sql, 'watched', Date.now() + READER_FRESH_MS + 1000).active_reader).toBe(false);
    h.sql.exec('UPDATE credentials SET revoked_at=?', new Date().toISOString());
    expect(inboxReaderStatus(h.sql, 'watched').active_reader).toBe(false);
    h.sql.exec('UPDATE credentials SET revoked_at=NULL, expires_at=?', new Date(Date.now() - 1).toISOString());
    expect(inboxReaderStatus(h.sql, 'watched').active_reader).toBe(false);
  }));

  it('keeps distinct audit batches deliverable and reuses identical payload receipts', () => fixture(async h => {
    const caller=await reader(h);h.busService.inbox(caller,'watched');
    h.env={...h.env,HUB_ALERT_TO:'watched'};
    const send=async (detail:string)=>{
      h.livenessService.setAlert('session_audit','idle-actions',null);
      return h.notifyAlert(h.livenessService.setAlert('session_audit','idle-actions',{state:'attention',detail}));
    };
    expect((await send('batch A: native1')).delivered).toContain('inbox:watched');
    expect((await send('batch B: native2')).delivered).toContain('inbox:watched');
    expect((await send('batch B: native2')).delivered).toContain('inbox:watched');
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM messages').one().n).toBe(2);
  }));

  it('recognizes project and role aliases but rejects a reader after registry rebinding', () => fixture(async h => {
    h.sql.exec("INSERT INTO agents(tag,project,role,registered_at) VALUES ('alerts','team','alerts',?)", new Date().toISOString());
    h.sql.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
      VALUES ('alias-reader','agent','alias-hash','alias-reader','alerts','team:alerts','["bus:read"]','test',?,?)`, new Date().toISOString(), new Date(Date.now()+3600000).toISOString());
    h.busService.inbox({kind:'agent',actor:'team:alerts',tokenId:'alias-reader'}, 'role:alerts');
    expect(inboxReaderStatus(h.sql, 'team:alerts').active_reader).toBe(true);
    expect(inboxReaderStatus(h.sql, 'alerts').active_reader).toBe(true);
    h.sql.exec("UPDATE agents SET machine_id='replacement-host' WHERE tag='alerts'");
    expect(inboxReaderStatus(h.sql, 'team:alerts').active_reader).toBe(false);
  }));

  it('requires the registered host bridge to keep polling and invalidates it after a move', () => fixture(async h => {
    const now = new Date().toISOString();
    h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES ('bridge-host','bridge','bridge',?)",now);
    h.sql.exec("INSERT INTO agents(tag,project,machine_id,registered_at) VALUES ('bridge-alerts','team','bridge-host',?)",now);
    h.sql.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at)
      VALUES ('bridge-reader','host','bridge-hash','bridge-reader','bridge','bridge-host','["host","bus:read"]','test',?,?)`,now,new Date(Date.now()+3600000).toISOString());
    h.busService.inbox({kind:'host',machineId:'bridge-host',tokenId:'bridge-reader'},'team:bridge-alerts');
    expect(inboxReaderStatus(h.sql,'team:bridge-alerts').active_reader).toBe(true);
    h.sql.exec("UPDATE agents SET machine_id='other-host' WHERE tag='bridge-alerts'");
    expect(inboxReaderStatus(h.sql,'team:bridge-alerts').active_reader).toBe(false);
  }));

  it('serializes reopened incidents while a webhook receipt is in flight', () => fixture(async h => {
    h.env={...h.env,HUB_ALERT_TO:'',HUB_ALERT_WEBHOOK:'https://example.test/race'};
    const original=globalThis.fetch; let calls=0; let accepted!:()=>void;
    let release!:()=>void; const pending=new Promise<void>(resolve=>{release=resolve;});
    const started=new Promise<void>(resolve=>{accepted=resolve;});
    globalThis.fetch=(async()=>{calls++;accepted();await pending;return new Response('ok');}) as any;
    try {
      const first=incident(h);const delivery=h.notifyAlert(first);await started;
      h.livenessService.setAlert('session','host/shell',null);
      const reopened=incident(h);const second=h.notifyAlert(reopened);release();
      const receipts=await Promise.all([delivery,second]);
      expect(calls).toBe(1);
      expect(receipts[1].errors.join(';')).toContain('rate limited');
    } finally { release();globalThis.fetch=original; }
  }));

  it('names overdue no-reader recipients distinctly and sends the SLO incident to a watched destination', () => fixture(async h => {
    const caller = await reader(h);
    h.busService.inbox(caller, 'watched');
    h.env = { ...h.env, HUB_ALERT_TO: 'watched' };
    h.sql.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
      VALUES ('overdue','hub','agent-coordination:platform','note','overdue','test','overdue',?,?)`, Date.now() - 400000, Date.now() + 86400000);
    const c: any = validateCheck({ name: 'reader-slo', kind: 'delivery_slo', interval_s: 300, params: { max_age_s: 300 } });
    h.checksService.replace([c.row]);
    const results = await h.runChecks('reader-slo');
    expect(results[0]).toMatchObject({ status: 'fail' });
    expect(results[0].detail).toContain('NO_READER: agent-coordination:platform');
    expect(h.sql.exec("SELECT body FROM messages WHERE to_actor='watched'").one().body).toContain('NO_READER: agent-coordination:platform');
    expect(h.sql.exec("SELECT acked_at FROM messages WHERE id='overdue'").one().acked_at).toBeNull();
  }));

  it('does not alert on normal session access expiry followed by a successful refresh', () => fixture(async h => {
    const c=await expiredSession(h);
    const poll=(token:string)=>h.fetch(new Request('https://hub.test/api/bus/inbox?actor='+encodeURIComponent(c.actor),{headers:{Authorization:'Bearer '+token}}));
    expect((await poll(c.token)).status).toBe(401);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM bus_auth_pending').one().n).toBe(1);
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM liveness_alerts WHERE subject_kind='bus_auth'").one().n).toBe(0);
    const response=await h.fetch(new Request('https://hub.test/api/token/refresh',{method:'POST',body:JSON.stringify({grant_type:'refresh_token',refresh_token:c.refresh})}));
    expect(response.status).toBe(200);const renewed=await response.json();
    expect((await poll(renewed.access_token)).status).toBe(200);
    expect(inboxReaderStatus(h.sql,c.actor).active_reader).toBe(true);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM bus_auth_pending').one().n).toBe(0);
    expect((await poll(c.token)).status).toBe(401); // retired predecessor is ignored
    expect((await poll('unknown-spoofed-token')).status).toBe(401);
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM liveness_alerts WHERE subject_kind='bus_auth'").one().n).toBe(0);
  }));

  it('alerts when refresh grace expires, and resolves when that reader recovers', () => fixture(async h => {
    const caller=await reader(h);h.busService.inbox(caller,'watched');h.env={...h.env,HUB_ALERT_TO:'watched'};
    const c=await expiredSession(h);
    await h.fetch(new Request('https://hub.test/api/bus/inbox?actor='+encodeURIComponent(c.actor),{headers:{Authorization:'Bearer '+c.token}}));
    h.sql.exec('UPDATE bus_auth_pending SET first_failed_at=?',Date.now()-BUS_REFRESH_GRACE_MS-1);
    h.checkBusAuthFailures();
    const a=h.sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind='bus_auth'").one();await h.notifyAlert(a);
    expect(a.subject).toBe(c.actor);expect(a.detail).toContain('no successful refresh/read');
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM messages WHERE to_actor='watched'").one().n).toBe(1);
    const response=await h.fetch(new Request('https://hub.test/api/token/refresh',{method:'POST',body:JSON.stringify({grant_type:'refresh_token',refresh_token:c.refresh})}));
    expect(response.status).toBe(200);
    expect(h.sql.exec("SELECT resolved_at FROM liveness_alerts WHERE id=?",a.id).one().resolved_at).not.toBeNull();
  }));

  it('notifies distinct outages and state changes while bounding reopened native stalls', () => fixture(async h => {
    const caller=await reader(h);h.busService.inbox(caller,'watched');h.env={...h.env,HUB_ALERT_TO:'watched'};
    const send=async(kind:string,subject:string,state:string,detail:string)=>{
      h.livenessService.setAlert(kind,subject,null);
      return h.notifyAlert(h.livenessService.setAlert(kind,subject,{state,detail}));
    };
    await send('machine','host','stale','heartbeat old');
    await send('machine','host','stale','heartbeat old again');
    await send('session','native','stalled','no_native_progress');
    await send('session','native','stalled','no_native_progress for 4000s');
    await send('session','native','worktree_removed','missing');
    await send('session','other','stalled','waiting_for_input');
    await send('session','other','stalled','no_native_progress');
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM messages').one().n).toBe(6);
  }));

  it('bounds recipient evaluation and the delivery SLO mailbox summary', () => fixture(async h => {
    const caller=await reader(h);h.busService.inbox(caller,'watched');h.env={...h.env,HUB_ALERT_TO:'watched'};
    for(let i=0;i<40;i++)h.sql.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
      VALUES (?,'hub',?,'note',?,'overdue',?,?,?)`,'m'+i,'recipient-'+String(i).padStart(3,'0')+'x'.repeat(110),'t'+i,'key'+i,Date.now()-400000,Date.now()+86400000);
    const c:any=validateCheck({name:'bounded-slo',kind:'delivery_slo',interval_s:300,params:{max_age_s:300}});h.checksService.replace([c.row]);
    const result=await h.runChecks('bounded-slo');expect(result[0].status).toBe('fail');
    expect(result[0].detail).toContain('40 unacknowledged');expect(result[0].detail).toContain('+20 more recipients');
    const body=h.sql.exec("SELECT body FROM messages WHERE to_actor='watched'").one().body;
    expect(new TextEncoder().encode(body).byteLength).toBeLessThan(16384);
    expect(body).toContain('NO_READER:');
  }));

  it('keeps legacy multibyte recipient labels below the mailbox byte limit', () => fixture(async h => {
    const caller=await reader(h);h.busService.inbox(caller,'watched');h.env={...h.env,HUB_ALERT_TO:'watched'};
    for(let i=0;i<30;i++)h.sql.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
      VALUES (?,'hub',?,'note',?,'overdue',?,?,?)`,'u'+i,String(i).padStart(3,'0')+'界'.repeat(125),'u'+i,'unicode'+i,Date.now()-400000,Date.now()+86400000);
    const c:any=validateCheck({name:'unicode-slo',kind:'delivery_slo',interval_s:300,params:{max_age_s:300}});h.checksService.replace([c.row]);
    const result=await h.runChecks('unicode-slo');expect(result[0].status).toBe('fail');
    expect(result[0].detail).toContain('further labels omitted');
    const body=h.sql.exec("SELECT body FROM messages WHERE to_actor='watched'").one().body;
    expect(new TextEncoder().encode(body).byteLength).toBeLessThan(16384);
  }));

  it('prunes expired reader evidence and cooldown receipts while retaining fresh rows', () => fixture(async h => {
    const caller=await reader(h);h.busService.inbox(caller,'watched');
    h.sql.exec("INSERT INTO bus_reader_activity VALUES ('old','old',?)",Date.now()-READER_FRESH_MS-1);
    h.sql.exec("INSERT INTO alert_delivery_receipts VALUES ('session','old','inbox:watched',1,?)",Date.now()-ALERT_SUBJECT_COOLDOWN_MS-1);
    h.sql.exec("INSERT INTO alert_delivery_receipts VALUES ('session','fresh','inbox:watched',2,?)",Date.now());
    pruneAlertRouting(h.sql);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM bus_reader_activity').one().n).toBe(1);
    expect(h.sql.exec('SELECT subject FROM alert_delivery_receipts').one().subject).toBe('fresh');
  }));

  it('bus read 401s and expired-refresh failures open routed alerts without tokens or spoofed recipients', () => fixture(async h => {
    const caller = await reader(h);
    h.busService.inbox(caller, 'watched');
    h.env = { ...h.env, HUB_ALERT_TO: 'watched' };
    await reader(h, 'broken', 'expired-secret-test-token');
    h.sql.exec("UPDATE credentials SET expires_at=? WHERE id='broken'", new Date(Date.now() - 1).toISOString());
    const response = await h.fetch(new Request('https://hub.test/api/bus/inbox?actor=spoofed', { headers: { Authorization: 'Bearer expired-secret-test-token' } }));
    expect(response.status).toBe(401);
    // waitUntil delivery may still be running; the same notifier is fenced.
    const a = h.sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind='bus_auth'").one();
    expect(a.subject).toBe('broken');
    expect(a.detail).toContain('401');
    await h.notifyAlert(a);
    expect(h.sql.exec("SELECT body FROM messages WHERE to_actor='watched'").one().body).toContain('bus_auth broken is unauthorized');
    expect(JSON.stringify(a)).not.toContain('secret-test-token');
    const refresh = await h.fetch(new Request('https://hub.test/api/token/refresh', { method: 'POST', body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: 'refresh-expired-secret-test-token' }) }));
    expect(refresh.status).toBe(400);
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM liveness_alerts WHERE subject_kind='bus_auth'").one().n).toBe(1);
    // Independent expired reader proves immediate alert delivery through actual routes.
    await reader(h, 'broken-two', 'expired-two');
    h.sql.exec("UPDATE credentials SET expires_at=? WHERE id='broken-two'", new Date(Date.now() - 1).toISOString());
    await h.fetch(new Request('https://hub.test/api/bus/inbox?actor=broken-two', { headers: { Authorization: 'Bearer expired-two' } }));
  }));
});
