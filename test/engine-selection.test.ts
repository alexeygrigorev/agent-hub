import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { QuotaStore } from '../src/quota';
import { modelCandidates } from '../src/model-routing';
import { analyzeQuotaBurn, computeEngineDecision, fallbackFactor, isDesktopCodexExcluded, isHealthyAlternative, scheduleBurnFactor } from '../src/pacing';
import routingPolicy from '../config/model-routing.json';
import { freshCpu } from './cpu-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const H = 3600_000;
const D = 24 * H;

function makeQuota(now: number, opts: { pct5h?: number; hours5h?: number; pct7d?: number; hours7d?: number; limitReached?: boolean }) {
  const iso = new Date(now).toISOString();
  const windows: Record<string, any> = {};
  if (typeof opts.pct5h === 'number') {
    windows['5h'] = {
      present: true,
      percent_remaining: opts.pct5h,
      reset_at: new Date(now + (opts.hours5h ?? 2.5) * H).toISOString(),
      rolling: true,
      error: null,
    };
  }
  if (typeof opts.pct7d === 'number') {
    windows['7d'] = {
      present: true,
      percent_remaining: opts.pct7d,
      reset_at: new Date(now + (opts.hours7d ?? 100.8) * H).toISOString(),
      rolling: false,
      error: null,
    };
  }
  return {
    status: 'ok',
    error: null,
    observed_at: iso,
    details: { limit_reached: opts.limitReached ?? false },
    windows,
  };
}

