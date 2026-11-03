// Intake 3d1f8702: unspecified starts must spread by quota headroom across engines
// (Claude sonnet, Z.ai, OpenCode muse, Antigravity, Codex luna) instead of always
// landing on Codex luna, and unmetered engines (no host quota API) must stay placeable.
// Intake 9647e6d5: fully unpinned auto-starts draw weighted by headroom with no
// fixed order; the deterministic top pick stays for pins, previews and callers
// without a random source; default routing never resolves high-effort models
// (Antigravity is gemini-4-argon-medium, never gemini-4-argon-high).
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { drawAutoStart } from '../src/fleet';
import { mulberry32, weightedDrawIndex } from '../src/draw';
import { policy } from '../src/model-routing';
import { freshCpu } from './cpu-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Windows are half elapsed (on schedule for scheduleBurnFactor), so this test exercises the load/headroom spread
// and not the burn-rate boost a nearly-expired window gives the fullest account.
const metered = (at: string, pct: number, now: number) => JSON.stringify({
  status: 'ok', error: null, observed_at: at, details: { limit_reached: false },
  windows: {
    '5h': { present: true, percent_remaining: pct, reset_at: new Date(now + 2.5 * 3600000).toISOString(), error: null },
    '7d': { present: true, percent_remaining: pct, reset_at: new Date(now + 3.5 * 86400000).toISOString(), error: null },
  },
});
const broken = (at: string) => JSON.stringify({ status: 'unknown', error: 'unavailable', observed_at: at, details: { limit_reached: null }, windows: {} });

describe('default tier policy', () => {
  it('does not list luna first and excludes haiku, sol-high and GLM on opencode', () => {
    const daily = policy.tiers.daily as string[];
    expect(policy.default_tier).toBe('daily');
    expect(daily[0]).not.toBe('luna');
    expect(daily.indexOf('luna')).toBeGreaterThan(daily.indexOf('sonnet'));
    expect(daily.indexOf('luna')).toBeGreaterThan(daily.indexOf('muse'));
    const byId = new Map((policy.models as any[]).map(m => [m.id, m]));
    for (const id of daily) {
      const m = byId.get(id);
      expect(/haiku/i.test(m.model), id).toBe(false);
      expect(m.id === 'sol' && m.effort === 'high', id).toBe(false);
      expect(m.engine === 'opencode' && /glm/i.test(m.model), id).toBe(false);
    }
    expect(byId.get('luna')).toMatchObject({ effort: 'max' });
    expect(byId.get('sonnet')).toMatchObject({ effort: 'medium' });
  });

  it('never defaults to high effort: antigravity is medium and argon-high is gone', () => {
    const byId = new Map((policy.models as any[]).map(m => [m.id, m]));
    // Intake 9647e6d5: an unpinned auto-start must never resolve Antigravity
    // to gemini-4-argon-high. Both Antigravity-family legs run at medium.
    expect(byId.get('argon')).toMatchObject({ model: 'gemini-4-argon-medium', effort: 'medium' });
    expect(byId.get('argon-gemini')).toMatchObject({ model: 'gemini-2.5-pro', effort: 'medium' });
    const daily = policy.tiers.daily as string[];
    expect(daily).not.toContain('haiku');
    expect(daily).not.toContain('astra');
    for (const id of daily) {
      const m = byId.get(id);
      expect(m.model, id).not.toBe('gemini-4-argon-high');
      expect(String(m.id).toLowerCase(), id).not.toContain('astra');
    }
  });
});

