import { HttpError } from './http';
import { isDeepWorkModel, isExplicitOnlyModel, isModelPolicyCandidateAllowed } from './model-policy';
import type { Hub2Env } from './types';
import { allowsAstra, sessionPurpose, type SessionPurpose } from './session-purpose';

type Candidate = { id: string; engine: string; model: string; effort?: string; account_id: string; weight: number; capabilities: string[]; enabled: boolean };
type AccountView = { credential_status?: string; account?: { min_remaining?: number; enabled?: boolean } | null; quota?: { remaining?: number } | null; hosts?: string[]; credential_usable?: boolean };
type MachineView = { machine_id?: string; engines?: string[]; connected?: boolean; last_seen?: number };
export type PickedRoute = {
  machine_id: string; engine: string; model: string; effort?: string; account_id: string;
  policy_id: string; policy_version: unknown; override_reason: string | null;
  weight: number; quota_remaining: number; required_capabilities: string[];
};
export type PickDiagnostics = {
  pool: Array<{ policy_id: string; engine: string; model: string; effort: string | null; weight: number; share: number }>;
  excluded: Array<{ policy_id: string; account_id: string; reason: string }>;
};
// Weighting knobs, kept identical to ~/bin/model-roulette so both pools behave the same.
const MIN_HEADROOM = 5;      // % quota left below which a pool has no headroom
const DEEP_BIAS = 3;         // opus/sol multiplier for tier deep/complex/serious
const NEUTRAL_QUOTA = 50;    // pools without a collector reading (zai) get a neutral weight
const DEEP_TIERS = new Set(['deep', 'complex', 'serious']);
export type PickInput = {
  purpose?: SessionPurpose;
  machine_id?: string; engine?: string; model?: string; effort?: string; override_reason?: string;
  tier?: string; interactive: boolean; with_goal?: boolean; required_capabilities?: string[]; exclude_policy_ids?: string[];
};

const internalActor = JSON.stringify({ id: 'hub2-model-picker', kind: 'operator', scopes: ['operator:admin', 'routing:read'] });

async function read(namespace: DurableObjectNamespace, id: string, path: string): Promise<any> {
  const response = await namespace.get(namespace.idFromName(id)).fetch('https://picker.internal' + path, {
    headers: { 'x-hub2-actor': internalActor },
  });
  if (!response.ok) throw new HttpError('model_picker_data_unavailable', 503);
  return response.json();
}

function weightedChoice<T>(values: Array<{ value: T; weight: number }>, random = Math.random): T {
  const total = values.reduce((sum, row) => sum + row.weight, 0);
  if (!values.length || !Number.isFinite(total) || total <= 0) throw new HttpError('model_policy_unavailable', 503);
  let draw = random() * total;
  for (const row of values) {
    draw -= row.weight;
    if (draw < 0) return row.value;
  }
  return values[values.length - 1].value;
}

export async function pickModelRoute(env: Hub2Env, input: PickInput, random = Math.random): Promise<PickedRoute> {
  return (await pickModelRouteDetailed(env, input, random)).route;
}

