import type { SqlStorage } from '@cloudflare/workers-types';
import { LivenessService } from './liveness';
import { openWorktreeIntake } from './worktree-intakes';

/** Operator custody is durable before any host filesystem effect. Census
 * labels never create these intents. An exact host receipt completes them. */
export const ADOPTION_SCHEMA = `CREATE TABLE IF NOT EXISTS session_adoption_intents (
  id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL,
  native_json TEXT NOT NULL, actor TEXT NOT NULL, owner_token_id TEXT,
  intake_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(machine_id,native_id)
);`;

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const tuple = (s: any) => [s?.native_id, s?.tag, s?.workspace, s?.engine, s?.created_at_ms];
const same = (a: any, b: any) => JSON.stringify(tuple(a)) === JSON.stringify(tuple(b));

function observed(sql: SqlStorage, machine: string, input: any, now: number): any | null {
  if (!input || typeof input.native_id !== 'string' || !uuid.test(input.native_id)) return null;
  const row: any = sql.exec('SELECT * FROM fleet_sessions WHERE machine_id=? AND native_id=?', machine, input.native_id).toArray()[0];
  const age = now - Date.parse(row?.observed_at);
  if (!row || !Number.isFinite(age) || age < -30000 || age > 180000 ||
      !Number.isSafeInteger(row.created_at_ms) || row.created_at_ms <= 0 ||
      !same(row, input) || !['running', 'starting', 'uncertain'].includes(row.phase) ||
      !row.tag || !row.workspace || !row.engine) return null;
  return row;
}

export function queueOperatorAdoptions(sql: SqlStorage, machine: string, inputs: any[], actor: string,
  token: string | null, now: number): { queued: number; existing: number; deferred: number } {
  let queued = 0, existing = 0, deferred = 0;
  for (const input of inputs) {
    // Existing canonical sessions, including protected humans, never acquire
    // a new lock merely because an operator repeats an adoption request.
    if (typeof input?.native_id === 'string' && sql.exec('SELECT id FROM session_control_sessions WHERE id=?', input.native_id).toArray().length) {
      existing++; continue;
    }
    const row = observed(sql, machine, input, now);
    if (!row) { deferred++; continue; }
    const managed = sql.exec('SELECT id FROM fleet_launches WHERE machine_id=? AND native_id=?', machine, row.native_id).toArray();
    if (managed.length) { deferred++; continue; }
    const prior: any = sql.exec('SELECT * FROM session_adoption_intents WHERE machine_id=? AND native_id=?', machine, row.native_id).toArray()[0];
    if (prior) {
      if (prior.actor === actor && same(JSON.parse(prior.native_json), row)) queued++;
      else deferred++;
      continue;
    }
    const native = { native_id: row.native_id, tag: row.tag, workspace: row.workspace,
      cwd: row.cwd, engine: row.engine, created_at_ms: row.created_at_ms, phase: row.phase };
    const intake = openWorktreeIntake(sql, { id: row.native_id, machine_id: machine,
      request_json: '{}', binding_json: JSON.stringify(native) });
    sql.exec("INSERT INTO session_adoption_intents VALUES (?,?,?,?,?,?,?,'pending',?)",
      crypto.randomUUID(), machine, row.native_id, JSON.stringify(native), actor, token, intake, new Date(now).toISOString());
    queued++;
  }
  return { queued, existing, deferred };
}

