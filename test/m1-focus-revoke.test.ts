import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { doFetch, defaultEnv, createHelpers, loginAndGetState } from './m1-focus-setup';
import { runInDurableObject } from 'cloudflare:test';
import { Hub } from '../src/hub';
import { verifyBearerScope } from '../src/auth/token';
import { sha256 } from '../src/auth/crypto';

let helpers: any;
let originalFetch = globalThis.fetch;

beforeAll(async () => {
  helpers = await createHelpers();
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    if (input.toString().includes('/.well-known/jwks.json')) {
      return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return originalFetch(input, init);
  };
});

afterAll(() => {
  globalThis.fetch = originalFetch;
});

describe('M1 Focus: REVOKE', () => {

  it('REVOKE: unauthenticated denied; ownedBearer revokes own credential/family; other actor/machine cannot revoke', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    let myAcc = 'hubt_acc_revoke_mine';
    let otherAcc = 'hubt_acc_revoke_other';
    
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(`INSERT INTO credentials (id, kind, token_hash, scopes, family_id, subject, created_by_sub, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'c_my', 'host', await sha256(myAcc), '["host"]', 'fam_my', 'subj_my', 'sub_my', new Date().toISOString(), new Date(Date.now() + 86400000).toISOString()
      );
      instance.sql.exec(`INSERT INTO credentials (id, kind, token_hash, scopes, family_id, subject, created_by_sub, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'c_other', 'host', await sha256(otherAcc), '["host"]', 'fam_other', 'subj_other', 'sub_other', new Date().toISOString(), new Date(Date.now() + 86400000).toISOString()
      );
    });

    // unauthenticated denied
    let r1 = await doFetch(new Request('http://example.com/api/token/revoke', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: myAcc })
    }));
    expect(r1.status).toBeGreaterThanOrEqual(400);

    // other actor cannot revoke
    let r2 = await doFetch(new Request('http://example.com/api/token/revoke', {
      method: 'POST', headers: { 'Authorization': `Bearer ${otherAcc}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ token: myAcc })
    }));
    // Since it's a bearer token for a different subject, it should be rejected. Or it can't revoke `token` not in its family
    expect(r2.status).toBeGreaterThanOrEqual(400);

    // ownedBearer revokes own credential/family
    let r3 = await doFetch(new Request('http://example.com/api/token/revoke', {
      method: 'POST', headers: { 'Authorization': `Bearer ${myAcc}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ token: myAcc })
    }));
    expect(r3.status).toBe(200);

    // Verify
    await runInDurableObject(stub, async (instance: Hub) => {
      const vbs = verifyBearerScope as any;
      expect(await vbs(instance.sql, 'Bearer ' + myAcc, 'host')).toBeNull();
      expect(await vbs(instance.sql, 'Bearer ' + otherAcc, 'host')).not.toBeNull();
    });
  });

  it('REVOKE: signedadmin+validcsrf byID succeeds; missing/wrongcsrf/wrongOrigin denied; old revoked token cannot authorize', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    let targetAcc = 'acc_target';
    
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(`INSERT INTO credentials (id, kind, token_hash, scopes, family_id, subject, created_by_sub, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'c_target', 'host', await sha256(targetAcc), '["host"]', 'fam_target', 'subj_t', 'sub_t', new Date().toISOString(), new Date(Date.now() + 86400000).toISOString()
      );
    });

    const { loginCookie, state, nonce } = await loginAndGetState();
    const idToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    
    const orig = globalThis.fetch;
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken }));
      if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
      return orig(i);
    };
    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, { headers: { 'Cookie': loginCookie }, redirect: 'manual' }));
    globalThis.fetch = orig;
    const sessionCookie = cbRes.headers.get('Set-Cookie')!;
    
    const meRes = await doFetch(new Request('http://example.com/auth/me', { headers: { 'Cookie': sessionCookie } }));
    const csrfToken = ((await meRes.json()) as any).csrf_token;

    // wrong CSRF denied
    let r1 = await doFetch(new Request('http://example.com/api/token/revoke', {
      method: 'POST', headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': 'wrong' }, body: JSON.stringify({ id: 'c_target' })
    }));
    expect(r1.status).toBeGreaterThanOrEqual(400);

    // missing CSRF denied
    let r2 = await doFetch(new Request('http://example.com/api/token/revoke', {
      method: 'POST', headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json' }, body: JSON.stringify({ id: 'c_target' })
    }));
    expect(r2.status).toBeGreaterThanOrEqual(400);

    // wrong Origin denied
    let r3 = await doFetch(new Request('http://example.com/api/token/revoke', {
      method: 'POST', headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken, 'Origin': 'http://hacker.com' }, body: JSON.stringify({ id: 'c_target' })
    }));
    expect(r3.status).toBeGreaterThanOrEqual(400);

    // valid succeeds
    let r4 = await doFetch(new Request('http://example.com/api/token/revoke', {
      method: 'POST', headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken }, body: JSON.stringify({ id: 'c_target' })
    }));
    expect(r4.status).toBe(200);

    // old revoked token cannot authorize
    let r5 = await doFetch(new Request('http://example.com/api/healthz', {
      headers: { 'Authorization': `Bearer ${targetAcc}` }
    }));
    expect(r5.status).toBe(401);
    await runInDurableObject(stub, async (instance: Hub) => {
      const vbs = verifyBearerScope as any;
      expect(await vbs(instance.sql, 'Bearer ' + targetAcc, 'host')).toBeNull();
    });
  });

});
