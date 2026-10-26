import {freshCpu} from './cpu-fixture';
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv, createHelpers, loginAndGetState } from './m1-focus-setup';
import type { Hub } from '../src/hub';
import { evaluateQuotaAdmission } from '../src/quota_admission';
import { modelCandidates } from '../src/model-routing';

// M13 quota dashboard integration: real HTTP + SQLite. Offline fixtures only:
// no live API calls, no real tokens, no subprocess execution.
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
    sub: 'admin-quota-m13',
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
      new Request('http://example.com/auth/me', { headers: { Cookie: sessionCookie } })
    );
    const meBody = (await meRes.json()) as any;
    return { sessionCookie, csrfToken: meBody.csrf_token };
  } finally {
    globalThis.fetch = savedFetch;
  }
}

async function pairHost(name: string, hostname: string, sessionCookie: string, csrfToken: string) {
  const initRes = await doFetch(
    new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name, hostname, requested_scopes: ['host'] }),
    })
  );
  expect(initRes.status).toBe(200);
  const { device_code, user_code } = (await initRes.json()) as any;
  const approveRes = await doFetch(
    new Request(`http://example.com/api/device/approve/${user_code}`, {
      method: 'POST',
      headers: { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
      body: JSON.stringify({ granted_scopes: ['host'] }),
    })
  );
  expect(approveRes.status).toBe(200);
  const { machine_id } = (await approveRes.json()) as any;
  const tokenRes = await doFetch(
    new Request('http://example.com/api/device/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code }),
    })
  );
  expect(tokenRes.status).toBe(200);
  const { access_token } = (await tokenRes.json()) as any;
  const regRes = await doFetch(
    new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { Authorization: `Bearer ${access_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ hostname, os: 'linux', network: 'public', roles: ['worker'], capabilities: { launcher: '0.1.0' } }),
    })
  );
  expect(regRes.status).toBe(200);
  return { machineId: machine_id as string, accessToken: access_token as string };
}

async function pairAgent(actor: string, scopes: string[], sessionCookie: string, csrfToken: string) {
  const initRes = await doFetch(
    new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'agent', name: actor, actor, requested_scopes: scopes }),
    })
  );
  expect(initRes.status).toBe(200);
  const { device_code, user_code } = (await initRes.json()) as any;
  const approveRes = await doFetch(
    new Request(`http://example.com/api/device/approve/${user_code}`, {
      method: 'POST',
      headers: { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
      body: JSON.stringify({ granted_scopes: scopes }),
    })
  );
  expect(approveRes.status).toBe(200);
  const tokenRes = await doFetch(
    new Request('http://example.com/api/device/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code }),
    })
  );
  expect(tokenRes.status).toBe(200);
  const { access_token } = (await tokenRes.json()) as any;
  return access_token as string;
}

const isoNow = (deltaMs = 0) => new Date(Date.now() + deltaMs).toISOString();

function snap11(hostId: string, captured: string) {
  return {
    schema_version: '1.1',
    collector_version: 'quota_snapshot/1.1',
    captured_at: captured,
    host_id: hostId,
    status: 'ok',
    error: null,
    routes: {
      codex: {
        status: 'ok', error: null, observed_at: captured, details: { limit_reached: false },
        windows: {
          '7d': { percent_remaining: 78.5, reset_at: isoNow(86400000), present: true, rolling: false, credits_remaining: 12.5, spend_used: null, spend_limit: null, unit: 'credits', profile: null },
          '5h': { percent_remaining: null, reset_at: null, present: false, rolling: true },
        },
      },
      go: {
        status: 'ok', error: null, observed_at: captured,
        details: { limit_reached: false, spend_used: 150, spend_limit: 1000, unit: 'usd_cents', profile: 'opencode-go/muse-spark-1.3-contributor' },
        windows: {
          '7d': { percent_remaining: 72, reset_at: isoNow(86400000), present: true, rolling: false },
        },
      },
    },
  };
}

function quotaPost(token: string, snap: unknown) {
  return doFetch(
    new Request('http://example.com/api/quota', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(snap),
    })
  );
}

