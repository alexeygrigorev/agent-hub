import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { FleetService } from '../src/fleet';
import { freshCpu } from './cpu-fixture';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const machine = 'cBynxGDoKVPoGpGWxvCTvg';

describe('pinned session refusal diagnostics (c22a5453)', () => {
  it('identifies unavailable workspace/cwd on each pinned route without bypassing quota refusals', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('pinned-workspace-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const now = Date.now();
      const at = new Date(now).toISOString();
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const caps = { engines: ['codex'], fleet_modes: ['interactive', 'headless'], workspaces: ['demo'] };
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities)
        VALUES (?, 'tabo', 'tabo', 'Windows', 'arm64', ?, ?, ?, 0, ?)`,
        machine, at, at, JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(at) }), JSON.stringify(caps));
      for (const label of ['codex-orin', 'shared-codex']) {
        hub.sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES (?, 'codex', 4, 15, 1)`, label);
        hub.sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES (?, 'codex', ?, ?)`, machine, label, 'hub-' + label);
      }
      const plan = (extra: any) => fleet.plan({ mode: 'interactive', machine, workspace: 'demo', prompt: 'workspace probe', idempotency_key: crypto.randomUUID(), ...extra });
      for (const extra of [{ workspace: 'unadvertised' }, { cwd: 'D:\\unadvertised\\repo' }]) {
        const result: any = plan(extra);
        expect(result.selected).toBeNull();
        expect(result.rejected).toHaveLength(2);
        expect(result.rejected.every((r: any) => r.machine === machine && r.reason === 'workspace_unavailable')).toBe(true);
      }
      const quota = ['codex-orin', 'shared-codex'].map(account => ({
        engine: 'codex', account, profile: 'hub-' + account, mode: 'interactive',
        reason: 'codex_quota_gate_codex_reserve_required', observed_at: at,
      }));
      hub.sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify({ ...caps, fleet_route_rejections: quota }), machine);
      const held: any = plan({ workspace: 'unadvertised' });
      expect(held.selected).toBeNull();
      expect(held.rejected).toHaveLength(2);
      expect(held.rejected.every((r: any) => r.reason === 'codex_quota_gate_codex_reserve_required')).toBe(true);
    });
  });

  for (const status of [400, 409]) {
    it(`preserves per-route rejection details on start and show (reserve ${status})`, async () => {
      const bindings = env as unknown as Env;
      const stub = bindings.HUB.get(bindings.HUB.idFromName('pinned-refusal-' + crypto.randomUUID()));
      await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
        const rejected = [
          { machine, engine: 'codex', account: 'codex-orin', reason: 'codex_quota_gate_quota_check_failed' },
          { machine, engine: 'codex', account: 'shared-codex', reason: 'workspace_unavailable' },
        ];
        const service = new SessionControlService(hub.sql,
          cb => state.storage.transactionSync(cb), Date.now, {
            reserve: input => {
              expect(input.machine).toBe(machine);
              return { status, value: { error: 'no_eligible_route', rejected } };
            },
          });
        service.init();
        seedFixtureIntake(hub.sql);
        const response = await service.handle(new Request('https://hub.test/api/sessions', {
          method: 'POST', body: JSON.stringify(withFixtureIntake('/api/sessions', {
            key: 'pinned-refusal', workspace: 'demo', machine, mode: 'interactive',
          }, 'POST')),
        }), admin);
        expect(response.status).toBe(status);
        const created: any = await response.json();
        expect(created.id).toEqual(expect.any(String));
        expect(created.state).toBe(status === 400 ? 'refused' : 'held');
        expect(created.reason).toBe(rejected[0].reason);
        expect(created.rejected).toEqual(rejected);
        const stored: any = hub.sql.exec('SELECT hold_json FROM session_control_sessions WHERE id = ?', created.id).toArray()[0];
        expect(JSON.parse(stored.hold_json).rejected).toEqual(rejected);
        expect(JSON.parse(stored.hold_json).reason).toBe(created.reason);
        const shown: any = await (await service.handle(new Request(
          'https://hub.test/api/sessions/' + created.id), admin)).json();
        expect(shown.state).toBe(created.state);
        expect(shown.reason).toBe(created.reason);
        expect(shown.rejected).toEqual(rejected);
        // Legacy rows that lost their original diagnostic still expose an
        // explicit unknown reason rather than implying successful placement.
        hub.sql.exec('UPDATE session_control_sessions SET hold_json = NULL WHERE id = ?', created.id);
        const legacy: any = await (await service.handle(new Request(
          'https://hub.test/api/sessions/' + created.id), admin)).json();
        expect(legacy.reason).toEqual(expect.any(String));
        expect(legacy.reason.length).toBeGreaterThan(0);
        expect(Array.isArray(legacy.rejected)).toBe(true);
      });
    });
  }
});
