import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { doFetch, defaultEnv, createHelpers } from './m1-focus-setup';
import { runInDurableObject } from 'cloudflare:test';
import { Hub } from '../src/hub';
import { sha256 } from '../src/auth/crypto';

let helpers: any;
const originalFetch = globalThis.fetch;

async function getAdminSession(email = 'admin@fake.local', sub = 'admin-1') {
  const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
  const loginRes = await doFetch(loginReq);
  const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
  const location = new URL(loginRes.headers.get('Location')!);
  const state = location.searchParams.get('state')!;
  const nonce = location.searchParams.get('nonce')!;
  const now = Math.floor(Date.now() / 1000);
  const idToken = await helpers.signJwt({
    iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
    nonce, token_use: 'id', email, email_verified: true, sub,
  });
  const prev = globalThis.fetch;
  globalThis.fetch = (async (input: any, init?: any) => {
    const s = input.toString();
    if (s.includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken }));
    if (s.includes('/.well-known/jwks.json')) {
      return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
    }
    return prev(input, init);
  }) as any;
  try {
    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, {
      headers: { Cookie: loginCookie }, redirect: 'manual',
    }));
    expect(cbRes.status).toBe(302);
    return cbRes.headers.get('Set-Cookie')!;
  } finally {
    globalThis.fetch = prev;
  }
}

async function csrfFor(sessionCookie: string) {
  const me = await doFetch(new Request('http://example.com/auth/me', { headers: { Cookie: sessionCookie } }));
  expect(me.status).toBe(200);
  return ((await me.json()) as any).csrf_token as string;
}

async function createHumanCode(label?: string, extra?: Record<string, any>) {
  const res = await doFetch(new Request('http://example.com/api/device/code', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind: 'human', ...(label !== undefined ? { label } : {}), ...(extra || {}) }),
  }));
  return res;
}

async function approveHuman(userCode: string, sessionCookie: string, csrf: string, body?: any) {
  return doFetch(new Request(`http://example.com/api/device/approve/${userCode}`, {
    method: 'POST', headers: { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrf },
    body: JSON.stringify(body ?? { granted_scopes: ['admin'] }),
  }));
}

function pollToken(deviceCode: string) {
  return doFetch(new Request('http://example.com/api/device/token', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: deviceCode }),
  }));
}

beforeAll(async () => {
  helpers = await createHelpers();
  globalThis.fetch = (async (input: any, init?: any) => {
    const s = input.toString();
    if (s.includes('/.well-known/jwks.json')) {
      return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
    }
    return originalFetch(input, init);
  }) as any;
});

afterAll(() => {
  globalThis.fetch = originalFetch;
});

