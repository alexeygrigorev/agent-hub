// Operator concurrency limits (`hub limits`, intake cbcadaa1).
// A target is an account label (canonical or legacy alias) or an engine name.
// An engine resolves to every account of its provider (claude -> hub-claude and
// shared-claude, opencode -> go, zcodex -> zai). Only when no account has that
// provider do registry routes for the engine name decide; provider matching
// comes first so `codex` never pulls in zai accounts routed through engine
// codex. Raise-only by default: an engine-wide set raises accounts below n and
// keeps higher caps; lowering needs an explicit allow_lower.
import { canonicalAccountLabel, canonicalQuotaProvider, dedupeAccountsByCanonical } from './quota';
import { engineProvider } from './quota_admission';

export const LIMIT_MIN = 1;
export const LIMIT_MAX = 100;

export type LimitRow = { label: string; provider: string; max_concurrency: number };
export type LimitChange = { label: string; account: string; provider: string; from: number; to: number;
  result: 'raised' | 'unchanged' | 'lowered' | 'kept_higher' };

export function resolveLimitTarget(target: string, rows: LimitRow[], routes: { account: string; engine: string }[]):
    { kind: 'account' | 'engine'; rows: LimitRow[] } {
  const accounts = rows.filter(r => r.label === target || canonicalAccountLabel(r.label) === target);
  if (accounts.length) return { kind: 'account', rows: accounts };
  const provider = engineProvider(target) ?? canonicalQuotaProvider(target);
  const byProvider = rows.filter(r => canonicalQuotaProvider(r.provider) === provider);
  if (byProvider.length) return { kind: 'engine', rows: byProvider };
  const routed = new Set(routes.filter(r => r.engine === target).map(r => canonicalAccountLabel(r.account)));
  return { kind: 'engine', rows: rows.filter(r => routed.has(canonicalAccountLabel(r.label))) };
}

/** Plans one set over canonical accounts (a legacy alias row and its canonical
 * row are one account, as in show); returns the conflict reason when an exact
 * account would be lowered without allow_lower. */
export function planLimitSet(kind: 'account' | 'engine', rows: LimitRow[], max: number, allowLower: boolean):
    { changes: LimitChange[]; error?: 'account_lowering_requires_allow_lower' } {
  const changes = dedupeAccountsByCanonical(rows).map(r => {
    const from = Number(r.max_concurrency);
    const result: LimitChange['result'] = max > from ? 'raised' : max === from ? 'unchanged' : allowLower ? 'lowered' : 'kept_higher';
    return { label: r.label, account: canonicalAccountLabel(r.label), provider: canonicalQuotaProvider(r.provider), from,
      to: result === 'kept_higher' ? from : max, result };
  });
  if (kind === 'account' && changes.some(c => c.result === 'kept_higher')) return { changes, error: 'account_lowering_requires_allow_lower' };
  return { changes };
}

/** Raw fleet_accounts writes for planned changes: every row of a changed
 * canonical account moves, but a legacy alias row above the target keeps its
 * cap unless the change is an explicit lowering. */
export function limitRowUpdates(rows: LimitRow[], changes: LimitChange[]): { label: string; from: number; to: number }[] {
  const updates: { label: string; from: number; to: number }[] = [];
  for (const c of changes) {
    if (c.to === c.from) continue;
    for (const r of rows) {
      if (canonicalAccountLabel(r.label) !== c.account || canonicalQuotaProvider(r.provider) !== c.provider) continue;
      const from = Number(r.max_concurrency);
      const to = c.result === 'lowered' ? c.to : Math.max(from, c.to);
      if (to !== from) updates.push({ label: r.label, from, to });
    }
  }
  return updates;
}
