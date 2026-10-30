import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { planLimitSet, resolveLimitTarget } from '../src/fleet-limits';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = { caller: { kind: 'host', actor: 'machine:limits-host', isAdmin: false }, scopes: ['host'] };
const now = Date.now();
const iso = (offset = 0) => new Date(now + offset).toISOString();

describe('cbcadaa1 hub limits', () => {
  it('maps an engine to every account of its provider and a label to one account', () => {
    const rows = [{ label: 'hub-claude', provider: 'claude', max_concurrency: 8 }, { label: 'shared-claude', provider: 'claude', max_concurrency: 100 },
      { label: 'urgent-muse', provider: 'go', max_concurrency: 4 }, { label: 'shared-codex', provider: 'codex', max_concurrency: 8 }];
    expect(resolveLimitTarget('claude', rows, []).rows.map(r => r.label)).toEqual(['hub-claude', 'shared-claude']);
    expect(resolveLimitTarget('hub-claude', rows, [])).toMatchObject({ kind: 'account', rows: [{ label: 'hub-claude' }] });
    // A canonical label also addresses its legacy alias row.
    expect(resolveLimitTarget('shared-go', rows, []).rows.map(r => r.label)).toEqual(['urgent-muse']);
    expect(resolveLimitTarget('opencode', rows, [{ account: 'urgent-muse', engine: 'opencode' }]).rows.map(r => r.label)).toEqual(['urgent-muse']);
    expect(resolveLimitTarget('nope', rows, []).rows).toEqual([]);
    const claude = resolveLimitTarget('claude', rows, []).rows;
    expect(planLimitSet('engine', claude, 13, false).changes.map(c => [c.label, c.to, c.result]))
      .toEqual([['hub-claude', 13, 'raised'], ['shared-claude', 100, 'kept_higher']]);
    expect(planLimitSet('engine', claude, 13, true).changes.map(c => c.result)).toEqual(['raised', 'lowered']);
    expect(planLimitSet('account', [rows[1]], 13, false).error).toBe('account_lowering_requires_allow_lower');
  });

  it('shows caps with usage and sets them live and persistently, admin only', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('limits-cbcadaa1-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      hub.sql.exec(`INSERT INTO machines (id,name,hostname,registered_at,last_checkin_at,drained,capabilities)
        VALUES ('limits-host','limits-host','limits-host',?,?,0,?)`, iso(), iso(), JSON.stringify({ engines: ['claude'], fleet_modes: ['interactive'], workspaces: ['demo'] }));
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('hub-claude','claude',8,5,1)");
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-claude','claude',100,5,1)");
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex','codex',8,0,1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('limits-host','claude','hub-claude',NULL)");
      hub.sql.exec(`INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,
        machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        VALUES ('live','test-admin','human','existing','live-key','{}','limits-host','claude','hub-claude','interactive','demo','','running',?,?)`, iso(), iso());
      const call = (method: string, payload?: unknown, auth: any = admin) => fleet.handle(new Request('https://hub.test/api/fleet/limits',
        { method, body: payload === undefined ? undefined : JSON.stringify(payload) }), auth);
      const cap = (label: string) => Number(hub.sql.exec('SELECT max_concurrency FROM fleet_accounts WHERE label=?', label).one().max_concurrency);

      expect((await call('GET', undefined, host)).status).toBe(403);
      expect((await call('PATCH', { target: 'claude', max_concurrency: 13 }, host)).status).toBe(403);
      const shown: any = await (await call('GET')).json();
      expect(shown.engines).toEqual([{ engine: 'claude', accounts: ['hub-claude', 'shared-claude'], max_concurrency: 108, active: 1 },
        { engine: 'codex', accounts: ['shared-codex'], max_concurrency: 8, active: 0 }]);
      expect(shown.accounts.find((a: any) => a.account === 'hub-claude')).toMatchObject({ provider: 'claude', engines: ['claude'], max_concurrency: 8, active: 1, available_slots: 7 });

      for (const bad of [{ target: 'claude', max_concurrency: 0 }, { target: 'claude', max_concurrency: 101 }, { target: 'claude', max_concurrency: 2.5 },
        { target: 'claude' }, { target: 'claude', max_concurrency: 13, allow_lower: 'yes' }, { target: 'claude', max_concurrency: 13, extra: 1 }])
        expect((await call('PATCH', bad)).status).toBe(400);
      expect((await call('PATCH', { target: 'missing', max_concurrency: 13 })).status).toBe(404);

      const set: any = await (await call('PATCH', { target: 'claude', max_concurrency: 13 })).json();
      expect(set).toMatchObject({ status: 'configured', kind: 'engine', changes: [
        { account: 'hub-claude', from: 8, to: 13, result: 'raised' }, { account: 'shared-claude', from: 100, to: 100, result: 'kept_higher' }] });
      expect([cap('hub-claude'), cap('shared-claude'), cap('shared-codex')]).toEqual([13, 100, 8]);
      expect(hub.sql.exec("SELECT actor, target, detail FROM audit WHERE action='fleet_limits_set'").toArray()).toEqual([
        { actor: 'test-admin', target: 'claude', detail: JSON.stringify([{ account: 'hub-claude', from: 8, to: 13 }]) }]);
      expect(((await (await call('GET')).json()) as any).accounts.find((a: any) => a.account === 'hub-claude')).toMatchObject({ max_concurrency: 13, available_slots: 12 });

      // Idempotent repeat, account-level lowering needs allow_lower and is atomic.
      expect(((await (await call('PATCH', { target: 'claude', max_concurrency: 13 })).json()) as any).changes[0].result).toBe('unchanged');
      expect((await call('PATCH', { target: 'hub-claude', max_concurrency: 10 })).status).toBe(409);
      expect(cap('hub-claude')).toBe(13);
      expect((await call('PATCH', { target: 'hub-claude', max_concurrency: 10, allow_lower: true })).status).toBe(200);
      expect(cap('hub-claude')).toBe(10);

      // Persisted: a fresh service over the same storage reads the new cap.
      const restarted = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      restarted.init();
      expect(restarted.accountStatus(hub.sql.exec("SELECT * FROM fleet_accounts WHERE label='hub-claude'").one())).toMatchObject({ max_concurrency: 10 });
    });
  });
});
