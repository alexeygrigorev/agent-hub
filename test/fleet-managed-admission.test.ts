import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const refusal = { engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex',
  mode: 'interactive', reason: 'codex_quota_gate_quota_unavailable', observed_at: iso() };

async function fixture(refusals: any[], fn: (service: FleetService) => void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('managed-admission-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    service.init();
    hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
      'host', 'host', 'host', iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80,
        cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['interactive', 'headless'], workspaces: ['demo'],
        cpu_count: 8, fleet_route_rejections: refusals }));
    const labels = ['shared-codex', 'codex-orin'];
    const req = new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({
      accounts: labels.map(label => ({ label, provider: 'codex', max_concurrency: 10, reserve_percent: 15, enabled: true })),
      routes: labels.map(account => ({ machine_id: 'host', engine: 'codex', account, profile: 'hub-' + account })),
    }) });
    expect((await service.handle(req, admin)).status).toBe(200);
    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false },
      windows: { weekly: { present: true, percent_remaining: 99, reset_at: iso(86400000) } } };
    const subs = new Request('https://hub.test/api/fleet/subscriptions', { method: 'PUT', body: JSON.stringify({
      subscriptions: labels.map(account => ({ service: 'codex', account, plan: 'pro', quota })),
    }) });
    expect((await service.handle(subs, { caller: { machineId: 'host', kind: 'host', actor: 'machine:host' }, scopes: ['host'] })).status).toBe(200);
    fn(service);
  });
}

describe('managed Codex account admission', () => {
  it('fails over from a refused managed account despite fresh 99% quota and a healthy sibling gate', async () => {
    await fixture([refusal], service => {
      const plan = service.plan({ workspace: 'demo', mode: 'interactive', prompt: 'test', idempotency_key: crypto.randomUUID() });
      expect(plan.selected?.account).toBe('codex-orin');
      expect(plan.rejected).toContainEqual(expect.objectContaining({ reason: refusal.reason }));
      expect(plan.candidates?.some((r: any) => r.account === 'shared-codex')).toBe(false);
    });
  });
  it.each([
    { ...refusal, profile: 'different-profile' },
    { ...refusal, mode: 'headless' },
    { ...refusal, observed_at: iso(-300001) },
    { ...refusal, reason: 'untrusted arbitrary output' },
  ])('scopes refusals to the exact fresh route: %j', async bad => {
    await fixture([bad], service => {
      const plan = service.plan({ workspace: 'demo', mode: 'interactive', prompt: 'test', idempotency_key: crypto.randomUUID() });
      expect(plan.candidates?.some((r: any) => r.account === 'shared-codex')).toBe(true);
    });
  });
});