describe('Hub engine-selection decision logic (intake 83d27f69)', () => {
  it('claude at 15% loses to antigravity at 60% (2026-11-02 17:15Z fleet numbers)', async () => {
    const now = Date.parse('2026-11-02T17:15:00Z');
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('engine-sel-claude-vs-agy-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const machine = 'hetzner';
      hub.sql.exec(
        'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, 'RMTHZ', iso(), iso(),
        JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }),
        JSON.stringify({ engines: ['claude', 'antigravity', 'opencode', 'codex', 'zcodex'], fleet_modes: ['headless', 'interactive'], workspaces: ['agent-hub'], cpu_count: 12 }),
      );
      const admin = { caller: { isAdmin: true, actor: 'human:admin', kind: 'human' }, scopes: ['admin'] } as any;
      const put = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT',
        body: JSON.stringify({
          accounts: [
            { label: 'hub-claude', provider: 'claude', max_concurrency: 8, reserve_percent: 0, enabled: true },
            { label: 'shared-claude', provider: 'claude', max_concurrency: 8, reserve_percent: 0, enabled: true },
            { label: 'hub-antigravity', provider: 'gemini', max_concurrency: 6, reserve_percent: 0, enabled: true },
            { label: 'shared-go', provider: 'go', max_concurrency: 4, reserve_percent: 0, enabled: true },
            { label: 'zai-central', provider: 'zai', max_concurrency: 6, reserve_percent: 0, enabled: true },
            { label: 'shared-codex', provider: 'codex', max_concurrency: 6, reserve_percent: 0, enabled: true },
          ],
          routes: [
            { machine_id: machine, engine: 'claude', account: 'hub-claude', profile: 'hub-hub-claude' },
            { machine_id: machine, engine: 'claude', account: 'shared-claude', profile: null },
            { machine_id: machine, engine: 'antigravity', account: 'hub-antigravity', profile: 'hub-hub-antigravity' },
            { machine_id: machine, engine: 'opencode', account: 'shared-go', profile: 'hub-shared-go' },
            { machine_id: machine, engine: 'zcodex', account: 'zai-central', profile: null },
            { machine_id: machine, engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' },
          ],
        }),
      }), admin);
      expect(put.status).toBe(200);

      const sub = (account: string, service: string, q: any) =>
        hub.sql.exec('INSERT INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)',
          machine, account, service, null, iso(), JSON.stringify(q));
      const updateSub = (account: string, service: string, pct: number) =>
        hub.sql.exec('UPDATE fleet_subscriptions SET observed_at = ?, quota_json = ? WHERE machine_id = ? AND account = ? AND service = ?',
          iso(), JSON.stringify(makeQuota(now, { pct7d: pct, hours7d: 100.8 })), machine, account, service);
      sub('hub-claude', 'claude', makeQuota(now, { pct7d: 15, hours7d: 100.8 }));
      sub('shared-claude', 'claude', makeQuota(now, { pct7d: 15, hours7d: 100.8 }));
      sub('hub-antigravity', 'gemini', makeQuota(now, { pct7d: 60, hours7d: 100.8 }));
      sub('shared-go', 'go', makeQuota(now, { pct7d: 47, hours7d: 100.8 }));
      sub('zai-central', 'zai', makeQuota(now, { pct7d: 31, hours7d: 100.8 }));
      sub('shared-codex', 'codex', makeQuota(now, { pct7d: 54, hours7d: 100.8 }));

      const deepReason = 'This task explicitly requires deep reasoning.';
      // Argon is deep-only (intake 8852ba59): compare claude vs antigravity
      // on an explicit deep goal, where both engines are eligible.
      const plan: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'implement task', tier: 'deep', engine_override_reason: deepReason });
      expect(plan.selected).toBeTruthy();
      expect(plan.selected.engine).toBe('antigravity');
      expect(plan.selected.account).toBe('hub-antigravity');
      expect(plan.selected.decision_reason).toContain('auto: antigravity/hub-antigravity');
      expect(plan.selected.decision_reason).toContain('rem=60%');

      const claudeCand = plan.candidates.find((c: any) => c.engine === 'claude');
      const agyCand = plan.candidates.find((c: any) => c.engine === 'antigravity');
      expect(claudeCand).toBeTruthy();
      expect(agyCand.score).toBeGreaterThan(claudeCand.score * 10);
      expect(claudeCand.decision.factors.depleted).toBe(true);
      expect(claudeCand.selection).toMatchObject({ effort: 'medium' });
      expect(['sonnet', 'opus']).toContain(claudeCand.selection.id);
      expect(plan.candidates.filter((c: any) => c.engine === 'claude').every((c: any) =>
        (c.selection?.id === 'sonnet' || c.selection?.id === 'opus') && c.selection?.effort === 'medium')).toBe(true);
      // Deep includes astra (codex burn-released for deep); haiku stays out.
      expect(plan.candidates.some((c: any) => c.selection?.id === 'astra')).toBe(true);
      expect(plan.candidates.some((c: any) => /haiku/i.test(c.selection?.model ?? ''))).toBe(false);

      // Astra requires an explicit deep/review tier; even another tier that
      // contains it in policy must not turn routine placement into Astra.
      const ordinaryAsk: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'routine ask', tier: 'ask' });
      expect(ordinaryAsk.candidates.some((c: any) => c.selection?.id === 'astra')).toBe(false);
      const explicitDeep: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'deep review', tier: 'deep' });
      expect(explicitDeep.candidates.some((c: any) => c.selection?.id === 'astra')).toBe(true);

      // Luna is a config-driven fallback (rules.fallback_models): while a healthy
      // non-fallback route exists its score is down-weighted by rules.fallback_factor
      // INSIDE the decision function. It is never removed from the candidates and no
      // engine is preferred by raw headroom (Alexey 2026-11-02: spread by burn rate).
      updateSub('hub-claude', 'claude', 80);
      updateSub('shared-claude', 'claude', 80);
      updateSub('hub-antigravity', 'gemini', 50);
      updateSub('shared-go', 'go', 45);
      updateSub('zai-central', 'zai', 40);
      updateSub('shared-codex', 'codex', 95);
      const withHealthy: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'ordinary start' });
      // Argon is deep-only (intake 8852ba59): daily covers muse/sonnet/luna,
      // while argon needs an explicit deep goal.
      for (const id of ['muse', 'sonnet', 'luna'])
        expect(withHealthy.candidates.some((c: any) => c.selection?.id === id), id).toBe(true);
      expect(withHealthy.candidates.some((c: any) => c.selection?.id === 'argon'), 'argon-daily').toBe(false);
      const withHealthyDeep: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'ordinary deep start', tier: 'deep', engine_override_reason: 'This task explicitly requires deep reasoning.' });
      expect(withHealthyDeep.candidates.some((c: any) => c.selection?.id === 'argon'), 'argon-deep').toBe(true);
      expect(withHealthy.candidates.some((c: any) =>
        c.engine === 'zcodex' && c.account === 'zai-central' && c.selection?.provider === 'zai')).toBe(true);
      const lunaHealthy = withHealthy.candidates.find((c: any) => c.selection?.id === 'luna');
      expect(lunaHealthy.decision.factors.fallback_factor).toBe((routingPolicy.rules as any).fallback_factor);
      expect(lunaHealthy.decision_reason).toContain('fallback=');
      const product = (f: any) => f.headroom_factor * f.burn_factor * f.provider_weight * f.tier_fit * f.load_factor * f.failure_factor * f.host_factor * f.tbm_weight * f.fallback_factor;
      expect(lunaHealthy.score / product(lunaHealthy.decision.factors)).toBeCloseTo(1, 1);
      // Non-fallback candidates are untouched by the factor.
      expect(withHealthy.candidates.filter((c: any) => c.selection?.id !== 'luna').every((c: any) => c.decision.factors.fallback_factor === 1)).toBe(true);

      // Current fleet shape (2026-11-02): claude 10%, zai 8%, muse 43%, codex 45%,
      // antigravity 40%. Claude is depleted and must lose to every healthy engine;
      // healthy codex stays a real candidate (only the fallback factor applies).
      updateSub('hub-claude', 'claude', 10);
      updateSub('shared-claude', 'claude', 10);
      updateSub('zai-central', 'zai', 8);
      updateSub('shared-go', 'go', 43);
      updateSub('hub-antigravity', 'gemini', 40);
      updateSub('shared-codex', 'codex', 45);
      const current: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'current quota' });
      const best = (pred: (c: any) => boolean) => Math.max(...current.candidates.filter(pred).map((c: any) => c.score));
      expect(current.selected.engine).not.toBe('claude');
      expect(best((c: any) => c.engine === 'claude')).toBeLessThan(best((c: any) => c.engine === 'codex'));
      expect(best((c: any) => c.engine === 'claude')).toBeLessThan(best((c: any) => c.engine === 'opencode'));
      // Argon is deep-only (intake 8852ba59): no antigravity candidate on the
      // default daily goal; the deep goal keeps antigravity ahead of depleted claude.
      expect(current.candidates.some((c: any) => c.engine === 'antigravity'), 'antigravity-daily').toBe(false);
      const currentDeep: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'current quota deep', tier: 'deep', engine_override_reason: 'This task explicitly requires deep reasoning.' });
      const bestDeep = (pred: (c: any) => boolean) => Math.max(...currentDeep.candidates.filter(pred).map((c: any) => c.score));
      expect(bestDeep((c: any) => c.engine === 'claude')).toBeLessThan(bestDeep((c: any) => c.engine === 'antigravity'));
      expect(current.candidates.find((c: any) => c.engine === 'codex').decision.factors.fallback_factor).toBe((routingPolicy.rules as any).fallback_factor);

      // With no healthy non-fallback route left the fallback is not down-weighted
      // and may win at max effort.
      updateSub('hub-claude', 'claude', 15);
      updateSub('shared-claude', 'claude', 15);
      updateSub('hub-antigravity', 'gemini', 15);
      updateSub('shared-go', 'go', 15);
      updateSub('zai-central', 'zai', 15);
      updateSub('shared-codex', 'codex', 95);
      const noHealthy: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'ordinary start fallback' });
      expect(noHealthy.selected?.selection).toMatchObject({ id: 'luna', model: 'gpt-6-luna', effort: 'max' });
      expect(noHealthy.selected.decision.factors.fallback_factor).toBe(1);

      // A model pin can cross tier membership, so luna + deep stays on Luna.
      const lunaDeep: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'pinned Luna', engine: 'luna', tier: 'deep' });
      expect(lunaDeep.selected?.selection).toMatchObject({ id: 'luna', model: 'gpt-6-luna', effort: 'max' });
      expect(lunaDeep.rejected.some((r: any) => r.reason === 'tier_unavailable')).toBe(false);
    });
  });

  it('picks muse (opencode/shared-go) and zai (zai-central) when each is most behind schedule', async () => {
    const now = Date.parse('2026-11-02T17:15:00Z');
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('engine-sel-muse-zai-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const machine = 'hetzner';
      hub.sql.exec(
        'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, 'RMTHZ', iso(), iso(),
        JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }),
        JSON.stringify({ engines: ['claude', 'antigravity', 'opencode', 'zcodex'], fleet_modes: ['headless'], workspaces: ['agent-hub'], cpu_count: 12 }),
      );
      const admin = { caller: { isAdmin: true, actor: 'human:admin', kind: 'human' }, scopes: ['admin'] } as any;
      await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT',
        body: JSON.stringify({
          accounts: [
            { label: 'shared-claude', provider: 'claude', max_concurrency: 6, reserve_percent: 0, enabled: true },
            { label: 'hub-antigravity', provider: 'gemini', max_concurrency: 6, reserve_percent: 0, enabled: true },
            { label: 'shared-go', provider: 'go', max_concurrency: 4, reserve_percent: 0, enabled: true },
            { label: 'zai-central', provider: 'zai', max_concurrency: 6, reserve_percent: 0, enabled: true },
          ],
          routes: [
            { machine_id: machine, engine: 'claude', account: 'shared-claude', profile: null },
            { machine_id: machine, engine: 'antigravity', account: 'hub-antigravity', profile: 'hub-hub-antigravity' },
            { machine_id: machine, engine: 'opencode', account: 'shared-go', profile: 'hub-shared-go' },
            { machine_id: machine, engine: 'zcodex', account: 'zai-central', profile: null },
          ],
        }),
      }), admin);

      const setSub = (account: string, service: string, q: any) =>
        hub.sql.exec('INSERT OR REPLACE INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)',
          machine, account, service, null, iso(), JSON.stringify(q));

      // Case A: muse (shared-go) has 47% left with only 0.6h (12% of 5h window) until reset -> ratio = 3.92 -> burn factor 3.0
      setSub('shared-claude', 'claude', makeQuota(now, { pct7d: 15, hours7d: 100.8 }));
      setSub('hub-antigravity', 'gemini', makeQuota(now, { pct7d: 60, hours7d: 140 }));
      setSub('shared-go', 'go', makeQuota(now, { pct5h: 47, hours5h: 0.6 }));
      setSub('zai-central', 'zai', makeQuota(now, { pct5h: 31, hours5h: 3.5 }));

      const planMuse: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'run muse' });
      expect(planMuse.selected?.engine).toBe('opencode');
      expect(planMuse.selected?.account).toBe('shared-go');
      expect(planMuse.selected?.selection?.id).toBe('muse');
      expect(planMuse.selected?.decision?.factors?.burn_status).toBe('behind_schedule');

      // Also verify modelCandidates accepts managed profile hub-shared-go for muse
      const mCands = modelCandidates(
        { tier: 'daily' },
        { provider: 'go', engine: 'opencode', profile: 'hub-shared-go', account: 'shared-go' },
        [{ engine: 'opencode', profile: 'hub-shared-go', account: 'shared-go', model: 'opencode-go/muse-spark-1.3-contributor', effort: null }],
        makeQuota(now, { pct5h: 47, hours5h: 0.6 }),
        now,
      );
      expect(mCands.map((c: any) => c.id)).toContain('muse');

      // Case B: zai-central has 31% left with 0.3h (6% of 5h window) until reset -> ratio = 5.17 -> burn factor 3.0, while others are ahead of schedule
      setSub('hub-antigravity', 'gemini', makeQuota(now, { pct7d: 35, hours7d: 150 }));
      setSub('shared-go', 'go', makeQuota(now, { pct5h: 25, hours5h: 4.5 }));
      setSub('zai-central', 'zai', makeQuota(now, { pct5h: 31, hours5h: 0.3 }));

      const planZai: any = fleet.plan({ machine, mode: 'headless', workspace: 'agent-hub', prompt: 'run zai' });
      expect(planZai.selected?.engine).toBe('zcodex');
      expect(planZai.selected?.account).toBe('zai-central');
      expect(['glm-flash', 'glm-flash-zcodex']).toContain(planZai.selected?.selection?.id);
      expect(planZai.selected?.decision?.factors?.burn_status).toBe('behind_schedule');
    });
  });

  it('excludes exhausted engines (0% remaining or limit_reached) and never places desktop on OpenAI codex', async () => {
    const now = Date.parse('2026-11-02T17:15:00Z');
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('engine-sel-desktop-exhausted-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const desktopId = '0sJC0fDcz0bzhG4XQp5EeQ';
      hub.sql.exec(
        'INSERT INTO machines (id, name, hostname, os, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)',
        desktopId, 'desktop', 'DESKTOP-FP4OP26', 'Windows', iso(), iso(),
        JSON.stringify({ mem_avail_mb: 24000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }),
        JSON.stringify({ engines: ['codex', 'zcodex', 'opencode', 'claude'], fleet_modes: ['headless'], workspaces: ['agent-hub'], cpu_count: 8 }),
      );
      const admin = { caller: { isAdmin: true, actor: 'human:admin', kind: 'human' }, scopes: ['admin'] } as any;
      await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT',
        body: JSON.stringify({
          accounts: [
            { label: 'shared-codex', provider: 'codex', max_concurrency: 4, reserve_percent: 0, enabled: true },
            { label: 'shared-claude', provider: 'claude', max_concurrency: 4, reserve_percent: 0, enabled: true },
            { label: 'shared-go', provider: 'go', max_concurrency: 4, reserve_percent: 0, enabled: true },
            { label: 'zai-central', provider: 'zai', max_concurrency: 4, reserve_percent: 0, enabled: true },
          ],
          routes: [
            { machine_id: desktopId, engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' },
            { machine_id: desktopId, engine: 'claude', account: 'shared-claude', profile: null },
            { machine_id: desktopId, engine: 'opencode', account: 'shared-go', profile: 'hub-shared-go' },
            { machine_id: desktopId, engine: 'zcodex', account: 'zai-central', profile: null },
          ],
        }),
      }), admin);

      const setSub = (account: string, service: string, q: any) =>
        hub.sql.exec('INSERT OR REPLACE INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)',
          desktopId, account, service, null, iso(), JSON.stringify(q));

      // Codex has 90% left, Claude is exhausted (0%, limit_reached: true), Muse has 47%, Zai has 31%
      setSub('shared-codex', 'codex', makeQuota(now, { pct7d: 90, hours7d: 48 }));
      setSub('shared-claude', 'claude', makeQuota(now, { pct7d: 0, hours7d: 48, limitReached: true }));
      setSub('shared-go', 'go', makeQuota(now, { pct5h: 47, hours5h: 2.0 }));
      setSub('zai-central', 'zai', makeQuota(now, { pct5h: 31, hours5h: 2.5 }));

      const plan: any = fleet.plan({ machine: desktopId, mode: 'headless', workspace: 'agent-hub', prompt: 'desktop task' });
      // Codex must be rejected on desktop with desktop_codex_excluded
      expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: desktopId, engine: 'codex', reason: 'desktop_codex_excluded' }));
      // Exhausted claude must be rejected with account_blocked
      expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: desktopId, engine: 'claude', reason: 'account_blocked' }));
      // Selected must be opencode (muse) or zcodex (zai), never codex or claude
      expect(plan.selected?.engine).toBe('opencode');
      expect(plan.selected?.account).toBe('shared-go');
      // Z.ai (provider=zai) on desktop remains eligible in candidates
      expect(plan.candidates.map((c: any) => c.engine)).toContain('zcodex');
      expect(plan.candidates.map((c: any) => c.engine)).not.toContain('codex');
    });
  });

  it('rejects a pinned engine without --reason and exposes decision_reason in sessions show, quota, and engines recommend', async () => {
    const now = Date.parse('2026-11-02T17:15:00Z');
    const iso = (d = 0) => new Date(now + d).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('engine-sel-session-quota-rec-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const quotaStore = new QuotaStore(hub.sql, undefined, undefined, () => now);
      const sessions = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), () => now, {
        reserve: (input, auth) => fleet.reserve(input, auth),
      });
      sessions.init();

      const machine = 'hetzner';
      hub.sql.exec(
        'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, 'RMTHZ', iso(), iso(),
        JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }),
        JSON.stringify({ engines: ['claude', 'antigravity', 'opencode'], fleet_modes: ['headless', 'interactive'], workspaces: ['agent-hub'], cpu_count: 12 }),
      );
      const admin = { caller: { isAdmin: true, actor: 'human:admin', kind: 'human' }, scopes: ['admin'] } as any;
      await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT',
        body: JSON.stringify({
          accounts: [
            { label: 'shared-claude', provider: 'claude', max_concurrency: 6, reserve_percent: 0, enabled: true },
            { label: 'hub-antigravity', provider: 'gemini', max_concurrency: 6, reserve_percent: 0, enabled: true },
            { label: 'shared-go', provider: 'go', max_concurrency: 4, reserve_percent: 0, enabled: true },
          ],
          routes: [
            { machine_id: machine, engine: 'claude', account: 'shared-claude', profile: null },
            { machine_id: machine, engine: 'antigravity', account: 'hub-antigravity', profile: 'hub-hub-antigravity' },
            { machine_id: machine, engine: 'opencode', account: 'shared-go', profile: 'hub-shared-go' },
          ],
        }),
      }), admin);

      const setSub = (account: string, service: string, q: any) =>
        hub.sql.exec('INSERT OR REPLACE INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)',
          machine, account, service, null, iso(), JSON.stringify(q));
      setSub('shared-claude', 'claude', makeQuota(now, { pct7d: 15, hours7d: 100.8 }));
      setSub('hub-antigravity', 'gemini', makeQuota(now, { pct7d: 60, hours7d: 100.8 }));
      setSub('shared-go', 'go', makeQuota(now, { pct7d: 47, hours7d: 100.8 }));

      // 1. Pinned engine without engine_override_reason is rejected with 400 engine_reason_required
      const noReasonRes = await sessions.handle(new Request('https://hub.test/api/sessions', {
        method: 'POST',
        body: JSON.stringify({ key: 'pin-no-reason', mode: 'interactive', workspace: 'agent-hub', engine: 'claude' }),
      }), admin);
      expect(noReasonRes.status).toBe(400);
      expect(await noReasonRes.json()).toMatchObject({ error: 'engine_reason_required' });

      const recNoReason = await fleet.handle(new Request('https://hub.test/api/fleet/recommend?engine=claude'), admin);
      expect(recNoReason.status).toBe(400);
      expect(await recNoReason.json()).toMatchObject({ error: 'engine_reason_required' });

      // 2. An unpinned start draws among eligible routes and records the draw
      // probability in the session decision shown by GET /api/sessions/:id.
      const autoRes = await sessions.handle(new Request('https://hub.test/api/sessions', {
        method: 'POST',
        body: JSON.stringify({ key: 'auto-pick-1', mode: 'interactive', workspace: 'agent-hub' }),
      }), admin);
      expect(autoRes.status).toBe(201);
      const autoBody: any = await autoRes.json();
      expect(['claude', 'antigravity', 'opencode']).toContain(autoBody.engine);
      expect(typeof autoBody.decision_score).toBe('number');
      expect(autoBody.decision_reason).toContain(`auto: ${autoBody.engine}/`);
      expect(autoBody.decision_reason).toContain('draw_probability=');

      const showRes = await sessions.handle(new Request(`https://hub.test/api/sessions/${autoBody.id}`), admin);
      expect(showRes.status).toBe(200);
      const showBody: any = await showRes.json();
      expect(showBody.decision_reason).toBe(autoBody.decision_reason);
      expect(showBody.decision?.mode).toBe('auto');

      // 3. Pinned session start WITH engine_override_reason records mode='pinned' and override_reason
      const pinnedRes = await sessions.handle(new Request('https://hub.test/api/sessions', {
        method: 'POST',
        body: JSON.stringify({
          key: 'pin-with-reason',
          mode: 'interactive',
          workspace: 'agent-hub',
          engine: 'claude',
          engine_override_reason: 'reproduce claude-specific parser bug',
        }),
      }), admin);
      expect(pinnedRes.status).toBe(201);
      const pinnedBody: any = await pinnedRes.json();
      expect(pinnedBody.engine).toBe('claude');
      expect(pinnedBody.decision_reason).toContain('pinned: claude/shared-claude');
      expect(pinnedBody.decision_reason).toContain('reproduce claude-specific parser bug');

      // 4. GET /api/fleet/recommend returns recommendation without reserving
      const recRes = await fleet.handle(new Request('https://hub.test/api/fleet/recommend?tier=daily&workspace=agent-hub'), admin);
      expect(recRes.status).toBe(200);
      const recBody: any = await recRes.json();
      expect(recBody.selected).toBeTruthy();
      expect(recBody.selected.engine).not.toBe('claude');
      expect(recBody.decision_reason).toContain('auto:');

      // 5. Quota view (hub quota) includes burn_ratio, burn_factor, burn_status, decision_score, decision_reason
      const quotaView: any = quotaStore.getFleetView({ host_id: null, provider: null, engine: null, account: null }, now).body;
      const claudePool = quotaView.pools.find((p: any) => p.account === 'shared-claude');
      const agyPool = quotaView.pools.find((p: any) => p.account === 'hub-antigravity');
      expect(claudePool).toMatchObject({ burn_status: 'depleted', depleted: true });
      expect(claudePool.decision_reason).toContain('status=depleted');
      expect(agyPool.decision_score).toBeGreaterThan(claudePool.decision_score);
    });
  });

  it('applies the config-driven fallback factor only while a healthy alternative exists, never removing or banning', () => {
    const now = Date.parse('2026-11-02T17:15:00Z');
    const q = makeQuota(now, { pct7d: 45, hours7d: 100.8 });
    const base = { machine: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null, provider: 'codex',
      model: { id: 'luna', model: 'gpt-6-luna' }, tier: 'daily', quota: q, headroomPercent: 45, nowMs: now,
      cpuLoad: 0, pendingLaunches: 0, familyLoad: 0, accountActive: 0, accountMaxConcurrency: 4, recentAccountFailures: 0,
      degraded: false, tbmWeight: 1 };
    const plain = computeEngineDecision(base);
    const withAlt = computeEngineDecision({ ...base, healthyAlternative: true });
    const factor = (routingPolicy.rules as any).fallback_factor;
    expect(factor).toBeLessThan(1);
    expect(factor).toBeGreaterThan(0);
    expect(plain.factors.fallback_factor).toBe(1);
    expect(withAlt.factors.fallback_factor).toBe(factor);
    expect(withAlt.score).toBeGreaterThan(0);
    expect(withAlt.score / plain.score).toBeCloseTo(factor, 2);
    // Non-fallback models are never touched.
    const sonnet = computeEngineDecision({ ...base, engine: 'claude', provider: 'claude', model: { id: 'sonnet', model: 'sonnet' }, healthyAlternative: true });
    expect(sonnet.factors.fallback_factor).toBe(1);
    expect(fallbackFactor(true, false)).toBe(1);
    // Healthy means: not exhausted, not behind-schedule-depleted, at least fallback_headroom_percent left.
    expect(isHealthyAlternative({ factors: { remaining_pct: 45, burn_status: 'on_pace' } })).toBe(true);
    expect(isHealthyAlternative({ factors: { remaining_pct: 10, burn_status: 'depleted' } })).toBe(false);
    expect(isHealthyAlternative({ factors: { remaining_pct: 60, burn_status: 'depleted' } })).toBe(false);
    expect(isHealthyAlternative({ factors: { remaining_pct: 20, burn_status: 'behind_schedule' } })).toBe(false);
    expect(isHealthyAlternative(null)).toBe(false);
  });

  it('breaks ties deterministically across identical scores', () => {
    const now = Date.parse('2026-11-02T17:15:00Z');
    const q = makeQuota(now, { pct7d: 50, hours7d: 84 });
    const d1 = computeEngineDecision({
      machine: 'hetzner', engine: 'antigravity', account: 'hub-antigravity', profile: null, provider: 'gemini',
      model: { id: 'argon', model: 'gemini-4-argon-medium' }, tier: 'daily', quota: q, headroomPercent: 50, nowMs: now,
      cpuLoad: 0, pendingLaunches: 0, familyLoad: 0, accountActive: 0, accountMaxConcurrency: 4, recentAccountFailures: 0,
      degraded: false, tbmWeight: 1,
    });
    const d2 = computeEngineDecision({
      machine: 'hetzner', engine: 'opencode', account: 'shared-go', profile: null, provider: 'go',
      model: { id: 'muse', model: 'opencode-go/muse-spark-1.3-contributor' }, tier: 'daily', quota: q, headroomPercent: 50, nowMs: now,
      cpuLoad: 0, pendingLaunches: 0, familyLoad: 0, accountActive: 0, accountMaxConcurrency: 4, recentAccountFailures: 0,
      degraded: false, tbmWeight: 1,
    });
    expect(d1.score).toBe(d2.score);
    const list = [
      { machine: 'hetzner', engine: 'opencode', account: 'shared-go', score: d2.score, decision: d2 },
      { machine: 'hetzner', engine: 'antigravity', account: 'hub-antigravity', score: d1.score, decision: d1 },
    ];
    const comparator = (a: any, b: any) =>
      b.score - a.score
      || (b.decision?.factors?.burn_ratio ?? 0) - (a.decision?.factors?.burn_ratio ?? 0)
      || (b.decision?.factors?.remaining_pct ?? 0) - (a.decision?.factors?.remaining_pct ?? 0)
      || (a.decision?.factors?.active_account ?? 0) - (b.decision?.factors?.active_account ?? 0)
      || a.machine.localeCompare(b.machine)
      || a.engine.localeCompare(b.engine)
      || String(a.account ?? '').localeCompare(String(b.account ?? ''));
    const sorted1 = [...list].sort(comparator);
    const sorted2 = [...list].reverse().sort(comparator);
    expect(sorted1.map(x => x.engine)).toEqual(['antigravity', 'opencode']);
    expect(sorted2.map(x => x.engine)).toEqual(['antigravity', 'opencode']);
  });
});