export function pendingOperatorAdoptions(sql: SqlStorage, machine: string, now: number): any[] {
  // Filter before LIMIT: stale/withdrawn requests must not permanently
  // starve newer valid operator intents on a busy host.
  return sql.exec(`SELECT i.* FROM session_adoption_intents i
    JOIN fleet_sessions f ON f.machine_id=i.machine_id AND f.native_id=i.native_id
    LEFT JOIN credentials c ON c.id=i.owner_token_id
    WHERE i.machine_id=? AND i.state='pending'
      AND f.observed_at>=? AND f.observed_at<=? AND f.phase IN ('running','starting','uncertain')
      AND json_extract(i.native_json,'$.created_at_ms')=f.created_at_ms
      AND json_extract(i.native_json,'$.tag')=f.tag
      AND json_extract(i.native_json,'$.workspace')=f.workspace
      AND json_extract(i.native_json,'$.engine')=f.engine
      AND (i.owner_token_id IS NULL OR (c.id IS NOT NULL AND c.revoked_at IS NULL AND c.expires_at>?))
    ORDER BY i.created_at,i.id LIMIT 100`, machine,
      new Date(now - 180000).toISOString(), new Date(now + 30000).toISOString(), new Date(now).toISOString()).toArray()
    .map((row: any) => ({ id: row.id, session_id: row.native_id, intake_task_id: row.intake_id,
      native: JSON.parse(row.native_json) }));
}

export function completeOperatorAdoption(sql: SqlStorage, machine: string, input: any, now: number,
  hostFamily: string | null): { ok: boolean; error?: string; replay?: boolean } {
  const intent: any = typeof input?.id === 'string' && sql.exec('SELECT * FROM session_adoption_intents WHERE id=? AND machine_id=?', input.id, machine).toArray()[0];
  if (!intent || input.lock_verified !== true || !same(JSON.parse(intent.native_json), input.native)) return { ok: false, error: 'adoption_receipt_mismatch' };
  if (intent.state === 'completed') return { ok: true, replay: true };
  if (intent.state !== 'pending' || !observed(sql, machine, input.native, now)) return { ok: false, error: 'adoption_native_unverified' };
  if (intent.owner_token_id) {
    const owner: any = sql.exec('SELECT revoked_at,expires_at FROM credentials WHERE id=?', intent.owner_token_id).toArray()[0];
    if (!owner || owner.revoked_at || !(Date.parse(owner.expires_at) > now)) return { ok: false, error: 'adoption_owner_revoked' };
  }
  if (sql.exec('SELECT id FROM session_control_sessions WHERE id=?', intent.native_id).toArray().length ||
      sql.exec('SELECT id FROM fleet_launches WHERE machine_id=? AND native_id=?', machine, intent.native_id).toArray().length) return { ok: false, error: 'adoption_identity_conflict' };
  const native = JSON.parse(intent.native_json);
  const receipt = input.repository;
  if (input.worktree !== false && (!receipt || typeof receipt.common_dir !== 'string' || !receipt.common_dir.startsWith('/') ||
      !Number.isSafeInteger(receipt.device) || !Number.isSafeInteger(receipt.inode) ||
      typeof input.lock_path !== 'string' || !input.lock_path.startsWith('/'))) return { ok: false, error: 'adoption_repository_unverified' };
  const at = new Date(now).toISOString();
  const request = { provenance: 'observed_native', origin: 'operator_adoption', workspace: native.workspace,
    cwd: native.cwd, intake_task_id: intent.intake_id };
  const binding = { native_id: native.native_id, tag: native.tag, workspace: native.workspace,
    native_created_at_ms: native.created_at_ms, generation: 1, provenance: 'observed_native',
    adopted_at: at, adoption_intent_id: intent.id, adoption_lock: input.worktree !== false,
    lock_path: input.lock_path, repository: receipt };
  sql.exec(`INSERT INTO session_control_sessions
    (id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,session_type,
     owner_token_id,host_family,created_at,updated_at)
    VALUES (?,?,?,?,?,?,'interactive','running',?,'aplexer',?,?,?,?)`,
    native.native_id, intent.actor, 'native-' + native.native_id, JSON.stringify(request), machine,
    native.engine, JSON.stringify(binding), intent.owner_token_id, hostFamily, at, at);
  sql.exec("UPDATE session_adoption_intents SET state='completed' WHERE id=?", intent.id);
  const alerts = new LivenessService(sql, undefined, () => now); alerts.init();
  alerts.setAlert('session_adoption', machine + '/' + native.native_id, null);
  return { ok: true };
}
