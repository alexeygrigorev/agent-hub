import { describe, it, expect, beforeAll } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv, createHelpers, loginAndGetState } from './m1-focus-setup';
import { Hub } from '../src/hub';
import { METRICS_MAX_BODY_BYTES, WINDOW_HOURS, HOUR_MS } from '../src/metrics';
import { sha256 } from '../src/auth/crypto';

const CANONICAL = ['agent-branches', 'agent-dashboard', 'quota-launcher', 'agent-coordination'] as const;

let stableHelpersPromise: ReturnType<typeof createHelpers> | null = null;
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
    sub: 'admin-metrics-test',
  });
  const savedFetch = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL) => {
    if (input.toString().includes('/oauth2/token')) {
      return new Response(JSON.stringify({ id_token: idToken }), { status: 200 });
    }
    if (input.toString().includes('/.well-known/jwks.json')) {
      return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
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
    if (cbRes.status >= 400) {
      throw new Error(`callback ${cbRes.status}`);
    }
    const sessionCookie = cbRes.headers.get('Set-Cookie')!.split(';')[0];
    const meRes = await doFetch(new Request('http://example.com/auth/me', { headers: { Cookie: sessionCookie } }));
    if (meRes.status !== 200) {
      throw new Error(`auth/me ${meRes.status}`);
    }
    const meBody = (await meRes.json()) as { csrf_token: string };
    return { sessionCookie, csrfToken: meBody.csrf_token };
  } finally {
    globalThis.fetch = savedFetch;
  }
}

async function pairHost(name: string, hostname: string): Promise<{ machineId: string; accessToken: string }> {
  const initRes = await doFetch(
    new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name, hostname, requested_scopes: ['host'] }),
    })
  );
  expect(initRes.status).toBe(200);
  const { device_code, user_code } = (await initRes.json()) as { device_code: string; user_code: string };
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
  const machineId = ((await approveRes.json()) as { machine_id: string }).machine_id;
  const tokenRes = await doFetch(
    new Request('http://example.com/api/device/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code }),
    })
  );
  expect(tokenRes.status).toBe(200);
  const tokenBody = (await tokenRes.json()) as { access_token: string };
  const regRes = await doFetch(
    new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { Authorization: `Bearer ${tokenBody.access_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        hostname,
        os: 'linux',
        network: 'public',
        roles: ['worker'],
        capabilities: { launcher: '0.1.0', engines: ['grok'], jobs: ['root-run'], aplexer: true },
      }),
    })
  );
  expect(regRes.status).toBe(200);
  return { machineId, accessToken: tokenBody.access_token };
}

async function pairAgent(): Promise<string> {
  const initRes = await doFetch(
    new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_kind: 'agent',
        name: 'metrics-agent',
        requested_scopes: ['bus:read', 'bus:send'],
      }),
    })
  );
  const { device_code, user_code } = (await initRes.json()) as { device_code: string; user_code: string };
  const { sessionCookie, csrfToken } = await getAdminSessionAndCsrf();
  expect(
    (
      await doFetch(
        new Request(`http://example.com/api/device/approve/${user_code}`, {
          method: 'POST',
          headers: {
            Cookie: sessionCookie,
            'Content-Type': 'application/json',
            'x-csrf-token': csrfToken,
          },
          body: JSON.stringify({ granted_scopes: ['bus:read', 'bus:send'] }),
        })
      )
    ).status
  ).toBe(200);
  const tokenRes = await doFetch(
    new Request('http://example.com/api/device/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code }),
    })
  );
  return ((await tokenRes.json()) as { access_token: string }).access_token;
}

function buckets(asOfMs: number, unknown = false) {
  const start = asOfMs - WINDOW_HOURS * HOUR_MS;
  return Array.from({ length: WINDOW_HOURS }, (_, i) => ({
    bucket_start: new Date(start + i * HOUR_MS).toISOString(),
    bucket_end: new Date(start + (i + 1) * HOUR_MS).toISOString(),
    active_agents: unknown ? null : i === WINDOW_HOURS - 1 ? 1 : 0,
    agent_hours: unknown ? null : i === WINDOW_HOURS - 1 ? 0.25 : 0,
  }));
}

function utilProject(asOfMs: number, unknown = false) {
  return {
    unknown,
    unique_agents: unknown ? null : 1,
    total_agent_hours: unknown ? null : 0.25,
    coverage: unknown ? null : 0.01,
    unattributed_agent_hours: unknown ? null : 0,
    hourly_buckets: buckets(asOfMs, unknown),
  };
}

