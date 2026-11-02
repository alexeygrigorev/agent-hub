import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { FleetService, decodeRepoWorkspace, encodeRepoWorkspace } from '../src/fleet';
import { freshCpu } from './cpu-fixture';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const machine = 'cBynxGDoKVPoGpGWxvCTvg';

describe('repo-identity workspace (intake 8028bb06)', () => {
  it('round-trips owner/name and refuses ambiguous or hostile identities', () => {
    for (const repo of ['alexeygrigorev/agent-hub', 'a/b.c', 'org/my_repo-2', 'org/x.y.z']) {
      const ws = encodeRepoWorkspace(repo, '8028bb06')!;
      expect(ws).toMatch(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/);
      expect(decodeRepoWorkspace(ws)).toEqual({ id: '8028bb06', repo });
    }
    for (const bad of ['agent-hub', 'a/b/c', '../x', 'a/..', 'a/.hidden', 'a/x__y', 'a/x_d_y', 'a b/c', '/etc/passwd']) {
      expect(encodeRepoWorkspace(bad, '8028bb06')).toBeNull();
    }
    expect(encodeRepoWorkspace('a/b', 'nothex!!')).toBeNull();
    expect(decodeRepoWorkspace('/home/alexey/git/agent-hub')).toBeNull();
  });

  it('places on a Windows host without any host path when it advertises repo_workspaces', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('repo-ws-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const now = Date.now();
      const at = new Date(now).toISOString();
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const caps = { engines: ['codex'], fleet_modes: ['interactive', 'headless'], workspaces: ['C:\\Users\\alexey\\git'] };
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities)
        VALUES (?, 'desktop', 'desktop', 'Windows', 'x64', ?, ?, ?, 0, ?)`,
        machine, at, at, JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(at) }), JSON.stringify(caps));
      hub.sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('codex-orin', 'codex', 4, 15, 1)`);
      hub.sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES (?, 'codex', 'codex-orin', 'hub-codex-orin')`, machine);
      const workspace = encodeRepoWorkspace('alexeygrigorev/agent-hub', '8028bb06')!;
      const plan = (workspaceId: string) => fleet.plan({ mode: 'interactive', machine, workspace: workspaceId, prompt: 'p', idempotency_key: crypto.randomUUID() }) as any;
      // A Linux path is what used to be rejected on Windows hosts.
      expect(plan('/home/alexey/git/agent-hub').rejected.some((r: any) => r.reason === 'workspace_unavailable')).toBe(true);
      // Host that has not been updated (no repo_workspaces): still refused, never silently placed.
      expect(plan(workspace).rejected.some((r: any) => r.reason === 'workspace_unavailable')).toBe(true);
      hub.sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify({ ...caps, repo_workspaces: true }), machine);
      const ok = plan(workspace);
      expect(ok.rejected.filter((r: any) => r.reason === 'workspace_unavailable')).toHaveLength(0);
      expect(ok.selected?.machine_id ?? ok.selected?.machine).toBe(machine);
    });
  });

  it('start accepts repo instead of workspace and stores the repo-identity workspace', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('repo-start-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const seen: any[] = [];
      const service = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), Date.now, {
        reserve: input => { seen.push(input); return { status: 400, value: { error: 'no_eligible_route', rejected: [] } }; },
      });
      service.init();
      seedFixtureIntake(hub.sql);
      const start = (body: any) => service.handle(new Request('https://hub.test/api/sessions', {
        method: 'POST', body: JSON.stringify(withFixtureIntake('/api/sessions', body, 'POST')) }), admin);
      await start({ key: 'repo-ok', repo: 'alexeygrigorev/agent-hub', mode: 'interactive' });
      expect(seen).toHaveLength(1);
      expect(decodeRepoWorkspace(seen[0].workspace)?.repo).toBe('alexeygrigorev/agent-hub');
      const both = await start({ key: 'repo-both', repo: 'a/b', workspace: 'demo', mode: 'interactive' });
      expect(both.status).toBe(400);
      const bad = await start({ key: 'repo-bad', repo: '../etc', mode: 'interactive' });
      expect(bad.status).toBe(400);
    });
  });
});
