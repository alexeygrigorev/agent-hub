// Hub-owned gh token / shared git SSH key / commit identity (intake 54d5b703).
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { HostCredentialService, sanitizeStatus, sshFingerprint, validPayload } from '../src/host-credentials';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const hostAuth = (machineId: string) => ({ caller: { kind: 'host', machineId, actor: 'machine:' + machineId }, scopes: ['host'] });
const TOKEN = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0'.repeat(2);
const PUB = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl agent-hub';
const PRIV = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n-----END OPENSSH PRIVATE KEY-----\n';
const IDENT = { name: 'Agent Hub', email: 'agents@example.com' };

type Fetch = (url: string, init?: RequestInit) => Promise<Response>;
function github(script: Record<string, { status: number; json?: any; headers?: Record<string, string> }>) {
  const calls: { url: string; method: string; auth: string | null }[] = [];
  const fn: Fetch = async (url, init) => {
    const h = new Headers(init?.headers);
    calls.push({ url, method: init?.method ?? 'GET', auth: h.get('Authorization') });
    const r = script[(init?.method ?? 'GET') + ' ' + url];
    return r ? new Response(JSON.stringify(r.json ?? {}), { status: r.status, headers: r.headers }) : new Response('{}', { status: 404 });
  };
  return { fn, calls };
}

