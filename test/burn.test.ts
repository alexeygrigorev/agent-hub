import { describe, expect, it } from 'vitest';
import { burnFactor, burnForecast, burnReleasesExplicitOnly, burnRules } from '../src/burn';
import { modelCandidates, policy } from '../src/model-routing';

// Burn-aware routing (intake ebfe9d85, t1822u): use-it-or-lose-it scoring.
const H = 3600000;
const NOW = Date.parse('2026-10-28T11:40:00Z');
const iso = (offsetMs: number) => new Date(NOW + offsetMs).toISOString();
const q = (windows: Record<string, any>) => ({ status: 'ok', observed_at: iso(-1000), details: { limit_reached: false }, windows });
const weekly = (pct: number, hoursLeft: number) => ({ '7d': { present: true, percent_remaining: pct, reset_at: iso(hoursLeft * H) } });

// Synthetic subscription scenarios. The 95%-left/28h case is the intake's
// scoring example; these fixtures are not live account measurements.
const claude = q({ '5h': { present: true, percent_remaining: 100, reset_at: iso(0.5 * H) }, ...weekly(95, 28) });
const zai = q({ '5h': { present: true, percent_remaining: 91, reset_at: iso(4 * H), rolling: true }, ...weekly(53, 73) });
const codex = q({ '5h': { present: false, percent_remaining: null, reset_at: null }, ...weekly(79, 162) });

const route = (provider: string, engine: string) => ({ provider, engine, profile: null, account: provider + '-acct' });
const ad = (engine: string, model: string, effort: string | null, account: string) => [{ engine, profile: null, model, effort, account }];
const best = (list: any[]) => list.reduce((a, b) => (b.weight > a.weight ? b : a));

describe('burn forecast', () => {
  it('projects unused quota at the weekly reset from the cycle burn rate', () => {
    const f = burnForecast(claude, NOW)!;
    expect(f.window).toBe('7d');
    expect(f.hours_to_reset).toBe(28);
    // 5% used in 140h -> ~1% more before reset -> ~94% expires unused.
    expect(f.burn_rate_percent_per_hour).toBeCloseTo(5 / 140, 3);
    expect(f.projected_unused_percent).toBeCloseTo(94, 0);
    expect(f.waste_alert).toBe(true);
    expect(f.factor).toBeGreaterThan(3);
  });

  it('uses a horizon of at least seven days', () => {
    expect(burnRules().horizonHours).toBeGreaterThanOrEqual(168);
    expect(burnRules({ reset_horizon_hours: 24 }).horizonHours).toBe(168);
    expect(policy.rules.reset_horizon_hours).toBeGreaterThanOrEqual(168);
    // 95% left with three days to go still carries real pressure (old 24h horizon gave none).
    expect(burnForecast(q(weekly(95, 72)), NOW)!.factor).toBeGreaterThan(2);
  });

  it('is neutral for accounts that will burn their quota anyway or just reset', () => {
    const burning = burnForecast(q(weekly(10, 24)), NOW)!; // 90% used in 144h
    expect(burning.projected_unused_percent).toBe(0);
    expect(burning.factor).toBe(1);
    expect(burning.waste_alert).toBe(false);
    const fresh = burnForecast(codex, NOW)!; // early-cycle quota: no rate inferred yet
    expect(fresh.burn_rate_percent_per_hour).toBeNull(); // below burn_min_elapsed_hours
    expect(fresh.factor).toBeLessThan(1.1);
    expect(fresh.waste_alert).toBe(false); // no alert without a measured rate
  });

  it('handles short-only subscriptions and ignores missing/broken windows and passed resets', () => {
    expect(burnForecast(q({ '5h': { present: true, percent_remaining: 100, reset_at: iso(H) } }), NOW)!.factor).toBeGreaterThan(3);
    expect(burnForecast(q({ '7d': { present: true, percent_remaining: 90, reset_at: iso(-H) } }), NOW)).toBeNull();
    expect(burnForecast(q({ '7d': { present: true, percent_remaining: 90, reset_at: iso(H), error: 'unavailable' } }), NOW)).toBeNull();
    expect(burnForecast(null, NOW)).toBeNull();
    expect(burnFactor(null)).toBe(1);
  });

  it('picks the long window that resets first and keeps the reserve out of the waste', () => {
    const f = burnForecast(q({ '7d': { present: true, percent_remaining: 60, reset_at: iso(40 * H) }, monthly: { present: true, percent_remaining: 90, reset_at: iso(400 * H) } }), NOW)!;
    expect(f.window).toBe('7d');
    const withReserve = burnForecast(q(weekly(95, 28)), NOW, 15)!;
    expect(withReserve.projected_unused_percent).toBeCloseTo(79, 0);
  });

  it('never forecasts from stale, future, failed, exhausted or malformed evidence', () => {
    for (const broken of [
      { ...claude, observed_at: iso(-21 * 60000) },
      { ...claude, observed_at: iso(H) },
      { ...claude, status: 'unavailable' },
      { ...claude, details: { limit_reached: true } },
      q(weekly(95, 169)),
      q({ '7d': { present: false, percent_remaining: 95, reset_at: iso(H) } }),
    ]) {
      expect(burnForecast(broken, NOW)).toBeNull();
      expect(burnReleasesExplicitOnly(burnForecast(broken, NOW))).toBe(false);
    }
  });

  it('keeps a rolling-window rate unknown rather than inferring a fixed cycle', () => {
    const f = burnForecast(q({ '5h': { present: true, percent_remaining: 95, reset_at: iso(H), rolling: true } }), NOW)!;
    expect(f.factor).toBeGreaterThan(1);
    expect(f.burn_rate_percent_per_hour).toBeNull();
    expect(f.waste_alert).toBe(false);
  });
});