export async function pickModelRouteDetailed(env: Hub2Env, input: PickInput, random = Math.random): Promise<{ route: PickedRoute; diagnostics: PickDiagnostics }> {
  const purpose = sessionPurpose(input.purpose);
  if (input.model && /astra/i.test(input.model) && !allowsAstra(purpose))
    throw new HttpError('astra_not_for_implementation', 400);
  if (input.with_goal && !input.interactive) throw new HttpError('goal_requires_interactive', 400);
  const override = !!(input.machine_id || input.engine || input.model || input.effort);
  if (input.override_reason && input.override_reason.length > 500)
    throw new HttpError('invalid_override_reason', 400);
  const required = [...new Set([input.interactive ? 'interactive' : 'headless', ...(input.with_goal ? ['goal'] : []), ...(input.required_capabilities || [])])];
  if (required.length > 32 || required.some(value => typeof value !== 'string' || !value || value.length > 80))
    throw new HttpError('invalid_required_capabilities', 400);

  const policy = await read(env.CONTROL, 'global', '/_routing-policy') as { version: unknown; candidates: Candidate[] };
  if (!Array.isArray(policy.candidates) || !policy.candidates.length || policy.candidates.length > 32)
    throw new HttpError('model_policy_unconfigured', 503);
  if (input.model && !allowsAstra(purpose) && policy.candidates.some(candidate =>
    (input.model === candidate.id || input.model === candidate.model) && isExplicitOnlyModel(candidate)))
    throw new HttpError('astra_not_for_implementation', 400);
  const excluded=new Set(input.exclude_policy_ids||[]);
  const candidates = policy.candidates.filter(candidate => candidate.enabled && !excluded.has(candidate.id)
    && (!isExplicitOnlyModel(candidate) || allowsAstra(purpose))
    && (candidate.id !== 'claude-fable-5-2' || input.model === candidate.id || input.model === candidate.model)
    && (!isExplicitOnlyModel(candidate) || (override && !!input.model && (input.model === candidate.model || input.model === candidate.id)))
    && isModelPolicyCandidateAllowed(candidate)
    && (!input.engine || input.engine === candidate.engine)
    && (!input.model || input.model === candidate.model || input.model === candidate.id)
    && (!input.effort || input.effort === candidate.effort));
  if (!candidates.length) throw new HttpError('model_policy_unavailable', 503);

  const ids = new Set<string>();
  let after='';
  do {
    const directory = await read(env.CONTROL, 'global', '/api/directory?kind=machines&limit=50'+(after?'&after='+encodeURIComponent(after):'')) as { items?: Array<{ id: string }>; next?: string | null };
    for(const row of directory.items||[])if(typeof row.id==='string')ids.add(row.id);
    const next=directory.next||'';
    if(next&&next<=after)throw new HttpError('machine_directory_invalid_cursor',503);
    after=next;
  }while(after);
  if (input.machine_id) ids.add(input.machine_id);

  const accountIds = [...new Set(candidates.map(candidate => candidate.account_id))];
  const accounts = new Map<string, AccountView>();
  await Promise.all(accountIds.map(async id => {
    try { accounts.set(id, await read(env.ACCOUNTS, id, '/_internal/routing') as AccountView); }
    catch { accounts.set(id, {}); }
  }));
  for (const account of accounts.values()) for (const host of account.hosts || []) ids.add(host);
  if (!ids.size) throw new HttpError('no_connected_host', 503, JSON.stringify({machines:[]}));

  const machines = new Map<string, MachineView>();
  await Promise.all([...ids].map(async id => {
    try { machines.set(id, await read(env.MACHINES, id, '/_internal/routing') as MachineView); }
    catch { machines.set(id, { machine_id: id, engines: [] }); }
  }));

  for (const id of ids) if (machines.get(id)?.connected !== true) ids.delete(id);
  if (input.machine_id && !ids.has(input.machine_id) || !ids.size)
    throw new HttpError('no_connected_host', 503, JSON.stringify({machines:[...machines].map(([id,machine])=>({machine_id:id,connected:machine.connected===true,last_seen:machine.last_seen??null,age_ms:typeof machine.last_seen==='number'?Math.max(0,Date.now()-machine.last_seen):null}))}));

  // Headroom weight per candidate; luna is one option among many, so it never outweighs the mean of the others.
  const deep = DEEP_TIERS.has((input.tier || '').toLowerCase());
  const headroom = (candidate: Candidate): number | null => {
    const quota = accounts.get(candidate.account_id)?.quota?.remaining;
    if (Number.isFinite(quota)) return Math.max(0, Number(quota));
    return candidate.engine === 'zai' ? NEUTRAL_QUOTA : null;
  };
  const dropped: PickDiagnostics['excluded'] = [];
  const ready = (candidate: Candidate): boolean => {
    const view = accounts.get(candidate.account_id);
    return view?.credential_usable === true && view.account?.enabled !== false;
  };
  const advertised = (candidate: Candidate): boolean => [...ids].some(id => machines.get(id)?.engines?.includes(candidate.engine));
  const scored = candidates.map(candidate => {
    const left = headroom(candidate);
    const base = Math.max(0.0001, candidate.weight) * (left === null ? 0.1 : Math.max(0.01, left)) * (deep && isDeepWorkModel(candidate) ? DEEP_BIAS : 1);
    return { candidate, left, base };
  });
  // Auth/quota/advertisement observations narrow the pool, but never turn into a refusal when nothing survives.
  const narrow = <T,>(rows: T[], keep: (row: T) => boolean): T[] => { const kept = rows.filter(keep); return kept.length ? kept : rows; };
  let pool = scored;
  for (const row of pool) if (!ready(row.candidate)) {
    const status = accounts.get(row.candidate.account_id)?.credential_status || 'unavailable';
    dropped.push({ policy_id: row.candidate.id, account_id: row.candidate.account_id, reason: 'credential_' + status });
  }
  pool = narrow(pool, row => ready(row.candidate));
  pool = narrow(pool, row => row.left === null || row.left >= MIN_HEADROOM);
  pool = narrow(pool, row => advertised(row.candidate));
  if (dropped.length)
    console.error('[hub2] MODEL_PICKER_CREDENTIAL_EXCLUDED alert=dev@dtcdev.click', JSON.stringify(dropped));
  const others = pool.filter(row => !/luna/i.test(row.candidate.model)).map(row => row.base);
  const cap = others.length ? others.reduce((sum, weight) => sum + weight, 0) / others.length : Infinity;
  const weights = new Map(pool.map(row => [row.candidate.id, /luna/i.test(row.candidate.model) ? Math.min(row.base, cap) : row.base]));

  const options: Array<{ value: PickedRoute; weight: number }> = [];
  for (const { candidate, left } of pool) {
    const account = accounts.get(candidate.account_id) || {};
    const approved = (account.hosts || []).filter(id => ids.has(id));
    const hostIds = input.machine_id ? [input.machine_id] : (approved.length ? approved : [...ids]);
    const matching = hostIds.filter(id => machines.get(id)?.engines?.includes(candidate.engine));
    // A missing/offline engine advertisement does not hold a start. Try a host
    // anyway; its native launch result will decide whether the route works.
    const selectedHosts = matching.length ? matching : hostIds;
    for (const machineId of selectedHosts) {
      const weight = weights.get(candidate.id)! / selectedHosts.length;
      options.push({ value: {
        machine_id: machineId, engine: candidate.engine, model: candidate.model,
        ...(candidate.effort ? { effort: candidate.effort } : {}), account_id: candidate.account_id,
        policy_id: candidate.id, policy_version: policy.version,
        override_reason: override ? input.override_reason?.trim() || 'explicit_route' : null,
        weight, quota_remaining: left ?? 0, required_capabilities: required,
      }, weight });
    }
  }
  if(!options.length)throw new HttpError('model_policy_unavailable',503);
  const total = options.reduce((sum, option) => sum + option.weight, 0);
  const route = weightedChoice(options, random);
  const byPolicy = new Map<string, PickDiagnostics['pool'][number]>();
  for (const { value, weight } of options) {
    const row = byPolicy.get(value.policy_id) || { policy_id: value.policy_id, engine: value.engine, model: value.model, effort: value.effort ?? null, weight: 0, share: 0 };
    row.weight += weight; row.share = row.weight / total; byPolicy.set(value.policy_id, row);
  }
  return { route, diagnostics: { pool: [...byPolicy.values()], excluded: dropped } };
}

export { weightedChoice };
