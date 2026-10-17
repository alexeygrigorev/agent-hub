import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// M12e R3 host-authority prerequisite (ported from frozen metrics f20,
// session/registry surfaces only — no M9 metrics imports). All cases drive
// ACTUAL hub routes (device/code, device/approve, device/token,
// machines/register, token/refresh) plus real SQLite assertions and the
// session-control offer/binding surfaces for compatibility. No standalone SQL
// updates as auth substitutes; no metrics tables.

const adminAuth = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const NATIVE_ID = '11111111-1111-4111-8111-111111111111';
const NATIVE_WS = '/srv/hub/demo';
const NATIVE_CREATED = 1792223000000;
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';

function hubRequest(path: string, data?: any, method = 'POST', headers?: Record<string, string>) {
  return new Request('https://hub.test' + path, {
    method,
    headers: { 'Content-Type': 'application/json', ...(headers || {}) },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
}

function fakeReserve() {
  let seq = 0;
  return (_input: any) => ({
    value: { id: 'launch-' + ++seq, state: 'offered', machine: 'hetzner', engine: 'codex' },
    status: 201,
  });
}

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', state = 'running') {
  const at = new Date().toISOString();
  sql.exec(
    `INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
     VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, state, null, at, at,
  );
}

function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string) {
  const at = new Date().toISOString();
  sql.exec(
    `INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at)
     VALUES (?, ?, ?, 'codex', 'running', NULL, ?, ?, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, at, workspace,
  );
}

async function adminCookieHeader(hubEnv: Env) {
  const raw = await createSessionCookie(hubEnv, {
    sub: 'synthetic-admin', email: 'admin@fake.local', isAdmin: true,
    csrfToken: 'synthetic', exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return { Cookie: raw.split(';')[0], 'x-csrf-token': 'synthetic' };
}

async function issueHostPairing(
  hub: Hub, headers: Record<string, string>, name: string, machineId?: string,
) {
  const codeRes = await hub.fetch(
    hubRequest('/api/device/code', { client_kind: 'host', name, hostname: 'synthetic.invalid', requested_scopes: ['host'] }),
  );
  expect(codeRes.status).toBe(200);
  const codes: any = await codeRes.json();
  const approveRes = await hub.fetch(
    hubRequest('/api/device/approve/' + codes.user_code,
      { granted_scopes: ['host'], ...(machineId ? { machine_id: machineId } : {}) }, 'POST', headers),
  );
  expect(approveRes.status).toBe(200);
  return codes;
}

async function exchangeCode(hub: Hub, deviceCode: string) {
  return hub.fetch(
    hubRequest('/api/device/token', {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: deviceCode,
    }),
  );
}

async function freshHub(tag: string) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('host-auth-r6-' + tag + '-' + crypto.randomUUID()));
  return { bindings, stub };
}

const registerBody = {
  hostname: 'synthetic.invalid', os: 'linux', network: 'public',
  roles: ['worker'], capabilities: { launcher: '0.1.0' },
};

describe('host-enrollment authority prerequisite (R3, session/registry surfaces)', () => {
  it('H1 streamed register refuses a credential revoked during the body before mutation', async () => {
    const { stub } = await freshHub('revoke-stream');
    await runInDurableObject(stub, async (hub: Hub) => {
      const at = new Date().toISOString();
      const far = new Date(Date.now() + 30 * 86400000).toISOString();
      const token = 'hubt_stream_host_token_1111111111111111';
      const hash = await sha256(token);
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('stream-cred', 'host', ?, 'stream-ref', 'stream-family', 'hetzner', 'hetzner', NULL, '["host"]', 'test', ?, ?)`,
        hash, at, far,
      );
      hub.sql.exec(
        `INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'old-host', NULL, ?)`, at,
      );
      const payload = new TextEncoder().encode(JSON.stringify(registerBody));
      let step = 0;
      const stream = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (step === 0) { step = 1; controller.enqueue(payload.slice(0, 8)); return; }
          hub.sql.exec('UPDATE credentials SET revoked_at = ? WHERE id = ?', new Date().toISOString(), 'stream-cred');
          controller.enqueue(payload.slice(8));
          controller.close();
        },
      });
      const res = await hub.fetch(
        new Request('https://hub.test/api/machines/register', {
          method: 'POST',
          headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
          body: stream,
        }),
      );
      expect(res.status).toBe(401);
      const machine: any = hub.sql.exec('SELECT hostname, credential_id FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      expect(machine.hostname).toBe('old-host');
      expect(machine.credential_id).toBeNull();
    });
  });

  it('H2 superseded family re-register is denied and cannot hijack the bound session', async () => {
    const { stub } = await freshHub('supersede');
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fakeReserve() });
      s.init();
      const at = new Date().toISOString();
      const far = new Date(Date.now() + 30 * 86400000).toISOString();
      const oldToken = 'hubt_old_host_token_1111111111111111';
      const newToken = 'hubt_new_host_token_2222222222222222';
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('host-cred-1', 'host', ?, 'ref-1', 'host-family-1', 'hetzner', 'hetzner', NULL, '["host"]', 'test', ?, ?),
                ('host-cred-new', 'host', ?, 'ref-new', 'host-family-2', 'hetzner', 'hetzner', NULL, '["host"]', 'test', ?, ?)`,
        await sha256(oldToken), at, far, await sha256(newToken), at, far,
      );
      hub.sql.exec(
        `INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-new', ?)`, at,
      );
      const created: any = await (await s.handle(hubRequest('/api/sessions', { key: 'h2-k1', workspace: 'demo', machine: 'hetzner' }), adminAuth)).json();
      const tag = 'hub-' + created.fleet_launch_id;
      ensureFleetLaunch(hub.sql, created.fleet_launch_id);
      reportFleetSession(hub.sql, 'hetzner', NATIVE_ID, tag, NATIVE_WS);
      const hijack = await hub.fetch(
        hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + oldToken }),
      );
      expect(hijack.status).toBe(403);
      const machine: any = hub.sql.exec('SELECT credential_id FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      expect(machine.credential_id).toBe('host-cred-new');
      const bindOld = await s.handle(
        hubRequest(`/api/sessions/${created.id}/binding`, { native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }),
        { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner', tokenId: 'host-cred-1' }, scopes: ['host'] },
      );
      expect(bindOld.status).toBe(409);
    });
  });

  it('H3 delayed older approved pairing exchange is denied without consuming; newer family keeps its session', async () => {
    const { stub, bindings } = await freshHub('delayed-exchange');
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fakeReserve() });
      s.init();
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codeOld = await issueHostPairing(hub, headers, 'pair-old', 'hetzner');
      const codeNew = await issueHostPairing(hub, headers, 'pair-new', 'hetzner');
      const exB = await exchangeCode(hub, codeNew.device_code);
      expect(exB.status).toBe(200);
      const tokB: any = await exB.json();
      const credB: any = hub.sql.exec('SELECT id FROM credentials WHERE token_hash = ?', await sha256(tokB.access_token)).toArray()[0];
      const created: any = await (await s.handle(hubRequest('/api/sessions', { key: 'h3-k1', workspace: 'demo', machine: 'hetzner' }), adminAuth)).json();
      const tag = 'hub-' + created.fleet_launch_id;
      ensureFleetLaunch(hub.sql, created.fleet_launch_id);
      reportFleetSession(hub.sql, 'hetzner', NATIVE_ID, tag, NATIVE_WS);
      const bindB = await s.handle(
        hubRequest(`/api/sessions/${created.id}/binding`, { native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }),
        { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner', tokenId: credB.id }, scopes: ['host'] },
      );
      expect(bindB.status).toBe(200);
      await s.handle(hubRequest(`/api/sessions/${created.id}/send`, { key: 'h3-s1', body: 'go' }), adminAuth);
      // Delayed older pairing exchange: denied, code stays approved (retryable), machine binding untouched.
      const exA = await exchangeCode(hub, codeOld.device_code);
      expect(exA.status).toBe(400);
      const pairingA: any = hub.sql.exec('SELECT status FROM device_pairings WHERE user_code = ?', codeOld.user_code).toArray()[0];
      expect(pairingA.status).toBe('approved');
      const machine: any = hub.sql.exec('SELECT credential_id FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      expect(machine.credential_id).toBe(credB.id);
      const offersB: any = await (await s.handle(hubRequest('/api/session-commands/offers', undefined, 'GET'),
        { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner', tokenId: credB.id }, scopes: ['host'] })).json();
      expect(offersB.offers.length).toBe(1);
    });
  });

  it('H4 pending approved C leaves B register/refresh/offers operational', async () => {
    const { stub, bindings } = await freshHub('pending-c');
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fakeReserve() });
      s.init();
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codesB = await issueHostPairing(hub, headers, 'pair-b', 'hetzner');
      const tokB: any = await (await exchangeCode(hub, codesB.device_code)).json();
      const regB = await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokB.access_token }));
      expect(regB.status).toBe(200);
      // C approved but never exchanged.
      await issueHostPairing(hub, headers, 'pair-c', 'hetzner');
      const regBAgain = await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokB.access_token }));
      expect(regBAgain.status).toBe(200);
      const refB = await hub.fetch(hubRequest('/api/token/refresh', { grant_type: 'refresh_token', refresh_token: tokB.refresh_token }));
      expect(refB.status).toBe(200);
      const tokB2: any = await refB.json();
      const credB2: any = hub.sql.exec('SELECT id FROM credentials WHERE token_hash = ?', await sha256(tokB2.access_token)).toArray()[0];
      const created: any = await (await s.handle(hubRequest('/api/sessions', { key: 'h4-k1', workspace: 'demo', machine: 'hetzner' }), adminAuth)).json();
      const tag = 'hub-' + created.fleet_launch_id;
      ensureFleetLaunch(hub.sql, created.fleet_launch_id);
      reportFleetSession(hub.sql, 'hetzner', NATIVE_ID, tag, NATIVE_WS);
      const bindB = await s.handle(
        hubRequest(`/api/sessions/${created.id}/binding`, { native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }),
        { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner', tokenId: credB2.id }, scopes: ['host'] },
      );
      expect(bindB.status).toBe(200);
      await s.handle(hubRequest(`/api/sessions/${created.id}/send`, { key: 'h4-s1', body: 'go' }), adminAuth);
      const offers: any = await (await s.handle(hubRequest('/api/session-commands/offers', undefined, 'GET'),
        { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner', tokenId: credB2.id }, scopes: ['host'] })).json();
      expect(offers.offers.length).toBe(1);
    });
  });

  it('H5 after C exchange, old B is denied and C owns the machine', async () => {
    const { stub, bindings } = await freshHub('takeover');
    await runInDurableObject(stub, async (hub: Hub) => {
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codesB = await issueHostPairing(hub, headers, 'pair-b', 'hetzner');
      const tokB: any = await (await exchangeCode(hub, codesB.device_code)).json();
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokB.access_token }))).status).toBe(200);
      const codesC = await issueHostPairing(hub, headers, 'pair-c', 'hetzner');
      const tokC: any = await (await exchangeCode(hub, codesC.device_code)).json();
      const machine: any = hub.sql.exec('SELECT credential_id, approval_seq FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      const credC: any = hub.sql.exec('SELECT id, approval_seq FROM credentials WHERE token_hash = ?', await sha256(tokC.access_token)).toArray()[0];
      expect(machine.credential_id).toBe(credC.id);
      expect(machine.approval_seq).toBe(credC.approval_seq);
      const regB = await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokB.access_token }));
      expect(regB.status).toBe(403);
      const regC = await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokC.access_token }));
      expect(regC.status).toBe(200);
    });
  });

  it('H6 refresh inherits approval authority and keeps the machine binding on the descendant', async () => {
    const { stub, bindings } = await freshHub('refresh-inherit');
    await runInDurableObject(stub, async (hub: Hub) => {
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codes = await issueHostPairing(hub, headers, 'pair-b', 'hetzner');
      const tok: any = await (await exchangeCode(hub, codes.device_code)).json();
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tok.access_token }))).status).toBe(200);
      const before: any = hub.sql.exec('SELECT id, approval_seq FROM credentials WHERE token_hash = ?', await sha256(tok.access_token)).toArray()[0];
      const ref = await hub.fetch(hubRequest('/api/token/refresh', { grant_type: 'refresh_token', refresh_token: tok.refresh_token }));
      expect(ref.status).toBe(200);
      const tok2: any = await ref.json();
      const after: any = hub.sql.exec('SELECT id, approval_seq FROM credentials WHERE token_hash = ?', await sha256(tok2.access_token)).toArray()[0];
      expect(after.approval_seq).toBe(before.approval_seq);
      const machine: any = hub.sql.exec('SELECT credential_id FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      expect(machine.credential_id).toBe(after.id);
      const reg = await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tok2.access_token }));
      expect(reg.status).toBe(200);
    });
  });

  it('H7 refresh descendant of a superseded family cannot reclaim the machine', async () => {
    const { stub, bindings } = await freshHub('stale-refresh');
    await runInDurableObject(stub, async (hub: Hub) => {
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codesA = await issueHostPairing(hub, headers, 'pair-a', 'hetzner');
      const tokA: any = await (await exchangeCode(hub, codesA.device_code)).json();
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokA.access_token }))).status).toBe(200);
      const codesB = await issueHostPairing(hub, headers, 'pair-b', 'hetzner');
      const tokB: any = await (await exchangeCode(hub, codesB.device_code)).json();
      // A refreshes while live (same stale family/authority as before).
      const refA = await hub.fetch(hubRequest('/api/token/refresh', { grant_type: 'refresh_token', refresh_token: tokA.refresh_token }));
      expect(refA.status).toBe(200);
      const tokA2: any = await refA.json();
      const regA2 = await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokA2.access_token }));
      expect(regA2.status).toBe(403);
      const regB = await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tokB.access_token }));
      expect(regB.status).toBe(200);
    });
  });

  it('H8 legacy both-NULL bootstrap binds an unbound machine; legacy unwitnessed grant cannot replace a bound owner', async () => {
    const { stub } = await freshHub('legacy');
    await runInDurableObject(stub, async (hub: Hub) => {
      const at = new Date().toISOString();
      const far = new Date(Date.now() + 30 * 86400000).toISOString();
      // Pre-migration rows carry no witness on either side: unbound bootstrap passes.
      const legacyToken = 'hubt_legacy_token_1111111111111111';
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('legacy-cred', 'host', ?, 'legacy-ref', 'legacy-family', 'hetzner', 'hetzner', NULL, '["host"]', 'test', ?, ?)`,
        await sha256(legacyToken), at, far,
      );
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'old', NULL, ?)`, at);
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + legacyToken }))).status).toBe(200);
      // A second legacy unwitnessed credential can never replace the bound legacy owner.
      const legacyToken2 = 'hubt_legacy_token_2222222222222222';
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('legacy-cred-2', 'host', ?, 'legacy-ref-2', 'legacy-family-2', 'hetzner', 'hetzner', NULL, '["host"]', 'test', ?, ?)`,
        await sha256(legacyToken2), at, far,
      );
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + legacyToken2 }))).status).toBe(403);
      const machine: any = hub.sql.exec('SELECT credential_id FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      expect(machine.credential_id).toBe('legacy-cred');
    });
  });

  it('H9 stale witnessed pairing denied on unbound witnessed machine; fresh approval bootstraps', async () => {
    const { stub, bindings } = await freshHub('unbound-witness');
    await runInDurableObject(stub, async (hub: Hub) => {
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codes1 = await issueHostPairing(hub, headers, 'pair-1', 'hetzner');
      const tok1: any = await (await exchangeCode(hub, codes1.device_code)).json();
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tok1.access_token }))).status).toBe(200);
      // Unbind without clearing the witness (simulates a retired credential row).
      hub.sql.exec('UPDATE machines SET credential_id = NULL WHERE id = ?', 'hetzner');
      hub.sql.exec('DELETE FROM credentials WHERE token_hash = ?', await sha256(tok1.access_token));
      const witness: any = hub.sql.exec('SELECT approval_seq FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      expect(witness.approval_seq).not.toBeNull();
      // A legacy unwitnessed approved pairing cannot bootstrap a witnessed machine.
      const staleHash = await sha256('stale-device-code');
      hub.sql.exec(
        `INSERT INTO device_pairings (device_code_hash, user_code, client_kind, name, hostname, requested_scopes, granted_scopes, status, approved_by_sub, machine_id, interval_s, last_poll_at, created_at, expires_at)
         VALUES (?, 'STALE-1', 'host', 'hetzner', 'hetzner', '["host"]', '["host"]', 'approved', 'admin', 'hetzner', 5, ?, ?, ?)`,
        staleHash, at, at, new Date(Date.now() + 600000).toISOString(),
      );
      const exStale = await exchangeCode(hub, 'stale-device-code');
      expect(exStale.status).toBe(400);
      const staleRow: any = hub.sql.exec('SELECT status FROM device_pairings WHERE user_code = ?', 'STALE-1').toArray()[0];
      expect(staleRow.status).toBe('approved');
      // Fresh approval mints the current witness and bootstraps.
      const codes2 = await issueHostPairing(hub, headers, 'pair-2', 'hetzner');
      const exFresh = await exchangeCode(hub, codes2.device_code);
      expect(exFresh.status).toBe(200);
      const tok2: any = await exFresh.json();
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tok2.access_token }))).status).toBe(200);
    });
  });

  it('H10 approval replay mints no new authority and issuance retry stays idempotent', async () => {
    const { stub, bindings } = await freshHub('replay');
    await runInDurableObject(stub, async (hub: Hub) => {
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codes = await issueHostPairing(hub, headers, 'pair-r', 'hetzner');
      const seqBefore: any = hub.sql.exec('SELECT approval_seq FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      const replay = await hub.fetch(
        hubRequest('/api/device/approve/' + codes.user_code, { granted_scopes: ['host'] }, 'POST', headers),
      );
      expect(replay.status).toBe(400);
      const seqAfter: any = hub.sql.exec('SELECT approval_seq FROM machines WHERE id = ?', 'hetzner').toArray()[0];
      expect(seqAfter.approval_seq).toBe(seqBefore.approval_seq);
      const ex1 = await exchangeCode(hub, codes.device_code);
      expect(ex1.status).toBe(200);
      const ex2 = await exchangeCode(hub, codes.device_code);
      expect(ex2.status).toBe(400);
      const err: any = await ex2.json();
      expect(err.error).toBe('invalid_grant');
    });
  });

  it('H11 legitimate current-family session flow keeps working after hostauth (positive control)', async () => {
    const { stub, bindings } = await freshHub('positive');
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fakeReserve() });
      s.init();
      const headers = await adminCookieHeader(bindings as unknown as Env);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', ?)`, at);
      const codes = await issueHostPairing(hub, headers, 'pair-pos', 'hetzner');
      const tok: any = await (await exchangeCode(hub, codes.device_code)).json();
      expect((await hub.fetch(hubRequest('/api/machines/register', registerBody, 'POST', { Authorization: 'Bearer ' + tok.access_token }))).status).toBe(200);
      const cred: any = hub.sql.exec('SELECT id FROM credentials WHERE token_hash = ?', await sha256(tok.access_token)).toArray()[0];
      const hostAuth = { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner', tokenId: cred.id }, scopes: ['host'] };
      const created: any = await (await s.handle(hubRequest('/api/sessions', { key: 'h11-k1', workspace: 'demo', machine: 'hetzner' }), adminAuth)).json();
      const tag = 'hub-' + created.fleet_launch_id;
      ensureFleetLaunch(hub.sql, created.fleet_launch_id);
      reportFleetSession(hub.sql, 'hetzner', NATIVE_ID, tag, NATIVE_WS);
      expect((await s.handle(
        hubRequest(`/api/sessions/${created.id}/binding`, { native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }),
        hostAuth,
      )).status).toBe(200);
      expect((await s.handle(hubRequest(`/api/sessions/${created.id}/send`, { key: 'h11-s1', body: 'hello' }), adminAuth)).status).toBe(201);
      const offers: any = await (await s.handle(hubRequest('/api/session-commands/offers', undefined, 'GET'), hostAuth)).json();
      expect(offers.offers.length).toBe(1);
      expect((await s.handle(
        hubRequest(`/api/session-commands/${offers.offers[0].command_id}/claim`, { correlation: offers.offers[0].correlation, attempt_id: ATTEMPT }), hostAuth,
      )).status).toBe(200);
    });
  });
});
