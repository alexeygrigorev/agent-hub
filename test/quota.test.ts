import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv, createHelpers, loginAndGetState } from './m1-focus-setup';
import { Hub } from '../src/hub';
import { QuotaStore, QUOTA_STALE_MS, QUOTA_MAX_BODY_BYTES } from '../src/quota';
import { sha256 } from '../src/auth/crypto';

// Offline test fixtures only: no live API calls, no real tokens, no subprocess execution.
const originalFetch = globalThis.fetch;

let stableHelpersPromise: Promise<any> | null = null;
function getStableHelpers() {
  if (!stableHelpersPromise) stableHelpersPromise = createHelpers();
  return stableHelpersPromise;
}

async function getAdminSessionAndCsrf(): Promise<{ sessionCookie: string; csrfToken: string }> {
  const { loginCookie, state, nonce } = await loginAndGetState();
  const helpers = await getStableHelpers();
  const now = Math.floor(Date.now() / 1000);
  const idToken = await helpers.signJwt({
    iss: 'https://auth.fake.local',
    aud: 'fake_client_id',
    exp: now + 3600,
    nonce,
    token_use: 'id',
    email: 'admin@fake.local',
    email_verified: true,
    sub: 'admin-quota-test',
  });

  const savedFetch = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL) => {
    if (input.toString().includes('/oauth2/token')) {
      return new Response(JSON.stringify({ id_token: idToken }), { status: 200 });
    }
    if (input.toString().includes('/.well-known/jwks.json')) {
      return new Response(
        JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      );
    }
    return savedFetch(input);
  };

  try {
    const cbRes = await doFetch(
      new Request(`http://example.com/auth/callback?code=fake&state=${state}`, {
        headers: { Cookie: loginCookie },
        redirect: 'manual',
      })
    );
    const sessionCookie = cbRes.headers.get('Set-Cookie')!.split(';')[0];
    const meRes = await doFetch(
      new Request('http://example.com/auth/me', {
        headers: { Cookie: sessionCookie },
      })
    );
    const meBody = (await meRes.json()) as any;
    return { sessionCookie, csrfToken: meBody.csrf_token };
  } finally {
    globalThis.fetch = savedFetch;
  }
}

async function pairAndRegisterHost(name: string, hostname: string): Promise<{ machineId: string; accessToken: string; refreshToken: string }> {
  const initRes = await doFetch(
    new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_kind: 'host',
        name,
        hostname,
        requested_scopes: ['host'],
      }),
    })
  );
  expect(initRes.status).toBe(200);
  const { device_code, user_code } = (await initRes.json()) as any;

  const { sessionCookie, csrfToken } = await getAdminSessionAndCsrf();
  const approveRes = await doFetch(
    new Request(`http://example.com/api/device/approve/${user_code}`, {
      method: 'POST',
      headers: {
        Cookie: sessionCookie,
        'Content-Type': 'application/json',
        'x-csrf-token': csrfToken,
      },
      body: JSON.stringify({ granted_scopes: ['host'] }),
    })
  );
  expect(approveRes.status).toBe(200);
  const approveBody = (await approveRes.json()) as any;
  const machineId = approveBody.machine_id as string;

  const tokenRes = await doFetch(
    new Request('http://example.com/api/device/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
        device_code,
      }),
    })
  );
  expect(tokenRes.status).toBe(200);
  const tokenBody = (await tokenRes.json()) as any;

  const regRes = await doFetch(
    new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${tokenBody.access_token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        hostname,
        os: 'linux',
        network: 'public',
        roles: ['worker'],
        capabilities: { launcher: '0.1.0', engines: ['codex', 'gemini', 'grok'], jobs: ['root-run'], aplexer: true },
      }),
    })
  );
  expect(regRes.status).toBe(200);
  expect(await regRes.json()).toEqual({ status: 'registered', machine_id: machineId });

  return {
    machineId,
    accessToken: tokenBody.access_token,
    refreshToken: tokenBody.refresh_token,
  };
}

async function registerWith(accessToken: string, body: Record<string, unknown>): Promise<Response> {
  return doFetch(
    new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        os: 'linux',
        network: 'public',
        roles: ['worker'],
        capabilities: { launcher: '0.1.0' },
        ...body,
      }),
    })
  );
}