function usageProject(unknown = false) {
  return {
    unknown,
    total_input_tokens: unknown ? null : 10,
    total_output_tokens: unknown ? null : 4,
    total_cache_read_tokens: null,
    total_cache_creation_tokens: null,
    total_reasoning_tokens: null,
    total_noncache_input_tokens: null,
    has_unknown_tokens: false,
    has_unknown_cache: true,
    has_unknown_reasoning: true,
    cache_coverage: null,
    reasoning_coverage: null,
    event_count: unknown ? 0 : 1,
  };
}

function acceptedProject(unknown = false) {
  return {
    unknown,
    accepted_outcomes: unknown ? null : 1,
    task_count: unknown ? null : 2,
    commit_count: unknown ? null : 1,
    review_count: unknown ? null : 1,
  };
}

function buildSnapshot(hostId: string, asOfIso: string, key: string, extras: Record<string, unknown> = {}) {
  const asOfMs = Date.parse(asOfIso);
  const projectsUtil: Record<string, unknown> = {};
  const projectsUsage: Record<string, unknown> = {};
  const projectsAccepted: Record<string, unknown> = {};
  for (const id of CANONICAL) {
    projectsUtil[id] = utilProject(asOfMs, id !== 'agent-branches');
    projectsUsage[id] = usageProject(id !== 'agent-branches');
    projectsAccepted[id] = acceptedProject(id !== 'agent-branches');
  }
  return {
    schema_version: '1.0',
    as_of: asOfIso,
    host_id: hostId,
    request_key: key,
    coverage: { utilization: 'partial', usage: 'partial', accepted: 'partial' },
    gaps: [{ code: 'missing' }],
    utilization: { unknown: false, projects: projectsUtil },
    usage: { unknown: false, projects: projectsUsage },
    accepted: {
      unknown: false,
      accepted_outcomes: 1,
      task_count: 2,
      commit_count: 1,
      review_count: 1,
      projects: projectsAccepted,
    },
    ...extras,
  };
}

function post(token: string, body: unknown) {
  return doFetch(
    new Request('http://example.com/api/metrics', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    })
  );
}