describe('M13 quota dashboard vertical slice (HTTP + SQLite)', () => {
  let sessionCookie = '';
  let csrfToken = '';

  beforeAll(async () => {
    ({ sessionCookie, csrfToken } = await getAdminSessionAndCsrf());
  });

  afterAll(() => {
    globalThis.fetch = originalFetch;
  });

  it('round-trips 1.1 native units and collector_version; 1.0 rejects new keys; secrets rejected', async () => {
    const host = await pairHost('m13-native-host', 'm13native.local', sessionCookie, csrfToken);
    const captured = isoNow(-1000);
    const postRes = await quotaPost(host.accessToken, snap11(host.machineId, captured));
    expect(postRes.status).toBe(200);
    const posted = (await postRes.json()) as any;
    expect(posted.schema_version).toBe('1.1');
    expect(posted.collector_version).toBe('quota_snapshot/1.1');

    const getRes = await doFetch(
      new Request('http://example.com/api/quota', { headers: { Authorization: `Bearer ${host.accessToken}` } })
    );
    expect(getRes.status).toBe(200);
    const body = (await getRes.json()) as any;
    expect(body.schema_version).toBe('1.1');
    expect(body.collector_version).toBe('quota_snapshot/1.1');
    expect(body.routes.codex.windows['7d'].credits_remaining).toBe(12.5);
    expect(body.routes.codex.windows['7d'].unit).toBe('credits');
    expect(body.routes.go.details.spend_used).toBe(150);
    expect(body.routes.go.details.spend_limit).toBe(1000);
    expect(body.routes.go.details.unit).toBe('usd_cents');
    expect(body.routes.go.details.profile).toBe('opencode-go/muse-spark-1.3-contributor');

    // Stored DTO keeps natives and carries no secret material.
    const stub = (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      const row = instance.sql.exec('SELECT routes_json, collector_version FROM host_quota_snapshots WHERE host_id = ?', host.machineId).one() as any;
      expect(row.collector_version).toBe('quota_snapshot/1.1');
      expect(row.routes_json).toContain('muse-spark-1.3-contributor');
      expect(row.routes_json).not.toMatch(/token|cookie|email|secret|bearer|authorization/i);
    });

    // Explicit version negotiation: 1.0 payloads reject the new keys.
    const v10 = { ...snap11(host.machineId, isoNow(-500)), schema_version: '1.0' };
    delete (v10 as any).collector_version;
    expect((await quotaPost(host.accessToken, v10)).status).toBe(400);

    // Secret and malformed native keys are rejected in 1.1 too.
    const badSecrets = snap11(host.machineId, isoNow(-400)) as any;
    badSecrets.routes.codex.details.access_token = 'secret';
    expect((await quotaPost(host.accessToken, badSecrets)).status).toBe(400);
    const badEmail = snap11(host.machineId, isoNow(-300)) as any;
    badEmail.routes.codex.windows['7d'].email = 'someone@example.com';
    expect((await quotaPost(host.accessToken, badEmail)).status).toBe(400);
    const badUnit = snap11(host.machineId, isoNow(-200)) as any;
    badUnit.routes.codex.windows['7d'].unit = 'dollars';
    expect((await quotaPost(host.accessToken, badUnit)).status).toBe(400);
    const badCredits = snap11(host.machineId, isoNow(-100)) as any;
    badCredits.routes.codex.windows['7d'].credits_remaining = -5;
    expect((await quotaPost(host.accessToken, badCredits)).status).toBe(400);
    const badProfile = snap11(host.machineId, isoNow()) as any;
    badProfile.routes.go.details.profile = 'not a profile!';
    expect((await quotaPost(host.accessToken, badProfile)).status).toBe(400);
  });

  it('fleet view dedupes a shared pool with min-remaining and provenance; filters and grants hold', async () => {
    const hostA = await pairHost('m13-fleet-a', 'm13a.local', sessionCookie, csrfToken);
    const hostB = await pairHost('m13-fleet-b', 'm13b.local', sessionCookie, csrfToken);
    const adminHeaders = { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken };
    const regRes = await doFetch(
      new Request('http://example.com/api/fleet/accounts', {
        method: 'PUT', headers: adminHeaders,
        body: JSON.stringify({
          accounts: [
            { label: 'm13-shared', provider: 'codex', max_concurrency: 4, reserve_percent: 15, enabled: true },
            { label: 'm13-zai', provider: 'zai', max_concurrency: 2, reserve_percent: 0, enabled: true },
          ],
          routes: [
            { machine_id: hostA.machineId, engine: 'codex', account: 'm13-shared', profile: null },
            { machine_id: hostB.machineId, engine: 'codex', account: 'm13-shared', profile: null },
            { machine_id: hostA.machineId, engine: 'zcodex', account: 'm13-zai', profile: 'zcodex' },
          ],
        }),
      })
    );
    expect(regRes.status).toBe(200);

    const sub = (percent: number, observed = isoNow(-1000)) => ({
      subscriptions: [{
        service: 'codex', account: 'm13-shared', plan: 'pro',
        quota: { status: 'ok', observed_at: observed, details: { limit_reached: false },
          windows: { weekly: { present: true, percent_remaining: percent, reset_at: isoNow(86400000) } } },
      }],
    });
    const putA = await doFetch(new Request('http://example.com/api/fleet/subscriptions', {
      method: 'PUT', headers: { Authorization: `Bearer ${hostA.accessToken}`, 'Content-Type': 'application/json' }, body: JSON.stringify(sub(80)),
    }));
    expect(putA.status).toBe(200);
    const putB = await doFetch(new Request('http://example.com/api/fleet/subscriptions', {
      method: 'PUT', headers: { Authorization: `Bearer ${hostB.accessToken}`, 'Content-Type': 'application/json' }, body: JSON.stringify(sub(60)),
    }));
    expect(putB.status).toBe(200);

    // Canonical binding: codex subscription cannot claim the zai account.
    const mismatch = await doFetch(new Request('http://example.com/api/fleet/subscriptions', {
      method: 'PUT', headers: { Authorization: `Bearer ${hostA.accessToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscriptions: [{ service: 'codex', account: 'm13-zai', plan: 'pro',
        quota: { status: 'ok', observed_at: isoNow(-1000), details: { limit_reached: false },
          windows: { weekly: { present: true, percent_remaining: 50, reset_at: isoNow(86400000) } } } }] }),
    }));
    expect(mismatch.status).toBe(400);

    const fleetRes = await doFetch(
      new Request('http://example.com/api/quota?view=fleet', { headers: { Cookie: sessionCookie } })
    );
    expect(fleetRes.status).toBe(200);
    const fleet = (await fleetRes.json()) as any;
    expect(fleet.schema_version).toBe('1.1');
    const pool = fleet.pools.find((p: any) => p.provider === 'codex' && p.account === 'm13-shared');
    expect(pool).toBeDefined();
    // One shared pool, conservative minimum, both hosts retained as provenance — never summed.
    expect(pool.state).toBe('fresh');
    expect(pool.remaining_percent).toBe(60);
    expect(pool.observed_by_hosts.sort()).toEqual([hostA.machineId, hostB.machineId].sort());

    // Filters narrow; unauthenticated callers are denied the fleet view. Host
    // bearers hold the read grant for this sanitized usage projection (AC5:
    // `hub quota` works for host-credential operators).
    const filtered = (await (await doFetch(new Request('http://example.com/api/quota?view=fleet&provider=zai', { headers: { Cookie: sessionCookie } }))).json()) as any;
    expect(filtered.pools.every((p: any) => p.provider === 'zai')).toBe(true);
    expect(filtered.pools.some((p: any) => p.provider === 'codex')).toBe(false);
    const badFilter = await doFetch(new Request('http://example.com/api/quota?view=fleet&provider=nope', { headers: { Cookie: sessionCookie } }));
    expect(badFilter.status).toBe(400);
    const hostView = await doFetch(new Request('http://example.com/api/quota?view=fleet', { headers: { Authorization: `Bearer ${hostA.accessToken}` } }));
    expect(hostView.status).toBe(200);
    const hostBody = (await hostView.json()) as any;
    expect(Array.isArray(hostBody.pools)).toBe(true);
    const anonDenied = await doFetch(new Request('http://example.com/api/quota?view=fleet'));
    expect(anonDenied.status).toBe(401);

    // Admin sessions keep the legacy contract: no host selector, no fleet flag → 400.
    const bareSession = await doFetch(new Request('http://example.com/api/quota', { headers: { Cookie: sessionCookie } }));
    expect(bareSession.status).toBe(400);

    // Assistant grants: fleet-launch agent and routing-client read; bus-only agent refused.
    const fleetAgent = await pairAgent('m13quota-agent', ['bus:send', 'bus:read', 'fleet:launch'], sessionCookie, csrfToken);
    const agentOk = await doFetch(new Request('http://example.com/api/quota?view=fleet', { headers: { Authorization: `Bearer ${fleetAgent}` } }));
    expect(agentOk.status).toBe(200);
    const busAgent = await pairAgent('m13quota-bus', ['bus:send', 'bus:read'], sessionCookie, csrfToken);
    const agentDenied = await doFetch(new Request('http://example.com/api/quota?view=fleet', { headers: { Authorization: `Bearer ${busAgent}` } }));
    expect(agentDenied.status).toBe(403);
    const rcRes = await doFetch(new Request('http://example.com/api/fleet/routing/clients/m13quota', {
      method: 'POST', headers: { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken }, body: '{}',
    }));
    expect(rcRes.status).toBe(201);
    const rcToken = ((await rcRes.json()) as any).token as string;
    const rcOk = await doFetch(new Request('http://example.com/api/quota?view=fleet', { headers: { Authorization: `Bearer ${rcToken}` } }));
    expect(rcOk.status).toBe(200);

    // Verify auto-discovery of codex-orin and snapshot fallback in fleet view
    const snapOrin = snap11(hostA.machineId, isoNow(-1000));
    (snapOrin.routes as any)['codex-orin'] = {
      status: 'ok',
      error: null,
      observed_at: null,
      details: { limit_reached: false },
      windows: {
        '7d': { percent_remaining: 99.0, reset_at: isoNow(86400000), present: true, rolling: false },
      },
    };
    expect((await quotaPost(hostA.accessToken, snapOrin)).status).toBe(200);
    const fleetWithOrin = (await (await doFetch(new Request('http://example.com/api/quota?view=fleet', { headers: { Cookie: sessionCookie } }))).json()) as any;
    // Canonical provider model: the second Codex home is provider `codex`
    // with its own account, never a separate `codex-orin` provider. Grouping
    // by provider shows both Codex accounts; every row exposes the harness.
    const orinPool = fleetWithOrin.pools.find((p: any) => p.account === 'codex-orin');
    expect(orinPool).toBeDefined();
    expect(orinPool.provider).toBe('codex');
    expect(orinPool.state).toBe('fresh');
    expect(orinPool.remaining_percent).toBe(99.0);
    expect(orinPool.observed_by_hosts).toContain(hostA.machineId);
    expect(orinPool.harness).toContain('codex');
    expect(fleetWithOrin.pools.some((p: any) => p.provider === 'codex-orin')).toBe(false);
    expect(fleetWithOrin.canonical.providers['codex-orin']).toBe('codex');
    // Legacy provider filter keeps working and selects both Codex accounts.
    const legacyFiltered = (await (await doFetch(new Request('http://example.com/api/quota?view=fleet&provider=codex-orin', { headers: { Cookie: sessionCookie } }))).json()) as any;
    expect(legacyFiltered.pools.length).toBeGreaterThan(0);
    expect(legacyFiltered.pools.every((p: any) => p.provider === 'codex')).toBe(true);

    // Revoked credentials lose the grant.
    const stub = (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec("UPDATE credentials SET revoked_at = ? WHERE kind = 'agent' AND actor = ?", new Date().toISOString(), 'm13quota-agent');
    });
    const revoked = await doFetch(new Request('http://example.com/api/quota?view=fleet', { headers: { Authorization: `Bearer ${fleetAgent}` } }));
    expect(revoked.status).toBe(401);
  });

  it('window error fails a sibling closed while benign absence stays eligible; reserves and reset rules hold', () => {
    const t = Date.now();
    const good = (windows: any, details: any = { limit_reached: false }, observedAt = new Date(t - 1000).toISOString()) => ({
      provider: 'codex', quota: { status: 'ok', observed_at: observedAt, details, windows },
      subscriptionObservedAt: new Date(t - 1000).toISOString(), nowMs: t, reserves: { codex: 15, grok: 5 },
    });
    const weekly = (pct: number) => ({ present: true, percent_remaining: pct, reset_at: new Date(t + 3600000).toISOString() });
    expect(good({ weekly: weekly(80) }).provider).toBe('codex');

    const ok = evaluateQuotaAdmission(good({ weekly: weekly(80) }));
    expect(ok.ok).toBe(true);
    expect(ok.remaining).toBe(80);

    const inactive = { present: true, percent_remaining: 100, reset_at: null };
    expect(evaluateQuotaAdmission({ ...good({ '5h': inactive, weekly: weekly(99) }), provider: 'claude' })).toMatchObject({ ok: true, remaining: 99 });
    expect(evaluateQuotaAdmission({ ...good({ '5h': { ...inactive, percent_remaining: 99 }, weekly: weekly(99) }), provider: 'claude' }).ok).toBe(false);
    expect(evaluateQuotaAdmission(good({ '5h': inactive, weekly: weekly(99) })).ok).toBe(false);
    expect(evaluateQuotaAdmission({ ...good({ '5h': inactive }), provider: 'claude' }).ok).toBe(false);
    expect(evaluateQuotaAdmission({ ...good({ '5h': inactive, weekly: { ...weekly(99), reset_at: null } }), provider: 'claude' }).ok).toBe(false);

    // Genuine unavailable sibling fails closed even with a healthy 80% weekly window.
    const sibling = evaluateQuotaAdmission(good({
      five_hour: { present: false, percent_remaining: null, reset_at: null, error: 'unavailable' },
      weekly: weekly(80),
    }));
    expect(sibling.ok).toBe(false);
    expect(sibling.reason).toBe('window_unavailable');

    // Benign absence of an optional unsupported window is ignored.
    const benign = evaluateQuotaAdmission(good({
      five_hour: { present: false, percent_remaining: null, reset_at: null },
      weekly: weekly(80),
    }));
    expect(benign.ok).toBe(true);

    // Reserves apply to every available applicable window.
    expect(evaluateQuotaAdmission(good({ weekly: weekly(15) })).ok).toBe(false);
    expect(evaluateQuotaAdmission(good({ weekly: weekly(15) })).reason).toBe('reserve');
    expect(evaluateQuotaAdmission(good({ weekly: weekly(15.1) })).ok).toBe(true);
    const grokLow = evaluateQuotaAdmission({ ...good({ weekly: weekly(5) }), provider: 'grok',
      quota: { ...good({ weekly: weekly(5) }).quota, details: { limit_reached: false, has_grok_code_access: true } } });
    expect(grokLow.ok).toBe(false);
    expect(grokLow.reason).toBe('reserve');

    // Known false required: null/true limit_reached and missing Grok entitlement fail.
    expect(evaluateQuotaAdmission(good({ weekly: weekly(80) }, { limit_reached: null })).reason).toBe('malformed');
    expect(evaluateQuotaAdmission(good({ weekly: weekly(80) }, { limit_reached: true })).reason).toBe('limit_reached');
    expect(evaluateQuotaAdmission({ ...good({ weekly: weekly(80) }), provider: 'grok' }).reason).toBe('grok_entitlement');

    // Stale, future and malformed observations fail; at least one available window required.
    expect(evaluateQuotaAdmission(good({ weekly: weekly(80) }, { limit_reached: false }, new Date(t - 1300000).toISOString())).reason).toBe('stale_observation');
    expect(evaluateQuotaAdmission(good({ weekly: weekly(80) }, { limit_reached: false }, new Date(t + 60000).toISOString())).reason).toBe('future_observation');
    expect(evaluateQuotaAdmission(good({ weekly: { present: true, percent_remaining: Number.NaN, reset_at: new Date(t + 3600000).toISOString() } })).reason).toBe('malformed');
    expect(evaluateQuotaAdmission(good({ weekly: { present: true, percent_remaining: true as any, reset_at: new Date(t + 3600000).toISOString() } })).reason).toBe('malformed');
    expect(evaluateQuotaAdmission(good({ weekly: { present: true, percent_remaining: 101, reset_at: new Date(t + 3600000).toISOString() } })).reason).toBe('malformed');
    expect(evaluateQuotaAdmission(good({ weekly: { present: true, percent_remaining: 80, reset_at: 'not-a-date' } })).reason).toBe('malformed');
    expect(evaluateQuotaAdmission(good({ five_hour: { present: false, percent_remaining: null, reset_at: null } })).reason).toBe('no_available_window');

    // A passed reset without a post-reset measurement fails; a post-reset measurement admits.
    const passedReset = new Date(t - 600000).toISOString();
    const preResetObs = new Date(t - 900000).toISOString();
    const postResetObs = new Date(t - 300000).toISOString();
    const resetFail = evaluateQuotaAdmission(good(
      { weekly: { present: true, percent_remaining: 80, reset_at: passedReset } }, { limit_reached: false }, preResetObs));
    expect(resetFail.ok).toBe(false);
    expect(resetFail.reason).toBe('reset_passed_since_observation');
    const resetOk = evaluateQuotaAdmission({ ...good(
      { weekly: { present: true, percent_remaining: 80, reset_at: passedReset } }, { limit_reached: false }, postResetObs),
      subscriptionObservedAt: postResetObs });
    expect(resetOk.ok).toBe(true);

    // A nulled shortlist admits nothing downstream: null quota yields no model candidates.
    expect(modelCandidates({ tier: 'daily' }, { provider: 'codex', engine: 'codex', profile: null }, [], null, t)).toEqual([]);
  });

  it('exhausted quota refuses plan and atomic claim; fresh evidence recovers', async () => {
    const host = await pairHost('m13-claim-host', 'm13claim.local', sessionCookie, csrfToken);
    const adminHeaders = { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken };
    // Fleet admission gates on machine capabilities, heartbeat gate and resources: seed them directly.
    const stub = (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(
        "UPDATE machines SET last_checkin_at = ?, last_heartbeat = ?, capabilities = ? WHERE id = ?",
        isoNow(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(isoNow(),4), gate: { launch_allowed: true, at: isoNow() } }),
        JSON.stringify({ engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 4 }), host.machineId);
    });
    expect((await doFetch(new Request('http://example.com/api/fleet/accounts', {
      method: 'PUT', headers: adminHeaders,
      body: JSON.stringify({
        accounts: [{ label: 'm13-claim', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true }],
        routes: [{ machine_id: host.machineId, engine: 'codex', account: 'm13-claim', profile: null }],
      }),
    }))).status).toBe(200);

    const quota = (limit: boolean, pct = 80) => ({
      subscriptions: [{ service: 'codex', account: 'm13-claim', plan: 'pro',
        quota: { status: 'ok', observed_at: isoNow(-1000), details: { limit_reached: limit },
          windows: { weekly: { present: true, percent_remaining: pct, reset_at: isoNow(86400000) } } } }],
    });
    const authSub = { Authorization: `Bearer ${host.accessToken}`, 'Content-Type': 'application/json' };
    const putSub = (body: unknown) => doFetch(new Request('http://example.com/api/fleet/subscriptions', {
      method: 'PUT', headers: authSub, body: JSON.stringify(body),
    }));
    expect((await putSub(quota(false))).status).toBe(200);

    const launchBody = (key: string) => ({
      method: 'POST', headers: adminHeaders,
      body: JSON.stringify({ workspace: 'demo', prompt: 'm13 claim proof', idempotency_key: key }),
    });
    const launchRes = await doFetch(new Request('http://example.com/api/fleet/launches', launchBody('m13-claim-1') as RequestInit));
    expect(launchRes.status).toBe(201);
    const launch = (await launchRes.json()) as any;

    // Exhaust the engine: plan refuses and the atomic claim re-check refuses too.
    expect((await putSub(quota(true))).status).toBe(200);
    const planned = (await (await doFetch(new Request('http://example.com/api/fleet/plan', launchBody('m13-claim-2') as RequestInit))).json()) as any;
    expect(planned.selected).toBeFalsy();
    const claimRes = await doFetch(new Request(`http://example.com/api/fleet/launches/${launch.id}/claim`, {
      method: 'POST', headers: authSub,
      body: JSON.stringify({ gate_allowed: true, model: 'gpt-6.1-sol', observed_at: isoNow(), account: 'm13-claim', profile_supported: true }),
    }));
    expect(claimRes.status).toBe(409);

    // Fresh post-exhaustion evidence recovers both plan and claim.
    expect((await putSub(quota(false))).status).toBe(200);
    const planned2 = (await (await doFetch(new Request('http://example.com/api/fleet/plan', launchBody('m13-claim-3') as RequestInit))).json()) as any;
    expect(planned2.selected).toBeTruthy();
    const claimRes2 = await doFetch(new Request(`http://example.com/api/fleet/launches/${launch.id}/claim`, {
      method: 'POST', headers: authSub,
      body: JSON.stringify({ gate_allowed: true, model: 'gpt-6.1-sol', observed_at: isoNow(), account: 'm13-claim', profile_supported: true }),
    }));
    expect(claimRes2.status).toBe(200);
    // Finish the launch so later registry writes are not held by an active launch.
    const doneRes = await doFetch(new Request(`http://example.com/api/fleet/launches/${launch.id}/result`, {
      method: 'POST', headers: authSub,
      body: JSON.stringify({ state: 'exited', confirmed_exit: true }),
    }));
    expect(doneRes.status).toBe(200);
  });

  it('cooldown hold blocks plan and claim even with healthy percent; retention, update and expiry behave', async () => {
    const t = Date.now();
    const held = (cd: string | null) => ({
      provider: 'codex',
      quota: { status: 'ok', observed_at: new Date(t - 1000).toISOString(),
        details: { limit_reached: false, cooldown_until: cd },
        windows: { weekly: { present: true, percent_remaining: 80, reset_at: new Date(t + 3600000).toISOString() } } },
      subscriptionObservedAt: new Date(t - 1000).toISOString(), nowMs: t, reserves: { codex: 15, grok: 5 },
    });
    expect(evaluateQuotaAdmission(held(new Date(t + 60000).toISOString())).reason).toBe('cooldown_active');
    expect(evaluateQuotaAdmission(held(new Date(t - 1000).toISOString())).reason).toBe('eligible');
    expect(evaluateQuotaAdmission(held(null)).reason).toBe('eligible');
    expect(evaluateQuotaAdmission(held('soon')).reason).toBe('malformed');

    const host = await pairHost('m13-hold-host', 'm13hold.local', sessionCookie, csrfToken);
    const adminHeaders = { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken };
    const stub = (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(
        "UPDATE machines SET last_checkin_at = ?, last_heartbeat = ?, capabilities = ? WHERE id = ?",
        isoNow(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(isoNow(),4), gate: { launch_allowed: true, at: isoNow() } }),
        JSON.stringify({ engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 4 }), host.machineId);
    });
    expect((await doFetch(new Request('http://example.com/api/fleet/accounts', {
      method: 'PUT', headers: adminHeaders,
      body: JSON.stringify({
        accounts: [{ label: 'm13-hold', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true }],
        routes: [{ machine_id: host.machineId, engine: 'codex', account: 'm13-hold', profile: null }],
      }),
    }))).status).toBe(200);

    const subWith = (details: unknown) => ({
      subscriptions: [{ service: 'codex', account: 'm13-hold', plan: 'pro',
        quota: { status: 'ok', observed_at: isoNow(-1000), details,
          windows: { weekly: { present: true, percent_remaining: 80, reset_at: isoNow(86400000) } } } }],
    });
    const authSub = { Authorization: `Bearer ${host.accessToken}`, 'Content-Type': 'application/json' };
    const putSub = (body: unknown) => doFetch(new Request('http://example.com/api/fleet/subscriptions', {
      method: 'PUT', headers: authSub, body: JSON.stringify(body),
    }));
    const planNow = async (key: string) => (await (await doFetch(new Request('http://example.com/api/fleet/plan', {
      method: 'POST', headers: adminHeaders, body: JSON.stringify({ workspace: 'demo', prompt: 'm13 hold proof', idempotency_key: key }),
    }))).json()) as any;

    // Known active hold blocks even with a healthy 80%.
    expect((await putSub(subWith({ limit_reached: false, cooldown_until: isoNow(60000) }))).status).toBe(200);
    expect((await planNow('m13-hold-1')).selected).toBeFalsy();

    // A later report that omits the field must not erase the unexpired hold.
    expect((await putSub(subWith({ limit_reached: false }))).status).toBe(200);
    const stored: any = await runInDurableObject(stub, async (instance: Hub) =>
      JSON.parse((instance.sql.exec('SELECT quota_json FROM fleet_subscriptions WHERE machine_id = ? AND account = ?', host.machineId, 'm13-hold').one() as any).quota_json));
    expect(typeof stored.details.cooldown_until).toBe('string');
    expect((await planNow('m13-hold-2')).selected).toBeFalsy();

    // An updated deadline is honored; an expired deadline with fresh percent recovers.
    expect((await putSub(subWith({ limit_reached: false, cooldown_until: isoNow(120000) }))).status).toBe(200);
    expect((await planNow('m13-hold-3')).selected).toBeFalsy();
    expect((await putSub(subWith({ limit_reached: false, cooldown_until: isoNow(-1000) }))).status).toBe(200);
    expect((await planNow('m13-hold-4')).selected).toBeTruthy();

    // Malformed deadlines never enter storage.
    expect((await putSub(subWith({ limit_reached: false, cooldown_until: 'soon' }))).status).toBe(400);
  });

  it('modelCandidates admits only eligible routes through the canonical gate', async () => {
    const t = Date.now();
    // Grok is disabled by canonical model policy (no-new-Grok steering), so
    // placement eligibility is proven on go/codex routes instead.
    const goAd = [{ engine: 'opencode', profile: null, model: 'opencode-go/muse-spark-1.3-contributor', effort: null, account: 'm13-g' }];
    const goRoute = { provider: 'go', engine: 'opencode', profile: null, account: 'm13-g' };
    const quotaFor = (pct: number | null, details: unknown = { limit_reached: false }) => ({
      status: 'ok', observed_at: new Date(t - 1000).toISOString(), details,
      windows: { '7d': { present: true, percent_remaining: pct, reset_at: new Date(t + 3600000).toISOString() } },
    });
    expect(modelCandidates({ tier: 'daily' }, goRoute, goAd, quotaFor(80), t).length).toBeGreaterThan(0);
    // Below the Codex 15% reserve, and with an unavailable sibling, no model is offered.
    const codexAd = [{ engine: 'codex', profile: null, model: 'gpt-6.1-sol', effort: 'high', account: 'm13-c' }];
    const codexRoute = { provider: 'codex', engine: 'codex', profile: null, account: 'm13-c' };
    const codexInput = { tier: 'daily', preferred_runner: 'codex' };
    expect(modelCandidates(codexInput, codexRoute, codexAd, quotaFor(10), t)).toEqual([]);
    expect(modelCandidates(codexInput, codexRoute, codexAd, {
      status: 'ok', observed_at: new Date(t - 1000).toISOString(), details: { limit_reached: false },
      windows: {
        '5h': { present: false, percent_remaining: null, reset_at: null, error: 'unavailable' },
        weekly: { present: true, percent_remaining: 80, reset_at: new Date(t + 3600000).toISOString() },
      },
    }, t)).toEqual([]);
    // Disabled-provider engines offer no models even with healthy quota.
    const grokAd = [{ engine: 'grok', profile: null, model: 'grok-4.6', effort: 'high', account: 'm13-g' }];
    const grokRoute = { provider: 'grok', engine: 'grok', profile: null, account: 'm13-g' };
    expect(modelCandidates({ tier: 'daily' }, grokRoute, grokAd,
      quotaFor(80, { limit_reached: false, has_grok_code_access: true }), t)).toEqual([]);
  });

  it('disabled grok engine refuses new reserve and pre-hold claim; replay and running results preserved', async () => {
    const host = await pairHost('m13-grokhold-host', 'm13grokhold.local', sessionCookie, csrfToken);
    const adminHeaders = { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken };
    const stub = (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(
        "UPDATE machines SET last_checkin_at = ?, last_heartbeat = ?, capabilities = ? WHERE id = ?",
        isoNow(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(isoNow(),4), gate: { launch_allowed: true, at: isoNow() } }),
        JSON.stringify({ engines: ['grok', 'codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 4 }), host.machineId);
    });
    // A differently labelled grok account is still held: the engine decides, not the label.
    expect((await doFetch(new Request('http://example.com/api/fleet/accounts', {
      method: 'PUT', headers: adminHeaders,
      body: JSON.stringify({
        accounts: [
          { label: 'm13-grok-main', provider: 'grok', max_concurrency: 2, reserve_percent: 5, enabled: true },
          { label: 'm13-codex-ok', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true },
        ],
        routes: [
          { machine_id: host.machineId, engine: 'grok', account: 'm13-grok-main', profile: null },
          { machine_id: host.machineId, engine: 'codex', account: 'm13-codex-ok', profile: null },
        ],
      }),
    }))).status).toBe(200);
    const quota = (service: string, account: string) => ({
      subscriptions: [{ service, account, plan: 'pro',
        quota: { status: 'ok', observed_at: isoNow(-1000),
          details: service === 'grok' ? { limit_reached: false, has_grok_code_access: true } : { limit_reached: false },
          windows: { weekly: { present: true, percent_remaining: 80, reset_at: isoNow(86400000) } } } }],
    });
    const authSub = { Authorization: `Bearer ${host.accessToken}`, 'Content-Type': 'application/json' };
    expect((await doFetch(new Request('http://example.com/api/fleet/subscriptions', {
      method: 'PUT', headers: authSub, body: JSON.stringify(quota('grok', 'm13-grok-main')),
    }))).status).toBe(200);
    expect((await doFetch(new Request('http://example.com/api/fleet/subscriptions', {
      method: 'PUT', headers: authSub, body: JSON.stringify(quota('codex', 'm13-codex-ok')),
    }))).status).toBe(200);

    const launchBody = (key: string, engine?: string) => ({
      method: 'POST', headers: adminHeaders,
      body: JSON.stringify({ workspace: 'demo', prompt: 'm13 grok hold proof', idempotency_key: key, ...(engine ? { engine } : {}) }),
    }) as RequestInit;
    // Direct reserve on the held engine is refused even with healthy quota.
    const refused = await doFetch(new Request('http://example.com/api/fleet/launches', launchBody('m13-grok-1', 'grok')));
    expect(refused.status).toBe(409);
    expect(((await refused.json()) as any).error).toBe('provider_disabled');
    // The allowed engine still reserves.
    const okRes = await doFetch(new Request('http://example.com/api/fleet/launches', launchBody('m13-codex-1', 'codex')));
    expect(okRes.status).toBe(201);

    // A pre-hold grok offer (seeded as offered) cannot be claimed, but replays
    // idempotently and a running launch still reports results (telemetry preserved).
    const offerId = crypto.randomUUID();
    // Byte-exact canonical bound (fleet.ts reserve: plan.request key order + access).
    const canonicalIntent = JSON.stringify({ mode: 'headless', machine: null, engine: null, workspace: 'demo', cwd: null,
      prompt: 'm13 grok hold proof', parent_id: null, summary: 'Agent task',
      access: { mode: 'legacy_unspecified', claim_id: null, project: null, scopes: [] } });
    await runInDurableObject(stub, async (instance: Hub) => {
      const at = isoNow();
      instance.sql.exec(
        "INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        offerId, 'human:admin-quota-m13', 'human', null, 'pre-hold grok', 'm13-grok-pre', canonicalIntent,
        host.machineId, 'grok', 'm13-grok-main', null, 'headless', 'demo', 'pre-hold', 'offered', at, at);
    });
    const seededCheck: any = await runInDurableObject(stub, async (instance: Hub) =>
      instance.sql.exec('SELECT actor, request_key, state FROM fleet_launches WHERE id = ?', offerId).one());
    expect(seededCheck).toMatchObject({ actor: 'human:admin-quota-m13', request_key: 'm13-grok-pre', state: 'offered' });
    const claimRes = await doFetch(new Request(`http://example.com/api/fleet/launches/${offerId}/claim`, {
      method: 'POST', headers: authSub,
      body: JSON.stringify({ gate_allowed: true, model: 'gpt-6.1-sol', observed_at: isoNow(), account: 'm13-grok-main', profile_supported: true }),
    }));
    expect(claimRes.status).toBe(409);
    expect(((await claimRes.json()) as any).error).toBe('provider_disabled');
    const replayRes = await doFetch(new Request('http://example.com/api/fleet/launches', launchBody('m13-grok-pre')));
    const replayBody = (await replayRes.json()) as any;
    const diag: any = await runInDurableObject(stub, async (instance: Hub) =>
      instance.sql.exec("SELECT actor, request_key, state FROM fleet_launches WHERE request_key = 'm13-grok-pre' ORDER BY created_at").toArray());
    expect(diag.map((r: any) => r.actor)).toEqual(['human:admin-quota-m13']);
    expect(replayRes.status).toBe(200);
    expect(replayBody.replay).toBe(true);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec("UPDATE fleet_launches SET state = 'running', native_id = ? WHERE id = ?", 'native-1', offerId);
    });
    const resultRes = await doFetch(new Request(`http://example.com/api/fleet/launches/${offerId}/result`, {
      method: 'POST', headers: authSub,
      body: JSON.stringify({ state: 'uncertain' }),
    }));
    expect(resultRes.status).toBe(200);
  });

  it('stored quota DTOs carry no secret material', async () => {
    const host = await pairHost('m13-privacy-host', 'm13privacy.local', sessionCookie, csrfToken);
    expect((await quotaPost(host.accessToken, snap11(host.machineId, isoNow(-1000)))).status).toBe(200);
    const stub = (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      const blobs: string[] = [];
      for (const row of instance.sql.exec('SELECT routes_json FROM host_quota_snapshots').toArray() as any[]) blobs.push(row.routes_json);
      for (const row of instance.sql.exec('SELECT quota_json FROM fleet_subscriptions').toArray() as any[]) blobs.push(row.quota_json);
      expect(blobs.length).toBeGreaterThan(0);
      for (const blob of blobs) {
        expect(blob).not.toMatch(/access_token|refresh_token|cookie|email|secret|bearer|authorization|account_id|organization_id|raw_output/i);
      }
    });
  });
});