describe('desktop codex guard is config-driven (intake 83d27f69 review)', () => {
  const desktop = { id: '0sJC0fDcz0bzhG4XQp5EeQ', name: 'desktop', hostname: 'DESKTOP-FP4OP26' };
  const guard = (routingPolicy as any).rules.desktop_codex_guard;

  it('excludes OpenAI codex on the desktop by id or hostname, nothing else', () => {
    expect(isDesktopCodexExcluded(desktop, { engine: 'codex', provider: 'codex' })).toBe(true);
    expect(isDesktopCodexExcluded({ id: 'other', hostname: 'DESKTOP-FP4OP26' }, { engine: 'codex', provider: 'codex' }, 'codex')).toBe(true);
    // a host that merely has the generic name desktop is not the guarded machine
    expect(isDesktopCodexExcluded({ id: 'x', name: 'desktop', hostname: 'desktop' }, { engine: 'codex', provider: 'codex' }, 'codex')).toBe(false);
    expect(isDesktopCodexExcluded({ id: 'hetzner', hostname: 'RMTHZ' }, { engine: 'codex', provider: 'codex' }, 'codex')).toBe(false);
  });

  it('keeps zai (codex harness profile and zcodex), muse and antigravity eligible on the desktop', () => {
    expect(isDesktopCodexExcluded(desktop, { engine: 'codex', provider: 'codex', profile: 'hub-zai-central' }, 'zai')).toBe(false);
    expect(isDesktopCodexExcluded(desktop, { engine: 'zcodex', provider: 'zai' }, 'zai')).toBe(false);
    expect(isDesktopCodexExcluded(desktop, { engine: 'opencode', provider: 'go' }, 'go')).toBe(false);
    expect(isDesktopCodexExcluded(desktop, { engine: 'antigravity', provider: 'gemini' }, 'gemini')).toBe(false);
  });

  it('can be lifted with enabled=false', () => {
    guard.enabled = false;
    try {
      expect(isDesktopCodexExcluded(desktop, { engine: 'codex', provider: 'codex' }, 'codex')).toBe(false);
    } finally {
      guard.enabled = true;
    }
    expect(isDesktopCodexExcluded(desktop, { engine: 'codex', provider: 'codex' }, 'codex')).toBe(true);
  });
});