// Verbatim create_quota_snapshot output (host/agent_hub_host/quota_snapshot.py, host_id 'mach1',
// captured at 2026-10-14T12:00:30Z) for the reviewer's C/D/H shapes plus a mixed-provider case.
// PRE_FIX_COLLECTOR_OUTPUT is the original collector (8314119); FIXED_COLLECTOR_OUTPUT is the current one.
const COLLECTOR_CAPTURED_MS = Date.parse('2026-10-14T12:00:30Z');
const PRE_FIX_COLLECTOR_OUTPUT: Record<string, any> = {
  C_invalid_percent_with_reset: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "error", "error": "unavailable", "observed_at": null, "details": {"limit_reached": false}, "windows": {"5h": {"percent_remaining": null, "reset_at": "2026-10-14T15:00:00Z", "present": false, "rolling": true, "error": "unavailable"}, "7d": {"percent_remaining": 70.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}}},
  D_null_percent_with_reset: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "error", "error": "unavailable", "observed_at": null, "details": {"limit_reached": true}, "windows": {"5h": {"percent_remaining": null, "reset_at": "2026-10-14T15:00:00Z", "present": false, "rolling": true, "error": "unavailable"}, "7d": {"percent_remaining": 0.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}}},
  H_absent_window_with_percent: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "ok", "error": null, "observed_at": null, "details": {"limit_reached": false}, "windows": {"5h": {"percent_remaining": 10.0, "reset_at": null, "present": false, "rolling": true, "error": null}, "7d": {"percent_remaining": 50.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}}},
  K_mixed_good_providers_with_bad_codex_5h: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "error", "error": "unavailable", "observed_at": null, "details": {"limit_reached": true}, "windows": {"5h": {"percent_remaining": null, "reset_at": "2026-10-14T15:00:00Z", "present": false, "rolling": true, "error": "unavailable"}, "7d": {"percent_remaining": 0.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}, "claude": {"status": "ok", "error": null, "observed_at": "2026-10-14T11:58:00Z", "details": {"limit_reached": false}, "windows": {"5h": {"percent_remaining": 55.0, "reset_at": "2026-10-14T14:30:00Z", "present": true, "rolling": false, "error": null}, "7d": {"percent_remaining": 87.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}, "monthly": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": false, "error": null}}}, "grok": {"status": "ok", "error": null, "observed_at": null, "details": {"limit_reached": false, "has_grok_code_access": true}, "windows": {"5h": {"percent_remaining": 92.0, "reset_at": "2026-10-14T16:00:00Z", "present": true, "rolling": true, "error": null}, "7d": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": false, "error": null}, "monthly": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": false, "error": null}}}}},
};
const FIXED_COLLECTOR_OUTPUT: Record<string, any> = {
  C_invalid_percent_with_reset: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "error", "error": "unavailable", "observed_at": null, "details": {"limit_reached": false}, "windows": {"5h": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": true, "error": "unavailable"}, "7d": {"percent_remaining": 70.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}}},
  D_null_percent_with_reset: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "error", "error": "unavailable", "observed_at": null, "details": {"limit_reached": true}, "windows": {"5h": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": true, "error": "unavailable"}, "7d": {"percent_remaining": 0.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}}},
  H_absent_window_with_percent: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "error", "error": "unavailable", "observed_at": null, "details": {"limit_reached": false}, "windows": {"5h": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": true, "error": "unavailable"}, "7d": {"percent_remaining": 50.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}}},
  K_mixed_good_providers_with_bad_codex_5h: {"schema_version": "1.0", "captured_at": "2026-10-14T12:00:30Z", "host_id": "mach1", "status": "ok", "error": null, "routes": {"codex": {"status": "error", "error": "unavailable", "observed_at": null, "details": {"limit_reached": true}, "windows": {"5h": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": true, "error": "unavailable"}, "7d": {"percent_remaining": 0.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}}}, "claude": {"status": "ok", "error": null, "observed_at": "2026-10-14T11:58:00Z", "details": {"limit_reached": false}, "windows": {"5h": {"percent_remaining": 55.0, "reset_at": "2026-10-14T14:30:00Z", "present": true, "rolling": false, "error": null}, "7d": {"percent_remaining": 87.0, "reset_at": "2026-10-20T00:00:00Z", "present": true, "rolling": false, "error": null}, "monthly": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": false, "error": null}}}, "grok": {"status": "ok", "error": null, "observed_at": null, "details": {"limit_reached": false, "has_grok_code_access": true}, "windows": {"5h": {"percent_remaining": 92.0, "reset_at": "2026-10-14T16:00:00Z", "present": true, "rolling": true, "error": null}, "7d": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": false, "error": null}, "monthly": {"percent_remaining": null, "reset_at": null, "present": false, "rolling": false, "error": null}}}}},
};

function buildValidSnapshot(hostId: string, capturedAtIso: string, overrides?: Partial<Record<string, any>>) {
  return {
    schema_version: '1.0',
    captured_at: capturedAtIso,
    host_id: hostId,
    status: 'ok',
    error: null,
    routes: {
      codex: {
        status: 'ok',
        error: null,
        observed_at: capturedAtIso,
        details: {
          limit_reached: false,
        },
        windows: {
          '5h': {
            percent_remaining: null,
            reset_at: null,
            present: false,
            rolling: true,
          },
          '7d': {
            percent_remaining: 78.5,
            reset_at: '2026-10-20T00:00:00Z',
            present: true,
            rolling: false,
          },
        },
      },
      grok: {
        status: 'ok',
        error: null,
        observed_at: capturedAtIso,
        details: {
          limit_reached: false,
          has_grok_code_access: true,
        },
        windows: {
          '5h': {
            percent_remaining: 92,
            reset_at: '2026-10-14T12:00:00Z',
            present: true,
            rolling: true,
          },
        },
      },
      gemini: {
        status: 'ok',
        error: null,
        observed_at: capturedAtIso,
        details: {
          limit_reached: false,
        },
        windows: {
          daily: {
            percent_remaining: 64,
            reset_at: '2026-10-15T00:00:00+00:00',
            present: true,
            rolling: false,
          },
        },
      },
    },
    ...(overrides || {}),
  };
}

describe('Quota API (/api/quota) & QuotaStore Contract Suite (Offline Fixtures Only)', () => {
  let stub: any;
  let hostAlpha: { machineId: string; accessToken: string; refreshToken: string };
  let hostBeta: { machineId: string; accessToken: string; refreshToken: string };

  beforeAll(async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    stub = defaultEnv.HUB.get(id);
    hostAlpha = await pairAndRegisterHost('quota-host-alpha', 'alpha.local');
    hostBeta = await pairAndRegisterHost('quota-host-beta', 'beta.local');
  });

  afterAll(() => {
    globalThis.fetch = originalFetch;
  });

  it('register returns the credential-bound machine_id for already paired hosts and never another host id', async () => {
    expect(hostAlpha.machineId).not.toBe(hostBeta.machineId);

    // Already paired host re-registers on every start: same credential, same machine_id
    const againAlpha = await registerWith(hostAlpha.accessToken, { hostname: 'alpha.local' });
    expect(againAlpha.status).toBe(200);
    expect(await againAlpha.json()).toEqual({ status: 'registered', machine_id: hostAlpha.machineId });

    const againBeta = await registerWith(hostBeta.accessToken, { hostname: 'beta.local' });
    expect(againBeta.status).toBe(200);
    expect(await againBeta.json()).toEqual({ status: 'registered', machine_id: hostBeta.machineId });

    // Cross-host: alpha's token claiming beta's identity in the body still binds to alpha only
    const betaRowBefore = await runInDurableObject(stub, async (instance: Hub) =>
      instance.sql.exec('SELECT hostname, credential_id FROM machines WHERE id = ?', hostBeta.machineId).toArray()[0]
    );
    const spoof = await registerWith(hostAlpha.accessToken, {
      hostname: 'beta.local',
      machine_id: hostBeta.machineId,
      id: hostBeta.machineId,
    });
    expect(spoof.status).toBe(200);
    expect(await spoof.json()).toEqual({ status: 'registered', machine_id: hostAlpha.machineId });
    const betaRowAfter = await runInDurableObject(stub, async (instance: Hub) =>
      instance.sql.exec('SELECT hostname, credential_id FROM machines WHERE id = ?', hostBeta.machineId).toArray()[0]
    );
    expect(betaRowAfter).toEqual(betaRowBefore);
    await registerWith(hostAlpha.accessToken, { hostname: 'alpha.local' });

    // Bad or missing bearer: 401 and no machine_id in the body
    for (const auth of [undefined, 'Bearer hubt_not_a_real_token', `Bearer ${hostAlpha.refreshToken}`]) {
      const res = await doFetch(
        new Request('http://example.com/api/machines/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...(auth ? { Authorization: auth } : {}) },
          body: JSON.stringify({ hostname: 'x', os: 'linux', network: 'public', roles: [], capabilities: {} }),
        })
      );
      expect(res.status).toBe(401);
      expect(JSON.stringify(await res.json())).not.toContain('machine_id');
    }
  });

  it('retires host quota reads when host has no snapshot', async () => {
    const res = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'GET',
        headers: { Authorization: `Bearer ${hostAlpha.accessToken}` },
      })
    );
    expect(res.status).toBe(410);
    expect(res.headers.get('Cache-Control')).toBe('private, no-store');
    const body = (await res.json()) as any;
    expect(body).toEqual({ error: 'machine_quota_collection_disabled', source: 'hub_server' });
  });

  it('rejects anonymous, job, agent, human, expired, revoked, unregistered, cross-machine, and header/body spoof with zero SQLite writes', async () => {
    const baseNowMs = 1760420000000; // 2025-10-14T...
    const capturedIso = new Date(baseNowMs - 5000).toISOString();

    await runInDurableObject(stub, async (instance: Hub) => {
      const sql = instance.sql;
      const nowIso = new Date(baseNowMs).toISOString();
      const pastIso = new Date(baseNowMs - 60000).toISOString();
      const futureIso = new Date(baseNowMs + 3600000).toISOString();

      // Seed specialized test tokens directly into credentials table
      const jobToken = 'hubt_jobtoken_quota_neg_test_00000001';
      const agentToken = 'hubt_agenttoken_quota_neg_test_000002';
      const humanToken = 'hubt_humantoken_quota_neg_test_000003';
      const expiredHostToken = 'hubt_exphost_quota_neg_test_000000004';
      const revokedHostToken = 'hubt_revhost_quota_neg_test_000000005';
      const unregHostToken = 'hubt_unreghost_quota_neg_test_0000006';

      sql.exec(
        `INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
         VALUES (?, 'job', ?, 'fam_j', 'job-1', ?, 'role:root', ?, 'admin', ?, ?, NULL)`,
        'cred_q_job',
        await sha256(jobToken),
        hostAlpha.machineId,
        JSON.stringify(['lease:root', 'host']),
        nowIso,
        futureIso
      );

      sql.exec(
        `INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
         VALUES (?, 'agent', ?, 'fam_a', 'agent-1', ?, 'agent-1', ?, 'admin', ?, ?, NULL)`,
        'cred_q_agent',
        await sha256(agentToken),
        hostAlpha.machineId,
        JSON.stringify(['bus:send', 'bus:read']),
        nowIso,
        futureIso
      );

      sql.exec(
        `INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
         VALUES (?, 'human', ?, 'fam_h', 'human-1', NULL, 'admin', ?, 'admin', ?, ?, NULL)`,
        'cred_q_human',
        await sha256(humanToken),
        JSON.stringify(['admin']),
        nowIso,
        futureIso
      );

      sql.exec(
        `INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
         VALUES (?, 'host', ?, 'fam_exp', 'exp-host', ?, NULL, ?, 'admin', ?, ?, NULL)`,
        'cred_q_exp',
        await sha256(expiredHostToken),
        hostAlpha.machineId,
        JSON.stringify(['host']),
        pastIso,
        pastIso
      );

      sql.exec(
        `INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
         VALUES (?, 'host', ?, 'fam_rev', 'rev-host', ?, NULL, ?, 'admin', ?, ?, ?)`,
        'cred_q_rev',
        await sha256(revokedHostToken),
        hostAlpha.machineId,
        JSON.stringify(['host']),
        pastIso,
        futureIso,
        nowIso
      );

      sql.exec(
        `INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
         VALUES (?, 'host', ?, 'fam_unreg', 'unreg-host', 'missing_machine_id_999', NULL, ?, 'admin', ?, ?, NULL)`,
        'cred_q_unreg',
        await sha256(unregHostToken),
        JSON.stringify(['host']),
        nowIso,
        futureIso
      );

      instance.quotaStore.setClock(() => baseNowMs);
      try {
        const countRows = () =>
          (sql.exec('SELECT COUNT(*) AS cnt FROM host_quota_snapshots').toArray()[0] as any).cnt as number;
        const beforeCount = countRows();
        expect(beforeCount).toBe(0);

        const validBodyForAlpha = JSON.stringify(buildValidSnapshot(hostAlpha.machineId, capturedIso));

        // 1. Anonymous POST & GET
        const anonPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: validBodyForAlpha,
          })
        );
        expect(anonPost.status).toBe(401);
        expect(anonPost.headers.get('Cache-Control')).toBe('private, no-store');

        const anonGet = await instance.fetch(new Request('http://example.com/api/quota'));
        expect(anonGet.status).toBe(401);

        // 2. Unknown token
        const unkPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: 'Bearer hubt_unknown_token_value_1234567890',
              'Content-Type': 'application/json',
            },
            body: validBodyForAlpha,
          })
        );
        expect(unkPost.status).toBe(401);

        // 3. Expired host token
        const expPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${expiredHostToken}`,
              'Content-Type': 'application/json',
            },
            body: validBodyForAlpha,
          })
        );
        expect(expPost.status).toBe(401);

        // 4. Revoked host token
        const revPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${revokedHostToken}`,
              'Content-Type': 'application/json',
            },
            body: validBodyForAlpha,
          })
        );
        expect(revPost.status).toBe(401);

        // 5. Job kind token (even if scopes array had 'host')
        const jobPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${jobToken}`,
              'Content-Type': 'application/json',
            },
            body: validBodyForAlpha,
          })
        );
        expect(jobPost.status).toBe(403);

        const jobGet = await instance.fetch(
          new Request('http://example.com/api/quota', {
            headers: { Authorization: `Bearer ${jobToken}` },
          })
        );
        expect(jobGet.status).toBe(403);

        // 6. Agent kind token
        const agentPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${agentToken}`,
              'Content-Type': 'application/json',
            },
            body: validBodyForAlpha,
          })
        );
        expect(agentPost.status).toBe(403);

        // 7. Human kind token
        const humanPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${humanToken}`,
              'Content-Type': 'application/json',
            },
            body: validBodyForAlpha,
          })
        );
        expect(humanPost.status).toBe(403);

        // 8. Unregistered machine token
        const unregPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${unregHostToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(buildValidSnapshot('missing_machine_id_999', capturedIso)),
          })
        );
        expect(unregPost.status).toBe(403);

        // 9. Cross-machine body spoof: hostAlpha token posting hostBeta body.host_id
        const crossBodyPost = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(buildValidSnapshot(hostBeta.machineId, capturedIso)),
          })
        );
        expect(crossBodyPost.status).toBe(403);

        // 10. Header spoofing on POST and GET
        for (const spoofHeader of ['X-Host-Id', 'X-Machine-Id', 'X-Forwarded-Host']) {
          const spoofPost = await instance.fetch(
            new Request('http://example.com/api/quota', {
              method: 'POST',
              headers: {
                Authorization: `Bearer ${hostAlpha.accessToken}`,
                'Content-Type': 'application/json',
                [spoofHeader]: hostBeta.machineId,
              },
              body: validBodyForAlpha,
            })
          );
          expect(spoofPost.status).toBe(403);

          const spoofGet = await instance.fetch(
            new Request('http://example.com/api/quota', {
              method: 'GET',
              headers: {
                Authorization: `Bearer ${hostAlpha.accessToken}`,
                [spoofHeader]: hostBeta.machineId,
              },
            })
          );
          expect(spoofGet.status).toBe(403);
        }

        // 11. Query host_id spoof on POST and GET
        const querySpoofPost = await instance.fetch(
          new Request(`http://example.com/api/quota?host_id=${encodeURIComponent(hostBeta.machineId)}`, {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: validBodyForAlpha,
          })
        );
        expect(querySpoofPost.status).toBe(403);

        const querySpoofGet = await instance.fetch(
          new Request(`http://example.com/api/quota?host_id=${encodeURIComponent(hostBeta.machineId)}`, {
            method: 'GET',
            headers: { Authorization: `Bearer ${hostAlpha.accessToken}` },
          })
        );
        expect(querySpoofGet.status).toBe(403);

        // Verify ZERO rows written across all negative auth/boundary attempts
        expect(countRows()).toBe(0);
      } finally {
        instance.quotaStore.setClock(undefined);
      }
    });
  });

  it('retires authenticated per-machine quota collection without inserting a snapshot', async () => {
    const t0 = 1760421000000;
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.quotaStore.setClock(() => t0);
      const response = await instance.fetch(new Request('http://example.com/api/quota', {
        method: 'POST',
        headers: { Authorization: `Bearer ${hostAlpha.accessToken}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(buildValidSnapshot(hostAlpha.machineId, new Date(t0 - 1000).toISOString())),
      }));
      expect(response.status).toBe(410);
      expect(await response.json()).toEqual({ error: 'machine_quota_collection_disabled', source: 'hub_server' });
      expect(instance.sql.exec('SELECT COUNT(*) AS n FROM host_quota_snapshots WHERE host_id = ?', hostAlpha.machineId).one()?.n).toBe(0);
      instance.quotaStore.setClock(undefined);
    });
  });

  it('accepts the banked probe-failure marker on v1.1 routes and rejects overlong or mistyped markers', async () => {
    // Intake 7bf6f6ee AC3: `banked_resets_error` must survive validation so
    // downstream banked views read it as unknown, never as 0 banked. Opaque
    // short string only; overlong/non-string markers are refused.
    const tNow = 1760430000000;
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.quotaStore.setClock(() => tNow);
      try {
        const capIso = new Date(tNow - 1000).toISOString();
        const snap: any = buildValidSnapshot(hostAlpha.machineId, capIso, { schema_version: '1.1' } as any);
        snap.routes.codex.details.banked_resets = [];
        snap.routes.codex.details.banked_resets_error = 'quota_unreachable';
        const res = instance.quotaStore.putSnapshot(hostAlpha.machineId, snap, tNow);
        expect(res.ok).toBe(true);
        const stored = instance.sql.exec('SELECT routes_json FROM host_quota_snapshots WHERE host_id = ?', hostAlpha.machineId).toArray()[0] as any;
        expect(JSON.parse(stored.routes_json).codex.details.banked_resets_error).toBe('quota_unreachable');

        for (const bad of ['', 'x'.repeat(257), 42, true]) {
          const badSnap: any = buildValidSnapshot(hostAlpha.machineId, new Date(tNow - 500).toISOString(), { schema_version: '1.1' } as any);
          badSnap.routes.codex.details.banked_resets_error = bad;
          expect(instance.quotaStore.putSnapshot(hostAlpha.machineId, badSnap, tNow + 1000).ok).toBe(false);
        }
      } finally {
        instance.quotaStore.setClock(undefined);
      }
    });
  });

  it('enforces data-driven window names regex, 32-window limit, native details.stale, and window error fail-closed propagation', async () => {
    const tBase = 1760425000000;

    await runInDurableObject(stub, async (instance: Hub) => {
      instance.quotaStore.setClock(() => tBase);
      try {
        const capIso = new Date(tBase - 1000).toISOString();

        // 1. Dynamic window names + optional window error: "unavailable" + native details.stale: true
        const dynamicPayload = {
          schema_version: '1.0',
          captured_at: capIso,
          host_id: hostBeta.machineId,
          status: 'ok',
          error: null,
          routes: {
            claude: {
              status: 'ok',
              error: null,
              observed_at: capIso,
              details: {
                limit_reached: false,
                stale: true, // Native stale must force route status: "error", error: "unavailable"
              },
              windows: {
                sonnet_7d_window: {
                  percent_remaining: 55,
                  reset_at: '2026-10-19T00:00:00Z',
                  present: true,
                  rolling: false,
                },
              },
            },
            zai: {
              status: 'ok',
              error: null,
              observed_at: capIso,
              details: {
                limit_reached: false,
              },
              windows: {
                'glm-coding-window_1': {
                  percent_remaining: 90,
                  reset_at: '2026-10-15T00:00:00Z',
                  present: true,
                  rolling: true,
                  error: null,
                },
                optional_broken_win: {
                  percent_remaining: null,
                  reset_at: null,
                  present: false,
                  rolling: false,
                  error: 'unavailable', // Explicit upstream window error marks route unavailable fail-closed
                },
              },
            },
            copilot: {
              status: 'ok',
              error: null,
              observed_at: capIso,
              details: {
                limit_reached: false,
              },
              windows: {
                monthly_chat: {
                  percent_remaining: null,
                  reset_at: null,
                  present: false,
                  rolling: false,
                  error: null, // Empty optional window without error remains absent/null, route stays ok
                },
              },
            },
          },
        };

        const res = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostBeta.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(dynamicPayload),
          })
        );
        expect(res.status).toBe(410);
        expect(await res.json()).toEqual({ error: 'machine_quota_collection_disabled', source: 'hub_server' });

        // 2. Up to 32 windows allowed, 33 windows rejected
        const tNext = tBase + 10_000;
        instance.quotaStore.setClock(() => tNext);
        const capNext = new Date(tNext - 1000).toISOString();

        const windows33: Record<string, any> = {};
        for (let i = 0; i < 33; i++) {
          windows33[`win_${i}`] = {
            percent_remaining: 50,
            reset_at: null,
            present: true,
            rolling: true,
          };
        }
        const tooManySnap = buildValidSnapshot(hostBeta.machineId, capNext, {
          routes: {
            go: {
              status: 'ok',
              error: null,
              observed_at: capNext,
              details: { limit_reached: false },
              windows: windows33,
            },
          },
        });
        const tooManyRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostBeta.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(tooManySnap),
          })
        );
        expect(tooManyRes.status).toBe(400);
      } finally {
        instance.quotaStore.setClock(undefined);
      }
    });
  });

  it('rejects malformed percentages, invalid reset timezones, invalid window names, unknown fields/accounts, oversized bodies, future and older/replay captures', async () => {
    const tNow = 1760430000000;

    await runInDurableObject(stub, async (instance: Hub) => {
      instance.quotaStore.setClock(() => tNow);
      try {
        instance.quotaStore.putSnapshot(hostAlpha.machineId, buildValidSnapshot(hostAlpha.machineId, new Date(tNow - 2000).toISOString()), tNow - 1000);
        const baselineRow = instance.sql
          .exec('SELECT captured_at, received_at, routes_json FROM host_quota_snapshots WHERE host_id = ?', hostAlpha.machineId)
          .toArray()[0] as any;
        expect(baselineRow).toBeDefined();

        const validCapIso = new Date(tNow - 1000).toISOString();

        // Direct store check for NaN, Infinity, -Infinity (since JSON.stringify turns them to null)
        for (const badNum of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, -0.1, -10, 100.01, 999, true, '50']) {
          const snap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
          (snap.routes.grok.windows['5h'] as any).percent_remaining = badNum;
          const res = instance.quotaStore.putSnapshot(hostAlpha.machineId, snap, tNow);
          expect(res.ok).toBe(false);
          expect(res.status).toBe(400);
        }

        // Raw NaN in HTTP JSON body
        const rawNanRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: `{"schema_version":"1.0","captured_at":"${validCapIso}","host_id":"${hostAlpha.machineId}","status":"ok","error":null,"routes":{"grok":{"status":"ok","error":null,"observed_at":null,"details":{"limit_reached":false,"has_grok_code_access":true},"windows":{"5h":{"percent_remaining":NaN,"reset_at":null,"present":true,"rolling":true}}}}}`,
          })
        );
        expect(rawNanRes.status).toBe(400);

        // Invalid reset_at: empty string, missing timezone, non-UTC timezone, invalid calendar date
        for (const badReset of ['', '2026-10-14T12:00:00', '2026-10-14T12:00:00+02:00', '2026-02-30T00:00:00Z', 12345]) {
          const snap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
          (snap.routes.grok.windows['5h'] as any).reset_at = badReset;
          const r = await instance.fetch(
            new Request('http://example.com/api/quota', {
              method: 'POST',
              headers: {
                Authorization: `Bearer ${hostAlpha.accessToken}`,
                'Content-Type': 'application/json',
              },
              body: JSON.stringify(snap),
            })
          );
          expect(r.status).toBe(400);
        }

        // Invalid window names
        for (const badWinName of ['', '-bad', '_bad', 'bad/win', 'bad space', 'a'.repeat(49)]) {
          const snap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
          (snap.routes.grok.windows as any)[badWinName] = {
            percent_remaining: 50,
            reset_at: null,
            present: true,
            rolling: true,
          };
          const r = await instance.fetch(
            new Request('http://example.com/api/quota', {
              method: 'POST',
              headers: {
                Authorization: `Bearer ${hostAlpha.accessToken}`,
                'Content-Type': 'application/json',
              },
              body: JSON.stringify(snap),
            })
          );
          expect(r.status).toBe(400);
        }

        // Unknown provider
        const unkProvSnap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
        (unkProvSnap.routes as any).openai = unkProvSnap.routes.codex;
        const unkProvRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(unkProvSnap),
          })
        );
        expect(unkProvRes.status).toBe(400);

        // Non-grok provider with has_grok_code_access
        const nonGrokAccessSnap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
        (nonGrokAccessSnap.routes.codex.details as any).has_grok_code_access = false;
        const nonGrokAccessRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(nonGrokAccessSnap),
          })
        );
        expect(nonGrokAccessRes.status).toBe(400);

        // Forbidden privacy/secret fields at top-level, route-level, details-level, window-level
        const extraTopSnap = { ...buildValidSnapshot(hostAlpha.machineId, validCapIso), accounts: ['secret@example.com'] };
        const extraRouteSnap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
        (extraRouteSnap.routes.codex as any).balances = { usd: 50 };
        const extraDetailsSnap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
        (extraDetailsSnap.routes.codex.details as any).raw_error = 'Bearer secret_token_xyz';
        const extraWinSnap = buildValidSnapshot(hostAlpha.machineId, validCapIso);
        (extraWinSnap.routes.codex.windows['7d'] as any).tokens_used = 12345;

        for (const badSnap of [extraTopSnap, extraRouteSnap, extraDetailsSnap, extraWinSnap]) {
          const r = await instance.fetch(
            new Request('http://example.com/api/quota', {
              method: 'POST',
              headers: {
                Authorization: `Bearer ${hostAlpha.accessToken}`,
                'Content-Type': 'application/json',
              },
              body: JSON.stringify(badSnap),
            })
          );
          expect(r.status).toBe(400);
          const errText = await r.text();
          expect(errText).not.toContain('secret@example.com');
          expect(errText).not.toContain('secret_token_xyz');
        }

        // Oversized body (> 32 KiB) via single payload and chunked stream
        const hugeString = 'x'.repeat(QUOTA_MAX_BODY_BYTES + 1024);
        const hugeRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ ...buildValidSnapshot(hostAlpha.machineId, validCapIso), pad: hugeString }),
          })
        );
        expect(hugeRes.status).toBe(413);

        const streamBody = new ReadableStream({
          start(controller) {
            const chunk = new Uint8Array(12000).fill(65);
            controller.enqueue(chunk);
            controller.enqueue(chunk);
            controller.enqueue(chunk); // 36,000 bytes > 32,768
            controller.close();
          },
        });
        const streamRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: streamBody,
            duplex: 'half',
          } as any)
        );
        expect(streamRes.status).toBe(413);

        // Future captured_at
        const futureCapIso = new Date(tNow + 5000).toISOString();
        const futureRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(buildValidSnapshot(hostAlpha.machineId, futureCapIso)),
          })
        );
        expect(futureRes.status).toBe(400);

        // Replay (exact same captured_at) and Older out-of-order post
        const replayRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(buildValidSnapshot(hostAlpha.machineId, baselineRow.captured_at)),
          })
        );
        expect(replayRes.status).toBe(410);

        const olderIso = new Date(Date.parse(baselineRow.captured_at) - 60000).toISOString();
        const olderRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(buildValidSnapshot(hostAlpha.machineId, olderIso)),
          })
        );
        expect(olderRes.status).toBe(410);

        // Verify stored row in SQLite was NOT modified or reset by any rejected/replay/older post
        const afterRow = instance.sql
          .exec('SELECT captured_at, received_at, routes_json FROM host_quota_snapshots WHERE host_id = ?', hostAlpha.machineId)
          .toArray()[0] as any;
        expect(afterRow).toEqual(baselineRow);
      } finally {
        instance.quotaStore.setClock(undefined);
      }
    });
  });

  it('persists across cold QuotaStore re-instantiation and enforces the exact 20-minute (1200s) stale boundary', async () => {
    const tCapture = 1760440000000;
    const tReceive = 1760440000000;
    const captureIso = new Date(tCapture).toISOString();

    await runInDurableObject(stub, async (instance: Hub) => {
      instance.quotaStore.setClock(() => tReceive);
      try {
        const snap = buildValidSnapshot(hostAlpha.machineId, captureIso);
        expect(instance.quotaStore.putSnapshot(hostAlpha.machineId, snap, tReceive).ok).toBe(true);

        // Simulate cold restart by instantiating a brand new QuotaStore against the same SQLite storage
        const coldStore = new QuotaStore(
          instance.sql,
          (instance as any).ctx,
          defaultEnv,
          () => tCapture + QUOTA_STALE_MS - 1 // 1199.999s (< 1200s)
        );
        const justBeforeStale = coldStore.getSnapshot(hostAlpha.machineId);
        expect(justBeforeStale.status).toBe(200);
        expect(justBeforeStale.body.stale).toBe(false);
        expect(justBeforeStale.body.status).toBe('ok');
        expect(justBeforeStale.body.error).toBeNull();
        expect(justBeforeStale.body.routes.grok.status).toBe('ok');

        // The legacy host read endpoint stays retired even when legacy rows exist.
        instance.quotaStore.setClock(() => tCapture + QUOTA_STALE_MS);
        const atBoundaryRes = await instance.fetch(
          new Request('http://example.com/api/quota', {
            method: 'GET',
            headers: { Authorization: `Bearer ${hostAlpha.accessToken}` },
          })
        );
        expect(atBoundaryRes.status).toBe(410);
        expect(atBoundaryRes.headers.get('Cache-Control')).toBe('private, no-store');
        const atBoundaryBody = (await atBoundaryRes.json()) as any;
        expect(atBoundaryBody).toEqual({ error: 'machine_quota_collection_disabled', source: 'hub_server' });

        // Caller clock headers / query params cannot spoof server read time to make stale snapshot look fresh
        const spoofClockRes = await instance.fetch(
          new Request(`http://example.com/api/quota?now=${encodeURIComponent(captureIso)}`, {
            method: 'GET',
            headers: {
              Authorization: `Bearer ${hostAlpha.accessToken}`,
              'X-Now': captureIso,
              'X-Server-Time': captureIso,
              Date: new Date(tCapture).toUTCString(),
            },
          })
        );
        expect(spoofClockRes.status).toBe(410);
        const spoofClockBody = (await spoofClockRes.json()) as any;
        expect(spoofClockBody).toEqual({ error: 'machine_quota_collection_disabled', source: 'hub_server' });
      } finally {
        instance.quotaStore.setClock(undefined);
      }
    });
  });

  it('supports verified admin session GET with explicit ?host_id and enforces Origin/CSRF read integrity', async () => {
    const { sessionCookie, csrfToken } = await getAdminSessionAndCsrf();
    const tFresh = Date.now();
    const capFresh = new Date(tFresh - 5000).toISOString();

    const stub = (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      expect(instance.quotaStore.putSnapshot(hostAlpha.machineId, buildValidSnapshot(hostAlpha.machineId, capFresh)).ok).toBe(true);
    });

    // Legacy admin host reads are retired even with a verified session.
    const noHostRes = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'GET',
        headers: { Cookie: sessionCookie, 'x-csrf-token': csrfToken },
      })
    );
    expect(noHostRes.status).toBe(410);

    // Explicit host selectors do not restore the retired read path.
    const adminGetRes = await doFetch(
      new Request(`http://example.com/api/quota?host_id=${encodeURIComponent(hostAlpha.machineId)}`, {
        method: 'GET',
        headers: {
          Cookie: sessionCookie,
          'x-csrf-token': csrfToken,
        },
      })
    );
    expect(adminGetRes.status).toBe(410);
    expect(adminGetRes.headers.get('Cache-Control')).toBe('private, no-store');
    expect(await adminGetRes.json()).toEqual({ error: 'machine_quota_collection_disabled', source: 'hub_server' });

    // 3. Admin GET with mismatched CSRF token or foreign Origin is denied
    const badCsrfRes = await doFetch(
      new Request(`http://example.com/api/quota?host_id=${encodeURIComponent(hostAlpha.machineId)}`, {
        method: 'GET',
        headers: {
          Cookie: sessionCookie,
          'x-csrf-token': 'wrong_csrf_token',
        },
      })
    );
    expect(badCsrfRes.status).toBe(403);

    const badOriginRes = await doFetch(
      new Request(`http://example.com/api/quota?host_id=${encodeURIComponent(hostAlpha.machineId)}`, {
        method: 'GET',
        headers: {
          Cookie: sessionCookie,
          Origin: 'https://evil.example.org',
        },
      })
    );
    expect(badOriginRes.status).toBe(403);

    // 4. Admin session cannot POST to /api/quota even with valid CSRF
    const adminPostRes = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'POST',
        headers: {
          Cookie: sessionCookie,
          'x-csrf-token': csrfToken,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(buildValidSnapshot(hostAlpha.machineId, new Date().toISOString())),
      })
    );
    expect(adminPostRes.status).toBe(403);
  });

  it('8. Supports all 7 allowlisted providers, preserves non-authority over leases/jobs/heartbeat, and enforces live /api/token/refresh & /api/token/revoke', async () => {
    const hostGamma = await pairAndRegisterHost('quota-host-gamma', 'gamma.local');

    const nowMs = Date.now();
    const capturedIso = new Date(nowMs - 10_000).toISOString();
    const resetIso = new Date(nowMs + 3600_000).toISOString();

    const makeRoute = (isGrok = false) => ({
      status: 'ok',
      error: null,
      observed_at: capturedIso,
      details: isGrok
        ? { limit_reached: false, has_grok_code_access: true }
        : { limit_reached: false },
      windows: {
        '5h': {
          percent_remaining: 80,
          reset_at: resetIso,
          present: true,
          rolling: true,
        },
      },
    });

    const allProvidersPayload = {
      schema_version: '1.0',
      captured_at: capturedIso,
      host_id: hostGamma.machineId,
      status: 'ok',
      error: null,
      routes: {
        codex: makeRoute(false),
        claude: makeRoute(false),
        gemini: makeRoute(false),
        grok: makeRoute(true),
        zai: makeRoute(false),
        go: makeRoute(false),
        copilot: makeRoute(false),
      },
    };

    // Snapshot DB state of machines/leases/jobs before quota POST/GET
    const beforeState = await runInDurableObject(stub, async (instance: Hub) => {
      const m = instance.sql
        .exec('SELECT last_checkin_at, last_heartbeat, drained FROM machines WHERE id = ?', hostGamma.machineId)
        .toArray()[0] as { last_checkin_at: string | null; last_heartbeat: string | null; drained: number };
      const leasesCount = (
        instance.sql.exec('SELECT COUNT(*) AS cnt FROM leases').toArray()[0] as { cnt: number }
      ).cnt;
      const jobsCount = (
        instance.sql.exec('SELECT COUNT(*) AS cnt FROM jobs').toArray()[0] as { cnt: number }
      ).cnt;
      return { m, leasesCount, jobsCount };
    });

    // POST all 7 providers
    const postRes = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${hostGamma.accessToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(allProvidersPayload),
      })
    );
    expect(postRes.status).toBe(410);

    // Unsupported method returns 405
    const putRes = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'PUT',
        headers: { Authorization: `Bearer ${hostGamma.accessToken}` },
      })
    );
    expect(putRes.status).toBe(405);

    // Verify non-authority invariant: leases, jobs, and machine heartbeat were not altered by /api/quota
    const afterState = await runInDurableObject(stub, async (instance: Hub) => {
      const m = instance.sql
        .exec('SELECT last_checkin_at, last_heartbeat, drained FROM machines WHERE id = ?', hostGamma.machineId)
        .toArray()[0] as { last_checkin_at: string | null; last_heartbeat: string | null; drained: number };
      const leasesCount = (
        instance.sql.exec('SELECT COUNT(*) AS cnt FROM leases').toArray()[0] as { cnt: number }
      ).cnt;
      const jobsCount = (
        instance.sql.exec('SELECT COUNT(*) AS cnt FROM jobs').toArray()[0] as { cnt: number }
      ).cnt;
      return { m, leasesCount, jobsCount };
    });
    expect(afterState).toEqual(beforeState);

    // Wrong-origin host revoke attempt (hostBeta attempting to revoke hostGamma's token) is denied (403) and leaves hostGamma untouched
    const crossRevokeRes = await doFetch(
      new Request('http://example.com/api/token/revoke', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${hostBeta.accessToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ token: hostGamma.accessToken }),
      })
    );
    expect(crossRevokeRes.status).toBe(403);

    const stillValidGet = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'GET',
        headers: { Authorization: `Bearer ${hostGamma.accessToken}` },
      })
    );
    expect(stillValidGet.status).toBe(410);

    // Refresh host token via /api/token/refresh: old token must be denied, new token must succeed
    const refreshRes = await doFetch(
      new Request('http://example.com/api/token/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          grant_type: 'refresh_token',
          refresh_token: hostGamma.refreshToken,
        }),
      })
    );
    expect(refreshRes.status).toBe(200);
    const refreshed = (await refreshRes.json()) as { access_token: string; refresh_token: string };
    expect(typeof refreshed.access_token).toBe('string');

    const oldTokenGet = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'GET',
        headers: { Authorization: `Bearer ${hostGamma.accessToken}` },
      })
    );
    expect(oldTokenGet.status).toBe(401);

    const newTokenGet = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'GET',
        headers: { Authorization: `Bearer ${refreshed.access_token}` },
      })
    );
    expect(newTokenGet.status).toBe(410);

    // Rotated credential of an already paired host still registers to the same machine_id
    const reRegister = await registerWith(refreshed.access_token, { hostname: 'gamma.local' });
    expect(reRegister.status).toBe(200);
    expect(await reRegister.json()).toEqual({ status: 'registered', machine_id: hostGamma.machineId });

    // Revoke refreshed token via /api/token/revoke: subsequent GET and POST must be denied
    const revokeRes = await doFetch(
      new Request('http://example.com/api/token/revoke', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${refreshed.access_token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ token: refreshed.access_token }),
      })
    );
    expect(revokeRes.status).toBe(200);

    const revokedGet = await doFetch(
      new Request('http://example.com/api/quota', {
        method: 'GET',
        headers: { Authorization: `Bearer ${refreshed.access_token}` },
      })
    );
    expect(revokedGet.status).toBe(401);
  });

  it('accepts genuine collector output for C/D/H error windows while still rejecting the pre-fix inconsistent shapes', async () => {
    const hostDelta = await pairAndRegisterHost('quota-host-delta', 'delta.local');
    const asDelta = (s: any, capturedMs: number) => ({
      ...structuredClone(s),
      host_id: hostDelta.machineId,
      captured_at: new Date(capturedMs).toISOString(),
    });
    const post = async (instance: Hub, body: any) => {
      const result = instance.quotaStore.putSnapshot(hostDelta.machineId, body, COLLECTOR_CAPTURED_MS + 60_000);
      return { status: result.status, json: async () => result.body };
    };
    const countRows = (instance: Hub) =>
      (instance.sql.exec('SELECT COUNT(*) AS cnt FROM host_quota_snapshots WHERE host_id = ?', hostDelta.machineId).toArray()[0] as any).cnt;

    await runInDurableObject(stub, async (instance: Hub) => {
      instance.quotaStore.setClock(() => COLLECTOR_CAPTURED_MS + 60_000);
      try {
        // Original negative cases stay rejected by the unchanged Hub validator, with zero writes
        for (const [name, s] of Object.entries(PRE_FIX_COLLECTOR_OUTPUT)) {
          const res = await post(instance, asDelta(s, COLLECTOR_CAPTURED_MS));
          expect({ name, status: res.status }).toEqual({ name, status: 400 });
          expect(((await res.json()) as any).error).toBe('inconsistent_absent_window');
        }
        expect(countRows(instance)).toBe(0);

        let capturedMs = COLLECTOR_CAPTURED_MS;
        for (const [name, s] of Object.entries(FIXED_COLLECTOR_OUTPUT)) {
          capturedMs += 1000;
          const res = await post(instance, asDelta(s, capturedMs));
          expect({ name, status: res.status }).toEqual({ name, status: 200 });
          const body = (await res.json()) as any;
          expect(body.routes.codex.status).toBe('error');
          expect(body.routes.codex.error).toBe('unavailable');
          expect(body.routes.codex.windows['5h']).toEqual({
            percent_remaining: null,
            reset_at: null,
            present: false,
            rolling: true,
            error: 'unavailable',
          });
          expect(body.routes.codex.windows['7d'].present).toBe(true);
        }

        // One bad Codex window no longer drops the good providers of the same snapshot
        const got = instance.quotaStore.getSnapshot(hostDelta.machineId).body as any;
        expect(got.captured_at).toBe(new Date(capturedMs).toISOString());
        expect(got.routes.claude.status).toBe('ok');
        expect(got.routes.claude.windows['5h'].percent_remaining).toBe(55);
        expect(got.routes.grok.status).toBe('ok');
        expect(got.routes.grok.windows['5h'].percent_remaining).toBe(92);
        expect(got.routes.codex.details).toEqual({ limit_reached: true });
        expect(countRows(instance)).toBe(1);
      } finally {
        instance.quotaStore.setClock(undefined);
      }
    });
  });
});
