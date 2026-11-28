import { HttpError } from './http';
import type { CredentialMonitorEvidence, EvidenceInvalidationReason, Hub2Env, PickerDiagnosticProducer } from './types';
import { buildRoutingPool, eligibleCandidates, weightedChoice, type AccountView, type MachineView,
  type RoutingPolicy, type RoutingSnapshot, type PickInput, type PickedRoute, type PickDiagnostics } from './routing-pool';
export { weightedChoice, buildRoutingPool } from './routing-pool';
export type { PickInput, PickedRoute, PickDiagnostics, RoutingSnapshot } from './routing-pool';

const internalActor = JSON.stringify({ id: 'hub2-model-picker', kind: 'operator', scopes: ['operator:admin', 'routing:read'] });

export type CommittedPickIdentity = {
  project_id: string; operation_id: string; attempt: 0 | 1; previous_picker?: unknown;
};

/** Real starts and auth fallback share the global Control draw sequence. */
export async function pickCommittedModelRoute(env: Hub2Env, input: PickInput, identity: CommittedPickIdentity): Promise<{
  route: PickedRoute; diagnostics: PickDiagnostics;
}> {
  const response = await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://picker.internal/_internal/picker/draw', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...identity, input }),
  });
  const value = await response.json() as { route: PickedRoute; diagnostics: PickDiagnostics; error?: string; detail?: string };
  if (!response.ok) throw new HttpError(value.error ?? 'model_picker_data_unavailable', response.status, value.detail);
  return value;
}

async function read(namespace: DurableObjectNamespace, id: string, path: string): Promise<any> {
  const response = await namespace.get(namespace.idFromName(id)).fetch('https://picker.internal' + path, {
    headers: { 'x-hub2-actor': internalActor },
  });
  if (!response.ok) throw new HttpError('model_picker_data_unavailable', 503);
  return response.json();
}

export async function loadRoutingSnapshot(env: Hub2Env, input: PickInput, passive = false): Promise<RoutingSnapshot> {
  const policy = await read(env.CONTROL, 'global', '/_routing-policy') as RoutingPolicy;
  const candidates = eligibleCandidates(policy, input);
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
    try { accounts.set(id, await read(env.ACCOUNTS, id, passive ? '/_internal/routing-snapshot' : '/_internal/routing') as AccountView); }
    catch { accounts.set(id, {}); }
  }));
  for (const account of accounts.values()) for (const host of account.hosts || []) ids.add(host);
  if (!ids.size) throw new HttpError('no_connected_host', 503, JSON.stringify({machines:[]}));

  const machines = new Map<string, MachineView>();
  await Promise.all([...ids].map(async id => {
    try { machines.set(id, await read(env.MACHINES, id, passive ? '/_internal/routing-snapshot' : '/_internal/routing') as MachineView); }
    catch { machines.set(id, { machine_id: id, engines: [] }); }
  }));

  return {policy, accounts, machines, observed_at: Date.now()};
}

export async function pickModelRoute(env: Hub2Env, input: PickInput, random = Math.random): Promise<PickedRoute> {
  return (await pickModelRouteDetailed(env, input, random)).route;
}

/** Optional diagnostics count the existing decisions; primary work never awaits IO. */
function diagnosticObservation(env: Hub2Env, producer: PickerDiagnosticProducer) {
  let observer: CredentialMonitorEvidence | undefined, identity: unknown, settled = false;
  const invalidate = (reason: EvidenceInvalidationReason) => { try { observer?.invalidate(reason); } catch {} };
  try { observer = env.PICKER_DIAGNOSTIC_EVIDENCE?.[producer]; identity = observer?.begin(); }
  catch { invalidate('picker_begin_failed'); observer = undefined; }
  return {
    complete(alert: boolean) {
      if (settled) { invalidate('picker_finish_failed'); return; }
      settled = true;
      try {
        // finish updates its memory/queue synchronously, but its durability is
        // awaited only by the fresh evidence barrier, never by a start/draw.
        void observer?.finish(identity, alert ? 'alert' : 'observation').catch(() => invalidate('picker_finish_failed'));
      } catch { invalidate('picker_finish_failed'); }
    },
    uncertain(reason: EvidenceInvalidationReason) { if (!settled) invalidate(reason); },
  };
}

