/// <reference types="node" />
// Production SHA-256 uses the Node builtin; the standalone Hub2 typecheck
// otherwise loads only Workers globals. @types/node is already a dependency.
import { createHash } from 'node:crypto';
import { HttpError } from './http';
import { isDeepWorkModel, isExplicitOnlyModel, isModelPolicyCandidateAllowed } from './model-policy';
import { allowsAstra, sessionPurpose, type SessionPurpose } from './session-purpose';

export type Candidate = { id: string; engine: string; model: string; effort?: string; account_id: string; weight: number; capabilities: string[]; enabled: boolean };
export type AccountView = { credential_status?: string; account?: { min_remaining?: number; enabled?: boolean | number } | null; quota?: { remaining?: number; captured_at?: number; valid_until?: number } | null; fresh?: boolean; hosts?: string[]; credential_usable?: boolean };
export type MachineView = { machine_id?: string; engines?: string[]; capabilities?: string[]; connected?: boolean; last_seen?: number; os?: string };
export type PickedRoute = {
  machine_id: string; engine: string; model: string; effort?: string; account_id: string;
  policy_id: string; policy_version: unknown; override_reason: string | null;
  weight: number; quota_remaining: number | null; required_capabilities: string[];
};
export type PickDiagnostics = {
  pool: Array<{ policy_id: string; engine: string; model: string; effort: string | null; weight: number; share: number }>;
  excluded: Array<{ policy_id: string; account_id: string; reason: string }>;
  warnings?: Array<{ account_id: string; reason: 'quota_stale'; captured_at: number | null }>;
  advisories?: Array<{policy_id: string; account_id: string; reason: string}>;
  pool_degraded?: boolean;
  share_floor?: { minimum_share: number; applied: boolean; reason: 'pool_too_large' | null };
};
// Base headroom knobs follow model-roulette; Hub adds exploration after Luna clipping.
const MIN_HEADROOM = 5;      // % quota left below which a pool has no headroom
const DEEP_BIAS = 3;         // opus/sol multiplier for tier deep/complex/serious
const NEUTRAL_QUOTA = 50;    // unknown observations get a neutral weight, never fabricated quota
const MIN_SHARE = 0.03;     // per eligible candidate; the remaining mass follows headroom
const QUOTA_MAX_AGE_MS = 30 * 60 * 1000;
const DEEP_TIERS = new Set(['deep', 'complex', 'serious']);
export type PickInput = {
  purpose?: SessionPurpose;
  machine_id?: string; engine?: string; model?: string; effort?: string; override_reason?: string;
  tier?: string; interactive: boolean; with_goal?: boolean; required_capabilities?: string[]; exclude_policy_ids?: string[];
  /** Launch workspace: a label or an absolute path on the target host. */
  workspace?: string;
};

export type RoutingPolicy = {version: unknown; candidates: Candidate[]};
export type RoutingSnapshot = {policy: RoutingPolicy; accounts: Map<string, AccountView>; machines: Map<string, MachineView>; observed_at: number};

/** Expired, invalidated or unproved observations never become current headroom. */
function currentQuota(view: AccountView | undefined, now: number): number | null {
  const quota = view?.quota;
  if (view?.fresh !== true || !quota || typeof quota.remaining !== 'number'
      || !Number.isFinite(quota.remaining) || quota.remaining < 0 || quota.remaining > 100
      || !Number.isSafeInteger(quota.captured_at) || !Number.isSafeInteger(quota.valid_until)
      || quota.captured_at! < 0 || quota.captured_at! > now + 5000
      || now - quota.captured_at! >= QUOTA_MAX_AGE_MS || quota.valid_until! <= now
      || quota.valid_until! < quota.captured_at! || quota.valid_until! > quota.captured_at! + 3600000) return null;
  return quota.remaining;
}

