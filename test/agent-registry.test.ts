import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv } from './m1-focus-setup';
import { createSessionCookie } from '../src/auth/session';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

async function admin() {
  return createSessionCookie(defaultEnv, { sub: 'human-test', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test', exp: Math.floor(Date.now() / 1000) + 600 });
}
const json = (method: string, path: string, body: any, cookie?: string) => doFetch(new Request('https://hub.test' + path, { method, headers: { 'Content-Type': 'application/json', ...(cookie ? { Cookie: cookie, 'X-CSRF-Token': 'csrf-test' } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) }));
const agents = async () => { let rows: any[] = []; const b = env as unknown as Env; await runInDurableObject(b.HUB.get(b.HUB.idFromName('hub')), async (hub: Hub) => { rows = hub.sql.exec('SELECT tag, machine_id, imported_from FROM agents').toArray(); }); return rows; };

describe('bus recipient registry', () => {
  it('registers a paired agent as a bus recipient on approval', async () => {
    const actor = 'paired-bot-' + crypto.randomUUID().slice(0, 8);
    const code: any = await (await json('POST', '/api/device/code', { client_kind: 'agent', name: actor, actor, requested_scopes: ['bus:send', 'bus:read'] })).json();
    expect((await json('POST', '/api/device/approve/' + code.user_code, {}, await admin())).status).toBe(200);
    expect((await agents()).find(a => a.tag === actor)).toEqual({ tag: actor, machine_id: null, imported_from: 'pairing' });
  });

  it('lets an admin register and remove bridged recipients with validation', async () => {
    const cookie = await admin(); const tag = 'coordinator-' + crypto.randomUUID().slice(0, 8);
    expect((await json('POST', '/api/agents', { tag })).status).toBe(401);
    expect((await json('POST', '/api/agents', { tag, role: 'root' }, cookie)).status).toBe(400);
    expect((await json('POST', '/api/agents', { tag, machine_id: 'no-such-machine' }, cookie)).status).toBe(400);
    expect((await json('POST', '/api/agents', { tag: 'bad tag' }, cookie)).status).toBe(400);
    expect((await json('POST', '/api/agents', { tag, token: 'x' }, cookie)).status).toBe(400);
    expect((await json('POST', '/api/agents', { tag, project: 'aplexer' }, cookie)).status).toBe(201);
    expect((await json('POST', '/api/agents', { tag }, cookie)).status).toBe(409);
    expect((await agents()).some(a => a.tag === tag)).toBe(true);
    expect((await json('DELETE', '/api/agents/' + tag, undefined, cookie)).status).toBe(200);
    expect((await agents()).some(a => a.tag === tag)).toBe(false);
  });
});