async function fixture(fetchFn: Fetch, fn: (svc: HostCredentialService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('host-credentials-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const svc = new HostCredentialService(hub.sql, cb => state.storage.transactionSync(cb), bindings, () => true, fetchFn);
    svc.init();
    for (const id of ['rmthz', 'tabo']) {
      hub.sql.exec('INSERT OR REPLACE INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        id, id, id, new Date().toISOString(), new Date().toISOString(), '{}', 0, '{}');
    }
    await fn(svc, hub.sql);
  });
}
const req = (path: string, method = 'GET', body?: unknown) => new Request('https://hub.test' + path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
const put = (svc: HostCredentialService, kind: string, payload: unknown) => svc.handle(req('/api/host-credentials/' + kind, 'PUT', { payload }), admin);
const sync = async (svc: HostCredentialService, machine: string, have = '') => (await svc.handle(req('/api/host-credentials/sync' + (have ? '?have=' + have : '')), hostAuth(machine))).json() as Promise<any>;
const okUser = { 'GET https://api.github.com/user': { status: 200, json: { login: 'alexeygrigorev' }, headers: { 'x-oauth-scopes': 'repo, read:org' } } };

describe('payload validation', () => {
  it('accepts exact payloads only', () => {
    expect(validPayload('gh_token', { token: TOKEN })).toBe(true);
    expect(validPayload('gh_token', { token: 'not-a-token' })).toBe(false);
    expect(validPayload('gh_token', { token: TOKEN, extra: 'x' })).toBe(false);
    expect(validPayload('git_ssh_key', { private_key: PRIV, public_key: PUB })).toBe(true);
    expect(validPayload('git_ssh_key', { private_key: 'x', public_key: PUB })).toBe(false);
    expect(validPayload('git_identity', IDENT)).toBe(true);
    expect(validPayload('git_identity', { name: 'a', email: 'bad' })).toBe(false);
  });
  it('fingerprints like ssh-keygen -l', async () => {
    expect(await sshFingerprint(PUB)).toMatch(/^SHA256:[A-Za-z0-9+/]{43}$/);
  });
  it('sanitizes host reports to bounded non-secret fields', () => {
    const r: any = sanitizeStatus({ gh: { logged_in: true, account: 'alexeygrigorev', scopes: ['repo', 'a b', 'x'.repeat(100)], error: 'ghp_secret value' }, ssh: { authenticated: true, key_fingerprint: 'nope' }, generations: { gh_token: 3, bogus: 1 } });
    expect(r.gh).toEqual({ logged_in: true, account: 'alexeygrigorev', scopes: ['repo'], error: null });
    expect(r.ssh.key_fingerprint).toBeNull();
    expect(r.generations).toEqual({ gh_token: 3 });
    expect(sanitizeStatus('x')).toBeNull();
  });
});

describe('host credential store', () => {
  it('stores sealed, bumps generations, and never returns secrets to admin or in ciphertext', async () => {
    const gh = github(okUser);
    await fixture(gh.fn, async (svc, sql) => {
      const res = await put(svc, 'gh_token', { token: TOKEN });
      expect(res.status).toBe(200);
      const body: any = await res.json();
      expect(body).toMatchObject({ status: 'stored', generation: 1, account: 'alexeygrigorev', scopes: ['repo', 'read:org'] });
      expect(JSON.stringify(body)).not.toContain(TOKEN);
      expect(((await put(svc, 'gh_token', { token: TOKEN.replace('A1b2', 'Z9y8') })).json() as Promise<any>)).resolves.toMatchObject({ generation: 2 });
      const raw = JSON.stringify(sql.exec('SELECT * FROM host_credentials').toArray());
      expect(raw).not.toContain('ghp_');
      const overview = JSON.stringify(await (await svc.handle(req('/api/host-credentials'), admin)).json());
      expect(overview).not.toContain('ghp_');
      expect(overview).toContain('alexeygrigorev');
    });
  });
  it('refuses a token GitHub rejects and a non-admin writer', async () => {
    const gh = github({ 'GET https://api.github.com/user': { status: 401 } });
    await fixture(gh.fn, async svc => {
      expect((await put(svc, 'gh_token', { token: TOKEN })).status).toBe(422);
      expect((await svc.handle(req('/api/host-credentials/gh_token', 'PUT', { payload: { token: TOKEN } }), hostAuth('rmthz'))).status).toBe(403);
      expect((await put(svc, 'nope', {})).status).toBe(404);
    });
  });
  it('syncs only changed kinds, everything after a re-sync request', async () => {
    const gh = github(okUser);
    await fixture(gh.fn, async svc => {
      await put(svc, 'gh_token', { token: TOKEN });
      await put(svc, 'git_ssh_key', { private_key: PRIV, public_key: PUB });
      await put(svc, 'git_identity', IDENT);
      const first = await sync(svc, 'rmthz');
      expect(Object.keys(first.credentials).sort()).toEqual(['gh_token', 'git_identity', 'git_ssh_key']);
      expect(first.credentials.gh_token.payload.token).toBe(TOKEN);
      const quiet = await sync(svc, 'rmthz', 'gh_token:1,git_ssh_key:1,git_identity:1');
      expect(quiet.credentials).toEqual({});
      expect(quiet.generations).toEqual({ gh_token: 1, git_ssh_key: 1, git_identity: 1 });
      expect((await svc.handle(req('/api/host-credentials/sync'), { caller: { isAdmin: true }, scopes: ['admin'] })).status).toBe(403);
    });
  });
  it('missing gh login on a host makes Hub schedule a re-sync for it (AC4)', async () => {
    const gh = github(okUser);
    await fixture(gh.fn, async svc => {
      await put(svc, 'gh_token', { token: TOKEN });
      const report = (logged_in: boolean) => svc.handle(req('/api/host-credentials/status', 'POST', { gh: { logged_in, account: logged_in ? 'alexeygrigorev' : null, scopes: ['repo'] }, ssh: { authenticated: true }, generations: { gh_token: 1 } }), hostAuth('tabo'));
      expect(await (await report(false)).json()).toMatchObject({ resync: true });
      const forced = await sync(svc, 'tabo', 'gh_token:1');
      expect(forced.forced).toBe(true);
      expect(forced.credentials.gh_token.payload.token).toBe(TOKEN);
      expect((await sync(svc, 'tabo', 'gh_token:1')).forced).toBe(false);
      expect(await (await report(true)).json()).toMatchObject({ resync: false });
      const overview: any = await (await svc.handle(req('/api/host-credentials'), admin)).json();
      const tabo = overview.machines.find((m: any) => m.machine_id === 'tabo');
      expect(tabo.gh).toMatchObject({ logged_in: true, account: 'alexeygrigorev' });
      expect(overview.machines.find((m: any) => m.machine_id === 'rmthz').stale).toBe(true);
    });
  });
});

describe('register shared key on GitHub (AC7)', () => {
  const setup = async (svc: HostCredentialService) => { await put(svc, 'gh_token', { token: TOKEN }); await put(svc, 'git_ssh_key', { private_key: PRIV, public_key: PUB }); };
  it('registers with the stored token and records it', async () => {
    const gh = github({ ...okUser, 'POST https://api.github.com/user/keys': { status: 201, json: { id: 1 } } });
    await fixture(gh.fn, async svc => {
      await setup(svc);
      const res = await svc.registerKey();
      expect(await res.json()).toMatchObject({ status: 'registered' });
      expect(gh.calls.at(-1)!.auth).toBe('Bearer ' + TOKEN);
      const overview: any = await (await svc.handle(req('/api/host-credentials'), admin)).json();
      expect(overview.kinds.find((k: any) => k.kind === 'git_ssh_key').registered).toBe(true);
    });
  });
  it('treats an already-registered key as registered and reports a missing scope plainly', async () => {
    const dup = github({ ...okUser, 'POST https://api.github.com/user/keys': { status: 422, json: { errors: [{ message: 'key is already in use' }] } } });
    await fixture(dup.fn, async svc => { await setup(svc); expect(await (await svc.registerKey()).json()).toMatchObject({ status: 'registered' }); });
    const noscope = github({ ...okUser, 'POST https://api.github.com/user/keys': { status: 404 } });
    await fixture(noscope.fn, async svc => {
      await setup(svc);
      const res = await svc.registerKey();
      expect(res.status).toBe(502);
      expect(await res.json()).toMatchObject({ status: 'missing_scope_admin_public_key' });
    });
  });
});