describe('default tier placement', () => {
  it('spreads unspecified starts across quota-ready and unmetered preferred engines', async () => {
    const now = Date.now();
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('default-tier-spread-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const machine = 'cBynxGDoKVPoGpGWxvCTvg';
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, machine, iso(), iso(), JSON.stringify({
          mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() },
        }), JSON.stringify({ engines: ['codex', 'claude', 'antigravity', 'zcodex', 'opencode'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] } as any;
      const put = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({
        accounts: [
          { label: 'shared-codex', provider: 'codex', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'shared-claude', provider: 'claude', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'hub-antigravity', provider: 'gemini', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'shared-zai', provider: 'zai', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'shared-go', provider: 'go', max_concurrency: 4, reserve_percent: 0, enabled: true },
        ],
        routes: [
          { machine_id: machine, engine: 'codex', account: 'shared-codex', profile: null },
          { machine_id: machine, engine: 'claude', account: 'shared-claude', profile: null },
          { machine_id: machine, engine: 'antigravity', account: 'hub-antigravity', profile: 'hub-hub-antigravity' },
          { machine_id: machine, engine: 'zcodex', account: 'shared-zai', profile: null },
          { machine_id: machine, engine: 'opencode', account: 'shared-go', profile: null },
        ] }) }), admin);
      expect(put.status, await put.clone().text()).toBe(200);
      const sub = (account: string, service: string, quota: string) => hub.sql.exec(
        'INSERT INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)', machine, account, service, null, iso(), quota);
      sub('shared-codex', 'codex', metered(iso(), 60, now));
      sub('shared-claude', 'claude', metered(iso(), 27, now));
      // The probes for the unmetered engines fail on the host, as observed live.
      sub('hub-antigravity', 'gemini', broken(iso()));
      sub('shared-zai', 'zai', metered(iso(), 60, now));
      sub('shared-go', 'go', broken(iso()));

      expect(fleet.accountStatus(hub.sql.exec("SELECT * FROM fleet_accounts WHERE label='hub-antigravity'").one())).toMatchObject({ status: 'ok' });
      expect(fleet.accountStatus(hub.sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-go'").one())).toMatchObject({ status: 'ok' });

      const recommendRes = await fleet.handle(new Request('https://hub.test/api/fleet/recommend', {
        method: 'POST',
        body: JSON.stringify({ machine, mode: 'headless', workspace: 'demo', prompt: 'draw probabilities' }),
      }), admin);
      expect(recommendRes.status, await recommendRes.clone().text()).toBe(200);
      const recommendBody: any = await recommendRes.json();
      expect(recommendBody.selected.engine).toBe(recommendBody.candidates[0].engine);
      expect(recommendBody.candidates.every((candidate: any) => Number.isFinite(candidate.draw_probability))).toBe(true);
      expect(recommendBody.candidates.reduce((sum: number, candidate: any) => sum + candidate.draw_probability, 0)).toBeCloseTo(1, 6);

      const engines: string[] = [];
      for (let i = 0; i < 4; i++) {
        const prompt = 'start ' + i;
        const plan = fleet.plan({ machine, mode: 'headless', workspace: 'demo', prompt });
        expect(plan.selected, JSON.stringify(plan.rejected)).toBeTruthy();
        engines.push(plan.selected.engine);
        const reserved = fleet.reserve({ workspace: 'demo', machine, engine: plan.selected.engine, account: plan.selected.account, prompt,
          idempotency_key: 'spread-' + i, mode: 'headless' }, { caller: admin.caller, scopes: ['admin'] });
        expect(reserved.status, JSON.stringify(reserved.value)).toBeLessThan(300);
      }
      expect(new Set(engines).size, engines.join(',')).toBeGreaterThanOrEqual(3);
      expect(engines.slice(0, 3).every(e => e === 'codex'), engines.join(',')).toBe(false);
      expect(engines).toContain('antigravity');
      expect(engines).toContain('opencode');
      expect(engines).toContain('zcodex');
    });
  });

  it('keeps healthy codex in the mix and starves depleted claude at the 2026-11-02 quota (claude 10, zai 8, muse 43, codex 45, antigravity 40)', async () => {
    const now = Date.now();
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('default-tier-mix-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const machine = 'cBynxGDoKVPoGpGWxvCTvg';
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, machine, iso(), iso(), JSON.stringify({
          mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() },
        }), JSON.stringify({ engines: ['codex', 'claude', 'antigravity', 'opencode', 'zcodex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] } as any;
      const put = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({
        accounts: [
          { label: 'shared-codex', provider: 'codex', max_concurrency: 6, reserve_percent: 0, enabled: true },
          { label: 'shared-claude', provider: 'claude', max_concurrency: 6, reserve_percent: 0, enabled: true },
          { label: 'hub-antigravity', provider: 'gemini', max_concurrency: 6, reserve_percent: 0, enabled: true },
          { label: 'shared-go', provider: 'go', max_concurrency: 6, reserve_percent: 0, enabled: true },
          { label: 'zai-central', provider: 'zai', max_concurrency: 6, reserve_percent: 0, enabled: true },
        ],
        routes: [
          { machine_id: machine, engine: 'codex', account: 'shared-codex', profile: null },
          { machine_id: machine, engine: 'claude', account: 'shared-claude', profile: null },
          { machine_id: machine, engine: 'antigravity', account: 'hub-antigravity', profile: 'hub-hub-antigravity' },
          { machine_id: machine, engine: 'opencode', account: 'shared-go', profile: 'hub-shared-go' },
          { machine_id: machine, engine: 'zcodex', account: 'zai-central', profile: null },
        ] }) }), admin);
      expect(put.status, await put.clone().text()).toBe(200);
      const sub = (account: string, service: string, pct: number) => hub.sql.exec(
        'INSERT INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)', machine, account, service, null, iso(), metered(iso(), pct, now));
      sub('shared-codex', 'codex', 45);
      sub('shared-claude', 'claude', 10);
      sub('hub-antigravity', 'gemini', 40);
      sub('shared-go', 'go', 43);
      sub('zai-central', 'zai', 8);

      const counts: Record<string, number> = {};
      for (let i = 0; i < 10; i++) {
        const prompt = 'mix ' + i;
        const plan = fleet.plan({ machine, mode: 'headless', workspace: 'demo', prompt });
        expect(plan.selected, JSON.stringify(plan.rejected)).toBeTruthy();
        counts[plan.selected.engine] = (counts[plan.selected.engine] ?? 0) + 1;
        const reserved = fleet.reserve({ workspace: 'demo', machine, engine: plan.selected.engine, account: plan.selected.account, prompt,
          idempotency_key: 'mix-' + i, mode: 'headless' }, { caller: admin.caller, scopes: ['admin'] });
        expect(reserved.status, JSON.stringify(reserved.value)).toBeLessThan(300);
      }
      console.log('ENGINE_MIX ' + JSON.stringify(counts));
      expect(counts.codex ?? 0, JSON.stringify(counts)).toBeGreaterThanOrEqual(1);
      expect(counts.claude ?? 0, JSON.stringify(counts)).toBeLessThanOrEqual(Math.min(counts.codex ?? 0, counts.antigravity ?? 0, counts.opencode ?? 0));
      expect(Object.keys(counts).length, JSON.stringify(counts)).toBeGreaterThanOrEqual(3);
    });
  });

  it('still withholds an unmetered engine that reports positive exhaustion', async () => {
    const now = Date.now();
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('unmetered-exhausted-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-go','go',2,0,1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','opencode','shared-go',NULL)");
      hub.sql.exec('INSERT INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)', 'm1', 'shared-go', 'go', null, iso(),
        JSON.stringify({ status: 'unknown', error: 'unavailable', observed_at: iso(), details: { limit_reached: true }, windows: {} }));
      const status = fleet.accountStatus(hub.sql.exec("SELECT * FROM fleet_accounts WHERE label='shared-go'").one());
      expect(status.status).toBe('blocked');
    });
  });
});