type PickerStage = 'snapshot' | 'pool' | 'decision';
function pickerFailureReason(stage: PickerStage, caught: unknown): EvidenceInvalidationReason {
  const fallback = stage === 'snapshot' ? 'picker_snapshot_failed'
    : stage === 'pool' ? 'picker_pool_failed' : 'picker_decision_incomplete';
  // Read only a few known public codes; never message/detail/provider strings.
  // Even a throwing code getter must not replace the original business error.
  try {
    if (caught instanceof HttpError) {
      if (stage === 'snapshot' && caught.code === 'model_picker_data_unavailable') return 'picker_snapshot_data_unavailable';
      if (stage !== 'decision' && caught.code === 'no_connected_host')
        return stage === 'snapshot' ? 'picker_snapshot_no_host' : 'picker_pool_no_host';
      if (stage !== 'decision' && (caught.code === 'model_policy_unavailable' || caught.code === 'model_policy_unconfigured' || caught.code === 'engine_auth_expired'))
        return stage === 'snapshot' ? 'picker_snapshot_policy_unavailable' : 'picker_pool_policy_unavailable';
    }
  } catch { /* Diagnostics cannot change or expose an unknown exception. */ }
  return fallback;
}

/** Only normal quota filtering is non-alerting; new/unknown reasons stay actionable. */
export function pickerExclusionsNeedAlert(excluded: PickDiagnostics['excluded']): boolean {
  return excluded.some(row => row.reason !== 'codex_reserve' && row.reason !== 'quota_below_minimum');
}

export async function pickModelRouteDetailed(env: Hub2Env, input: PickInput, random = Math.random): Promise<{route: PickedRoute; diagnostics: PickDiagnostics}> {
  const excluded = diagnosticObservation(env, 'picker_excluded');
  const quota = diagnosticObservation(env, 'picker_quota_stale');
  let stage: PickerStage = 'snapshot';
  let failureReason: EvidenceInvalidationReason = 'picker_decision_incomplete';
  try {
    const snapshot = await loadRoutingSnapshot(env, input);
    stage = 'pool';
    const {options, diagnostics} = buildRoutingPool(snapshot, input);
    stage = 'decision';
    const exclusionAlert = pickerExclusionsNeedAlert(diagnostics.excluded);
    excluded.complete(exclusionAlert);
    if (diagnostics.excluded.length) {
      const message = JSON.stringify(diagnostics.excluded);
      if (exclusionAlert) console.error('[hub2] MODEL_PICKER_EXCLUDED alert=dev@dtcdev.click', message);
      else console.info('[hub2] MODEL_PICKER_EXCLUDED observation', message);
    }
    quota.complete(!!diagnostics.warnings?.length);
    if (diagnostics.warnings?.length) console.error('[hub2] QUOTA_STALE alert=dev@dtcdev.click', JSON.stringify(diagnostics.warnings));
    return {route: weightedChoice(options, random), diagnostics};
  } catch (caught) {
    failureReason = pickerFailureReason(stage, caught);
    throw caught;
  } finally {
    // Unknown input/decision outcomes must not become a clean healthy sample.
    // Completed decisions remain counted even if weightedChoice later refuses.
    excluded.uncertain(failureReason); quota.uncertain(failureReason);
  }
}

/** One passive observation set; all draws are in memory and cannot advance live counters. */
export async function simulateModelRoutes(env: Hub2Env, input: PickInput, draws: number, random = Math.random) {
  if (!Number.isInteger(draws) || draws < 1 || draws > 1000) throw new HttpError('invalid_simulation_count', 400);
  const snapshot = await loadRoutingSnapshot(env, input, true);
  const {options, diagnostics} = buildRoutingPool(snapshot, input);
  const counts: Record<string, number> = Object.create(null);
  for (const row of diagnostics.pool) counts[row.policy_id] = 0;
  for (let n = 0; n < draws; n++) counts[weightedChoice(options, random).policy_id]++;
  return {observed_at: snapshot.observed_at, policy_version: snapshot.policy.version, draws, counts, ...diagnostics};
}