describe('burn-aware model candidates', () => {
  it('strongly prefers an expiring Claude account over the static Z.ai/Go preference', () => {
    const sonnet = best(modelCandidates({ tier: 'daily' }, route('claude', 'claude'), ad('claude', 'sonnet', 'medium', 'claude-acct'), claude, NOW));
    const glm = best(modelCandidates({ tier: 'daily' }, route('zai', 'zcodex'), ad('zcodex', 'glm-5.3-flash', 'high', 'zai-acct'), zai, NOW));
    expect(glm.factors.preference).toBeGreaterThan(1); // the static preference still exists...
    expect(sonnet.weight).toBeGreaterThan(2 * glm.weight); // ...but no longer outranks expiring quota.
    // Even a full Z.ai account that is not about to reset loses to expiring Claude.
    const fullZai = best(modelCandidates({ tier: 'daily' }, route('zai', 'zcodex'), ad('zcodex', 'glm-5.3-flash', 'high', 'zai-acct'), q(weekly(100, 168)), NOW));
    expect(sonnet.weight).toBeGreaterThan(fullZai.weight);
  });

  it('routes Claude Sonnet at medium effort for deep and review work when it is expiring', () => {
    for (const tier of ['deep', 'review']) {
      const [sonnet] = modelCandidates({ tier }, route('claude', 'claude'), ad('claude', 'sonnet', 'medium', 'claude-acct'), claude, NOW);
      expect(sonnet?.id).toBe('sonnet');
      expect(sonnet.effort).toBe('medium');
      expect(sonnet.factors.burn).toBeGreaterThan(3);
    }
  });

  it('releases explicit-only Codex for daily/deep only while its weekly quota would expire unused', () => {
    const sol = ad('codex', 'gpt-6.1-sol', 'medium', 'codex-acct');
    const astra = ad('codex', 'gpt-6-astra', 'high', 'codex-acct');
    const idle = q(weekly(90, 30)); // 10% used in 138h -> ~88% would expire
    expect(burnReleasesExplicitOnly(burnForecast(idle, NOW))).toBe(true);
    expect(modelCandidates({ tier: 'daily' }, route('codex', 'codex'), sol, idle, NOW)).toHaveLength(1);
    expect(modelCandidates({ tier: 'deep' }, route('codex', 'codex'), astra, idle, NOW)).toHaveLength(1);
    expect(modelCandidates({ tier: 'quick' }, route('codex', 'codex'), ad('codex', 'gpt-6-luna', 'max', 'codex-acct'), idle, NOW)).toHaveLength(0);
    // Early-cycle synthetic quota stays explicit-only without a proven waste estimate.
    expect(modelCandidates({ tier: 'daily' }, route('codex', 'codex'), sol, codex, NOW)).toHaveLength(0);
    expect(modelCandidates({ tier: 'daily', preferred_runner: 'codex' }, route('codex', 'codex'), sol, codex, NOW)).toHaveLength(1);
  });

  it('never bypasses admission: reserve and limit gates still drop the candidate', () => {
    const sol = ad('codex', 'gpt-6.1-sol', 'medium', 'codex-acct');
    const limited = { ...q(weekly(90, 30)), details: { limit_reached: true } };
    expect(modelCandidates({ tier: 'daily' }, route('codex', 'codex'), sol, limited, NOW)).toHaveLength(0);
    expect(policy.rules.allow_paid).toBe(false);
  });

  it.each(['shared-codex', 'codex-orin'])('releases the exact managed Codex account %s without crossing profiles', account => {
    const profile = 'hub-' + account;
    const r = { provider: account === 'codex-orin' ? 'codex-orin' : 'codex', engine: 'codex', account, profile };
    const a = [{ engine: 'codex', profile, model: 'gpt-6.1-sol', effort: 'medium', account }];
    expect(modelCandidates({ tier: 'daily' }, r, a, q(weekly(95, 28)), NOW).map(m => m.id)).toEqual(['sol']);
    expect(modelCandidates({ tier: 'daily' }, r, [{ ...a[0], account: 'other-account' }], q(weekly(95, 28)), NOW)).toEqual([]);
    expect(modelCandidates({ tier: 'daily' }, r, [{ ...a[0], profile: 'other-profile' }], q(weekly(95, 28)), NOW)).toEqual([]);
    const stale = { ...q(weekly(95, 28)), observed_at: iso(-H) };
    expect(modelCandidates({ tier: 'daily' }, r, a, stale, NOW)).toEqual([]);
  });

  it.each([['gemini', 'gemini', 'gemini-2.5-pro', 'deep', 'medium'], ['go', 'opencode', 'opencode-go/muse-spark-1.3-contributor', 'daily', null]])(
    'scores expiring quota for %s', (provider, engine, model, tier, effort) => {
      const [candidate] = modelCandidates({ tier }, route(provider, engine), ad(engine, model, effort, provider + '-acct'), q(weekly(95, 28)), NOW);
      expect(candidate.factors.burn).toBeGreaterThan(3);
    });
});