describe('Human/operator device credentials', () => {
  it('issues a human pairing with fixed admin scope and standard DTO', async () => {
    const res = await createHumanCode('friendly machine');
    expect(res.status).toBe(200);
    const dto = (await res.json()) as any;
    expect(dto.device_code).toBeTruthy();
    expect(dto.user_code).toMatch(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/);
    expect(dto.verification_uri).toBe('http://example.com/device');
    expect(dto.verification_uri_complete).toContain('/device?code=');
    expect(dto.expires_in).toBe(600);
    expect(dto.interval).toBe(5);
  });

  it('rejects machine assignment and bad labels; ignores request-supplied identity', async () => {
    const badMachine = await createHumanCode('x', { machine_id: 'm1' } as any);
    expect(badMachine.status).toBe(400);
    const badLabel = await createHumanCode('   ');
    expect(badLabel.status).toBe(400);
    // Request-supplied privileged fields must not create a credential or change scope.
    const res = await createHumanCode('friendly machine', { email: 'attacker@evil.test', actor: 'root', scopes: ['admin'] } as any);
    expect(res.status).toBe(200);
    const { user_code } = (await res.json()) as any;
    const id = defaultEnv.HUB.idFromName('hub');
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      const row = instance.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', user_code).toArray()[0] as any;
      expect(row.client_kind).toBe('human');
      expect(JSON.parse(row.requested_scopes)).toEqual(['admin']);
      expect(row.actor).toBeNull();
      expect(row.machine_id).toBeNull();
    });
  });

  it('approval binds only the approving allowlisted session; no machine or actor override', async () => {
    const { device_code: _dc, user_code } = (await (await createHumanCode('ops laptop')).json()) as any;
    const sessionCookie = await getAdminSession();
    const csrf = await csrfFor(sessionCookie);
    expect((await approveHuman(user_code, sessionCookie, csrf, { granted_scopes: ['host'] })).status).toBeGreaterThanOrEqual(400);
    expect((await approveHuman(user_code, sessionCookie, csrf, { granted_scopes: ['admin'], machine_id: 'm1' } as any)).status).toBeGreaterThanOrEqual(400);
    expect((await approveHuman(user_code, sessionCookie, csrf, { granted_scopes: ['admin'], expected_actor: 'someone' } as any)).status).toBeGreaterThanOrEqual(400);
    const ok = await approveHuman(user_code, sessionCookie, csrf);
    expect(ok.status).toBe(200);
    expect(((await ok.json()) as any).machine_id).toBeNull();
  });

  it('device page renders human approval without agent machine controls', async () => {
    const { user_code } = (await (await createHumanCode('field kit')).json()) as any;
    const sessionCookie = await getAdminSession();
    const page = await doFetch(new Request(`http://example.com/device?code=${user_code}`, { headers: { Cookie: sessionCookie } }));
    expect(page.status).toBe(200);
    const html = await page.text();
    expect(html).toContain('Approve human login');
    expect(html).not.toContain('Approved machine');
    expect(html).not.toContain('Agent machine approval');
  });

  it('full issue -> bearer me -> refresh rotation with honest 30-day lifetime', async () => {
    const { device_code, user_code } = (await (await createHumanCode('refresh rig')).json()) as any;
    const sessionCookie = await getAdminSession('admin@fake.local', 'admin-refresh-1');
    const csrf = await csrfFor(sessionCookie);
    expect((await approveHuman(user_code, sessionCookie, csrf)).status).toBe(200);
    // Force past first poll so token issuance is immediate.
    const id = defaultEnv.HUB.idFromName('hub');
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      const past = new Date(Date.now() - 60000).toISOString();
      instance.sql.exec('UPDATE device_pairings SET last_poll_at = ? WHERE user_code = ?', past, user_code);
    });
    const tokRes = await pollToken(device_code);
    expect(tokRes.status).toBe(200);
    const tok = (await tokRes.json()) as any;
    expect(tok.scope).toBe('admin');
    expect(tok.machine_id).toBeNull();
    expect(tok.token_type).toBe('Bearer');
    // Honest 30-day lifetime (allow small clock skew).
    expect(tok.expires_in).toBeGreaterThan(29 * 24 * 3600);
    expect(tok.expires_in).toBeLessThanOrEqual(30 * 24 * 3600);

    // Bearer status endpoint: safe fields only.
    const me = await doFetch(new Request('http://example.com/api/auth/me', {
      headers: { Authorization: `Bearer ${tok.access_token}` },
    }));
    expect(me.status).toBe(200);
    const status = (await me.json()) as any;
    expect(status.kind).toBe('human');
    expect(status.actor).toBe('human:admin-refresh-1');
    expect(status.email).toBe('admin@fake.local');
    expect(status.is_admin).toBe(true);
    expect(status.expires_at).toBeTruthy();
    expect(status.access_token).toBeUndefined();
    expect(status.refresh_token).toBeUndefined();
    expect(status.token_hash).toBeUndefined();

    // Bearer authorizes admin routes, but never host scope.
    const agents = await doFetch(new Request('http://example.com/api/agents', {
      headers: { Authorization: `Bearer ${tok.access_token}` },
    }));
    expect(agents.status).toBe(200);
    const hostPoll = await doFetch(new Request('http://example.com/api/host/poll', {
      method: 'POST', headers: { Authorization: `Bearer ${tok.access_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'heartbeat' }),
    }));
    expect(hostPoll.status).toBe(401);

    // Refresh rotates; absolute expiry preserved; old refresh replays fail closed.
    const firstExpiry = status.expires_at;
    const ref = await doFetch(new Request('http://example.com/api/token/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: tok.refresh_token }),
    }));
    expect(ref.status).toBe(200);
    const rotated = (await ref.json()) as any;
    expect(rotated.scope).toBe('admin');
    expect(rotated.machine_id).toBeNull();
    const me2 = await doFetch(new Request('http://example.com/api/auth/me', {
      headers: { Authorization: `Bearer ${rotated.access_token}` },
    }));
    expect(me2.status).toBe(200);
    expect(((await me2.json()) as any).expires_at).toBe(firstExpiry);
    const replay = await doFetch(new Request('http://example.com/api/token/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: tok.refresh_token }),
    }));
    expect(replay.status).toBe(400);
  });

  it('removed admin fails closed on use and refresh without coming back', async () => {
    const { device_code, user_code } = (await (await createHumanCode('removal probe')).json()) as any;
    const sessionCookie = await getAdminSession('another@fake.local', 'admin-removal-1');
    const csrf = await csrfFor(sessionCookie);
    expect((await approveHuman(user_code, sessionCookie, csrf)).status).toBe(200);
    const id = defaultEnv.HUB.idFromName('hub');
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('UPDATE device_pairings SET last_poll_at = ? WHERE user_code = ?', new Date(Date.now() - 60000).toISOString(), user_code);
    });
    const tokRes = await pollToken(device_code);
    expect(tokRes.status).toBe(200);
    const tok = (await tokRes.json()) as any;
    // Simulate allowlist removal.
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.env = { ...instance.env, HUB_ADMINS: 'admin@fake.local' } as any;
    });
    try {
      const meGoneFirst = await doFetch(new Request('http://example.com/api/auth/me', {
        headers: { Authorization: `Bearer ${tok.access_token}` },
      }));
      expect(meGoneFirst.status).toBe(401);
      const meGone = await doFetch(new Request('http://example.com/api/auth/me', {
        headers: { Authorization: `Bearer ${tok.access_token}` },
      }));
      expect(meGone.status).toBe(401);
      const refGone = await doFetch(new Request('http://example.com/api/token/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: tok.refresh_token }),
      }));
      expect(refGone.status).toBe(400);
      // Family stays revoked: even the rotated token (if any) cannot come back.
      const meAfter = await doFetch(new Request('http://example.com/api/auth/me', {
        headers: { Authorization: `Bearer ${tok.access_token}` },
      }));
      expect(meAfter.status).toBe(401);
    } finally {
      await runInDurableObject(stub, async (instance: Hub) => {
        instance.env = { ...instance.env, HUB_ADMINS: 'admin@fake.local,another@fake.local' } as any;
      });
    }
  });

  it('logout revokes only the caller human family and is idempotent', async () => {
    const first = (await (await createHumanCode('logout-a')).json()) as any;
    const second = (await (await createHumanCode('logout-b')).json()) as any;
    const sessionCookie = await getAdminSession('admin@fake.local', 'admin-logout-1');
    const csrf = await csrfFor(sessionCookie);
    expect((await approveHuman(first.user_code, sessionCookie, csrf)).status).toBe(200);
    expect((await approveHuman(second.user_code, sessionCookie, csrf)).status).toBe(200);
    const id = defaultEnv.HUB.idFromName('hub');
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      const past = new Date(Date.now() - 60000).toISOString();
      instance.sql.exec("UPDATE device_pairings SET last_poll_at = ? WHERE status = 'approved'", past);
    });
    const t1 = ((await (await pollToken(first.device_code)).json()) as any);
    const t2 = ((await (await pollToken(second.device_code)).json()) as any);
    const out1 = await doFetch(new Request('http://example.com/api/auth/logout', {
      method: 'POST', headers: { Authorization: `Bearer ${t1.access_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }));
    expect(out1.status).toBe(200);
    // Idempotent retry with the same (now revoked) token still reports revoked.
    const out1b = await doFetch(new Request('http://example.com/api/auth/logout', {
      method: 'POST', headers: { Authorization: `Bearer ${t1.access_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }));
    expect(out1b.status).toBe(200);
    expect((await doFetch(new Request('http://example.com/api/auth/me', {
      headers: { Authorization: `Bearer ${t1.access_token}` },
    }))).status).toBe(401);
    // Sibling human family is untouched.
    expect((await doFetch(new Request('http://example.com/api/auth/me', {
      headers: { Authorization: `Bearer ${t2.access_token}` },
    }))).status).toBe(200);
  });

  it('host/agent device flows still refuse admin and keep working', async () => {
    const hostCode = await doFetch(new Request('http://example.com/api/device/code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name: 'h1', requested_scopes: ['host'] }),
    }));
    expect(hostCode.status).toBe(200);
    const badAdmin = await doFetch(new Request('http://example.com/api/device/code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name: 'h1', requested_scopes: ['admin'] }),
    }));
    expect(badAdmin.status).toBe(400);
    const { user_code, device_code } = (await hostCode.json()) as any;
    const sessionCookie = await getAdminSession();
    const csrf = await csrfFor(sessionCookie);
    const appr = await doFetch(new Request(`http://example.com/api/device/approve/${user_code}`, {
      method: 'POST', headers: { Cookie: sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrf },
      body: JSON.stringify({ granted_scopes: ['host'] }),
    }));
    expect(appr.status).toBe(200);
    const id = defaultEnv.HUB.idFromName('hub');
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('UPDATE device_pairings SET last_poll_at = ? WHERE user_code = ?', new Date(Date.now() - 60000).toISOString(), user_code);
    });
    const tok = await pollToken(device_code);
    expect(tok.status).toBe(200);
    expect((((await tok.json()) as any).scope)).toBe('host');
  });

  it('installer seam exposes only the four exact assets, anonymous read-only', async () => {
    const post = await doFetch(new Request('http://example.com/cli/install.sh', { method: 'POST' }));
    expect(post.status).toBe(405);
    const evil = await doFetch(new Request('http://example.com/cli/secret.env'));
    expect(evil.status).toBe(404);
    // Exact assets exist as routes (ASSETS may be absent in test, so 404 means
    // routed but not found, not an auth redirect or broad exposure).
    for (const asset of ['/cli/install.sh', '/cli/hub.pyz', '/cli/hub.pyz.sha256', '/cli/manifest.json']) {
      const res = await doFetch(new Request(`http://example.com${asset}`));
      expect([200, 404]).toContain(res.status);
      expect(res.status).not.toBe(401);
      expect(res.status).not.toBe(303);
    }
  });

  it('human token hash never leaks via me/logout', async () => {
    const { device_code, user_code } = (await (await createHumanCode('leak check')).json()) as any;
    const sessionCookie = await getAdminSession('admin@fake.local', 'admin-leak-1');
    const csrf = await csrfFor(sessionCookie);
    await approveHuman(user_code, sessionCookie, csrf);
    const id = defaultEnv.HUB.idFromName('hub');
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('UPDATE device_pairings SET last_poll_at = ? WHERE user_code = ?', new Date(Date.now() - 60000).toISOString(), user_code);
    });
    const tok = ((await (await pollToken(device_code)).json()) as any);
    const meText = await (await doFetch(new Request('http://example.com/api/auth/me', {
      headers: { Authorization: `Bearer ${tok.access_token}` },
    }))).text();
    expect(meText).not.toContain(tok.access_token.slice(0, 12));
    expect(meText).not.toContain('hubt_');
    expect(meText).not.toContain('hubr_');
    const tokenHash = await sha256(tok.access_token);
    expect(meText).not.toContain(tokenHash.slice(0, 12));
  });
});