export function weightedChoice<T>(values: Array<{ value: T; weight: number }>, random = Math.random): T {
  const total = values.reduce((sum, row) => sum + row.weight, 0);
  if (!values.length || !Number.isFinite(total) || total <= 0) throw new HttpError('model_policy_unavailable', 503);
  let draw = random() * total;
  for (const row of values) {
    draw -= row.weight;
    if (draw < 0) return row.value;
  }
  return values[values.length - 1].value;
}

export function eligibleCandidates(policy: RoutingPolicy, input: PickInput): Candidate[] {
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

  return candidates;
}

export function buildRoutingPool(snapshot: RoutingSnapshot, input: PickInput) {
  const policy = snapshot.policy, candidates = eligibleCandidates(policy, input);
  const accounts = snapshot.accounts, machines = snapshot.machines;
  const accountIds = [...new Set(candidates.map(candidate => candidate.account_id))];
  const ids = new Set([...machines.keys()].sort());
  const override = !!(input.machine_id || input.engine || input.model || input.effort);
  const required = [...new Set([input.interactive ? 'interactive' : 'headless', ...(input.with_goal ? ['goal'] : []), ...(input.required_capabilities || [])])];
  for (const id of ids) if (machines.get(id)?.connected !== true) ids.delete(id);
  if (input.machine_id && !ids.has(input.machine_id) || !ids.size)
    throw new HttpError('no_connected_host', 503, JSON.stringify({machines:[...machines].map(([id,machine])=>({machine_id:id,connected:machine.connected===true,last_seen:machine.last_seen??null,age_ms:typeof machine.last_seen==='number'?Math.max(0,snapshot.observed_at-machine.last_seen):null}))}));

  // An absolute workspace path exists only on hosts of its own path family: never offer /home/... to a
  // Windows host or C:\... to a Linux one. Like the other observations this narrows and never refuses.
  const family = !input.workspace ? null : /^[A-Za-z]:\\/.test(input.workspace) ? 'windows' : input.workspace.startsWith('/') ? 'posix' : null;
  if (family && !input.machine_id) {
    const fits = [...ids].filter(id => {
      const os = (machines.get(id)?.os || '').toLowerCase();
      return !os || (os === 'windows') === (family === 'windows');
    });
    if (fits.length) for (const id of [...ids]) if (!fits.includes(id)) ids.delete(id);
  }

  // Headroom weight per candidate; luna is one option among many, so it never outweighs the mean of the others.
  const deep = DEEP_TIERS.has((input.tier || '').toLowerCase());
  const quotaNow = snapshot.observed_at;
  const observations = new Map(accountIds.map(id => [id, currentQuota(accounts.get(id), quotaNow)]));
  const warnings: NonNullable<PickDiagnostics['warnings']> = accountIds.filter(id => observations.get(id) === null).map(id => {
    const captured = accounts.get(id)?.quota?.captured_at;
    return { account_id: id, reason: 'quota_stale', captured_at: Number.isSafeInteger(captured) && captured! >= 0 ? captured! : null };
  });
  const headroom = (candidate: Candidate): number | null => observations.get(candidate.account_id) ?? null;
  const dropped: PickDiagnostics['excluded'] = [], advisories: NonNullable<PickDiagnostics['advisories']> = [];
  const centralReady = (candidate: Candidate): boolean => {
    const view = accounts.get(candidate.account_id);
    return view?.credential_usable === true && view.account?.enabled !== false && view.account?.enabled !== 0;
  };
  // A host observation is scoped to the exact checked Linux cwd, never to
  // every project using an installed engine. Labels and cold contexts are unknown.
  const contextHash = input.workspace?.startsWith('/') ? createHash('sha256').update(
    JSON.stringify(['hub2-local-auth-context-v1',input.workspace])).digest('base64url') : null;
  const localMissing = (candidate:Candidate,id:string):boolean => !!contextHash
    && candidate.engine==='claude' && candidate.account_id==='claude'
    && (machines.get(id)?.capabilities || []).some(flag => {
      const match = /^auth:missing:l:([hi]):claude:([A-Za-z0-9_-]{43}):([1-9][0-9]{0,9})$/.exec(flag);
      if (!match || match[1] !== (input.interactive?'i':'h') || match[2] !== contextHash) return false;
      const age = snapshot.observed_at - Number(match[3])*1000;
      return Number.isFinite(age) && age >= -5000 && age <= 60000;
    });
  const local = (candidate: Candidate, id: string): boolean => !localMissing(candidate,id) && machines.get(id)?.engines?.includes(candidate.engine) === true
    && machines.get(id)?.capabilities?.includes(`credentials:local:${input.interactive ? 'interactive' : 'headless'}:${candidate.engine}:${candidate.account_id}`) === true;
  const hostsFor = (candidate: Candidate): string[] => {
    const approved = (accounts.get(candidate.account_id)?.hosts || []).filter(id => ids.has(id));
    // Central credential delivery approval does not constrain host-owned logins.
    const hosts = input.machine_id ? [input.machine_id] : approved.length
      ? [...new Set([...approved,...[...ids].filter(id=>local(candidate,id))])] : [...ids];
    const matching = hosts.filter(id => machines.get(id)?.engines?.includes(candidate.engine));
    return matching.length ? matching : hosts;
  };
  const scored = candidates.map(candidate => {
    const left = headroom(candidate);
    const base = Math.max(0.0001, candidate.weight) * (left === null ? NEUTRAL_QUOTA : Math.max(0.01, left)) * (deep && isDeepWorkModel(candidate) ? DEEP_BIAS : 1);
    const originalHosts = hostsFor(candidate), observedHosts = originalHosts.filter(id=>!localMissing(candidate,id));
    const hosts = observedHosts.length ? observedHosts : originalHosts;
    const localHosts = observedHosts.filter(id => local(candidate,id));
    const knownLocalMissing = originalHosts.length>0 && observedHosts.length===0;
    const enabled = accounts.get(candidate.account_id)?.account?.enabled;
    return { candidate, left, base, hosts: !centralReady(candidate) && localHosts.length ? localHosts : hosts,
      credentialObserved: !knownLocalMissing && enabled !== false && enabled !== 0 && (centralReady(candidate) || localHosts.length > 0), localHosts,knownLocalMissing };
  });
  // Auth/quota/advertisement observations narrow the pool, but never turn into a refusal when nothing survives.
  const narrow = (rows: typeof scored, keep: (row: typeof scored[number]) => boolean, reason: (row: typeof scored[number]) => string) => {
    const kept = rows.filter(keep);
    for (const row of rows.filter(row => !keep(row))) (kept.length ? dropped : advisories).push({
      policy_id:row.candidate.id, account_id:row.candidate.account_id, reason:reason(row),
    });
    return kept.length ? kept : rows;
  };
  let pool = scored;
  pool = narrow(pool, row => row.credentialObserved, row => row.knownLocalMissing?'local_credentials_missing'
    :'credential_' + (accounts.get(row.candidate.account_id)?.credential_status || 'unavailable'));
  pool = narrow(pool, row => row.left === null || row.left >= MIN_HEADROOM, () => 'quota_below_minimum');
  pool = narrow(pool, row => row.hosts.some(id => machines.get(id)?.engines?.includes(row.candidate.engine)), () => 'engine_not_advertised');
  // Capability preference is global: a candidate without a capable host must not
  // survive its own advisory fallback when another candidate can satisfy launch.
  // Apply after the other eligibility observations, so capability hints cannot
  // bring back an account/host already removed by credential or quota preference.
  const compatibleHosts = (row: typeof scored[number]) => row.hosts.filter(id => {
    const machine = machines.get(id), engine = row.candidate.engine;
    const needed = [...required, ...(row.candidate.effort ? ['effort'] : [])];
    return machine?.engines?.includes(engine) && needed.every(capability =>
      capability.startsWith('os:') ? machine.os?.toLowerCase() === capability.slice(3).toLowerCase()
        : machine.capabilities?.includes(capability) || machine.capabilities?.includes(`${capability}:${engine}`));
  });
  const compatible = new Map(pool.map(row => [row.candidate.id, compatibleHosts(row)]));
  pool = narrow(pool, row => compatible.get(row.candidate.id)!.length > 0, () => 'capability_not_advertised');
  if (pool.some(row => compatible.get(row.candidate.id)!.length > 0))
    pool = pool.map(row => ({...row, hosts: compatible.get(row.candidate.id)!}));
  // Workspace advertisements are optional placement hints. A missing label
  // must not make every launch capability look unknown and restore an engine
  // already excluded because another engine can satisfy the requested goal.
  const label = input.workspace && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(input.workspace)
    ? `workspace:${input.workspace.toLowerCase()}` : null;
  if (label) {
    const workspaceHosts = new Map(pool.map(row => [row.candidate.id,
      row.hosts.filter(id => machines.get(id)?.capabilities?.includes(label))]));
    pool = narrow(pool, row => workspaceHosts.get(row.candidate.id)!.length > 0, () => 'capability_not_advertised');
    if (pool.some(row => workspaceHosts.get(row.candidate.id)!.length > 0))
      pool = pool.map(row => ({...row, hosts: workspaceHosts.get(row.candidate.id)!}));
  }
  for (const row of pool) if (row.localHosts.length) advisories.push({policy_id:row.candidate.id, account_id:row.candidate.account_id, reason:'local_login_unverified'});
  const others = pool.filter(row => !/luna/i.test(row.candidate.model)).map(row => row.base);
  const cap = others.length ? others.reduce((sum, weight) => sum + weight, 0) / others.length : Infinity;
  const weights = new Map(pool.map(row => [row.candidate.id, /luna/i.test(row.candidate.model) ? Math.min(row.base, cap) : row.base]));
  // p_i = MIN_SHARE + (1 - N * MIN_SHARE) * q_i. Apply once per
  // candidate, before host splitting. The affine mix preserves Luna's mean
  // bound and quota ordering, but deliberately replaces exact proportionality.
  // An oversized future pool cannot satisfy the floor: keep starts allowed,
  // retain the original weights, and report that the guarantee is unavailable.
  const residual = 1 - pool.length * MIN_SHARE;
  const shareFloorApplied = residual > 0;
  if (shareFloorApplied) {
    const total = [...weights.values()].reduce((sum, weight) => sum + weight, 0);
    for (const [id, weight] of weights) weights.set(id, MIN_SHARE * total + residual * weight);
  }

  const options: Array<{ value: PickedRoute; weight: number }> = [];
  for (const { candidate, left, hosts: selectedHosts } of pool) {
    for (const machineId of selectedHosts) {
      const weight = weights.get(candidate.id)! / selectedHosts.length;
      options.push({ value: {
        machine_id: machineId, engine: candidate.engine, model: candidate.model,
        ...(candidate.effort ? { effort: candidate.effort } : {}), account_id: candidate.account_id,
        policy_id: candidate.id, policy_version: policy.version,
        override_reason: override ? input.override_reason?.trim() || 'explicit_route' : null,
        weight, quota_remaining: left, required_capabilities: required,
      }, weight });
    }
  }
  if(!options.length)throw new HttpError('model_policy_unavailable',503);
  const total = options.reduce((sum, option) => sum + option.weight, 0);
  const byPolicy = new Map<string, PickDiagnostics['pool'][number]>();
  for (const { value, weight } of options) {
    const row = byPolicy.get(value.policy_id) || { policy_id: value.policy_id, engine: value.engine, model: value.model, effort: value.effort ?? null, weight: 0, share: 0 };
    row.weight += weight; row.share = row.weight / total; byPolicy.set(value.policy_id, row);
  }
  return { options, diagnostics: { pool: [...byPolicy.values()], excluded: dropped, warnings, advisories,
    share_floor: { minimum_share: MIN_SHARE, applied: shareFloorApplied, reason: shareFloorApplied ? null : 'pool_too_large' as const },
    pool_degraded:new Set([...byPolicy.values()].map(row=>row.model)).size < 3 } };
}
