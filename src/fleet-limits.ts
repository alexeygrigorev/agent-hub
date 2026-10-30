// Operator concurrency limits (`hub limits`, intake cbcadaa1).
// A target is an account label (canonical or legacy alias) or an engine name.
// An engine resolves to every account whose canonical provider is that engine,
// or that serves it through a registry route (claude -> hub-claude and
// shared-claude). Raise-only by default: an engine-wide set raises accounts
// below n and keeps higher caps; lowering needs an explicit allow_lower.
import { canonicalAccountLabel, canonicalQuotaProvider } from './quota';

export const LIMIT_MIN = 1;
export const LIMIT_MAX = 100;

export type LimitRow = { label: string; provider: string; max_concurrency: number };
export type LimitChange = { label: string; account: string; provider: string; from: number; to: number;
  result: 'raised' | 'unchanged' | 'lowered' | 'kept_higher' };

export function resolveLimitTarget(target: string, rows: LimitRow[], routes: { account: string; engine: string }[]):
    { kind: 'account' | 'engine'; rows: LimitRow[] } {
  const accounts = rows.filter(r => r.label === target || canonicalAccountLabel(r.label) === target);
  if (accounts.length) return { kind: 'account', rows: accounts };
  const provider = canonicalQuotaProvider(target);
  const routed = new Set(routes.filter(r => r.engine === target).map(r => r.account));
  return { kind: 'engine', rows: rows.filter(r => canonicalQuotaProvider(r.provider) === provider || routed.has(r.label)) };
}

/** Plans one set; returns the conflict reason when an exact account would be lowered without allow_lower. */
export function planLimitSet(kind: 'account' | 'engine', rows: LimitRow[], max: number, allowLower: boolean):
    { changes: LimitChange[]; error?: 'account_lowering_requires_allow_lower' } {
  const changes = rows.map(r => {
    const from = Number(r.max_concurrency);
    const result: LimitChange['result'] = max > from ? 'raised' : max === from ? 'unchanged' : allowLower ? 'lowered' : 'kept_higher';
    return { label: r.label, account: canonicalAccountLabel(r.label), provider: canonicalQuotaProvider(r.provider), from,
      to: result === 'kept_higher' ? from : max, result };
  });
  if (kind === 'account' && changes.some(c => c.result === 'kept_higher')) return { changes, error: 'account_lowering_requires_allow_lower' };
  return { changes };
}
