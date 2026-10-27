import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { evaluateQuotaAdmission } from '../src/quota_admission';
import policy from '../config/model-routing.json';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const now = Date.now();
const iso = (offset = 0) => new Date(now + offset).toISOString();
const quota = (remaining: number) => ({ status: 'ok', observed_at: iso(), details: { limit_reached: false },
  windows: { weekly: { present: true, percent_remaining: remaining, reset_at: iso(86400000) } } });

describe('c269 Codex zero reserve', () => {
  it.each([[9, true], [0, false]] as const)('canonical admission at %s percent', (remaining, ok) => {
    expect(evaluateQuotaAdmission({ provider: 'codex', quota: quota(remaining),
      subscriptionObservedAt: iso(), nowMs: now, reserves: policy.rules.reserve_percent }).ok).toBe(ok);
  });

  it.each(['shared-codex', 'codex-orin'])('registers zero reserve and blocks only depletion for %s', async account => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('reserve-c269-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      hub.sql.exec(`INSERT INTO machines (id,name,hostname,registered_at,last_checkin_at,drained,capabilities)
        VALUES ('reserve-host','reserve-host','reserve-host',?,?,0,?)`, iso(), iso(),
        JSON.stringify({ engines: ['codex'], fleet_modes: ['interactive'], workspaces: ['demo'] }));
      const response = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT',
        body: JSON.stringify({ accounts: [{ label: account, provider: 'codex', max_concurrency: 8,
          reserve_percent: 0, enabled: true }], routes: [{ machine_id: 'reserve-host', engine: 'codex',
          account, profile: null }] }) }), admin);
      expect(response.status).toBe(200);
      for (const [remaining, status] of [[9, 'ok'], [0, 'blocked']] as const) {
        const observed = await fleet.handle(new Request('https://hub.test/api/fleet/subscriptions', { method: 'PUT',
          body: JSON.stringify({ subscriptions: [{ service: 'codex', account, plan: 'pro', quota: quota(remaining) }] }) }),
        { caller: { machineId: 'reserve-host', kind: 'host', actor: 'machine:reserve-host' }, scopes: ['host'] });
        expect(observed.status).toBe(200);
        const row = hub.sql.exec('SELECT * FROM fleet_accounts WHERE label=?', account).one();
        expect(fleet.accountStatus(row)).toMatchObject({ status, remaining_percent: remaining, reserve_percent: 0 });
      }
    });
  });

  it('allows an operator to lower reserve while preserving live reservations and observations', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('reserve-update-c269-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex','codex',8,15,1)");
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-grok','grok',8,5,1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('reserve-host','codex','shared-codex',NULL)");
      hub.sql.exec("INSERT INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES ('reserve-host','shared-codex','codex','pro',?,?)", iso(), JSON.stringify(quota(9)));
      hub.sql.exec(`INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,
        machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        VALUES ('live','test-admin','human','existing','live-key','{}','reserve-host','codex','shared-codex',
        'interactive','demo','','running',?,?)`, iso(), iso());
      const before = ['fleet_routes', 'fleet_subscriptions', 'fleet_launches'].map(t => hub.sql.exec('SELECT * FROM ' + t).toArray());
      const patch = (accounts: unknown[], auth = admin) => fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PATCH', body: JSON.stringify({ accounts }) }), auth);
      expect((await patch([{ label: 'shared-codex', reserve_percent: 0 }],
        { caller: { kind: 'host', actor: 'machine:reserve-host', isAdmin: false }, scopes: ['host'] })).status).toBe(403);
      // A bad later item cannot partially apply the earlier valid lowering.
      expect((await patch([{ label: 'shared-codex', reserve_percent: 0 }, { label: 'missing', reserve_percent: 0 }])).status).toBe(409);
      expect(hub.sql.exec("SELECT reserve_percent FROM fleet_accounts WHERE label='shared-codex'").one().reserve_percent).toBe(15);
      expect((await patch([{ label: 'shared-grok', reserve_percent: 0 }])).status).toBe(400);
      expect((await patch([{ label: 'shared-codex', reserve_percent: 0 }])).status).toBe(200);
      expect(hub.sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-codex'").one()).toMatchObject({ reserve_percent: 0, max_concurrency: 8 });
      expect((await patch([{ label: 'shared-codex', reserve_percent: 1 }])).status).toBe(409);
      expect((await patch([{ label: 'shared-codex', reserve_percent: false }])).status).toBe(400);
      expect((await patch([{ label: 'shared-codex' }])).status).toBe(400);
      expect(['fleet_routes', 'fleet_subscriptions', 'fleet_launches'].map(t => hub.sql.exec('SELECT * FROM ' + t).toArray())).toEqual(before);
    });
  });
});