describe('single burn notion (pace + expiry forecast)', () => {
  const now = Date.parse('2026-11-02T17:15:00Z');
  it('combines schedule pace and expiry forecast with max, not a product', () => {
    const q = makeQuota(now, { pct7d: 95, hours7d: 28 });
    const a = analyzeQuotaBurn(q, now, false, { reservePercent: 0 });
    expect(a.pace_factor).toBeGreaterThan(1);
    expect(a.expiry_factor).toBeGreaterThan(1);
    expect(a.factor).toBeCloseTo(Math.max(a.pace_factor, a.expiry_factor), 6);
    expect(a.factor).toBeLessThan(a.pace_factor * a.expiry_factor);
  });
  it('ahead-of-schedule low headroom keeps its penalty and is never boosted', () => {
    const q = makeQuota(now, { pct7d: 15, hours7d: 100.8 });
    const a = analyzeQuotaBurn(q, now, false, { reservePercent: 0 });
    expect(a.factor).toBe(a.pace_factor);
    expect(a.factor).toBeLessThan(1);
  });
  it('interactive placement skips the expiry forecast', () => {
    const q = makeQuota(now, { pct7d: 95, hours7d: 28 });
    expect(analyzeQuotaBurn(q, now, false, { forecast: false }).expiry_factor).toBe(1);
  });
});
