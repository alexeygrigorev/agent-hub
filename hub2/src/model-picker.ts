import { HttpError } from './http';
import type { CredentialMonitorEvidence, Hub2Env, PickerDiagnosticProducer } from './types';
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
  const invalidate = () => { try { observer?.invalidate(); } catch {} };
  try { observer = env.PICKER_DIAGNOSTIC_EVIDENCE?.[producer]; identity = observer?.begin(); }
  catch { invalidate(); observer = undefined; }
  return {
    complete(alert: boolean) {
      if (settled) { invalidate(); return; }
      settled = true;
      try {
        // finish updates its memory/queue synchronously, but its durability is
        // awaited only by the fresh evidence barrier, never by a start/draw.
        void observer?.finish(identity, alert ? 'alert' : 'observation').catch(invalidate);
      } catch { invalidate(); }
    },
    uncertain() { if (!settled) invalidate(); },
  };
}

/** Only normal quota filtering is non-alerting; new/unknown reasons stay actionable. */
export function pickerExclusionsNeedAlert(excluded: PickDiagnostics['excluded']): boolean {
  return excluded.some(row => row.reason !== 'codex_reserve' && row.reason !== 'quota_below_minimum');
}

export async function pickModelRouteDetailed(env: Hub2Env, input: PickInput, random = Math.random): Promise<{route: PickedRoute; diagnostics: PickDiagnostics}> {
  const excluded = diagnosticObservation(env, 'picker_excluded');
  const quota = diagnosticObservation(env, 'picker_quota_stale');
  try {
    const {options, diagnostics} = buildRoutingPool(await loadRoutingSnapshot(env, input), input);
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
  } finally {
    // Unknown input/decision outcomes must not become a clean healthy sample.
    // Completed decisions remain counted even if weightedChoice later refuses.
    excluded.uncertain(); quota.uncertain();
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