describe('weighted random auto-start (intake 9647e6d5)', () => {
  it('draws units: zero mass falls back to first, single draw consumes one random', () => {
    expect(weightedDrawIndex([1, 2, 3], () => 0)).toBe(0);
    expect(weightedDrawIndex([1, 2, 3], () => 0.9999)).toBe(2);
    expect(weightedDrawIndex([0, 0, 0], () => 0.5)).toBe(0);
    expect(weightedDrawIndex([], () => 0.5)).toBe(0);
    let calls = 0;
    expect(weightedDrawIndex([5, 5], () => { calls++; return 0.75; })).toBe(1);
    expect(calls).toBe(1);
    expect(drawAutoStart([], {}, [], {}, Math.random)).toBeNull();
    const top = { score: 9 };
    expect(drawAutoStart([top, { score: 1 }], {}, [], {})).toBe(top);
    expect(drawAutoStart([top, { score: 1 }], { engine: 'codex' }, [], {}, () => 0.99)).toBe(top);
  });

  it('excludes depleted routes from the weighted draw even when they have a positive score', () => {
    const healthy = { engine: 'codex', decision: { score: 1, factors: { depleted: false } } };
    const depleted = { engine: 'claude', decision: { score: 100, factors: { depleted: true } } };
    expect(drawAutoStart([healthy, depleted], {}, [], {}, () => 0.9999)).toBe(healthy);
    expect((depleted as any).draw_probability).toBe(0);
  });

  it('spreads fully unpinned starts weighted by headroom with no fixed order', async () => {
    const now = Date.now();
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('auto-draw-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const machine = 'drawMachine01';
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, machine, iso(), iso(), JSON.stringify({
          mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() },
        }), JSON.stringify({ engines: ['codex', 'claude', 'antigravity', 'zcodex', 'opencode'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] } as any;
      const put = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({
        accounts: [
          { label: 'shared-codex', provider: 'codex', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'shared-claude', provider: 'claude', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'hub-antigravity', provider: 'gemini', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'zai-central', provider: 'zai', max_concurrency: 4, reserve_percent: 0, enabled: true },
          { label: 'shared-go', provider: 'go', max_concurrency: 4, reserve_percent: 0, enabled: true },
        ],
        routes: [
          { machine_id: machine, engine: 'codex', account: 'shared-codex', profile: null },
          { machine_id: machine, engine: 'claude', account: 'shared-claude', profile: null },
          { machine_id: machine, engine: 'antigravity', account: 'hub-antigravity', profile: 'hub-hub-antigravity' },
          { machine_id: machine, engine: 'zcodex', account: 'zai-central', profile: null },
          { machine_id: machine, engine: 'opencode', account: 'shared-go', profile: null },
        ] }) }), admin);
      expect(put.status, await put.clone().text()).toBe(200);
      const sub = (account: string, service: string, quota: string) => hub.sql.exec(
        'INSERT INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)', machine, account, service, null, iso(), quota);
      sub('shared-codex', 'codex', metered(iso(), 60, now));
      // 35% keeps Claude above the configured 30% low-headroom floor; the
      // separate depleted-route test proves below-floor routes get no draw.
      sub('shared-claude', 'claude', metered(iso(), 35, now));
      sub('zai-central', 'zai', metered(iso(), 31, now));
      // The probes for the unmetered engines fail on the host, as observed live.
      sub('hub-antigravity', 'gemini', broken(iso()));
      sub('shared-go', 'go', broken(iso()));

      // Without a random source the fully unpinned start stays deterministic.
      const top = fleet.plan({ mode: 'headless', workspace: 'demo', prompt: 'deterministic' });
      expect(top.selected, JSON.stringify(top.rejected)).toBeTruthy();
      expect(top.candidates!.length).toBe(5);
      expect(top.selected).toBe(top.candidates![0]);
      // A pinned engine keeps the top pick even with a random source.
      const pinned = fleet.plan({ mode: 'headless', workspace: 'demo', prompt: 'pinned', engine: 'codex' }, undefined, mulberry32(9647));
      expect(pinned.selected).toBe(pinned.candidates![0]);
      expect(pinned.selected.engine).toBe('codex');

      // Seeded draws spread over every engine with headroom, weighted by it.
      const draw = (seed: number, n: number) => {
        const rand = mulberry32(seed);
        const engines: string[] = [];
        for (let i = 0; i < n; i++) {
          const planned: any = fleet.plan({ mode: 'headless', workspace: 'demo', prompt: 'draw ' + i }, undefined, rand);
          expect(planned.selected, JSON.stringify(planned.rejected)).toBeTruthy();
          engines.push(planned.selected.engine as string);
        }
        return engines;
      };
      const seqA = draw(9647, 400);
      expect(draw(9647, 400)).toEqual(seqA);
      // No fixed order: another seed opens differently.
      expect(draw(9648, 10)).not.toEqual(seqA.slice(0, 10));
      const counts = new Map<string, number>();
      for (const e of seqA) counts.set(e, (counts.get(e) ?? 0) + 1);
      // Every engine with headroom gets starts; nothing stacks on one winner.
      expect([...counts.keys()].sort()).toEqual(['antigravity', 'claude', 'codex', 'opencode', 'zcodex']);
      for (const [, n] of counts) expect(n).toBeGreaterThanOrEqual(20);
      // Weighting follows quota headroom: 60% codex outdraws 35% claude.
      expect(counts.get('codex')!).toBeGreaterThan(counts.get('claude')!);
      // Luna never takes every start (the t2555u complaint).
      expect(counts.get('codex')).toBeLessThan(400);

      // The drawn legs are the wanted default models, never high-effort
      // Antigravity-high, haiku or astra.
      const seen = new Map<string, any>();
      {
        const rand = mulberry32(9647);
        for (let i = 0; i < 400; i++) {
          const planned: any = fleet.plan({ mode: 'headless', workspace: 'demo', prompt: 'legs ' + i }, undefined, rand);
          const sel = planned.selected.selection;
          if (!seen.has(planned.selected.engine)) seen.set(planned.selected.engine, sel);
          expect(sel.model, planned.selected.engine).not.toBe('gemini-4-argon-high');
          expect(String(sel.id).toLowerCase()).not.toContain('haiku');
          expect(String(sel.id).toLowerCase()).not.toContain('astra');
          expect(String(sel.model).toLowerCase()).not.toContain('haiku');
        }
      }
      expect(seen.get('claude')).toMatchObject({ id: 'sonnet', effort: 'medium' });
      expect(seen.get('antigravity')).toMatchObject({ id: 'argon', model: 'gemini-4-argon-medium', effort: 'medium' });
      expect(seen.get('opencode')).toMatchObject({ id: 'muse' });
      expect(seen.get('codex')).toMatchObject({ id: 'luna', effort: 'max' });
      expect(seen.get('zcodex')).toMatchObject({ model: 'glm-5.3-flash' });
    });
  });
});
