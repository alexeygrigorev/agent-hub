import { describe, it, expect } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { RepoRegistry } from '../src/repos';
import { toolRequest } from '../src/mcp-tools';
import { createSessionCookie } from '../src/auth/session';

const admin = { caller: { kind: 'human', isAdmin: true }, scopes: ['admin'] };
const host = { caller: { kind: 'host', machineId: 'hetzner' }, scopes: ['host'] };
const agent = { caller: { kind: 'agent' }, scopes: ['bus:read'] };
const repo = (over = {}) => ({ name: 'dtc-website', machine: 'hetzner', dir: '/srv/git/dtc-website', origin: 'Example/website', org: 'Example',
  purpose: 'Human purpose **verbatim**', stack: 'Django (Py 3.13, uv)', key_folders: '`content/`, `e2e/`', urls: 'dev.example.test, example.test',
  workflows: '`ci.yml`', default_branch: 'main', status: 'active', aliases: ['DTC website', 'PocketShell app/Android'], flags: [], ...over });
const req = (path: string, method = 'GET', data?: any) => new Request('https://hub.test/api/repos' + path, { method, ...(data ? { body: JSON.stringify(data) } : {}) });
async function fixture(fn: (registry: RepoRegistry, hub: Hub, state: any) => Promise<void>) {
  const binding = env as unknown as Env;
  const stub = binding.HUB.get(binding.HUB.idFromName('repo-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: any) => fn(hub.repoRegistry, hub, state));
}
const seed = (r: RepoRegistry, repos = [repo()]) => r.handle(req('/seed', 'POST', { repos }), admin);

describe('private repo registry', () => {
  it('records a host clone immediately and retains its cache marker across ordinary scans', () => fixture(async r => {
    await seed(r);
    const cloned = {name:'dtc-website',machine:'hetzner',dir:'/srv/git/dtc-website',origin:'Example/website',
      checkout_status:'present',cloned_on_demand:true};
    expect((await r.handle(req('/refresh','POST',{repos:[cloned]}),host)).status).toBe(200);
    let checkout = r.get('dtc-website').checkouts.find((c:any)=>c.host_id==='hetzner');
    expect(checkout.cloned_on_demand).toBe(true);
    expect(Number.isFinite(Date.parse(checkout.last_used_at))).toBe(true);
    const {cloned_on_demand, ...ordinary} = cloned;
    expect((await r.handle(req('/refresh','POST',{repos:[ordinary]}),host)).status).toBe(200);
    checkout = r.get('dtc-website').checkouts.find((c:any)=>c.host_id==='hetzner');
    expect(checkout.cloned_on_demand).toBe(true);
    expect((await r.handle(req('/refresh','POST',{repos:[{...cloned,cloned_on_demand:'true'}]}),host)).status).toBe(400);
  }));
  it('classifies duplicates through the exported Worker, authenticated health and Durable Object', async () => {
    const binding = env as unknown as Env;
    const csrf = crypto.randomUUID();
    const cookie = (await createSessionCookie(binding, {
      sub: 'repo-runtime-operator', email: 'admin@fake.local', isAdmin: true,
      csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600,
    })).split(';')[0];
    const health = await SELF.fetch('https://hub.test/api/healthz', { headers: { Cookie: cookie } });
    expect(health.status, await health.clone().text()).toBe(200);
    expect(await health.json()).toEqual({ status: 'ok', component: 'hub-do' });
    const anonymousHealth = await SELF.fetch('https://hub.test/api/healthz');
    expect(anonymousHealth.status).toBe(401);

    const call = (path: string, method = 'GET', body?: unknown, authenticated = true) =>
      SELF.fetch('https://hub.test/api/repos' + path, {
        method, headers: { 'Content-Type': 'application/json',
          ...(authenticated ? { Cookie: cookie, 'x-csrf-token': csrf } : {}) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    const target = 'runtime-target-' + crypto.randomUUID();
    const copy = 'runtime-copy-' + crypto.randomUUID();
    try {
      const seeded = await call('/seed', 'POST', { repos: [
        repo({ name: target, aliases: [] }), repo({ name: copy, aliases: [] }),
      ] });
      expect(seeded.status, await seeded.clone().text()).toBe(200);
      const update = { status: 'duplicate', canonical: target };
      expect((await call('/' + copy, 'PATCH', update, false)).status).toBe(401);
      const patched = await call('/' + copy, 'PATCH', update);
      expect(patched.status, await patched.clone().text()).toBe(200);
      expect(await patched.json()).toMatchObject({ repo: { name: copy, ...update } });
      const refreshed = await call('/refresh', 'POST', { repos: [{
        name: copy, machine: 'hetzner', dir: '/srv/git/copy', status: 'active', canonical: null,
      }] });
      expect(refreshed.status, await refreshed.clone().text()).toBe(200);
      const shown = await call('/' + copy);
      expect(await shown.json()).toMatchObject({ repo: { name: copy, ...update }, canonical: target });
      const invalid = await call('/' + copy, 'PATCH', { status: 'duplicate', canonical: copy });
      expect(invalid.status).toBe(400);
      expect(await invalid.json()).toEqual({ error: 'invalid_canonical' });
    } finally {
      const stub = binding.HUB.get(binding.HUB.idFromName('hub'));
      await runInDurableObject(stub, (hub: Hub) => {
        hub.sql.exec('DELETE FROM repo_registry WHERE name IN (?, ?)', target, copy);
      });
    }
  });
  it('proves PATCH API readiness using a missing repo without writing', async () => {
    const binding = env as unknown as Env;
    const csrf = crypto.randomUUID();
    const cookie = (await createSessionCookie(binding, {
      sub: 'repo-readiness-probe', email: 'admin@fake.local', isAdmin: true,
      csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600,
    })).split(';')[0];
    const probe = 'readiness-probe-' + crypto.randomUUID();
    const url = 'https://hub.test/api/repos/' + probe;
    const headers = { Cookie: cookie, 'x-csrf-token': csrf, 'Content-Type': 'application/json' };
    const get = () => SELF.fetch(url, { headers: { Cookie: cookie } });

    const before = await get();
    expect(before.status).toBe(404);
    expect(await before.json()).toEqual({ error: 'repo_not_found' });
    const response = await SELF.fetch(url, {
      method: 'PATCH', headers,
      body: JSON.stringify({ status: 'duplicate', canonical: 'readiness-probe-target' }),
    });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: 'repo_not_found' });
    const after = await get();
    expect(after.status).toBe(404);
    expect(await after.json()).toEqual({ error: 'repo_not_found' });
  });
  it('persists verbatim human cells across instances and reseeding', () => fixture(async (r, hub, state) => {
    expect((await seed(r)).status).toBe(200);
    const reloaded = new RepoRegistry(hub.sql, fn => state.storage.transactionSync(fn));
    expect(reloaded.get('dtc-website').purpose).toBe(repo().purpose);
    await seed(r, [repo({ purpose: 'changed' })]);
    expect(r.get('dtc-website').purpose).toBe(repo().purpose);
  }));
  it('refreshes machine fields, protects human fields and duplicate links', () => fixture(async r => {
    await seed(r);
    const scanned = { name: 'dtc-website', machine: 'hetzner', dir: '/srv/git/moved', workflows: 'ci.yml, deploy.yml', default_branch: 'master', flags: ['MOVED'], last_commit: { sha: 'a'.repeat(40), date: '2026-10-26T01:00:00Z' } };
    expect((await r.handle(req('/refresh', 'POST', { repos: [scanned] }), host)).status).toBe(200);
    const saved = r.get('dtc-website');
    for (const key of ['purpose', 'stack', 'key_folders', 'urls']) expect(saved[key]).toBe((repo() as any)[key]);
    expect(saved.default_branch).toBe('master');
    expect((await r.handle(req('/refresh', 'POST', { repos: [{ ...scanned, purpose: 'overwrite' }] }), host)).status).toBe(400);
    await seed(r, [repo({ name: 'old-site', status: 'duplicate', canonical: 'dtc-website' })]);
    await r.handle(req('/refresh', 'POST', { repos: [{ ...scanned, name: 'old-site', status: 'active' }] }), host);
    expect(r.get('old-site').status).toBe('duplicate');
    expect(r.get('dtc-website').dir).toBe('/srv/git/moved');
  }));
  it('imports real names that also occur as another entry alias without losing either entry', () => fixture(async r => {
    await seed(r, [repo({ aliases: ['content'] })]);
    expect((await seed(r, [repo({ name: 'content', origin: 'Example/content', aliases: [] })])).status).toBe(200);
    expect(r.all()).toHaveLength(2);
    expect(r.get('content').name).toBe('content');
    expect(r.get('dtc-website').aliases).toContain('content');
  }));
  it('filters list by org/status/flagged and shows alias/canonical', () => fixture(async r => {
    await seed(r);
    await seed(r, [repo({ name: 'old-site', org: 'Other', status: 'duplicate', canonical: 'dtc-website', aliases: [], flags: ['MISSING'] })]);
    const list = await (await r.handle(req('?org=Other&status=duplicate&flagged=true'), agent)).json() as any;
    expect(list.count).toBe(1);
    const show = await (await r.handle(req('/DTC%20website'), agent)).json() as any;
    expect(show.repo.name).toBe('dtc-website');
    expect(show.canonical).toBe('dtc-website');
  }));
  it('restricts mutations to operators and refresh to the bound machine', () => fixture(async r => {
    expect((await r.handle(req('/seed', 'POST', { repos: [repo()] }), host)).status).toBe(403);
    await seed(r);
    expect((await r.handle(req('/dtc-website', 'PATCH', { purpose: 'edit' }), agent)).status).toBe(403);
    expect((await r.handle(req('/dtc-website', 'PATCH', { purpose: 'edit' }), admin)).status).toBe(200);
    const foreign = { name: 'dtc-website', machine: 'elsewhere', dir: '/srv/git/site' };
    expect((await r.handle(req('/refresh', 'POST', { repos: [foreign] }), host)).status).toBe(403);
    expect(r.get('dtc-website').machine).toBe('hetzner');
    expect((await r.handle(req('/dtc-website', 'PATCH', { dir: '/outside' }), admin)).status).toBe(400);
  }));
  it('lets operators classify duplicates and clears canonical when status changes away', () => fixture(async r => {
    await seed(r);
    await seed(r, [repo({ name: 'old-site', status: 'active', canonical: null, aliases: [] })]);
    const response = await r.handle(req('/old-site', 'PATCH', { status: 'duplicate', canonical: 'dtc-website' }), admin);
    expect(response.status).toBe(200);
    expect(r.get('old-site')).toMatchObject({ status: 'duplicate', canonical: 'dtc-website' });
    const refresh = await r.handle(req('/refresh', 'POST', { repos: [{ name: 'old-site', machine: 'hetzner', dir: '/srv/git/old-site', status: 'active', flags: [] }] }), host);
    expect(refresh.status).toBe(200);
    expect(r.get('old-site')).toMatchObject({ status: 'duplicate', canonical: 'dtc-website' });
    const changed = await r.handle(req('/old-site', 'PATCH', { status: 'archived' }), admin);
    expect(changed.status).toBe(200);
    expect(r.get('old-site')).toMatchObject({ status: 'archived', canonical: null });
  }));
  it('rejects invalid duplicate mutations atomically', () => fixture(async r => {
    await seed(r);
    await seed(r, [repo({ name: 'old-site', status: 'active', canonical: null, aliases: [] })]);
    await seed(r, [repo({ name: 'archived-site', status: 'archived', canonical: null, aliases: [] })]);
    await seed(r, [repo({ name: 'legacy-loop', status: 'duplicate', canonical: 'old-site', aliases: [] })]);
    const invalid = [
      { status: 'unknown', purpose: 'must roll back' },
      { status: 'duplicate' },
      { status: 'duplicate', canonical: 'absent' },
      { status: 'duplicate', canonical: 'old-site' },
      { status: 'duplicate', canonical: 'archived-site' },
      { status: 'duplicate', canonical: 'legacy-loop' },
      { canonical: 'dtc-website' },
    ];
    for (const update of invalid) {
      expect((await r.handle(req('/old-site', 'PATCH', update), admin)).status).toBe(400);
      expect(r.get('old-site')).toMatchObject({ status: 'active', canonical: null });
    }
    expect((await r.handle(req('/old-site', 'PATCH', { status: 'duplicate', canonical: 'dtc-website', purpose: 'updated' }), agent)).status).toBe(403);
    expect(r.get('old-site')).toMatchObject({ status: 'active', canonical: null, purpose: repo().purpose });
  }));
  it('rejects invalid whole batches without partial writes', () => fixture(async r => {
    expect((await seed(r, [repo(), repo({ name: 'bad', dir: '/srv/../outside' })])).status).toBe(400);
    expect(r.all()).toHaveLength(0);
    expect((await r.handle(req(''), { error: Response.json({}, { status: 401 }) })).status).toBe(401);
  }));
  it('resolves logical labels and duplicates without pinning a machine', () => fixture(async r => {
    await seed(r);
    await seed(r, [repo({ name: 'old-site', status: 'duplicate', canonical: 'dtc-website', aliases: [] })]);
    for (const workspace of ['dtc-website', 'DTC website', 'PocketShell app/Android', 'old-site']) {
      const result = r.prepareLaunch({ workspace, prompt: 'Read the repo', mode: 'headless' }).input;
      expect(result.workspace).toBe('dtc-website');
      expect(result.machine).toBeUndefined();
      expect(result.repo_context.repo_CheckoutsByHost.hetzner.path).toBe('/srv/git/dtc-website');
      for (const key of ['origin', 'purpose', 'stack', 'key_folders', 'workflows', 'urls', 'default_branch']) expect(result.prompt).toContain(JSON.stringify((repo() as any)[key]));
    }
    expect(r.resolveLaunchRepo({ workspace: 'dtc-website', machine: 'other', prompt: 'x' }).input.machine).toBe('other');
    expect(r.prepareLaunch({ workspace: 'dtc-websit', prompt: 'x' }).error.matches[0]).toBe('dtc-website');
    expect(r.prepareLaunch({ workspace: 'dtc-website', prompt: '', mode: 'interactive' }).input.prompt).toBe('');
  }));
  it('routes portable repos as repo identities instead of pinning the registry machine (178ec5bb)', () => fixture(async r => {
    await seed(r);
    expect((await r.handle(req('/dtc-website', 'PATCH', { portable: 'yes' }), admin)).status).toBe(400);
    expect((await r.handle(req('/dtc-website', 'PATCH', { portable: true }), agent)).status).toBe(403);
    expect((await r.handle(req('/dtc-website', 'PATCH', { portable: true }), admin)).status).toBe(200);
    const result = r.prepareLaunch({ workspace: 'dtc-website', prompt: 'x', mode: 'headless', intake_task_id: '178ec5bb-99c4-41da-80d5-8c8d7e367422' }).input;
    expect(result.machine).toBeUndefined();
    expect(result.workspace).toMatch(/^repo-178ec5bb-/);
    expect(r.prepareLaunch({ workspace: 'dtc-website', machine: 'hetzner', prompt: 'x' }).input.workspace).toBe('dtc-website');
  }));
  it('validates --repo against registered origins before placement and suggests the canonical origin', () => fixture(async (r, hub) => {
    await seed(r, [repo({ name: 'ai-shipping-labs', org: 'AI-Shipping-Labs', origin: 'AI-Shipping-Labs/website' })]);
    const start = (value: string, key: string) => hub.sessionControlService.handle(new Request('https://hub.test/api/sessions', {
      method: 'POST', body: JSON.stringify({ repo: value, key, mode: 'interactive' }),
    }), admin);
    const startWorkspace = (value: string, key: string) => hub.sessionControlService.handle(new Request('https://hub.test/api/sessions', {
      method: 'POST', body: JSON.stringify({ workspace: value, key, mode: 'interactive' }),
    }), admin);

    const invalid = await start('AI-Shipping-Labs/ai-shipping-labs', 'bad-origin');
    expect(invalid.status).toBe(400);
    expect(await invalid.json()).toMatchObject({
      error: 'unknown_repo', repo: 'AI-Shipping-Labs/ai-shipping-labs', matches: ['AI-Shipping-Labs/website'], field: 'repo',
    });
    const bypass = await startWorkspace('repo-0123abcd-AI-Shipping-Labs__ai-shipping-labs', 'synthetic-bypass');
    expect(bypass.status).toBe(400);
    expect(await bypass.json()).toMatchObject({ error: 'unknown_repo', matches: ['AI-Shipping-Labs/website'], field: 'repo' });
    expect(hub.sql.exec("SELECT COUNT(*) AS n FROM session_control_sessions WHERE actor='admin'").one().n).toBe(0);

    const valid = await start('AI-Shipping-Labs/website', 'registered-origin');
    expect(valid.status).not.toBe(400);
    const accepted = await valid.json() as any;
    expect(accepted.workspace).toMatch(/^repo-[0-9a-f]{8}-AI-Shipping-Labs__website$/);
  }));
  it('refuses missing canonical entries and loops while retaining missing checkout evidence', () => fixture(async r => {
    await seed(r, [repo({ status: 'duplicate', canonical: 'absent' })]);
    expect(r.prepareLaunch({ workspace: 'dtc-website', prompt: 'x' }).error.error).toBe('canonical_repo_missing');
    await seed(r, [repo({ name: 'absent', status: 'duplicate', canonical: 'dtc-website', aliases: [] })]);
    expect(r.prepareLaunch({ workspace: 'dtc-website', prompt: 'x' }).error.error).toBe('duplicate_cycle');
    await seed(r, [repo({ name: 'missing-site', origin: 'Example/missing', flags: ['MISSING'], aliases: [] })]);
    expect(r.resolveLaunchRepo({ workspace: 'missing-site', prompt: 'x' }).input.repo_context.repo_CheckoutsByHost.hetzner.status).toBe('missing');
  }));
  it('wires registered labels and unknown-name diagnostics into real session start', () => fixture(async (r, hub) => {
    await seed(r);
    const start = (workspace: string, key: string) => hub.sessionControlService.handle(new Request('https://hub.test/api/sessions', { method: 'POST', body: JSON.stringify({ workspace, key, mode: 'interactive' }) }), admin);
    const bad = await start('dtc-websit', 'typo');
    expect(bad.status).toBe(400);
    expect((await bad.json() as any).matches[0]).toBe('dtc-website');
    const known = await start('PocketShell app/Android', 'known');
    const result = await known.json() as any;
    expect(result.error).not.toBe('invalid_session_request');
    expect(result.error).not.toBe('invalid_launch_request');
    expect(result.hold?.error).not.toBe('unknown_workspace');
  }));
  it('exposes MCP list/show/edit through fixed ordinary API routes', () => {
    expect(toolRequest('https://hub.test', 'repos_list', { query: { flagged: true } }, 'Bearer test').url).toContain('/api/repos?flagged=true');
    expect(toolRequest('https://hub.test', 'repos_show', { name: 'dtc-website' }, 'Bearer test').url).toContain('dtc-website');
    expect(toolRequest('https://hub.test', 'repos_edit', { name: 'dtc-website', payload: { purpose: 'x' } }, 'Bearer test').method).toBe('PATCH');
  });
});
describe('multi-host repository checkouts', () => {
  it('uses the injected clock for online checkout availability', () => fixture(async (r, hub, state) => {
    await seed(r);
    let clock = Date.parse('2020-01-01T00:00:00Z');
    const at = new Date(clock).toISOString();
    hub.sql.exec('INSERT INTO machines (id,name,hostname,capabilities,registered_at,last_checkin_at) VALUES (?,?,?,?,?,?)', 'hetzner', 'Clock host', 'clock', '{}', at, at);
    const registry = new RepoRegistry(hub.sql, fn => state.storage.transactionSync(fn), () => clock);
    expect(registry.get('dtc-website').checkouts[0].online).toBe(true);
    expect(registry.get('dtc-website').no_present_online).toBe(false);
    clock += 180001;
    expect(registry.get('dtc-website').checkouts[0].online).toBe(false);
    expect(registry.get('dtc-website').flags).toContain('NO_PRESENT_ONLINE');
  }));

  it('joins two authenticated hosts at different paths without overwriting either checkout', () => fixture(async (r, hub) => {
    await seed(r);
    const second = { caller: { kind: 'host', machineId: 'host2' }, scopes: ['host'] };
    const refresh = { name: 'dtc-website', machine: 'host2', dir: '/srv/repos/dtc-website', origin: 'example/website', default_branch: 'main', flags: [], branch: 'feature', dirty_counts: { staged: 0, unstaged: 1, untracked: 2 }, disk_free_bytes: 60000000000 };
    expect((await r.handle(req('/refresh', 'POST', { repos: [refresh] }), second)).status).toBe(200);
    let saved = r.get('dtc-website');
    expect(saved.repo_id).toBe('example/website');
    expect(saved.checkouts.map((c: any) => [c.machine_id, c.local_path])).toEqual([['hetzner', '/srv/git/dtc-website'], ['host2', '/srv/repos/dtc-website']]);
    expect(saved.purpose).toBe(repo().purpose);
    expect(saved.checkouts[1].dirty_counts.unstaged).toBe(1);
    expect((await r.handle(req('/refresh', 'POST', { repos: [{ ...refresh, machine: 'hetzner' }] }), second)).status).toBe(403);
    await r.handle(req('/refresh', 'POST', { repos: [{ ...refresh, flags: ['MISSING'], checkout_status: 'missing', origin: null, default_branch: null }] }), second);
    saved = r.get('dtc-website');
    expect(saved.checkouts[0].status).toBe('present');
    expect(saved.checkouts[1].status).toBe('missing');
    expect(saved.origin).toBe('example/website');
    expect(saved.default_branch).toBe('main');
    const raw = JSON.parse(String(hub.sql.exec('SELECT data FROM repo_registry').toArray()[0].data));
    expect(raw.machine).toBeUndefined(); expect(raw.dir).toBeUndefined();
    const launch = r.resolveLaunchRepo({ workspace: 'dtc-website', machine: 'hetzner' }).input;
    expect(launch.workspace).toBe('dtc-website');
    expect(launch.machine).toBe('hetzner');
    expect(launch.repo_context.repo_CheckoutsByHost.hetzner.path).toBe('/srv/git/dtc-website');
    expect(launch.repo_context.repo_CheckoutsByHost.host2.status).toBe('missing');
  }));
  it('matches the same origin under different directory names and promotes local identity', () => fixture(async r => {
    await seed(r, [repo({ origin: '', name: 'site' })]);
    expect(r.get('site').repo_id).toBe('local/site');
    await r.handle(req('/refresh', 'POST', { repos: [{ name: 'site', machine: 'hetzner', dir: '/srv/site', origin: 'Example/site' }] }), host);
    const second = { caller: { kind: 'host', machineId: 'host2' }, scopes: ['host'] };
    await r.handle(req('/refresh', 'POST', { repos: [{ name: 'renamed', machine: 'host2', dir: '/opt/projects/renamed', origin: 'example/SITE' }] }), second);
    expect(r.all()).toHaveLength(1);
    expect(r.get('example/site').checkouts).toHaveLength(2);
    expect(r.get('site').purpose).toBe(repo().purpose);
  }));
  it('keeps conflicting remotes separate and names case insensitive', () => fixture(async r => {
    await seed(r);
    await r.handle(req('/refresh', 'POST', { repos: [{ name: 'DTC-Website', machine: 'hetzner', dir: '/srv/git/site', origin: 'Other/site' }] }), host);
    expect(r.all()).toHaveLength(2);
    expect(r.get('DTC-WEBSITE').repo_id).toBe('example/website');
    expect(r.get('other-site').repo_id).toBe('other/site');
    expect(r.get('other-site').flags).toContain('NAME_COLLISION');
  }));
  it('migrates legacy data once, preserving human text, duplicates and checkout timestamps', () => fixture(async (r, hub, state) => {
    hub.sql.exec('DROP TABLE repo_checkouts'); hub.sql.exec('DROP TABLE repo_registry');
    hub.sql.exec('CREATE TABLE repo_registry (name TEXT PRIMARY KEY,machine TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL)');
    const at = '2026-10-26T00:00:00Z';
    const entries = [repo({ updated_at: at, last_commit: { sha: 'a'.repeat(40), date: at } }), repo({ name: 'old-site', status: 'duplicate', canonical: 'dtc-website', updated_at: at })];
    for (const entry of entries) hub.sql.exec('INSERT INTO repo_registry VALUES (?,?,?,?)', entry.name, entry.machine, JSON.stringify(entry), at);
    const migrated = new RepoRegistry(hub.sql, fn => state.storage.transactionSync(fn));
    migrated.init(); migrated.init();
    expect(migrated.all()).toHaveLength(2);
    expect(migrated.get('dtc-website').purpose).toBe(repo().purpose);
    expect(migrated.get('dtc-website').checkouts[0]).toMatchObject({ path: '/srv/git/dtc-website', head_sha: 'a'.repeat(40), updated_at: at, cloned_on_demand: false, last_used_at: null });
    expect(migrated.canonical(migrated.get('old-site')).name).toBe('dtc-website');
  }));
  it('projects online availability and host filters, and clears missing status after recovery', () => fixture(async (r, hub) => {
    await seed(r);
    expect(r.get('dtc-website').no_present_online).toBe(true);
    hub.sql.exec('INSERT INTO machines (id,name,hostname,capabilities,registered_at,last_checkin_at) VALUES (?,?,?,?,?,?)', 'hetzner', 'Test host', 'test', '{}', new Date().toISOString(), new Date().toISOString());
    expect(r.get('dtc-website').no_present_online).toBe(false);
    expect((await (await r.handle(req('?host=hetzner'), agent)).json() as any).count).toBe(1);
    expect((await (await r.handle(req('?missing_on=host2'), agent)).json() as any).count).toBe(1);
    await r.handle(req('/dtc-website', 'PATCH', { purpose: 'updated' }), admin);
    await r.handle(req('/refresh', 'POST', { repos: [{ name: 'dtc-website', machine: 'hetzner', dir: '/srv/git/dtc-website', checkout_status: 'missing' }] }), host);
    expect(r.get('dtc-website').no_present_online).toBe(true);
    await r.handle(req('/refresh', 'POST', { repos: [{ name: 'dtc-website', machine: 'hetzner', dir: '/srv/git/dtc-website', checkout_status: 'present' }] }), host);
    expect(r.get('dtc-website').no_present_online).toBe(false);
    expect(r.get('dtc-website').flags).not.toContain('NO_PRESENT_ONLINE');
  }));
});
