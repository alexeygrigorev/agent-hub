import { HttpError } from './http';
import { isModelPolicyCandidateAllowed } from './model-policy';
import type { Hub2Env } from './types';

type Candidate = { id: string; engine: string; model: string; effort?: string; account_id: string; weight: number; capabilities: string[]; enabled: boolean };
type AccountView = { account?: { min_remaining?: number; enabled?: boolean } | null; quota?: { remaining?: number } | null; hosts?: string[]; credential_usable?: boolean };
type MachineView = { machine_id?: string; engines?: string[] };
export type PickedRoute = {
  machine_id: string; engine: string; model: string; effort?: string; account_id: string;
  policy_id: string; policy_version: unknown; override_reason: string | null;
  weight: number; quota_remaining: number; required_capabilities: string[];
};
export type PickInput = {
  machine_id?: string; engine?: string; model?: string; effort?: string; override_reason?: string;
  interactive: boolean; with_goal?: boolean; required_capabilities?: string[]; exclude_policy_ids?: string[];
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
  if (input.with_goal && !input.interactive) throw new HttpError('goal_requires_interactive', 400);
  const override = !!(input.machine_id || input.engine || input.model || input.effort);
  if (override && (!input.override_reason?.trim() || input.override_reason.length > 500))
    throw new HttpError('override_reason_required', 400);
  const required = [...new Set([input.interactive ? 'interactive' : 'headless', ...(input.with_goal ? ['goal'] : []), ...(input.required_capabilities || [])])];
  if (required.length > 32 || required.some(value => typeof value !== 'string' || !value || value.length > 80))
    throw new HttpError('invalid_required_capabilities', 400);

  const policy = await read(env.CONTROL, 'global', '/_routing-policy') as { version: unknown; candidates: Candidate[] };
  if (!Array.isArray(policy.candidates) || !policy.candidates.length || policy.candidates.length > 32)
    throw new HttpError('model_policy_unconfigured', 503);
  const excluded=new Set(input.exclude_policy_ids||[]);
  const candidates = policy.candidates.filter(candidate => candidate.enabled && !excluded.has(candidate.id) && isModelPolicyCandidateAllowed(candidate)
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
  if (!ids.size) throw new HttpError('machine_route_unavailable', 503);

  const machines = new Map<string, MachineView>();
  await Promise.all([...ids].map(async id => {
    try { machines.set(id, await read(env.MACHINES, id, '/_internal/routing') as MachineView); }
    catch { machines.set(id, { machine_id: id, engines: [] }); }
  }));

  const options: Array<{ value: PickedRoute; weight: number }> = [];
  for (const candidate of candidates) {
    const account = accounts.get(candidate.account_id) || {};
    const quotaRemaining = Number.isFinite(account.quota?.remaining) ? Math.max(0, Number(account.quota!.remaining)) : null;
    const approved = (account.hosts || []).filter(id => ids.has(id));
    const hostIds = input.machine_id ? [input.machine_id] : (approved.length ? approved : [...ids]);
    const matching = hostIds.filter(id => machines.get(id)?.engines?.includes(candidate.engine));
    // A missing/offline engine advertisement does not hold a start. Try a host
    // anyway; its native launch result will decide whether the route works.
    const selectedHosts = matching.length ? matching : hostIds;
    for (const machineId of selectedHosts) {
      const weight = Math.max(0.0001, candidate.weight) * (quotaRemaining === null ? 0.1 : Math.max(0.01, quotaRemaining));
      options.push({ value: {
        machine_id: machineId, engine: candidate.engine, model: candidate.model,
        ...(candidate.effort ? { effort: candidate.effort } : {}), account_id: candidate.account_id,
        policy_id: candidate.id, policy_version: policy.version,
        override_reason: override ? input.override_reason!.trim() : null,
        weight, quota_remaining: quotaRemaining ?? 0, required_capabilities: required,
      }, weight });
    }
  }
  if(!options.length)throw new HttpError('model_policy_unavailable',503);
  const credentialReady=options.filter(option=>accounts.get(option.value.account_id)?.credential_usable===true
    && accounts.get(option.value.account_id)?.account?.enabled!==false);
  // Prefer known-healthy credentials, but never turn health/quota observations into a refusal.
  return weightedChoice(credentialReady.length?credentialReady:options,random);
}

export { weightedChoice };