describe('M9 metrics receiver', () => {
  let stub: any;
  let hostA: { machineId: string; accessToken: string };
  let hostB: { machineId: string; accessToken: string };
  let agentToken: string;
  const asOfMs = Math.floor(Date.now() / HOUR_MS) * HOUR_MS - 48 * HOUR_MS;
  const asOf = new Date(asOfMs).toISOString();
  const hourStamp = (offsetHours: number) => new Date(asOfMs + offsetHours * HOUR_MS).toISOString();

  beforeAll(async () => {
    stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub'));
    hostA = await pairHost('metrics-a', 'a.metrics.local');
    hostB = await pairHost('metrics-b', 'b.metrics.local');
    agentToken = await pairAgent();
  });

  it('rejects unauthenticated and non-host writes; admin session cannot write', async () => {
    expect((await doFetch(new Request('http://example.com/api/metrics'))).status).toBe(401);
    expect((await post(agentToken, buildSnapshot(hostA.machineId, asOf, 'metrics:agent-deny-1'))).status).toBe(403);
    const { sessionCookie, csrfToken } = await getAdminSessionAndCsrf();
    const adminPost = await doFetch(
      new Request('http://example.com/api/metrics', {
        method: 'POST',
        headers: {
          Cookie: sessionCookie,
          'Content-Type': 'application/json',
          'x-csrf-token': csrfToken,
        },
        body: JSON.stringify(buildSnapshot(hostA.machineId, asOf, 'metrics:admin-deny-1')),
      })
    );
    expect(adminPost.status).toBe(403);
    const body = (await adminPost.json()) as { reason?: string };
    expect(JSON.stringify(body)).not.toContain('metrics:admin-deny-1');
  });

  it('accepts own host snapshot, retries the same key, and refuses older as_of', async () => {
    const snap = buildSnapshot(hostA.machineId, asOf, 'metrics:host-a-1');
    const first = await post(hostA.accessToken, snap);
    expect(first.status).toBe(200);
    const firstBody = (await first.json()) as { host_id: string; as_of: string; accepted: { accepted_outcomes: number } };
    expect(firstBody.host_id).toBe(hostA.machineId);
    expect(firstBody.as_of).toBe(asOf);
    expect(firstBody.accepted.accepted_outcomes).toBe(1);
    const retry = await post(hostA.accessToken, snap);
    expect(retry.status).toBe(200);
    const conflict = await post(hostA.accessToken, { ...snap, coverage: { utilization: 'observed', usage: 'observed', accepted: 'observed' } });
    expect(conflict.status).toBe(409);
    const older = await post(
      hostA.accessToken,
      buildSnapshot(hostA.machineId, hourStamp(-1), 'metrics:host-a-old')
    );
    expect(older.status).toBe(409);
    const ownGet = await doFetch(
      new Request('http://example.com/api/metrics', { headers: { Authorization: `Bearer ${hostA.accessToken}` } })
    );
    expect(ownGet.status).toBe(200);
    const got = (await ownGet.json()) as { as_of: string };
    expect(got.as_of).toBe(asOf);
  });

  it('refuses foreign host_id, agent read of another host, and unknown/private fields', async () => {
    const foreign = await post(
      hostA.accessToken,
      buildSnapshot(hostB.machineId, hourStamp(1), 'metrics:foreign-1')
    );
    expect(foreign.status).toBe(403);
    const secret = await post(hostA.accessToken, {
      ...buildSnapshot(hostA.machineId, hourStamp(1), 'metrics:secret-1'),
      path: '/SECRET_MARKER_PRIVATE/usage.json',
    });
    expect(secret.status).toBe(400);
    const secretBody = await secret.text();
    expect(secretBody).not.toContain('SECRET_MARKER_PRIVATE');
    expect(secretBody).not.toContain('/SECRET');
    const nested = await post(hostA.accessToken, {
      ...buildSnapshot(hostA.machineId, hourStamp(1), 'metrics:quota-1'),
      usage: {
        unknown: false,
        projects: {
          'agent-branches': { ...usageProject(false), total_quota_delta_percent: 12 },
        },
      },
    });
    expect(nested.status).toBe(400);
    const crossGet = await doFetch(
      new Request(`http://example.com/api/metrics?host_id=${encodeURIComponent(hostB.machineId)}`, {
        headers: { Authorization: `Bearer ${hostA.accessToken}` },
      })
    );
    expect(crossGet.status).toBe(403);
    const agentGet = await doFetch(
      new Request('http://example.com/api/metrics', { headers: { Authorization: `Bearer ${agentToken}` } })
    );
    expect(agentGet.status).toBe(403);
  });

  it('rejects invalid schema, non-UTC stamps, extra hours, and oversized streams', async () => {
    const badStamp = await post(
      hostB.accessToken,
      buildSnapshot(hostB.machineId, '2026-10-17T12:00:00', 'metrics:naive-1')
    );
    expect(badStamp.status).toBe(400);
    const boolCount = buildSnapshot(hostB.machineId, asOf, 'metrics:bool-1');
    (boolCount.usage as { projects: Record<string, { total_input_tokens: unknown }> }).projects['agent-branches'].total_input_tokens = true;
    expect((await post(hostB.accessToken, boolCount)).status).toBe(400);
    const extraHour = buildSnapshot(hostB.machineId, asOf, 'metrics:hours-1');
    const util = extraHour.utilization as { projects: Record<string, { hourly_buckets: unknown[] }> };
    util.projects['agent-branches'].hourly_buckets.push({
      bucket_start: '2026-10-17T12:00:00.000Z',
      bucket_end: '2026-10-17T13:00:00.000Z',
      active_agents: 0,
      agent_hours: 0,
    });
    expect((await post(hostB.accessToken, extraHour)).status).toBe(400);
    const huge = 'x'.repeat(METRICS_MAX_BODY_BYTES + 8);
    const stream = await doFetch(
      new Request('http://example.com/api/metrics', {
        method: 'POST',
        headers: { Authorization: `Bearer ${hostB.accessToken}`, 'Content-Type': 'application/json' },
        body: `{"schema_version":"1.0","pad":"${huge}"}`,
      })
    );
    expect(stream.status).toBe(413);
  });

  it('binds current machine credential, drops rebound snapshots, and keeps latest across a DO turn', async () => {
    const snap = buildSnapshot(hostB.machineId, hourStamp(2), 'metrics:host-b-live');
    expect((await post(hostB.accessToken, snap)).status).toBe(200);
    await runInDurableObject(stub, async (instance: Hub) => {
      const row = instance.sql
        .exec('SELECT credential_id FROM machines WHERE id = ?', hostB.machineId)
        .toArray()[0] as { credential_id: string };
      instance.sql.exec('UPDATE machines SET credential_id = ? WHERE id = ?', 'cred_rebound_other', hostB.machineId);
      const rebound = await instance.metricsStore.handleRequest(
        new Request('http://example.com/api/metrics', {
          method: 'POST',
          headers: { Authorization: `Bearer ${hostB.accessToken}`, 'Content-Type': 'application/json' },
          body: JSON.stringify(buildSnapshot(hostB.machineId, hourStamp(3), 'metrics:host-b-rebound')),
        })
      );
      expect(rebound.status).toBe(403);
      instance.sql.exec('UPDATE machines SET credential_id = ? WHERE id = ?', row.credential_id, hostB.machineId);
    });
    const after = await doFetch(
      new Request('http://example.com/api/metrics', { headers: { Authorization: `Bearer ${hostB.accessToken}` } })
    );
    expect(after.status).toBe(404);
    const body = (await after.json()) as { as_of: string | null; status?: string };
    expect(body.as_of).toBe(null);
    expect(body.status).toBe('unknown');
    const restored = await post(hostB.accessToken, buildSnapshot(hostB.machineId, hourStamp(4), 'metrics:host-b-restored'));
    expect(restored.status).toBe(200);
  });

  it('lets an admin session read a host snapshot and the metrics page; agent tokens cannot', async () => {
    const { sessionCookie, csrfToken } = await getAdminSessionAndCsrf();
    const listed = await doFetch(
      new Request('http://example.com/api/metrics?limit=8', {
        headers: { Cookie: sessionCookie, 'x-csrf-token': csrfToken },
      })
    );
    expect(listed.status).toBe(200);
    const listBody = (await listed.json()) as { items: Array<{ host_id: string }> };
    expect(listBody.items.some((i) => i.host_id === hostA.machineId)).toBe(true);
    const got = await doFetch(
      new Request(`http://example.com/api/metrics?host_id=${encodeURIComponent(hostA.machineId)}`, {
        headers: { Cookie: sessionCookie, Origin: 'http://example.com' },
      })
    );
    expect(got.status).toBe(200);
    const pageAnon = await doFetch(new Request('http://example.com/metrics', { redirect: 'manual' }));
    expect(pageAnon.status).toBe(303);
    const pageAuth = await doFetch(
      new Request('http://example.com/metrics.html', { headers: { Cookie: sessionCookie }, redirect: 'manual' })
    );
    expect(pageAuth.status).toBe(200);
    const html = await pageAuth.text();
    expect(html).toContain('24 hourly UTC buckets');
    expect(html).toContain('Accepted outcomes');
    expect(html).not.toContain(hostA.accessToken);
    const jsRes = await doFetch(
      new Request('http://example.com/metrics.js', { headers: { Cookie: sessionCookie }, redirect: 'manual' })
    );
    expect(jsRes.status).toBe(200);
    const js = await jsRes.text();
    expect(js).toContain('textContent');
    expect(js).not.toContain('innerHTML');
    expect(js).toContain('Accepted outcomes');
    const jobTok = 'hubt_jobmetrics1';
    const routingTok = 'hubt_routingmetrics1';
    await runInDurableObject(stub, async (instance: Hub) => {
      const now = new Date(Date.now() + 86400000).toISOString();
      instance.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, scopes, created_by_sub, created_at, expires_at)
         VALUES (?, 'job', ?, 'fam-job', 'job-metrics', ?, 'test', ?, ?)`,
        'cred-job-metrics',
        await sha256(jobTok),
        JSON.stringify(['jobs:create']),
        now,
        now
      );
      instance.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES (?, 'routing-client', ?, 'fam-route', 'routing-client:metrics', 'routing-client:metrics', ?, 'test', ?, ?)`,
        'cred-route-metrics',
        await sha256(routingTok),
        JSON.stringify(['routing:submit']),
        now,
        now
      );
    });
    expect(
      (
        await doFetch(
          new Request('http://example.com/api/metrics', { headers: { Authorization: `Bearer ${jobTok}` } })
        )
      ).status
    ).toBe(403);
    expect(
      (
        await doFetch(
          new Request('http://example.com/api/metrics', { headers: { Authorization: `Bearer ${routingTok}` } })
        )
      ).status
    ).toBe(403);
  });

  it('admin Metrics GET allows Cloudflare X-Forwarded-Host and still denies X-Host-Id spoof', async () => {
    const { sessionCookie, csrfToken } = await getAdminSessionAndCsrf();
    const listed = await doFetch(
      new Request('http://example.com/api/metrics?limit=8', {
        headers: {
          Cookie: sessionCookie,
          'x-csrf-token': csrfToken,
          'X-Forwarded-Host': 'example.com',
        },
      })
    );
    expect(listed.status).toBe(200);
    const hostId = await doFetch(
      new Request('http://example.com/api/metrics?limit=8', {
        headers: {
          Cookie: sessionCookie,
          'x-csrf-token': csrfToken,
          'X-Host-Id': 'forged',
        },
      })
    );
    expect(hostId.status).toBe(403);
    const hostBody = (await hostId.json()) as { reason?: string };
    expect(hostBody.reason).toBe('header_spoof_denied');
  });

  it('keeps the metrics page on official dakit shell classes', async () => {
    const { sessionCookie } = await getAdminSessionAndCsrf();
    const page = await doFetch(
      new Request('http://example.com/metrics.html', { headers: { Cookie: sessionCookie }, redirect: 'manual' })
    );
    expect(page.status).toBe(200);
    const html = await page.text();
    expect(html).toContain('/vendor/dakit/dakit.css');
    expect(html).toContain('dk-shell');
  });
});
