/** Admin cancellation for both staged rollouts and the legacy global bump.
 * Legacy history retained versions, not old ciphertext/revision UUIDs. Restore
 * the recorded version by sealing the current configuration under a new UUID;
 * credential generations and concurrent operator changes are never reverted. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { bumpEngineEpoch } from './engine-epoch';
type Result = { error: string } | { rollout_id: string; engine: string; state: 'rolled_back';
  restored: Array<{ label: string; version: string; revision: string }>; replayed?: boolean };
type Dependencies = { sql: SqlStorage; tx: <T>(fn: () => T) => T; now: () => number;
  open: (row: any) => Promise<any>; seal: (bundle: any, revision: string) => Promise<string> };
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const LABEL = /^[a-z][a-z0-9_-]{0,47}$/;
const VERSION = /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/;
const object = (v: any) => !!v && typeof v === 'object' && !Array.isArray(v);
const cancellable = (state: string) => ['canary_pending', 'held_legacy'].includes(state);

export async function cancelEngineRollout(deps: Dependencies, id: string, input: any): Promise<Result> {
  if (!UUID.test(id)) return { error: 'invalid_rollout_id' };
  if (!object(input) || Object.keys(input).some(k => k !== 'reason')) return { error: 'invalid_cancel_request' };
  const reason = input.reason === undefined ? 'operator_cancelled' : input.reason;
  if (typeof reason !== 'string' || !reason.trim() || reason.length > 240 || /[\x00-\x1f\x7f]/.test(reason)) return { error: 'invalid_cancel_reason' };
  const { sql } = deps;
  const rollout: any = sql.exec('SELECT * FROM engine_rollouts WHERE id=?', id).toArray()[0];
  if (!rollout) return { error: 'rollout_not_found' };
  const replay = (): Result => ({ rollout_id: id, engine: rollout.engine, state: 'rolled_back', restored: [], replayed: true });
  if (rollout.state === 'rolled_back') return replay();
  if (!cancellable(rollout.state)) return { error: 'rollout_not_pending' };
  let detail: any, previous: any;
  try { detail = JSON.parse(rollout.detail); previous = JSON.parse(rollout.previous_json); }
  catch { return { error: 'invalid_rollout_history' }; }
  if (!object(detail) || !object(previous) || Object.keys(previous).length > 32) return { error: 'invalid_rollout_history' };
  const providers: Record<string, string> = { codex: 'codex', claude: 'claude', opencode: 'go', agy: 'antigravity', zcodex: 'zai' };
  if (!Object.hasOwn(providers, rollout.engine)) return { error: 'invalid_rollout_history' };
  const staged = detail.protocol === 2;
  const labels = staged ? [] : detail.bumped === undefined ? Object.keys(previous) : detail.bumped;
  if (!Array.isArray(labels) || labels.length > 32 || new Set(labels).size !== labels.length ||
      !labels.every(label => typeof label === 'string' && LABEL.test(label) && Object.hasOwn(previous, label) &&
        typeof previous[label] === 'string' && VERSION.test(previous[label]))) return { error: 'invalid_rollout_history' };
  const snapshots: Array<{ label: string; row: any; version: string; revision?: string; ciphertext?: string }> = [];
  try {
    for (const label of labels) {
      const row: any = sql.exec('SELECT * FROM engine_bundles WHERE label=?', label).toArray()[0];
      const item = { label, row, version: previous[label] } as typeof snapshots[number];
      snapshots.push(item);
      // An explicitly removed account stays removed. A newly recreated row
      // during crypto preparation is still detected by the transaction below.
      if (!row) continue;
      if (row.provider !== providers[rollout.engine] || (row.version !== rollout.target_version && row.version !== item.version)) return { error: 'bundle_changed' };
      if (row.version === item.version) continue;
      const bundle = await deps.open(row);
      if (bundle.label !== label || bundle.provider !== row.provider || bundle.version !== row.version) return { error: 'bundle_changed' };
      bundle.version = item.version;
      for (const key of ['revision', 'quota_provider', 'credential_generation', 'credential_paths', 'credential_status']) delete bundle[key];
      item.revision = crypto.randomUUID();
      item.ciphertext = await deps.seal(bundle, item.revision);
    }
  } catch { return { error: 'bundle_decryption_unavailable' }; }
  return deps.tx(() => {
    const current: any = sql.exec('SELECT * FROM engine_rollouts WHERE id=?', id).toArray()[0];
    if (current?.state === 'rolled_back') return replay();
    if (!current || !cancellable(current.state) || current.state !== rollout.state || current.target_version !== rollout.target_version ||
        current.detail !== rollout.detail || current.previous_json !== rollout.previous_json) return { error: 'rollout_changed' };
    for (const item of snapshots) {
      const row: any = sql.exec('SELECT revision FROM engine_bundles WHERE label=?', item.label).toArray()[0];
      if ((row?.revision ?? null) !== (item.row?.revision ?? null)) return { error: 'bundle_changed' };
    }
    const restored: Array<{ label: string; version: string; revision: string }> = [];
    for (const item of snapshots) {
      if (!item.revision) continue;
      sql.exec('UPDATE engine_bundles SET version=?,revision=?,ciphertext=? WHERE label=? AND revision=?',
        item.version, item.revision, item.ciphertext!, item.label, item.row.revision);
      restored.push({ label: item.label, version: item.version, revision: item.revision });
    }
    const at = deps.now();
    sql.exec("UPDATE engine_rollouts SET state='rolled_back',updated_at=?,detail=? WHERE id=?", at,
      JSON.stringify({ ...detail, reason: 'operator_cancelled', cancel_reason: reason.trim(), cancelled_at: at,
        rolled_back_at: at, cancel_mode: staged ? 'candidate_withdrawn' : 'legacy_version_restored' }), id);
    // New revisions invalidate installation/admission matching naturally.
    // Keep credentials, acknowledgements, routes and subscription history.
    bumpEngineEpoch(sql, at);
    return { rollout_id: id, engine: rollout.engine, state: 'rolled_back', restored };
  });
}
