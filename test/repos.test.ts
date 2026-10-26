import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { RepoRegistry } from '../src/repos';
import { toolRequest } from '../src/mcp-tools';

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
  }));
  it('imports real names that also occur as another entry alias without losing either entry', () => fixture(async r => {
    await seed(r, [repo({ aliases: ['content'] })]);
    expect((await seed(r, [repo({ name: 'content', aliases: [] })])).status).toBe(200);
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
  it('rejects invalid whole batches without partial writes', () => fixture(async r => {
    expect((await seed(r, [repo(), repo({ name: 'bad', dir: '/srv/../outside' })])).status).toBe(400);
    expect(r.all()).toHaveLength(0);
    expect((await r.handle(req(''), { error: Response.json({}, { status: 401 }) })).status).toBe(401);
  }));
  it('resolves labels and duplicates, pins machine and injects the execution brief', () => fixture(async r => {
    await seed(r);
    await seed(r, [repo({ name: 'old-site', status: 'duplicate', canonical: 'dtc-website', aliases: [] })]);
    for (const workspace of ['dtc-website', 'DTC website', 'PocketShell app/Android', 'old-site']) {
      const result = r.prepareLaunch({ workspace, prompt: 'Read the repo', mode: 'headless' }).input;
      expect(result.workspace).toBe('/srv/git/dtc-website');
      expect(result.machine).toBe('hetzner');
      for (const key of ['origin', 'purpose', 'stack', 'key_folders', 'workflows', 'urls', 'default_branch']) expect(result.prompt).toContain(JSON.stringify((repo() as any)[key]));
    }
    expect(r.prepareLaunch({ workspace: 'dtc-website', machine: 'other', prompt: 'x' }).error.error).toBe('repo_machine_mismatch');
    expect(r.prepareLaunch({ workspace: 'dtc-websit', prompt: 'x' }).error.matches[0]).toBe('dtc-website');
    expect(r.prepareLaunch({ workspace: 'dtc-website', prompt: '', mode: 'interactive' }).input.prompt).toBe('');
  }));
  it('refuses missing canonical entries, loops and missing dirs', () => fixture(async r => {
    await seed(r, [repo({ status: 'duplicate', canonical: 'absent' })]);
    expect(r.prepareLaunch({ workspace: 'dtc-website', prompt: 'x' }).error.error).toBe('canonical_repo_missing');
    await seed(r, [repo({ name: 'absent', status: 'duplicate', canonical: 'dtc-website', aliases: [] })]);
    expect(r.prepareLaunch({ workspace: 'dtc-website', prompt: 'x' }).error.error).toBe('duplicate_cycle');
    await seed(r, [repo({ name: 'missing-site', flags: ['MISSING'], aliases: [] })]);
    expect(r.prepareLaunch({ workspace: 'missing-site', prompt: 'x' }).error.error).toBe('repo_directory_unavailable');
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
