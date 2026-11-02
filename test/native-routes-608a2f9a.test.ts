// Intake 608a2f9a: Windows hosts advertised a logged-in zcodex/opencode route
// but Hub never offered it, because native (host-home) routes existed only
// where an operator had written them into the registry.
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { registerAdvertisedNativeRoutes } from '../src/registry';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const iso = () => new Date().toISOString();

async function hub(fn: (sql: any) => void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('native-routes-608a2f9a-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (h: Hub, state: DurableObjectState) => {
    const service = new FleetService(h.sql, (cb) => state.storage.transactionSync(cb), () => Date.now());
    service.init();
    h.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?)',
      'win', 'win', 'win', iso(), iso(), 0, JSON.stringify({ engines: ['codex', 'zcodex', 'opencode'] }));
    const put = await service.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({ accounts: [
      { label: 'shared-zai', provider: 'zai', max_concurrency: 2, reserve_percent: 15, enabled: true },
      { label: 'shared-go', provider: 'go', max_concurrency: 2, reserve_percent: 15, enabled: false },
      { label: 'shared-codex', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true },
    ], routes: [] }) }), admin);
    expect(put.status).toBe(200);
    fn(h.sql);
  });
}
const routes = (sql: any) => sql.exec("SELECT engine, account, profile FROM fleet_routes WHERE machine_id = 'win' ORDER BY account").toArray()
  .map((r: any) => [r.engine, r.account, r.profile]);

describe('advertised native routes become placeable (608a2f9a)', () => {
  it('registers an enabled catalog account whose engine matches its provider', () => hub((sql) => {
    expect(registerAdvertisedNativeRoutes('win', [{ engine: 'zcodex', account: 'shared-zai', profile: null }], sql)).toBe(1);
    expect(routes(sql)).toEqual([['zcodex', 'shared-zai', null]]);
    // Idempotent on every later advertisement.
    expect(registerAdvertisedNativeRoutes('win', [{ engine: 'zcodex', account: 'shared-zai', profile: null }], sql)).toBe(0);
  }));

  it('ignores managed profiles, unknown or disabled accounts and engine/provider mismatches', () => hub((sql) => {
    expect(registerAdvertisedNativeRoutes('win', [
      { engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' },
      { engine: 'codex', account: 'shared-codex', profile: null },
      { engine: 'opencode', account: 'shared-go', profile: null },
      { engine: 'zcodex', account: 'unknown-zai', profile: null },
      { engine: 'opencode', account: 'shared-zai', profile: null },
    ], sql)).toBe(0);
    expect(routes(sql)).toEqual([]);
  }));
});
