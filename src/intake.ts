/** Durable private intake. Payload text is data, never execution or operator authority. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { CallerContext } from './bus';
import { credentialActor, registryAddress } from './agent_registry';
import { sha256Sync } from './conversation-followthrough';
import { SCOPE_POLICY_VERSION, SCOPE_VERDICTS, classifyScope, getScopeStatus, scopeBasisHash, scopeHint } from './intake_scope';

type Auth = { caller?: CallerContext; scopes?: string[]; error?: Response };
type Obj = Record<string, any>;
type Tx = <T>(fn: () => T) => T;
import { WORKER_SCOPE, triageTargets, workerAccessContext } from './worker-access';
import { redactClaimData } from './work_claim';
import { IntakeWorktrees, WorktreeError } from './intake-worktrees';
import { externalSession } from './intake-external';

/** Context changes invalidate old completion claims; notes never grant launch authority. */
export function currentHistory(task: Obj): Obj[] {
  const history: Obj[] = task.history ?? [];
  const boundary = history.reduce((n, e, i) =>
    e.kind === 'update' && ['status', 'title', 'project', 'acceptance_criteria'].some(k => k in (e.changes ?? {})) ? i : n, -1);
  return history.slice(boundary + 1);
}
function currentDoneNote(task: Obj): Obj | undefined {
  return currentHistory(task).find(e => e.kind === 'note' &&
    /^DONE\b/m.test(e.text ?? '') &&
    (/\b[0-9a-f]{7,40}\b/i.test(e.text) || /https:\/\/[^\s]+\/(?:actions\/runs|deployments)\/[^\s]+/.test(e.text)) &&
    /live check:[ \t]*\S[^\n]*/i.test(e.text) && /result:[ \t]*\S[^\n]*/i.test(e.text));
}
export const VERIFICATION_MODES = ['required', 'skip', 'auto'];
function verificationMode(v: any): string {
  if (!VERIFICATION_MODES.includes(v)) throw new IntakeError('invalid_verification', 400, 'verification');
  return v;
}
/** Worktree-guard (intake a0f01448): an intake counts as closed for cleanup
 * and reap gating when it carries a qualifying DONE note (live proof of
 * every AC), even while its status stays open awaiting operator review. */
export function intakeHasDoneEvidence(task: Obj): boolean {
  return currentDoneNote(task) !== undefined;
}
/** Completion claims only pause automatic admission for human review. They cannot
 * close an intake, validate historical evidence, or alter its acceptance criteria. */
export function intakeCompletionHoldReason(task: Obj): string | null {
  if (currentDoneNote(task)) return 'done_evidence_requires_operator_review';
  const claim = currentHistory(task).some(e => e.kind === 'note' &&
    (/^DONE\b/m.test(e.text ?? '') ||
      (/^ORIGINAL FINAL STATE\b/m.test(e.text ?? '') && /\bclosed (?:done|cancelled)\b/i.test(e.text))));
  return claim ? 'reported_completion_requires_operator_review' : null;
}
/** Narrow intake launch-hook composition (wired in src/hub.ts): assignment
 * creates the Hub session through the canonical session-control service with
 * the caller's own auth, so placement is never duplicated and session
 * identities are never invented here. Absent outside the Hub composition. */
export type IntakeHooks = { forgotten?: (tasks: Obj[]) => Obj; startSession?: (input: Obj, auth: Auth) => Promise<{ status: number; body: any }> };
/** Soft intake capacity (t1038u): no hard 507 refuse on task row count for create.
 * `tasks` remains only as a schema safety-net ceiling (very high). Open (non-terminal)
 * count > OPEN_INTAKE_WARN_THRESHOLD emits a warning requiring DONE evidence before close. */
export const INTAKE_LIMITS = { request: 32768, tasks: 10000, taskBytes: 131072, events: 128,
  // Hot admission budget excludes retained archives and exact replay receipts.
  // 128 MiB accommodates 1,000 maximum-size task records; retained storage
  // has a separate 256 MiB planning budget below the Free plan's 1 GB/DO.
  grants: 256, rules: 128, receipts: 8192, totalBytes: 128 * 1024 * 1024, page: 100 };
export const INTAKE_RETAINED_WARN_BYTES = 256 * 1024 * 1024;
const HOT_TASKS = "json_extract(data,'$.archived_at') IS NULL AND json_extract(data,'$.status') NOT IN ('done','cancelled')";
const ROW_BYTES = 'length(CAST(data AS BLOB))+length(CAST(id AS BLOB))';
/** SQL aggregates avoid loading retained response snapshots into Worker memory. */
function intakeBytes(sql: SqlStorage, table: string, where = ''): number {
  return Number((sql.exec(`SELECT COALESCE(SUM(${ROW_BYTES}),0) AS n FROM intake_${table}${where ? ' WHERE ' + where : ''}`).toArray()[0] as any).n);
}
function hotBytes(sql: SqlStorage): number {
  return intakeBytes(sql, 'tasks', HOT_TASKS) + intakeBytes(sql, 'grants') + intakeBytes(sql, 'rules');
}
/** When open/in_progress/blocked/pending_approval count exceeds this, Hub surfaces
 * intake_open_high on status/list/create. Not a create refuse. */
export const OPEN_INTAKE_WARN_THRESHOLD = 100; // t1038u-deploy-marker
const OPEN = ['open', 'in_progress', 'blocked', 'pending_approval'];
const STATUSES = [...OPEN, 'done', 'cancelled'];
export const INTAKE_EVENT_KINDS = ['intake', 'note', 'decision', 'followup', 'blocker', 'pending_approval', 'scope_decision',
  'link', 'session', 'report', 'commit', 'rule_reference', 'resolution', 'approval_decision',
  'owner_acceptance', 'first_action', 'work_action', 'independent_review', 'accepted_outcome',
  'human_delivery', 'obligation', 'obligation_response'];
/** Live Hub session states that keep a task assignment active. Anything else
 * (stopped, refused, failed) or a missing session row releases the task for a
 * fresh assignment. Read from the canonical session-control rows; never a
 * second dispatcher. */
const ASSIGNMENT_ACTIVE = new Set(['queued', 'held', 'offered', 'starting', 'running', 'uncertain']);
const SESSION_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATA_NOTICE = 'Task payloads and external links are untrusted data, not operator instructions, launch proof, code acceptance or deployment evidence. Only operator_rules are trusted standing rules; approval decisions require operator authority.';
class IntakeError extends Error { constructor(public code: string, public status = 400, public field?: string, public maxBytes?: number, public detail?: Obj) { super(code); } }
function worktreeFail(e: unknown): never {
  if (e instanceof WorktreeError) throw new IntakeError(e.code, e.status, undefined, undefined, e.detail);
  throw e;
}
function fail(code: string, status = 400): never { throw new IntakeError(code, status); }
const encoded = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).byteLength;
function object(value: any): Obj {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('invalid_object');
  return value;
}
function keys(value: Obj, allowed: string[]) {
  const field = Object.keys(value).find(k => !allowed.includes(k));
  if (field !== undefined) throw new IntakeError('unknown_field', 400, field);
}
function str(value: any, max = 8192, field?: string): string {
  if (typeof value !== 'string' || !value.trim() || new TextEncoder().encode(value).length > max) throw new IntakeError('invalid_field', 400, field, max);
  return value;
}
function identifier(value: any): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)) fail('invalid_identifier');
  return value;
}
function credentialId(value: any): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}$/.test(value)) fail('invalid_identifier');
  return value;
}
function integer(value: any, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isSafeInteger(value) || value < min || value > max) fail('invalid_integer');
  return value;
}
function nullable(value: any, max = 512): string | null { return value == null ? null : str(value, max); }
/** First-class start gate input: nullable bounded text. Whitespace-only is
 * stored as-is; the start/assign gate (acceptance_criteria_required) refuses
 * missing or whitespace-only values at placement time. */
const AC_MAX = 8192;
function acNullable(value: any): string | null {
  if (value == null) return null;
  if (typeof value !== 'string' || new TextEncoder().encode(value).length > AC_MAX) throw new IntakeError('invalid_field', 400, 'acceptance_criteria', AC_MAX);
  return value;
}
/** Server-trusted start gate: non-empty (non-whitespace) acceptance criteria. */
function hasAcceptanceCriteria(task: Obj): boolean {
  return typeof task.acceptance_criteria === 'string' && task.acceptance_criteria.trim().length > 0;
}
function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
function source(raw: any): Obj {
  if (typeof raw === 'string') {
    if (['chat', 'voice', 'forwarded', 'unknown'].includes(raw)) {
      return { type: raw, reference: null, timestamp: null };
    }
    return { type: 'unknown', reference: nullable(raw, 2048), timestamp: null };
  }
  const s = object(raw); keys(s, ['type', 'reference', 'timestamp']);
  if (!['chat', 'voice', 'forwarded', 'unknown'].includes(s.type)) fail('invalid_source');
  const timestamp = nullable(s.timestamp, 64);
  if (timestamp !== null && !Number.isFinite(Date.parse(timestamp))) fail('invalid_timestamp');
  return { type: s.type, reference: nullable(s.reference, 2048), timestamp };
}
async function body(request: Request): Promise<Obj> {
  if (!request.body) fail('missing_body');
  const reader = request.body!.getReader(); const chunks: Uint8Array[] = []; let size = 0;
  try {
    for (;;) {
      const next = await reader.read(); if (next.done) break;
      size += next.value.byteLength;
      if (size > INTAKE_LIMITS.request) { await reader.cancel(); fail('request_capacity', 413); }
      chunks.push(next.value);
    }
    const raw = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
    return object(JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(raw)));
  } catch (e) { if (e instanceof IntakeError) throw e; return fail('invalid_json'); }
  finally { reader.releaseLock(); }
}

/** Raised outside the session transaction so a refused link rolls back placement too. */
export class IntakeSessionLinkError extends Error {
  constructor(public code: string) { super(code); }
}
/** Service-only linkage. Call inside the transaction creating/updating the session.
 * It proves the Hub accepted a launch intent, never that the native process ran.
 * Manual links are preserved and counted as already linked during backfill. */
export function recordIntakeSessionLink(sql: SqlStorage, session: Obj, now: number, hold?: any): boolean {
  const request = JSON.parse(session.request_json);
  const taskId = request.intake_task_id;
  if (typeof taskId !== 'string' || !taskId) return false;
  let rows: any[] = sql.exec('SELECT data FROM intake_tasks WHERE id=?', taskId).toArray();
  if (!rows.length && taskId.length >= 6) rows = sql.exec("SELECT data FROM intake_tasks WHERE id LIKE ? || '%' LIMIT 2", taskId).toArray();
  if (rows.length !== 1) return false; // pruned, missing or ambiguous historical task
  const task = JSON.parse(rows[0].data);
  if (task.history.some((e: Obj) => ['link', 'session'].includes(e.kind) && e.hub_session_id === session.id)) return false;
  if (task.history.length >= INTAKE_LIMITS.events) throw new IntakeSessionLinkError('history_capacity');
  const binding = JSON.parse(session.binding_json || '{}');
  const observed = request.provenance === 'observed_native';
  task.history.push({ id: crypto.randomUUID(), kind: 'link', hub_session_id: session.id,
    native_id: observed ? binding.native_id ?? null : null, engine: session.engine ?? request.engine ?? null, tier: request.tier ?? null,
    host: session.machine_id ?? null, hub: null, routing_job_id: null,
    provenance: { type: observed ? 'observed_native' : 'hub_launch_verified', reference: session.id, timestamp: session.created_at },
    evidence: observed ? 'observed_native' : 'hub_launch_verified', authority: observed ? 'untrusted_task_data' : 'hub_launch_verified', actor: session.actor,
    captured_at: now, intake_key: session.request_key,
    ...(session.state === 'held' && hold !== undefined ? { assignment_hold: hold } : {}) });
  task.version++; task.updated_at = now;
  if (encoded(task) > INTAKE_LIMITS.taskBytes) throw new IntakeSessionLinkError('task_capacity');
  sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify(task), task.id);
  if (hotBytes(sql) > INTAKE_LIMITS.totalBytes) throw new IntakeSessionLinkError('storage_capacity');
  sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
  return true;
}

export class IntakeService {
  public worktrees: IntakeWorktrees;
  constructor(public sql: SqlStorage, private tx: Tx = <T>(fn: () => T) => fn(), private hooks: IntakeHooks = {}) {
    this.worktrees = new IntakeWorktrees(sql);
  }
  init() {
    this.tx(() => {
      this.worktrees.init();
      this.sql.exec('CREATE TABLE IF NOT EXISTS intake_meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)');
      this.sql.exec('INSERT OR IGNORE INTO intake_meta VALUES(1,0)');
      for (const table of ['tasks', 'grants', 'rules', 'receipts']) {
        this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_${table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)`);
      }
      const projectionExists = this.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intake_task_projection'").toArray().length > 0;
      this.sql.exec('CREATE TABLE IF NOT EXISTS intake_task_projection(id TEXT PRIMARY KEY, data TEXT NOT NULL, links TEXT NOT NULL)');
      // Only events consumed by status/continuation enter this projection.
      // Notes and original audit bodies remain exclusively in the full ledger.
      const project = (prefix: string) => `json_set(json_remove(${prefix}data,'$.history'), '$.history',
        json((SELECT json_group_array(json(CASE WHEN json_extract(e.value,'$.kind')='intake' THEN
            json_object('kind','intake','text',CASE WHEN json_type(e.value,'$.text')='null' THEN NULL ELSE '' END,
              'transcription',json_extract(e.value,'$.transcription'), 'source',json_object('reference',json_extract(e.value,'$.source.reference')))
            ELSE e.value END)) FROM json_each(${prefix}data,'$.history') e
          WHERE json_extract(e.value,'$.kind') IN ('intake','link','report','commit','followup','blocker','pending_approval','resolution','approval_decision')))),
        (SELECT json_group_array(json(e.value)) FROM json_each(${prefix}data,'$.history') e
         WHERE json_extract(e.value,'$.kind') IN ('link','report'))`;
      if (!projectionExists) this.sql.exec(`INSERT INTO intake_task_projection SELECT id, ${project('')} FROM intake_tasks`);
      for (const op of ['INSERT', 'UPDATE']) this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_projection_${op.toLowerCase()} AFTER ${op} ON intake_tasks BEGIN
        INSERT INTO intake_task_projection(id,data,links) SELECT NEW.id, ${project('NEW.')}
        ON CONFLICT(id) DO UPDATE SET data=excluded.data,links=excluded.links; END`);
      this.sql.exec('CREATE TRIGGER IF NOT EXISTS intake_projection_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_task_projection WHERE id=OLD.id; END');
      // Independent, append-only lifecycle ledger. No FK: survives loss of a task row.
      this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, project TEXT NOT NULL,
        kind TEXT NOT NULL, actor TEXT NOT NULL, captured_at INTEGER NOT NULL,
        from_status TEXT, to_status TEXT, reason TEXT NOT NULL)`);
      this.sql.exec('CREATE INDEX IF NOT EXISTS intake_audit_task ON intake_audit(task_id,seq)');
      for (const operation of ['UPDATE', 'DELETE']) this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_audit_no_${operation.toLowerCase()}
        BEFORE ${operation} ON intake_audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END`);
      // Write-maintained accounting: status must never walk retained histories
      // and replay bodies just to decide whether to display a storage warning.
      this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_storage_totals (
        kind TEXT PRIMARY KEY, bytes INTEGER NOT NULL, hot INTEGER NOT NULL,
        events INTEGER NOT NULL, original_requests INTEGER NOT NULL, count INTEGER NOT NULL)`);
      for (const table of ['tasks', 'grants', 'rules', 'receipts', 'audit']) {
        const values = (prefix: string) => {
          const c = (name: string) => prefix + name;
          const bytes = table === 'audit'
            ? "length(CAST(json_object('seq'," + c('seq') + ",'task_id'," + c('task_id') + ",'project'," + c('project') + ",'kind'," + c('kind') + ",'actor'," + c('actor') + ",'captured_at'," + c('captured_at') + ",'from_status'," + c('from_status') + ",'to_status'," + c('to_status') + ",'reason'," + c('reason') + ") AS BLOB))"
            : `length(CAST(${c('data')} AS BLOB))+length(CAST(${c('id')} AS BLOB))`;
          const hot = table === 'tasks' ? `CASE WHEN json_extract(${c('data')},'$.archived_at') IS NULL AND json_extract(${c('data')},'$.status') NOT IN ('done','cancelled') THEN ${bytes} ELSE 0 END`
            : ['grants', 'rules'].includes(table) ? bytes : '0';
          const events = table === 'tasks' ? `COALESCE(length(CAST(json_extract(${c('data')},'$.history') AS BLOB)),0)` : '0';
          const original = table === 'tasks' ? `(SELECT COALESCE(SUM(length(CAST(e.value AS BLOB))),0) FROM json_each(json_extract(${c('data')},'$.history')) e WHERE json_extract(e.value,'$.kind')='intake')` : '0';
          return [bytes, hot, events, original, '1'];
        };
        // Only the first migration reads old retained data. Subsequent cold
        // starts read five tiny accounting rows, with no backfill scan.
        if (!this.sql.exec('SELECT 1 FROM intake_storage_totals WHERE kind=?', table).toArray().length) {
          this.sql.exec(`INSERT INTO intake_storage_totals SELECT ?, ${values('').map(v => `COALESCE(SUM(${v}),0)`).join(',')} FROM intake_${table}`, table);
        }
        for (const op of ['INSERT', 'UPDATE', 'DELETE']) {
          const columns = ['bytes', 'hot', 'events', 'original_requests', 'count'];
          const before = values('OLD.'); const after = values('NEW.');
          const updates = columns.map((col, i) => `${col}=${col}${op !== 'DELETE' ? `+(${after[i]})` : ''}${op !== 'INSERT' ? `-(${before[i]})` : ''}`).join(',');
          this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_storage_${table}_${op.toLowerCase()} AFTER ${op} ON intake_${table} BEGIN UPDATE intake_storage_totals SET ${updates} WHERE kind='${table}'; END`);
        }
      }
      const oldStatus = "json_extract(OLD.data,'$.status')";
      const newStatus = "json_extract(NEW.data,'$.status')";
      const auditActor = "COALESCE(json_extract(NEW.data,'$.lifecycle_actor'),json_extract(NEW.data,'$.history[#-1].actor'),'hub:legacy')";
      const auditTime = "COALESCE(json_extract(NEW.data,'$.updated_at'),CAST(strftime('%s','now') AS INTEGER)*1000)";
      const auditReason = "COALESCE(json_extract(NEW.data,'$.lifecycle_reason'),'legacy lifecycle transition')";
      // v2 (d8d509d2 AC9): an operator close without evidence audits as
      // close_unverified so it never counts as a verified completion.
      this.sql.exec('DROP TRIGGER IF EXISTS intake_audit_status');
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_audit_status_v2 AFTER UPDATE ON intake_tasks
        WHEN ${oldStatus} IS NOT ${newStatus} AND (${newStatus} IN ('done','cancelled') OR ${oldStatus} IN ('done','cancelled')) BEGIN
        INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason)
        VALUES(NEW.id,json_extract(NEW.data,'$.project'),CASE ${newStatus} WHEN 'done' THEN CASE json_extract(NEW.data,'$.outcome') WHEN 'closed_unverified' THEN 'close_unverified' ELSE 'close' END WHEN 'cancelled' THEN 'cancel' ELSE 'reopen' END,
          ${auditActor},${auditTime},${oldStatus},${newStatus},${auditReason}); END`);
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_audit_archive AFTER UPDATE ON intake_tasks
        WHEN json_extract(OLD.data,'$.archived_at') IS NULL AND json_extract(NEW.data,'$.archived_at') IS NOT NULL BEGIN
        INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason)
        VALUES(NEW.id,json_extract(NEW.data,'$.project'),'archive',${auditActor},json_extract(NEW.data,'$.archived_at'),${oldStatus},${newStatus},${auditReason}); END`);
      // Defensive audit for any future SQL deletion path. No product deletion endpoint exists.
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_audit_delete AFTER DELETE ON intake_tasks BEGIN
        INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason)
        VALUES(OLD.id,json_extract(OLD.data,'$.project'),'delete','hub:sql',CAST(strftime('%s','now') AS INTEGER)*1000,
        ${oldStatus},NULL,'direct SQL delete; initiating actor unavailable'); END`);
      // A renamed/rebound registry identity cannot revive a former grant by moving back.
      // These triggers affect only our grant store; credential/registry ownership is unchanged.
      const actor = "CASE WHEN COALESCE(OLD.project,'')='' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END";
      const revoke = (predicate: string) => `UPDATE intake_grants SET data=json_set(data,'$.revoked_at',CAST(strftime('%s','now') AS INTEGER)*1000,'$.revocation_reason','registry_binding_changed') WHERE json_extract(data,'$.revoked_at') IS NULL AND ${predicate}; UPDATE intake_meta SET revision=revision+1 WHERE id=1 AND changes()>0;`;
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_end_registration AFTER DELETE ON agents BEGIN ${revoke(`json_extract(data,'$.binding.actor')=(${actor})`)} END`);
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_end_binding AFTER UPDATE OF tag,project,machine_id ON agents WHEN OLD.tag IS NOT NEW.tag OR COALESCE(OLD.project,'')!=COALESCE(NEW.project,'') OR OLD.machine_id IS NOT NEW.machine_id BEGIN ${revoke(`json_extract(data,'$.binding.actor')=(${actor})`)} END`);
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_end_machine AFTER DELETE ON machines BEGIN ${revoke("json_extract(data,'$.binding.machine_id')=OLD.id")} END`);
    });
  }
  private rows(table: string): Obj[] { return this.sql.exec(`SELECT data FROM intake_${table} ORDER BY id`).toArray().map((r: any) => JSON.parse(r.data)); }
  private get(table: string, id: string): Obj | undefined {
    let r = this.sql.exec(`SELECT data FROM intake_${table} WHERE id=?`, id).toArray()[0] as any;
    if (!r && table === 'tasks' && id.length >= 6) {
      const candidates = this.sql.exec(`SELECT data FROM intake_tasks WHERE substr(id,1,?)=? LIMIT 2`, id.length, id).toArray() as any[];
      if (candidates.length > 1) fail('ambiguous_prefix');
      if (candidates.length === 1) r = candidates[0];
    }
    return r ? JSON.parse(r.data) : undefined;
  }
  private put(table: string, data: Obj) {
    if (encoded(data) > 262144) fail('record_byte_capacity', 507);
    this.sql.exec(`INSERT INTO intake_${table}(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data`, data.id, JSON.stringify(data));
  }
  private revision(): number { return (this.sql.exec('SELECT revision FROM intake_meta WHERE id=1').toArray()[0] as any).revision; }
  private capacity(additionalBytes = 0) {
    if (hotBytes(this.sql) + additionalBytes > INTAKE_LIMITS.totalBytes) fail('storage_capacity', 507);
  }
  /** Operator-only callers expose this aggregate, never payloads or identities.
   * Event/original-request bytes are subsets of task bytes, not additive totals. */
  storageUsage(): Obj {
    const totals = new Map((this.sql.exec('SELECT * FROM intake_storage_totals').toArray() as any[]).map(r => [r.kind, r]));
    const tasks = totals.get('tasks').bytes;
    const open = totals.get('tasks').hot;
    const receipts = totals.get('receipts').bytes;
    const grants = totals.get('grants').bytes;
    const rules = totals.get('rules').bytes;
    const history = totals.get('tasks');
    const counts: Obj = Object.fromEntries([...totals].map(([key, value]) => [key, value.count]));
    const audit = totals.get('audit').bytes;
    const hot = open + grants + rules;
    const retained = tasks + receipts + grants + rules + audit;
    return { accounting: 'utf8_json_and_primary_key_bytes; audit_json_projection; excludes SQLite overhead',
      bytes: { open_tasks: open, archived_or_terminal_tasks: tasks - open, receipts, grants, rules, audit,
        events_within_tasks: Number(history.events), original_requests_within_events: Number(history.original_requests) },
      counts, hot_bytes: hot, hot_limit_bytes: INTAKE_LIMITS.totalBytes,
      retained_bytes: retained, retained_planning_bytes: INTAKE_RETAINED_WARN_BYTES };
  }
  storageWarning(): Obj | null {
    const usage = this.storageUsage();
    if (usage.hot_bytes < INTAKE_LIMITS.totalBytes * 0.8 && usage.retained_bytes < INTAKE_RETAINED_WARN_BYTES * 0.8) return null;
    return { code: 'intake_storage_high', severity: 'warn', subject: 'intake:storage',
      title: 'Intake storage is high', source: 'intake_storage', scope: 'hub',
      detail: 'Plan additional retention storage before capacity is exhausted. Preserve every intake, event and replay receipt.',
      action: 'expand_retention_storage', threshold_ratio: 0.8, ...usage };
  }
  private bounded(table: string, max: number) { if (this.rows(table).length >= max) fail('record_capacity', 507); }
  /** Non-terminal task count used for soft open-intake warnings (never blocks create). */
  openNonTerminalCount(principal?: Obj, summaries?: Obj[]): number {
    const rows = summaries ?? this.sql.exec('SELECT data FROM intake_task_projection').toArray().map((r: any) => JSON.parse(r.data));
    return rows.filter(t => !t.archived_at && OPEN.includes(t.status) && (!principal || this.allowed(principal, t))).length;
  }
  /** Operator-visible soft warning when open intake is high. Null when under threshold. */
  intakeOpenWarning(principal?: Obj, summaries?: Obj[]): Obj | null {
    const rows = summaries ?? this.sql.exec('SELECT data FROM intake_task_projection').toArray().map((r: any) => JSON.parse(r.data));
    const open = rows.filter(t => !t.archived_at && OPEN.includes(t.status) && (!principal || this.allowed(principal, t)));
    const open_count = open.length;
    if (open_count <= OPEN_INTAKE_WARN_THRESHOLD) return null;
    return {
      code: 'intake_open_high',
      severity: 'warn',
      open_count,
      threshold: OPEN_INTAKE_WARN_THRESHOLD,
      message: 'Open intake is high. Closing requires a DONE note containing a commit SHA or deploy run URL AND a live check (command/URL plus result). Review evidence for each intake before closing.',
      action: 'review_done_evidence',
      items: open
        .map(t => ({ id: t.id, title: t.title, age_ms: Math.max(0, Date.now() - (t.created_at ?? Date.now())) })),
    };
  }
  /** Retain terminal payloads and replay receipts forever. No data expiration. */
  private prune(now: number) {
    for (const task of this.rows('tasks').filter(t => !t.archived_at && !OPEN.includes(t.status)).slice(0, 256)) {
      task.archived_at = now;
      task.lifecycle_actor = 'hub:archive'; task.lifecycle_reason = 'archive existing terminal intake; payload retained';
      this.put('tasks', task);
      this.sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
    }
  }

  private doneNote(task: Obj): Obj | undefined { return currentDoneNote(task); }
  private transition(task: Obj, status: string, actor: string, now: number, reason?: string, supersededBy?: string, force?: boolean, unverified = false) {
    if (status === task.status) return;
    if (status === 'done') {
      // AC9 (d8d509d2): an operator-ordered close without evidence records
      // outcome=closed_unverified and its reason; no DONE evidence is implied.
      const note = unverified ? { text: 'closed_unverified: ' + reason } : this.doneNote(task);
      if (!note) fail('done_evidence_required', 409);
      // Close gate (intake 4b161918): every linked worktree merged and removed.
      const gate = this.worktrees.gate(task.id);
      if (!gate.ok) {
        if (!force) throw new IntakeError('worktree_close_gate', 409, undefined, undefined, { worktrees: gate.blocking,
          hint: 'hub worktree refresh <intake> | hub worktree remove <intake> | hub intake close <intake> --force --reason TEXT' });
        if (task.history.length >= INTAKE_LIMITS.events - 1) fail('history_capacity', 507);
        task.history.push({ id: crypto.randomUUID(), kind: 'close_gate_override', reason, blocking: gate.blocking, actor,
          captured_at: now, authority: 'untrusted_task_data' });
        reason = 'force: ' + reason;
      } else reason = reason || note.text;
    }
    if (status === 'cancelled') {
      if (!reason?.trim() || (!supersededBy && !/\bdropped by Alexey\b/i.test(reason))) fail('cancel_reason_required', 409);
      if (supersededBy) {
        const replacement = this.get('tasks', identifier(supersededBy));
        if (!replacement || replacement.id === task.id) fail('invalid_superseded_by');
        task.superseded_by = replacement.id;
      }
    }
    task.lifecycle_actor = actor;
    task.lifecycle_reason = reason?.trim() || 'reopened for continued work';
    if (status === 'done') task.outcome = unverified ? 'closed_unverified' : 'completed';
    else if (OPEN.includes(status)) delete task.outcome;
    task.status = status;
    task.archived_at = OPEN.includes(status) ? null : now;
  }
  /** Fresh credential, binding and grant checks run in the same synchronous transaction as the effect. */
  private principal(auth: Auth): Obj {
    const caller = auth.caller!;
    if (!caller) fail('unauthorized', 401);
    if (caller.kind === 'human' && caller.isAdmin && auth.scopes?.includes('admin')) {
      let identity = caller.actor || 'admin';
      if (caller.tokenId) {
        const c = this.sql.exec('SELECT * FROM credentials WHERE id=?', caller.tokenId).toArray()[0] as any;
        let scopes: any; try { scopes = JSON.parse(c?.scopes); } catch { fail('unauthorized', 401); }
        if (!c || c.kind !== 'human' || c.token_hash !== caller.tokenHash || c.revoked_at ||
          !Number.isFinite(Date.parse(c.expires_at)) || Date.parse(c.expires_at) <= Date.now() || !Array.isArray(scopes) || !scopes.includes('admin') || !c.family_id) fail('unauthorized', 401);
        identity = 'human-family:' + c.family_id;
      }
      return { admin: true, actor: caller.actor || 'admin', identity, caller };
    }
    if (caller.kind !== 'agent' || !caller.tokenId || !caller.tokenHash) fail('forbidden', 403);
    const c = this.sql.exec('SELECT * FROM credentials WHERE id=?', caller.tokenId).toArray()[0] as any;
    const binding = this.binding(c);
    if (!binding || c.token_hash !== caller.tokenHash || binding.actor !== caller.actor) fail('forbidden', 403);
    const worker = workerAccessContext(this.sql, c.id);
    if (JSON.parse(c.scopes).includes(WORKER_SCOPE)) {
      if (!worker || worker.actor !== binding.actor) fail('forbidden', 403);
      const triage = triageTargets(this.sql, worker.session_id);
      return { admin: false, worker: true, actor: binding.actor, identity: 'family:' + binding.family_id, caller, triage,
        grants: [{ scope: 'task', target: worker.task_id, access: 'write', expires_at: worker.expires_at },
          ...[...triage].map(target => ({ scope: 'task', target, access: 'write', expires_at: worker.expires_at }))],
        valid_until: Math.min(Date.parse(c.expires_at), worker.expires_at) };
    }
    const grants = this.rows('grants').filter(g => !g.revoked_at && g.expires_at > Date.now() && canonical(g.binding) === canonical(binding));
    if (!grants.length) fail('forbidden', 403);
    return { admin: false, actor: binding.actor, identity: 'family:' + binding.family_id, grants, caller,
      valid_until: Math.min(Date.parse(c.expires_at), ...grants.map(g => g.expires_at)) };
  }
  private binding(c: any): Obj | null {
    if (!c || c.kind !== 'agent' || c.revoked_at || !Number.isFinite(Date.parse(c.expires_at)) || Date.parse(c.expires_at) <= Date.now()) return null;
    let scopes: any; try { scopes = JSON.parse(c.scopes); } catch { return null; }
    if (!Array.isArray(scopes) || scopes.includes('admin') || !scopes.includes('bus:read')) return null;
    const actor = credentialActor(this.sql, c); if (!actor) return null;
    const address = registryAddress(actor)!;
    const row = this.sql.exec("SELECT rowid AS incarnation, registered_at FROM agents WHERE tag=? AND COALESCE(project,'')=?", address.tag, address.project || '').toArray()[0] as any;
    if (!row?.registered_at) return null;
    if (typeof c.family_id !== 'string' || !c.family_id) return null;
    return { family_id: c.family_id,
      actor, project: address.project, machine_id: c.machine_id ?? null, registered_at: row.registered_at, incarnation: row.incarnation };
  }
  private allowed(p: Obj, task: Obj, write = false): boolean {
    return p.admin || p.grants.some((g: Obj) => (!write || g.access === 'write') &&
      (g.scope === 'all' || (g.scope === 'project' && g.target === task.project) || (g.scope === 'task' && g.target === task.id)));
  }
  public resolvePrincipal(auth: Auth): Obj {
    return this.principal(auth);
  }
  public isTaskAllowed(p: Obj, task: Obj, write = false): boolean {
    return this.allowed(p, task, write);
  }
  public getAuthorizedTask(taskId: string, auth: Auth): Obj | null {
    try {
      const p = this.principal(auth);
      const task = this.get('tasks', taskId);
      if (!task || !this.allowed(p, task)) return null;
      return task;
    } catch {
      return null;
    }
  }
  public getTaskById(taskId: string): Obj | null {
    return this.get('tasks', taskId) ?? null;
  }
  private operator(p: Obj) { if (!p.admin) fail('operator_required', 403); }
  private expected(b: Obj, version: number) { if (integer(b.expected_version) !== version) fail('version_conflict', 409); }
  private rules(tasks: Obj[]): Obj[] {
    return this.rows('rules').filter(r => r.project === null || tasks.some(t => t.project === r.project));
  }
  private unresolved(task: Obj): Obj[] {
    const resolved = new Set(task.history.filter((e: Obj) => e.kind === 'resolution' || e.kind === 'approval_decision').map((e: Obj) => e.ref));
    return task.history.filter((e: Obj) => ['followup', 'blocker', 'pending_approval'].includes(e.kind) && !resolved.has(e.id));
  }
  /** Read-only lookup of a canonical Hub session row. The session-control
   * service owns these rows; intake never writes them. A missing table (older
   * database migrated without session control) reads as no session. */
  private sessionRow(id: string): Obj | null {
    try {
      const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id=?', id).toArray()[0];
      return row ?? null;
    } catch { return null; }
  }
  /** The live Hub session id keeping this task assigned, if any. Guards (not
   * disclosure): the row is read regardless of actor, only its id is used. */
  private activeAssignment(task: Obj): string | null {
    for (let i = task.history.length - 1; i >= 0; i--) {
      const e = task.history[i];
      if (e?.kind === 'link' && typeof e.hub_session_id === 'string' && SESSION_UUID.test(e.hub_session_id)) {
        const row = this.sessionRow(e.hub_session_id);
        if (row && ASSIGNMENT_ACTIVE.has(row.state)) return e.hub_session_id;
        return null;
      }
    }
    return null;
  }
  /** Live projection of every Hub session linked to this task, newest first.
   * State and binding are read live from the canonical session rows at the
   * task's snapshot revision; full lifecycle detail is visible only to the
   * operator or the session's owning actor, everyone else sees the id with a
   * not_visible marker (the link event itself already discloses the id). */
  private assignedSessions(task: Obj, p: Obj): Obj[] {
    const seen = new Set<string>(); const out: Obj[] = [];
    for (let i = task.history.length - 1; i >= 0; i--) {
      const e = task.history[i];
      if (e?.kind === 'link' && e.evidence === 'external_live_verified' && e.native_id) {
        const key = 'external:' + e.host + '/' + e.native_id;
        if (!seen.has(key)) {
          seen.add(key);
          const n = externalSession(this.sql, e, Date.now());
          out.push({ hub_session_id: null, native_id: e.native_id, machine: e.host,
            state: n?.liveness === 'unknown' || !n ? 'unknown'
              : n.liveness === 'terminal' && !['exited', 'stopped', 'failed'].includes(n.phase) ? 'failed' : n.phase,
            liveness: n?.liveness ?? 'unknown', engine: n?.engine ?? e.engine,
            observed_at: n?.observed_at ?? null, linkage: 'external_session',
            evidence: 'external_live_verified', launch_verified: false });
        }
      }
      if (e?.kind !== 'link' || typeof e.hub_session_id !== 'string' || !SESSION_UUID.test(e.hub_session_id) || seen.has(e.hub_session_id)) continue;
      seen.add(e.hub_session_id);
      const row = this.sessionRow(e.hub_session_id);
      if (!row) { out.push({ hub_session_id: e.hub_session_id, linkage: 'unknown_session' }); continue; }
      if (!p.admin && row.actor !== p.actor) { out.push({ hub_session_id: e.hub_session_id, linkage: 'not_visible' }); continue; }
      let binding: Obj | null = null;
      try { binding = row.binding_json ? JSON.parse(row.binding_json) : null; } catch { binding = null; }
      out.push({ hub_session_id: e.hub_session_id, state: row.state, machine: row.machine_id ?? null,
        ...(row.state === 'held' ? { hold: e.assignment_hold ?? 'placement_pending' } : {}),
        engine: row.engine ?? null, mode: row.mode ?? null, fleet_launch_id: row.fleet_launch_id ?? null,
        binding, updated_at: row.updated_at ?? null, linkage: 'hub_session' });
    }
    return out;
  }
  private continuation(task: Obj): string {
    const pending = this.unresolved(task);
    if (task.status === 'pending_approval' || pending.some(e => e.kind === 'pending_approval')) return 'approval_blocked';
    const pendingVoice = task.history.some((e: Obj, i: number) => e.kind === 'intake' && e.text === null &&
      !task.history.slice(i + 1).some((later: Obj) => later.kind === 'intake' && later.transcription === 'provided' && later.source.reference === e.source.reference));
    if (task.uncertainty || pendingVoice) return 'uncertain';
    if (task.status === 'blocked' || pending.some(e => e.kind === 'blocker')) return 'blocked';
    if (task.status === 'in_progress') return 'active';
    return task.status === 'open' ? 'ready' : 'closed_or_waiting';
  }
  /** F2 scope projection. Null scope = legacy pre-policy task (grandfathered).
   * The classifier only hints; only an operator scope_decision unblocks. */
  private scopeView(task: Obj): Obj | null {
    if (!task.scope || typeof task.scope !== 'object') return null;
    const status = getScopeStatus(task);
    return {
      policy_version: task.scope.policy_version ?? null,
      flag: task.scope.flag ?? null,
      matched_terms: Array.isArray(task.scope.matched_terms) ? task.scope.matched_terms : [],
      decision: status.decision,
      clear: status.clear,
      ...(status.block_code ? { block_code: status.block_code } : {}),
      ...(status.stale_policy ? { stale_policy: true } : {}),
    };
  }
  private taskView(task: Obj, p: Obj): Obj {
    const { title, current_state, next_step, owner, uncertainty, acceptance_criteria, history, ...identity } = task;
    return { ...identity, control: { version: task.version, status: task.status, continuation_state: this.continuation(task),
      authority: 'hub_ledger_cas', execution_evidence: 'not_verified_by_intake' },
      scope_flag: task.scope?.flag ?? null, scope: this.scopeView(task),
      payload_data: { title, current_state, next_step, owner, uncertainty, acceptance_criteria: acceptance_criteria ?? null, history }, unresolved: this.unresolved(task),
      assigned_sessions: this.assignedSessions(task, p),
      worktrees: this.worktrees.list(task.id), close_gate: this.worktrees.gate(task.id) };
  }
  private view(task: Obj, p: Obj): Obj { return { ...this.taskView(task, p), operator_rules: this.rules([task]), data_notice: DATA_NOTICE }; }
  private page(url: URL, items: Obj[], revision: number): Obj {
    const rawLimit = url.searchParams.get('limit');
    let limit = 25;
    if (rawLimit !== null) {
      if (!/^[-+]?\d+$/.test(rawLimit.trim())) throw new IntakeError('invalid_limit', 400, 'limit');
      const requested = BigInt(rawLimit.trim());
      if (requested < 1n) throw new IntakeError('limit_out_of_range', 400, 'limit');
      limit = Number(requested > BigInt(INTAKE_LIMITS.page) ? BigInt(INTAKE_LIMITS.page) : requested);
    }
    const after = url.searchParams.get('after');
    if (after) credentialId(after);
    if (after && !url.searchParams.has('revision')) fail('revision_required');
    if (url.searchParams.has('revision') && integer(Number(url.searchParams.get('revision'))) !== revision) fail('stale_snapshot', 409);
    const remaining = items.filter(t => !after || t.id > after);
    const page = remaining.slice(0, limit);
    return { snapshot_version: revision, effective_limit: limit, items: page, next: remaining.length > limit ? page[page.length - 1].id : null, total: items.length };
  }
  private read(url: URL, p: Obj): Obj {
    const path = url.pathname.slice('/api/intake'.length); const revision = this.revision();
    if (path === '/storage') {
      this.operator(p);
      return { snapshot_version: revision, ...this.storageUsage(), warning: this.storageWarning() };
    }
    if (path === '/grants') {
      this.operator(p);
      return { snapshot_version: revision, items: this.rows('grants').map(({ binding, ...g }) => ({ ...g, actor: binding.actor, family_id: binding.family_id })) };
    }
    if (path === '/grantees') {
      this.operator(p);
      const actorParam = url.searchParams.get('actor');
      if (!actorParam || typeof actorParam !== 'string' || !actorParam.trim() || actorParam.length > 128) {
        fail('invalid_actor', 400);
      }
      const addr = registryAddress(actorParam);
      if (!addr) fail('invalid_actor', 400);

      const familyParam = url.searchParams.get('family_id') || url.searchParams.get('family');
      if (familyParam !== null && (typeof familyParam !== 'string' || !familyParam.trim() || familyParam.length > 128)) {
        fail('invalid_family', 400);
      }

      const nowIso = new Date().toISOString();
      let query = `SELECT id, kind, family_id, machine_id, scopes, created_at, expires_at, revoked_at, registry_actor, actor, subject
         FROM credentials
         WHERE kind = 'agent'
           AND revoked_at IS NULL
           AND expires_at > ?
           AND (registry_actor = ? OR actor = ? OR subject = ? OR (COALESCE(registry_actor, '') = '' AND (actor = ? OR subject = ?)))`;
      const queryParams: any[] = [nowIso, actorParam, actorParam, actorParam, addr.tag, addr.tag];
      if (familyParam) {
        query += ` AND family_id = ?`;
        queryParams.push(familyParam.trim());
      }
      const scanLimit = INTAKE_LIMITS.grants + 1;
      query += ` ORDER BY created_at DESC LIMIT ?`;
      queryParams.push(scanLimit);

      const rows = this.sql.exec(query, ...queryParams).toArray() as any[];
      if (rows.length > INTAKE_LIMITS.grants) {
        fail('record_capacity', 507);
      }

      const families = new Map<string, Obj>();
      for (const c of rows) {
        const binding = this.binding(c);
        if (!binding || binding.actor !== actorParam) continue;
        if (familyParam && binding.family_id !== familyParam.trim()) continue;
        let scopes: string[] = [];
        try { scopes = JSON.parse(c.scopes); } catch { continue; }
        if (!families.has(binding.family_id)) {
          families.set(binding.family_id, {
            id: c.id,
            credential_id: c.id,
            actor: binding.actor,
            family_id: binding.family_id,
            project: binding.project ?? null,
            machine_id: binding.machine_id ?? null,
            scopes,
            created_at: c.created_at,
            expires_at: c.expires_at,
            registered_at: binding.registered_at,
            incarnation: binding.incarnation,
          });
        }
      }

      if (families.size > INTAKE_LIMITS.grants) {
        fail('record_capacity', 507);
      }

      const allItems = Array.from(families.values()).sort((a, b) => a.id.localeCompare(b.id));
      const paged = this.page(url, allItems, revision);
      return {
        ...paged,
        actor: actorParam,
        ...(familyParam ? { family_id: familyParam.trim() } : {}),
      };
    }
    if (path === '/forgotten') {
      if (!this.hooks.forgotten) fail('watchdog_unavailable', 503);
      const project = url.searchParams.get('project');
      return this.hooks.forgotten(this.statusAuthorizedTasks(p).filter(t => !project || t.project === project));
    }
    if (path === '/rules') {
      this.operator(p); return { snapshot_version: revision, operator_rules: this.rows('rules'), data_notice: DATA_NOTICE };
    }
    const auditMatch = /^\/tasks\/([A-Za-z0-9._:-]+)\/audit$/.exec(path);
    if (path === '/audit' || auditMatch) {
      let id: string | undefined;
      if (auditMatch) {
        const task = this.get('tasks', auditMatch[1]);
        if (task) { if (!this.allowed(p, task)) fail('not_found', 404); id = task.id; }
        else {
          // A deleted task remains operator-readable from its durable audit identity.
          this.operator(p);
          const ids = this.sql.exec("SELECT DISTINCT task_id FROM intake_audit WHERE task_id=? OR substr(task_id,1,?)=? LIMIT 2", auditMatch[1], auditMatch[1].length, auditMatch[1]).toArray() as any[];
          if (ids.length !== 1) fail('not_found', 404);
          id = ids[0].task_id;
        }
      }
      const events = this.sql.exec('SELECT * FROM intake_audit ORDER BY seq DESC').toArray() as any[];
      const visible = events.filter(e => (!id || e.task_id === id) && (p.admin || this.allowed(p, { id: e.task_id, project: e.project })));
      // seq padded for the common revision-pinned cursor implementation.
      return this.page(url, visible.map(e => ({ ...e, id: String(Number.MAX_SAFE_INTEGER - e.seq).padStart(20, '0') })).sort((a, b) => a.id.localeCompare(b.id)), revision);
    }
    if (path === '/tasks' || path === '/handoff') {
      if (url.searchParams.has('authorization_valid_until') && Number(url.searchParams.get('authorization_valid_until')) !== (p.valid_until ?? null)) fail('stale_snapshot', 409);
      if (!p.admin && url.searchParams.has('after') && !url.searchParams.has('authorization_valid_until')) fail('authorization_snapshot_required');
      let tasks = this.rows('tasks').filter(t => this.allowed(p, t));
      if (url.searchParams.has('project')) tasks = tasks.filter(t => t.project === url.searchParams.get('project'));
      const status = url.searchParams.get('status');
      const archived = url.searchParams.get('archived');
      const all = url.searchParams.get('all');
      if (archived && archived !== 'true') fail('invalid_archived');
      if (all && all !== 'true') fail('invalid_all');
      if (archived === 'true') tasks = tasks.filter(t => t.archived_at);
      else if (path === '/handoff' || (all !== 'true' && status !== 'all' && !['done', 'cancelled'].includes(status ?? '')))
        tasks = tasks.filter(t => !t.archived_at && OPEN.includes(t.status));
      if (status && status !== 'all' && status !== 'open' && !STATUSES.includes(status)) fail('invalid_status');
      if (path === '/handoff' || status === 'open') tasks = tasks.filter(t => OPEN.includes(t.status) || this.unresolved(t).length > 0 || this.continuation(t) === 'uncertain');
      else if (status && status !== 'all') tasks = tasks.filter(t => t.status === status);
      const result = this.page(url, tasks, revision);
      const warn = this.intakeOpenWarning(p);
      return { ...result, authorization_valid_until: p.valid_until ?? null, items: result.items.map((t: Obj) => this.taskView(t, p)), operator_rules: this.rules(tasks),
        data_notice: DATA_NOTICE, authorized_scope: p.admin ? 'operator_all' : 'explicit_grants_only',
        completeness: 'All authorized matching records are included across pages at snapshot_version. Restart on stale_snapshot.',
        ...(p.admin ? { intake_storage_warning: this.storageWarning() } : {}),
        intake_policy: 'soft_unlimited_tasks_t1038u',
        open_non_terminal_count: this.openNonTerminalCount(p),
        open_intake_warn_threshold: OPEN_INTAKE_WARN_THRESHOLD,
        ...(warn ? { intake_open_warning: warn } : {}) };
    }
    const commandMatch = /^\/worktree-commands\/([0-9a-f-]{36})$/.exec(path);
    if (commandMatch) {
      const command = this.worktrees.command(commandMatch[1]);
      const owner = command && this.get('tasks', command.task_id);
      if (!command || !owner || !this.allowed(p, owner)) fail('not_found', 404);
      return { snapshot_version: revision, command };
    }
    const worktreesMatch = /^\/tasks\/([A-Za-z0-9._:-]+)\/worktrees$/.exec(path);
    if (worktreesMatch) {
      const task = this.get('tasks', worktreesMatch[1]);
      if (!task || !this.allowed(p, task)) fail('not_found', 404);
      return { snapshot_version: revision, task_id: task.id, status: task.status, worktrees: this.worktrees.list(task.id), close_gate: this.worktrees.gate(task.id) };
    }
    const match = /^\/tasks\/([A-Za-z0-9._:-]+)(?:\/(history|links|context))?$/.exec(path);
    if (!match) fail('not_found', 404);
    const task = this.get('tasks', match[1]);
    if (!task || !this.allowed(p, task)) fail('not_found', 404);
    if (url.searchParams.has('revision') && integer(Number(url.searchParams.get('revision'))) !== revision) fail('stale_snapshot', 409);
    return { snapshot_version: revision, task: this.view(task, p) };
  }
  private event(raw: Obj, task: Obj, p: Obj, now: number): Obj {
    // Session is the public spelling of an external session link. Keep the
    // existing canonical link validation and ownership checks.
    if (raw.kind === 'session') raw = { ...raw, kind: 'link' };
    if (['note', 'decision', 'followup', 'blocker', 'pending_approval', 'scope_decision'].includes(raw.kind) && raw.source === undefined)
      raw = { ...raw, source: { type: 'unknown' } };
    if (raw.kind === 'link' && raw.provenance === undefined) raw = { ...raw, provenance: { type: 'unknown' } };
    const kind = raw.kind; let fields: Obj;
    if (kind === 'intake') {
      keys(raw, ['kind', 'text', 'source', 'transcription']);
      const origin = source(raw.source);
      const transcription = raw.transcription ?? (origin.type === 'voice' ? 'provided' : 'not_applicable');
      if (!['provided', 'pending', 'unavailable', 'not_applicable'].includes(transcription)) fail('invalid_transcription');
      if (raw.text === null && (origin.type !== 'voice' || !origin.reference || !['pending', 'unavailable'].includes(transcription))) fail('invalid_voice_capture');
      if (raw.text !== null && ['pending', 'unavailable'].includes(transcription)) fail('invalid_transcription');
      fields = { text: raw.text === null ? null : str(raw.text, 8192, 'text'), source: origin, transcription };
    } else if (['note', 'decision', 'followup', 'blocker', 'pending_approval'].includes(kind)) {
      keys(raw, ['kind', 'text', 'source']); fields = { text: str(raw.text, 8192, 'text'), source: source(raw.source) };
    } else if (kind === 'link') {
      keys(raw, ['kind', 'native_id', 'engine', 'tier', 'host', 'hub', 'provenance', 'routing_job_id', 'hub_session_id', 'verify_live']);
      if (raw.verify_live !== undefined && typeof raw.verify_live !== 'boolean') fail('invalid_field');
      let hub_session_id: string | null = null;
      if (raw.hub_session_id != null) {
        if (typeof raw.hub_session_id !== 'string' || !SESSION_UUID.test(raw.hub_session_id)) fail('invalid_field');
        hub_session_id = raw.hub_session_id;
      }
      fields = { native_id: nullable(raw.native_id), engine: nullable(raw.engine, 128), tier: nullable(raw.tier, 128),
        host: nullable(raw.host), hub: nullable(raw.hub, 2048), provenance: source(raw.provenance),
        routing_job_id: nullable(raw.routing_job_id, 128), hub_session_id, evidence: 'external_unverified_not_launch_proof' };
      if (raw.verify_live === true) {
        this.operator(p);
        const n = externalSession(this.sql, fields, now);
        if (!n?.live) fail('external_session_not_live', 409);
        fields.host = n.machine_id;
        fields.engine = n.engine ?? fields.engine;
        fields.evidence = 'external_live_verified';
        fields.verified_at = now;
      }
      if (fields.routing_job_id && !p.admin) fail('canonical_link_requires_operator', 403);
      if (fields.routing_job_id && !this.sql.exec('SELECT id FROM routing_jobs WHERE id=?', fields.routing_job_id).toArray().length) fail('unknown_routing_job');
      // A Hub session link must reference the real canonical session row and is
      // visible only to the operator or the session's owning actor: the same
      // authorized actor that controls it through `hub sessions`. Missing or
      // foreign sessions render as not_found, never an oracle.
      if (hub_session_id) {
        const row = this.sessionRow(hub_session_id);
        if (!row || (!p.admin && row.actor !== p.actor)) fail('not_found', 404);
      }
    } else if (kind === 'report' || kind === 'commit') {
      keys(raw, ['kind', 'reference', 'source']); fields = { reference: str(raw.reference, 2048), source: source(raw.source), evidence: 'external_unverified' };
    } else if (kind === 'rule_reference') {
      keys(raw, ['kind', 'rule_id', 'version', 'source']); const rule = this.get('rules', identifier(raw.rule_id));
      if (!rule || integer(raw.version, 1) > rule.version || (rule.project !== null && rule.project !== task.project)) fail('unknown_rule_version');
      fields = { rule_id: raw.rule_id, version: raw.version, source: source(raw.source) };
    } else if (kind === 'resolution' || kind === 'approval_decision') {
      keys(raw, ['kind', 'ref', 'text', 'source', ...(kind === 'approval_decision' ? ['decision'] : [])]);
      const ref = task.history.find((e: Obj) => e.id === raw.ref);
      if (!ref || !this.unresolved(task).some(e => e.id === raw.ref)) fail('unknown_obligation');
      if (kind === 'approval_decision') {
        this.operator(p); if (ref.kind !== 'pending_approval' || !['granted', 'denied', 'withdrawn'].includes(raw.decision)) fail('invalid_approval_decision');
      } else if (ref.kind === 'pending_approval') fail('operator_required', 403);
      fields = { ref: identifier(raw.ref), text: str(raw.text, 8192, 'text'), source: source(raw.source), ...(kind === 'approval_decision' ? { decision: raw.decision } : {}) };
    } else if (kind === 'scope_decision') {
      // F2 scope override: operator-only. Task text (notes/decisions by
      // non-operators) can never approve itself: only this event, recorded
      // with operator authority and a server-computed basis hash, unblocks
      // assign/linked start. Changing project/text/AC changes the basis hash
      // and invalidates the decision (CAS at gate time).
      this.operator(p);
      keys(raw, ['kind', 'verdict', 'text', 'source']);
      const verdict = str(raw.verdict, 32);
      if (!(SCOPE_VERDICTS as string[]).includes(verdict)) fail('invalid_scope_verdict');
      const provenance = source(raw.source);
      if (verdict === 'gray_adr' && !provenance.reference) fail('scope_adr_required');
      fields = { verdict, text: str(raw.text, 8192, 'text'), source: provenance,
        basis_hash: scopeBasisHash(task), policy_version: SCOPE_POLICY_VERSION };
    } else if (kind === 'owner_acceptance') {
      keys(raw, ['kind', 'text', 'source', 'verdict']);
      if (!task.owner) {
        if (p.actor === task.history[0]?.actor || p.admin) {
          task.owner = p.actor;
        } else {
          fail('owner_acceptance_requires_owner', 403);
        }
      } else if (task.owner !== p.actor) {
        fail('owner_acceptance_requires_owner', 403);
      }
      const verdict = raw.verdict ? str(raw.verdict, 32) : 'accepted';
      if (!['accepted', 'refused'].includes(verdict)) fail('invalid_verdict');
      fields = { text: str(raw.text, 8192, 'text'), verdict, source: source(raw.source) };
    } else if (kind === 'first_action' || kind === 'work_action') {
      keys(raw, ['kind', 'action_type', 'text', 'source', 'evidence_digest', 'native_session', 'host_id']);
      const isOwner = task.owner && task.owner === p.actor;
      const isAssigned = task.history.some((e: Obj) => e.kind === 'assignment' && e.assignee === p.actor);
      if (!p.admin && !isOwner && !isAssigned) fail('forbidden', 403);
      const action_type = raw.action_type ? str(raw.action_type, 64) : 'work_action';
      const text = str(raw.text, 8192, 'text');
      let evidence_digest: string | null = null;
      if (raw.evidence_digest) {
        const dig = str(raw.evidence_digest, 64).toLowerCase();
        if (!/^[0-9a-f]{64}$/.test(dig)) fail('invalid_evidence_digest');
        evidence_digest = dig;
      }
      const native_session = raw.native_session ? str(raw.native_session, 128) : null;
      const host_id = raw.host_id ? str(raw.host_id, 128) : null;
      fields = {
        action_type,
        text,
        evidence_digest,
        native_session,
        host_id,
        source: source(raw.source),
      };
    } else if (kind === 'independent_review') {
      keys(raw, ['kind', 'source_sha', 'verdict', 'text', 'source', 'reviewer']);
      const source_sha = str(raw.source_sha, 64).toLowerCase();
      if (!/^[0-9a-f]{40}$/.test(source_sha)) fail('invalid_source_sha');
      const verdict = str(raw.verdict, 32);
      if (!['approved', 'changes_requested', 'rejected'].includes(verdict)) fail('invalid_verdict');

      const contributors = new Set<string>();
      if (Array.isArray(task.history)) {
        for (const item of task.history) {
          if (item.kind === 'commit' && item.actor) contributors.add(item.actor);
          if (item.kind === 'report' && item.actor) contributors.add(item.actor);
          if ((item.kind === 'work_action' || item.kind === 'first_action') && item.actor) contributors.add(item.actor);
          if (item.kind === 'assignment' && item.assignee) contributors.add(item.assignee);
        }
      }
      if (typeof task.owner === 'string' && task.owner.trim()) {
        contributors.add(task.owner.trim());
      }
      const claimedReviewer = raw.reviewer ? str(raw.reviewer, 128) : p.actor;
      if (contributors.has(claimedReviewer) || contributors.has(p.actor)) {
        fail('reviewer_not_independent', 403);
      }
      if (raw.reviewer && raw.reviewer !== p.actor) {
        fail('reviewer_mismatch', 403);
      }
      fields = {
        reviewer: claimedReviewer,
        source_sha,
        verdict,
        text: nullable(raw.text, 8192),
        source: source(raw.source),
      };
    } else if (kind === 'accepted_outcome') {
      keys(raw, ['kind', 'outcome_ref', 'source_sha', 'review_ref', 'verdict', 'text', 'source']);
      this.operator(p);
      const verdict = str(raw.verdict, 32);
      if (!['accepted', 'rejected'].includes(verdict)) fail('invalid_verdict');
      const outcome_ref = str(raw.outcome_ref, 2048);
      const source_sha = str(raw.source_sha, 64).toLowerCase();
      if (!/^[0-9a-f]{40}$/.test(source_sha)) fail('invalid_source_sha');
      const review_ref = str(raw.review_ref, 128);

      const reviewEvent = task.history.find((e: Obj) =>
        e.kind === 'independent_review' && e.id === review_ref
      );
      if (!reviewEvent) fail('missing_independent_review', 409);
      if (reviewEvent.verdict !== 'approved') fail('review_not_approved', 409);
      if (reviewEvent.source_sha !== source_sha) fail('source_sha_mismatch', 409);

      const reviewIndex = task.history.indexOf(reviewEvent);
      for (let j = reviewIndex + 1; j < task.history.length; j++) {
        const later = task.history[j];
        if (later.kind === 'commit' && later.reference && later.reference.toLowerCase() !== source_sha) {
          fail('stale_reviewed_source', 409);
        }
        if (later.kind === 'independent_review' && later.verdict !== 'approved') {
          fail('stale_reviewed_source', 409);
        }
      }

      fields = {
        outcome_ref,
        source_sha,
        review_ref: reviewEvent.id,
        verdict,
        text: nullable(raw.text, 8192),
        source: source(raw.source),
      };
    } else if (kind === 'human_delivery') {
      keys(raw, ['kind', 'recipient', 'text', 'source', 'outcome_ref', 'source_sha']);
      if (p.caller?.kind !== 'human' || !p.caller.actor.startsWith('human:')) fail('human_delivery_requires_human', 403);
      const recipient = p.caller.actor;
      if (raw.recipient && raw.recipient !== recipient) fail('recipient_mismatch', 403);

      const acceptedOutcome = task.history.slice().reverse().find((e: Obj) => e.kind === 'accepted_outcome' && e.verdict === 'accepted');
      if (!acceptedOutcome) fail('no_accepted_outcome', 409);
      if (raw.source_sha && raw.source_sha.toLowerCase() !== acceptedOutcome.source_sha.toLowerCase()) {
        fail('stale_outcome_source', 409);
      }
      if (raw.outcome_ref && raw.outcome_ref !== acceptedOutcome.outcome_ref) {
        fail('outcome_ref_mismatch', 409);
      }

      const outcomeIndex = task.history.lastIndexOf(acceptedOutcome);
      for (let j = outcomeIndex + 1; j < task.history.length; j++) {
        const later = task.history[j];
        if (later.kind === 'commit' && later.reference && later.reference.toLowerCase() !== acceptedOutcome.source_sha.toLowerCase()) {
          fail('stale_outcome_source', 409);
        }
      }

      fields = {
        recipient,
        delivered_by: p.caller.actor,
        accepted_outcome_id: acceptedOutcome.id,
        source_sha: acceptedOutcome.source_sha,
        outcome_ref: acceptedOutcome.outcome_ref,
        task_version: task.version,
        text: str(raw.text, 8192, 'text'),
        source: source(raw.source),
      };
    } else if (kind === 'obligation') {
      keys(raw, ['kind', 'obligation_id', 'text', 'default_action', 'wait_s', 'deadline', 'source']);
      const obligationId = identifier(raw.obligation_id || crypto.randomUUID());
      if (task.history.some((e: Obj) => e.kind === 'obligation' && e.obligation_id === obligationId)) {
        fail('duplicate_obligation_id', 409);
      }
      const text = str(raw.text, 8192, 'text');
      const text_hash = sha256Sync(text).slice(0, 16);
      const default_action = str(raw.default_action, 2048);
      const ALLOWED_REVERSIBLE_ACTIONS = ['proceed', 'proceed_with_reversible_default', 'hold_reversible_decision', 'rollback_to_last_checkpoint', 'reversible_fallback'];
      if (!ALLOWED_REVERSIBLE_ACTIONS.includes(default_action)) {
        fail('invalid_reversible_default_action');
      }
      const wait = raw.wait_s ? integer(raw.wait_s, 1, 86400 * 30) : 1200;
      const deadline = raw.deadline ? integer(raw.deadline) : now + wait * 1000;
      fields = {
        obligation_id: obligationId,
        text,
        text_hash,
        default_action,
        wait_s: wait,
        asked_at: now,
        deadline,
        source: source(raw.source),
      };
    } else if (kind === 'obligation_response') {
      keys(raw, ['kind', 'ref', 'response_type', 'text', 'source']);
      const ref = task.history.find((e: Obj) => e.obligation_id === raw.ref || e.id === raw.ref);
      if (!ref) fail('unknown_obligation');
      const responseType = str(raw.response_type, 32);
      if (!['answer', 'proceed'].includes(responseType)) fail('invalid_response_type');
      const refId = ref.obligation_id || ref.id;
      const priorResponses = task.history.filter((e: Obj) => e.kind === 'obligation_response' && e.ref === refId);
      const hasAnswer = priorResponses.some((e: Obj) => e.response_type === 'answer');
      const hasProceeded = priorResponses.some((e: Obj) => e.response_type === 'proceed');

      if (responseType === 'proceed') {
        if (hasProceeded) fail('obligation_already_proceeded', 409);
        if (hasAnswer) fail('obligation_already_answered', 409);
        if (now < Number(ref.deadline)) fail('cannot_proceed_before_deadline', 409);
      } else if (responseType === 'answer') {
        if (hasAnswer) fail('obligation_already_answered', 409);
      }

      const newStatus = responseType === 'proceed'
        ? 'proceeded'
        : hasProceeded
          ? 'answered-after-proceeding'
          : 'answered';

      fields = {
        ref: refId,
        response_type: responseType,
        resulting_status: newStatus,
        text: str(raw.text, 8192, 'text'),
        source: source(raw.source),
      };
    } else return fail('invalid_event_kind');
    const authority =
      kind === 'link' && fields.evidence === 'external_live_verified' ? 'host_census_verified' :
      kind === 'approval_decision' ? 'operator_record' :
      kind === 'scope_decision' ? 'operator_record' :
      kind === 'owner_acceptance' ? 'canonical_owner_acceptance' :
      (kind === 'first_action' || kind === 'work_action') ? 'canonical_hub_action_receipt' :
      kind === 'independent_review' ? 'canonical_independent_review' :
      kind === 'accepted_outcome' ? 'canonical_accepted_outcome' :
      kind === 'human_delivery' ? 'human_delivery_receipt' :
      kind === 'obligation' ? 'canonical_obligation' :
      kind === 'obligation_response' ? 'canonical_obligation_response' :
      'untrusted_task_data';
    return { ...fields, kind, id: crypto.randomUUID(), actor: p.actor, captured_at: now, authority };
  }
  /** Worktree registry writes: register, refresh (status) and remove. */
  private writeWorktrees(path: string, b: Obj, p: Obj, now: number): Obj {
    let task: Obj | undefined; let worktreeIds: (string | null)[] = [];
    if (path === '/worktrees/remove') {
      keys(b, ['key', 'session_id']);
      if (typeof b.session_id !== 'string' || !SESSION_UUID.test(b.session_id)) throw new IntakeError('invalid_field', 400, 'session_id');
      const found = this.worktrees.bySession(b.session_id);
      task = found ? this.get('tasks', found.taskId) : undefined;
      if (!found || !task || !this.allowed(p, task, true)) fail('not_found', 404);
      worktreeIds = found.worktreeIds.length ? found.worktreeIds : [null];
    } else {
      const m = /^\/tasks\/([A-Za-z0-9._:-]+)\/worktrees(?:\/(refresh|remove|register))?$/.exec(path)!;
      task = this.get('tasks', m[1]); if (!task || !this.allowed(p, task, true)) fail('not_found', 404);
      if (!m[2] || m[2] === 'register') {
        keys(b, ['key', 'worktree']);
        const w = object(b.worktree); keys(w, ['machine', 'path', 'branch', 'session_id']);
        if (w.session_id != null && (typeof w.session_id !== 'string' || !SESSION_UUID.test(w.session_id))) throw new IntakeError('invalid_field', 400, 'session_id');
        let recorded: { row: Obj; fresh: boolean };
        try { recorded = this.worktrees.record(task.id, { machine: w.machine, path: w.path, branch: w.branch ?? null, session_id: w.session_id ?? null, origin: 'registered' }); }
        catch (e) { worktreeFail(e); }
        if (recorded.fresh) this.pushHistory(task, [{ kind: 'worktree', action: 'registered', worktree_id: recorded.row.id, machine: recorded.row.machine,
          path: recorded.row.path, branch: recorded.row.branch ?? null }], p.actor, now, b.key);
        return { task_id: task.id, worktree: this.worktrees.view(recorded.row), worktrees: this.worktrees.list(task.id) };
      }
      keys(b, ['key', 'worktree_id']);
      if (b.worktree_id != null && (typeof b.worktree_id !== 'string' || !SESSION_UUID.test(b.worktree_id))) throw new IntakeError('invalid_field', 400, 'worktree_id');
      worktreeIds = [b.worktree_id ?? null];
      if (m[2] === 'refresh') {
        let out: Obj;
        try { out = this.worktrees.enqueue(task.id, 'status', b.worktree_id ?? null, p.actor); } catch (e) { worktreeFail(e); }
        return { task_id: task.id, commands: out.commands, worktrees: this.worktrees.list(task.id) };
      }
    }
    const commands: Obj[] = []; const refused: Obj[] = []; const events: Obj[] = [];
    for (const id of worktreeIds) {
      let out: Obj;
      try { out = this.worktrees.enqueue(task!.id, 'remove', id, p.actor); } catch (e) { worktreeFail(e); }
      commands.push(...out.commands); refused.push(...out.refused); events.push(...out.events);
    }
    if (events.length) this.pushHistory(task!, events, p.actor, now, b.key);
    return { task_id: task!.id, commands, refused, worktrees: this.worktrees.list(task!.id) };
  }
  private pushHistory(task: Obj, events: Obj[], actor: string, now: number, key?: string) {
    if (task.history.length + events.length > INTAKE_LIMITS.events) fail('history_capacity', 507);
    for (const e of events) task.history.push({ id: crypto.randomUUID(), ...e, actor, captured_at: now, authority: 'hub_worktree_registry',
      ...(key ? { intake_key: key } : {}) });
    task.version++; task.updated_at = now;
    if (encoded(task) > INTAKE_LIMITS.taskBytes) fail('task_capacity', 507);
    this.put('tasks', task);
  }
  /** Host-agent path (plan/observe/results): append registry events to the
   * owning task in the caller's transaction. Best-effort on capacity: the
   * registry row is the durable record; a full history only drops the event. */
  recordWorktreeEvents(items: { taskId: string; event: Obj }[], actor: string) {
    const now = Date.now();
    for (const { taskId, event } of items) {
      const task = this.get('tasks', taskId); if (!task) continue;
      try { this.pushHistory(task, [event], actor, now); } catch (e) { if (!(e instanceof IntakeError)) throw e; continue; }
      this.sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
    }
  }
  private write(path: string, b: Obj, p: Obj): Obj {
    const now = Date.now();
    if (path === '/tasks') {
      keys(b, ['key', 'project', 'title', 'text', 'source', 'transcription', 'acceptance_criteria', 'verification']);
      const project = identifier(b.project);
      const task: Obj = { id: crypto.randomUUID(), project, title: str(b.title, 256, 'title'), status: 'open', version: 1,
        created_at: now, updated_at: now, current_state: null, next_step: null, owner: null, uncertainty: null,
        acceptance_criteria: acNullable(b.acceptance_criteria), history: [],
        ...(b.verification !== undefined ? { verification: verificationMode(b.verification) } : {}) };
      if (!this.allowed(p, task, true)) fail('forbidden', 403);
      // t1038u: no hard task-row refuse on create; totalBytes/taskBytes/events still apply.
      // Soft warn when open count is high (surfaced on the response below).
      task.history.push(this.event({ kind: 'intake', text: b.text, source: b.source, transcription: b.transcription }, task, p, now));
      task.history[0].intake_key = b.key;
      // F2: the intake is ALWAYS saved; the classifier hint is stored as scope_flag.
      task.scope = scopeHint(task);
      this.put('tasks', task);
      const created = { task: this.view(task, p) } as Obj;
      const warn = this.intakeOpenWarning(p);
      if (warn) created.intake_open_warning = warn;
      return created;
    }
    if (path === '/grants') {
      this.operator(p); keys(b, ['key', 'credential_id', 'scope', 'target', 'access', 'expires_at', 'source']);
      const c = this.sql.exec('SELECT * FROM credentials WHERE id=?', credentialId(b.credential_id)).toArray()[0] as any;
      const binding = this.binding(c); if (!binding) fail('invalid_grantee');
      if (!['all', 'project', 'task'].includes(b.scope) || !['read', 'write'].includes(b.access)) fail('invalid_grant');
      const target = b.scope === 'all' ? null : identifier(b.target);
      if (b.scope === 'all' && b.target != null) fail('invalid_grant');
      if (b.scope === 'task' && !this.get('tasks', target!)) fail('invalid_grant');
      const expires = integer(b.expires_at, now + 1, Math.min(now + 30 * 86400000, Date.parse(c.expires_at)));
      this.bounded('grants', INTAKE_LIMITS.grants);
      const g = { id: crypto.randomUUID(), credential_id: c.id, binding, scope: b.scope, target, access: b.access, expires_at: expires,
        source: source(b.source), actor: p.actor, created_at: now, revoked_at: null };
      this.put('grants', g); return { grant: { id: g.id, scope: g.scope, target, access: g.access, expires_at: expires, actor: binding.actor } };
    }
    const revoke = /^\/grants\/([A-Za-z0-9._:-]+)\/revoke$/.exec(path);
    if (revoke) {
      this.operator(p); keys(b, ['key', 'source']); const g = this.get('grants', revoke[1]); if (!g) fail('not_found', 404);
      if (!g.revoked_at) { g.revoked_at = now; g.revocation = { source: source(b.source), actor: p.actor }; this.put('grants', g); }
      return { revoked: g.id };
    }
    if (path === '/rules') {
      this.operator(p); keys(b, ['key', 'id', 'expected_version', 'project', 'text', 'source', 'decision']);
      const id = identifier(b.id); const rule = this.get('rules', id) || { id, version: 0, project: nullable(b.project, 128), history: [] };
      if (rule.project !== nullable(b.project, 128)) fail('immutable_rule_scope');
      this.expected(b, rule.version); if (!rule.version) this.bounded('rules', INTAKE_LIMITS.rules);
      if (rule.history.length >= INTAKE_LIMITS.events) fail('history_capacity', 507);
      rule.history.push({ version: rule.version + 1, text: str(b.text), source: source(b.source), decision: str(b.decision, 2048), actor: p.actor, captured_at: now, authority: 'operator_rule' });
      rule.version++; this.put('rules', rule); return { operator_rule: rule };
    }
    if (path === '/worktrees/remove' || /^\/tasks\/[A-Za-z0-9._:-]+\/worktrees(\/(refresh|remove|register))?$/.test(path)) return this.writeWorktrees(path, b, p, now);
    const match = /^\/tasks\/([A-Za-z0-9._:-]+)(\/(?:events|continue))?$/.exec(path);
    if (!match) fail('not_found', 404);
    const task = this.get('tasks', match[1]); if (!task || !this.allowed(p, task, true)) fail('not_found', 404);
    this.expected(b, task.version);
    if (task.history.length >= INTAKE_LIMITS.events) fail('history_capacity', 507);
    if (match[2] === '/continue') {
      keys(b, ['key', 'expected_version']);
      if (this.continuation(task) !== 'ready') fail('continuation_not_ready', 409);
      this.transition(task, 'in_progress', p.actor, now);
      task.history.push({ id: crypto.randomUUID(), kind: 'continuation', actor: p.actor, captured_at: now, evidence: 'recorded_active_not_started', authority: 'untrusted_task_data' });
    } else if (match[2]) {
      keys(b, ['key', 'expected_version', 'event', 'note', 'text']);
      // Keep the original event envelope and the documented MCP note shorthand
      // on the same validation/authorization path. Never silently pick one.
      // Also accept note as a UTF-8 string and top-level text as note text so
      // common agent payloads do not flake as opaque invalid_object.
      if ('note' in b && 'event' in b) throw new IntakeError('invalid_field', 400, 'note');
      if ('text' in b && ('note' in b || 'event' in b)) throw new IntakeError('invalid_field', 400, 'text');
      if (!('note' in b) && !('event' in b) && !('text' in b)) fail('event_or_note_required');
      let rawNoteOrEvent: any = 'note' in b ? b.note : ('event' in b ? b.event : { text: b.text });
      let isNote = 'note' in b || 'text' in b;
      if (rawNoteOrEvent && typeof rawNoteOrEvent === 'object' && !Array.isArray(rawNoteOrEvent)) {
        if ('note' in rawNoteOrEvent && !('kind' in rawNoteOrEvent)) {
          rawNoteOrEvent = rawNoteOrEvent.note;
          isNote = true;
        } else if ('event' in rawNoteOrEvent && !('kind' in rawNoteOrEvent)) {
          rawNoteOrEvent = rawNoteOrEvent.event;
        }
      }
      if (typeof rawNoteOrEvent === 'string') {
        if (!isNote) fail('invalid_object');
        rawNoteOrEvent = { text: rawNoteOrEvent };
      }
      let event = object(rawNoteOrEvent);
      if (isNote) {
        keys(event, ['text', 'source']);
        event = { ...event, kind: 'note' };
      }
      task.history.push(this.event(event, task, p, now));
      // F2: new intake text changes the decision basis -> re-hint. Other
      // event kinds (notes, scope decisions themselves) never change the flag.
      if (task.scope && typeof task.scope === 'object' && task.history[task.history.length - 1]?.kind === 'intake') {
        task.scope = scopeHint(task, (task.scope.basis_version ?? 1) + 1);
      }
    } else {
      keys(b, ['key', 'expected_version', 'project', 'status', 'title', 'current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria', 'verification', 'reason', 'superseded_by', 'force', 'outcome']);
      // AC5 triage reviewers only fill missing AC; enforced at effect time so an
      // exact retry of a successful fill replays its receipt instead.
      if (p.worker && 'acceptance_criteria' in b && hasAcceptanceCriteria(task)) fail('forbidden', 403);
      const unverified = b.outcome === 'closed_unverified';
      if ('outcome' in b) {
        if (!unverified || b.status !== 'done') throw new IntakeError('invalid_outcome', 400, 'outcome');
        this.operator(p);
        if (typeof b.reason !== 'string' || !b.reason.trim()) fail('reason_required');
        // Idempotent: an intake already closed this way is returned unchanged.
        if (task.status === 'done' && task.outcome === 'closed_unverified') return { task: this.view(task, p) };
        if (['title', 'project', 'acceptance_criteria', 'verification'].some(k => k in b)) throw new IntakeError('invalid_outcome', 400, 'outcome');
      }
      if ('force' in b && typeof b.force !== 'boolean') throw new IntakeError('invalid_field', 400, 'force');
      if (b.force === true && (b.status !== 'done' || typeof b.reason !== 'string' || !b.reason.trim())) fail('force_reason_required');
      if (!['project', 'status', 'title', 'current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria', 'verification'].some(k => k in b)) fail('empty_update');
      const changes: Obj = {};
      const previousStatus = task.status;
      if (b.status === 'done' && ['title', 'project', 'acceptance_criteria'].some(k => k in b && b[k] !== task[k])) fail('done_evidence_required', 409);
      if ('status' in b) this.transition(task, b.status, p.actor, now, b.reason === undefined ? undefined : str(b.reason, 8192), b.superseded_by, b.force === true, unverified);
      for (const field of ['project', 'status', 'title', 'current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria']) if (field in b) {
        if (field === 'status' && !STATUSES.includes(b[field])) fail('invalid_status');
        const previous = field === 'status' ? previousStatus : task[field];
        task[field] = field === 'project' ? identifier(b[field]) : field === 'status' ? b[field]
          : field === 'acceptance_criteria' ? acNullable(b[field])
          : nullable(b[field], field === 'title' ? 256 : 8192);
        if (previous !== task[field]) changes[field] = task[field];
      }
      if (unverified && task.outcome === 'closed_unverified') { changes.outcome = 'closed_unverified'; changes.reason = task.lifecycle_reason; }
      // Post-DONE verification policy (d8d509d2 t1936u): required|skip|auto, default auto.
      if ('verification' in b) {
        const mode = verificationMode(b.verification);
        if ((task.verification ?? 'auto') !== mode) changes.verification = mode;
        task.verification = mode;
      }
      if (!this.allowed(p, task, true)) fail('forbidden', 403);
      task.history.push({ id: crypto.randomUUID(), kind: 'update', changes, actor: p.actor, captured_at: now, authority: 'untrusted_task_data' });
      // F2: AC (and title, which feeds the hint) can move the flag. The
      // stored operator decision stays in history but its basis hash no
      // longer matches, so the gate fails closed until a fresh decision.
      if (task.scope && typeof task.scope === 'object' && ('project' in changes || 'acceptance_criteria' in changes || 'title' in changes)) {
        task.scope = scopeHint(task, (task.scope.basis_version ?? 1) + 1);
      }
    }
    task.history[task.history.length - 1].intake_key = b.key;
    task.version++; task.updated_at = now;
    if (encoded(task) > INTAKE_LIMITS.taskBytes) fail('task_capacity', 507);
    this.put('tasks', task); return { task: this.view(task, p) };
  }
  /** M12b assignment: create the task's Hub session through the canonical
   * session-control service with the caller's own auth (existing Fleet
   * placement, existing model tiers, same owning actor), then record its real
   * session id as a task link plus next-step handoff context.
   *
   * No placement is duplicated and no session identity is invented here: the
   * session row comes back from the injected startSession hook (the Hub wires
   * the real SessionControlService.handle; without it assignment honestly
   * reports assignment_unavailable). The caller needs intake write access on
   * the task AND session-control writer authority (operator, or an agent
   * credential with fleet:launch); the latter is enforced by the canonical
   * service itself. The assigned session stays controllable through
   * `hub sessions` by the same actor, and handoff projects its live
   * state/binding from the canonical rows.
   *
   * Idempotency and custody follow the existing receipt rules: the Hub session
   * key is derived deterministically from (caller identity, task, intake key),
   * so a lost-response retry replays the same session and the same link
   * response, while a changed payload under the same key conflicts. A task
   * with a live (queued/held/offered/starting/running/uncertain) assigned
   * session refuses a second assignment until that session leaves the active
   * set. The service-owned link verifies the Hub launch intent; native execution
   * remains separately observed through the session lifecycle. */
  private async assign(taskId: string, raw: any, auth: Auth, json: (data: Obj, status?: number) => Response): Promise<Response> {
    identifier(taskId);
    const b = object(raw);
    keys(b, ['key', 'expected_version', 'session', 'provenance', 'next_step', 'current_state', 'owner']);
    identifier(b.key);
    const session = object(b.session);
    keys(session, ['engine', 'engine_override_reason', 'tier', 'machine', 'workspace', 'cwd', 'brief', 'access', 'mode', 'parent_id', 'with_goal']);
    if (typeof session.workspace !== 'string' || !session.workspace) fail('invalid_field');
    const provenance = source(b.provenance);
    const fingerprint = canonical({ path: '/tasks/' + taskId + '/assign', payload: b });
    const pre: Obj = this.tx(() => {
      const p = this.principal(auth);
      const task = this.get('tasks', taskId);
      if (!task || !this.allowed(p, task, true)) fail('not_found', 404);
      // Lost-response replay returns the original response even if the task
      // has advanced since; only fresh attempts face CAS and the live guard.
      const receiptId = p.identity + ':' + b.key;
      const previous = this.get('receipts', receiptId);
      if (previous) {
        if (previous.fingerprint !== fingerprint) fail('idempotency_conflict', 409);
        if (previous.response) return { replay: previous.response };
      }
      const sessionKey = 'ia-' + sha256Sync(p.identity + ':' + taskId + ':' + b.key).slice(0, 24);
      let prior: any = null;
      try { prior = this.sql.exec('SELECT * FROM session_control_sessions WHERE actor=? AND request_key=?', p.actor, sessionKey).toArray()[0]; } catch { /* session service absent: assignment_unavailable below */ }
      const ownLink = prior && task.history.find((e: Obj) => e.kind === 'link' && e.hub_session_id === prior.id && e.authority === 'hub_launch_verified');
      const version = previous?.started_version ?? task.version;
      const ownAdvance = !!ownLink && task.version === version + 1;
      this.expected(b, task.version - (ownAdvance ? 1 : 0));
      if (!ownLink && task.history.length >= INTAKE_LIMITS.events) fail('history_capacity', 507);
      if (!hasAcceptanceCriteria(task)) fail('acceptance_criteria_required', 400);
      // F2 scope gate (single check shared with linked session starts):
      // the intake is always saved, but assign refuses until an operator
      // scope decision exists. Research hints never block (classifier only).
      const preScope = getScopeStatus(task).block_code;
      if (preScope) fail(preScope, 409);
      const active = this.activeAssignment(task);
      if (active && active !== prior?.id) fail('active_assignment_exists', 409);
      // Persist the original assignment fingerprint before the async start.
      // A crash after the canonical start/link can then resume the same
      // assignment, without permitting a different payload under its key.
      if (!previous) {
        this.put('receipts', { id: receiptId, fingerprint, started_version: version, captured_at: Date.now() });
        this.capacity();
      }
      return { p, version, sessionKey };
    });
    if (pre.replay) return json(pre.replay);
    if (!this.hooks.startSession) fail('assignment_unavailable', 409);
    const sessionKey = pre.sessionKey;
    let started: { status: number; body: any };
    try {
      // The linked intake task id travels with the start so the canonical
      // session-control gate re-checks the same AC server-side before placement.
      started = await this.hooks.startSession({ ...session, key: sessionKey, intake_task_id: taskId }, auth);
    } catch { fail('assignment_failed', 502); }
    const created = started! && typeof started!.body === 'object' ? started!.body : null;
    if (!created || typeof created.id !== 'string' || !SESSION_UUID.test(created.id)) {
      const status = started && (started.status >= 400 && started.status < 500) ? started.status : 502;
      const error = created && typeof created.error === 'string' && created.error ? created.error : 'assignment_failed';
      return json({ error }, status);
    }
    return this.tx(() => {
      const now = Date.now();
      const fresh = this.principal(auth);
      const task = this.get('tasks', taskId);
      if (!task || !this.allowed(fresh, task, true)) fail('not_found', 404);
      const id = fresh.identity + ':' + b.key;
      const previous = this.get('receipts', id);
      if (previous) {
        if (previous.fingerprint !== fingerprint) fail('idempotency_conflict', 409);
        if (previous.response) return json(previous.response);
      }
      const startLink = task.history.find((e: Obj) => e.kind === 'link' && e.hub_session_id === created.id && e.authority === 'hub_launch_verified');
      // The canonical start committed our link. Only that one known version
      // advance belongs to this assignment; any concurrent edit still conflicts.
      this.expected(b, task.version - (startLink && task.version === pre.version + 1 ? 1 : 0));
      const extraEvents = (startLink ? 0 : 1) + (['current_state', 'next_step', 'owner'].some(field => field in b) ? 1 : 0);
      if (task.history.length + extraEvents > INTAKE_LIMITS.events) fail('history_capacity', 507);
      // Re-check after the start round-trip: AC cleared mid-flight still fails closed.
      if (!hasAcceptanceCriteria(task)) fail('acceptance_criteria_required', 400);
      // F2: scope decision revoked or basis changed mid-flight still fails closed.
      const postScope = getScopeStatus(task).block_code;
      if (postScope) fail(postScope, 409);
      const active = this.activeAssignment(task);
      if (active && active !== created.id) fail('active_assignment_exists', 409);
      // The created (or deterministically replayed) session must still be
      // ours: operator, or the session's owning actor. A foreign or missing
      // row fails closed without recording a link.
      const row = this.sessionRow(created.id);
      if (!row || (!fresh.admin && row.actor !== fresh.actor)) fail('not_found', 404);
      const link = this.event({ kind: 'link', native_id: null,
        engine: typeof row.engine === 'string' ? row.engine : null,
        tier: typeof session.tier === 'string' ? session.tier : null,
        host: typeof row.machine_id === 'string' ? row.machine_id : null, hub: null,
        provenance, routing_job_id: null, hub_session_id: created.id }, task, fresh, now);
      link.intake_key = b.key;
      if (created.state === 'held' && created.hold !== undefined) link.assignment_hold = created.hold;
      if (!startLink) task.history.push(link);
      const changes: Obj = {};
      for (const field of ['current_state', 'next_step', 'owner']) if (field in b) {
        task[field] = nullable(b[field], 8192);
        changes[field] = task[field];
      }
      if (Object.keys(changes).length) {
        task.history.push({ id: crypto.randomUUID(), kind: 'update', changes, actor: fresh.actor,
          captured_at: now, authority: 'untrusted_task_data', intake_key: b.key });
      }
      if (!startLink || Object.keys(changes).length) { task.version++; task.updated_at = now; }
      if (encoded(task) > INTAKE_LIMITS.taskBytes) fail('task_capacity', 507);
      this.put('tasks', task);
      const assigned = this.assignedSessions(task, fresh).find(s => s.hub_session_id === created.id) ??
        { hub_session_id: created.id, linkage: 'hub_session' };
      const response = { task: this.view(task, fresh), assigned_session: assigned,
        ...(created.state === 'held' ? { hold: created.hold ?? 'placement_pending', requested: created.requested } : {}),
        snapshot_version: this.revision() + 1, data_notice: DATA_NOTICE };
      this.sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
      this.put('receipts', { id, fingerprint, response }); this.capacity();
      return json(response);
    });
  }
  /** Readonly status-projection helpers for M14a. No write, grant or permission semantics change. */
  statusPrincipal(auth: Auth): Obj { return this.principal(auth); }  statusAuthorizedTasks(p: Obj): Obj[] { return this.rows('tasks').filter(t => this.allowed(p, t)); }
  statusTaskById(p: Obj, id: string): Obj | undefined {
    const task = this.get('tasks', id);
    return task && this.allowed(p, task) ? task : undefined;
  }
  statusTaskSummaries(p: Obj): Obj[] {
    return this.sql.exec('SELECT data FROM intake_task_projection ORDER BY id').toArray()
      .map((r: any) => JSON.parse(r.data)).filter(t => this.allowed(p, t));
  }
  statusUnresolved(task: Obj): Obj[] { return this.unresolved(task); }
  statusContinuation(task: Obj): string { return this.continuation(task); }
  statusRevision(): number { return this.revision(); }
  statusOpenNonTerminalCount(p?: Obj, summaries?: Obj[]): number { return this.openNonTerminalCount(p, summaries); }
  statusIntakeOpenWarning(p?: Obj, summaries?: Obj[]): Obj | null { return this.intakeOpenWarning(p, summaries); }
  /** Service-owned task creation for internal system services (e.g. disk monitoring).
   * Enforces task capacity, total storage capacity, single-record byte limit, and version/revision CAS. */
  serviceCreateTask(input: {
    project: string;
    title: string;
    text: string;
    currentState?: string | null;
    nextStep?: string | null;
    sourceRef?: string;
    actor?: string;
    now?: number;
  }): { ok: true; taskId: string } | { ok: false; error: string } {
    return this.tx(() => {
      const now = input.now ?? Date.now();
      try {
        this.prune(now);
        // t1038u: task row count is soft; only aggregate byte budget refuses create.
        this.capacity();
      } catch (e: any) {
        return { ok: false, error: e?.code || 'capacity_exceeded' };
      }
      const taskId = crypto.randomUUID();
      const flag = classifyScope(input.project, str(input.title, 256), str(input.text, 8192), null);
      const task: Obj = {
        id: taskId,
        project: input.project,
        title: str(input.title, 256),
        status: 'open',
        version: 1,
        created_at: now,
        updated_at: now,
        current_state: input.currentState ?? null,
        next_step: input.nextStep ?? null,
        owner: null,
        uncertainty: null,
        acceptance_criteria: null,
        scope: { policy_version: SCOPE_POLICY_VERSION, flag: flag.flag, matched_terms: flag.matched_terms },
        history: [
          {
            id: crypto.randomUUID(),
            kind: 'intake',
            actor: input.actor || 'hub:service:system',
            captured_at: now,
            text: str(input.text, 8192),
            source: { type: 'unknown', reference: nullable(input.sourceRef, 2048), timestamp: new Date(now).toISOString() },
            authority: 'operator_record',
          },
        ],
      };
      if (encoded(task) > INTAKE_LIMITS.taskBytes) {
        return { ok: false, error: 'task_capacity' };
      }
      try { this.capacity(encoded(task) + new TextEncoder().encode(task.id).byteLength); }
      catch (e: any) { return { ok: false, error: e.code || 'storage_capacity' }; }
      this.put('tasks', task);
      this.sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
      return { ok: true, taskId };
    });
  }

  /** Service-owned task mutation (status change, episode severity change, resolution) for internal services.
   * Atomically increments task.version and intake_meta revision.
   * Preserves original intake event (history[0]) and user history.
   * Enforces event and byte budgets. */
  serviceRecordEvent(taskId: string, input: {
    status?: string;
    currentState?: string | null;
    nextStep?: string | null;
    event?: {
      kind: string;
      text: string;
      actor?: string;
      sourceRef?: string;
      authority?: string;
    };
    now?: number;
  }): { ok: true; task: Obj } | { ok: false; error: string } {
    return this.tx(() => {
      const task = this.get('tasks', taskId);
      if (!task) return { ok: false, error: 'not_found' };
      const now = input.now ?? Date.now();
      const priorBytes = !task.archived_at && OPEN.includes(task.status) ? encoded(task) : 0;

      if (input.status) {
        if (!STATUSES.includes(input.status)) return { ok: false, error: 'invalid_status' };
        try { this.transition(task, input.status, input.event?.actor || 'hub:service:system', now, input.event?.text); }
        catch (e: any) { return { ok: false, error: e.code }; }
      }
      if (input.currentState !== undefined) {
        task.current_state = input.currentState !== null ? str(input.currentState, 8192) : null;
      }
      if (input.nextStep !== undefined) {
        task.next_step = input.nextStep !== null ? str(input.nextStep, 8192) : null;
      }

      if (input.event) {
        const newEvent = {
          id: crypto.randomUUID(),
          kind: input.event.kind,
          actor: input.event.actor || 'hub:service:system',
          captured_at: now,
          text: str(input.event.text, 8192),
          source: { type: 'unknown', reference: nullable(input.event.sourceRef, 2048), timestamp: new Date(now).toISOString() },
          authority: input.event.authority || 'operator_record',
        };

        task.history = task.history || [];
        if (task.history.length >= INTAKE_LIMITS.events) {
          const intake0 = task.history[0];
          const remaining = task.history.slice(1);
          const dropIdx = remaining.findIndex((e: Obj) => typeof e.actor === 'string' && e.actor.startsWith('hub:service:'));
          if (dropIdx >= 0) {
            remaining.splice(dropIdx, 1);
            task.history = [intake0, ...remaining];
          } else {
            return { ok: false, error: 'history_capacity' };
          }
        }
        task.history.push(newEvent);
      }

      task.version++;
      task.updated_at = now;

      if (encoded(task) > INTAKE_LIMITS.taskBytes) {
        return { ok: false, error: 'task_capacity' };
      }

      try { this.capacity((!task.archived_at && OPEN.includes(task.status) ? encoded(task) : 0) - priorBytes); }
      catch (e: any) { return { ok: false, error: e.code || 'storage_capacity' }; }
      this.put('tasks', task);
      this.sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
      return { ok: true, task };
    });
  }
  /** Host-agent worktree channel (machine already authenticated by the Hub). */
  async handleWorktreeHost(request: Request, machine: string, actor: string): Promise<Response> {
    const json = (data: Obj, status = 200) => Response.json(data, { status, headers: { 'Cache-Control': 'private, no-store' } });
    const path = new URL(request.url).pathname.slice('/api/worktree-host'.length);
    let payload: Obj | null = null;
    try {
      if (request.method === 'POST') payload = await body(request);
      else if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    } catch (e) { return json({ error: e instanceof IntakeError ? e.code : 'invalid_json' }, 400); }
    try {
      return this.tx(() => {
        if (path === '/commands' && request.method === 'GET') return json({ commands: this.worktrees.hostCommands(machine) });
        if (request.method !== 'POST' || !payload) return json({ error: 'not_found' }, 404);
        if (path === '/plan') {
          const out = this.worktrees.plan(machine, payload);
          this.recordWorktreeEvents(out.events, actor);
          return json({ plan: out.plan });
        }
        if (path === '/observe') {
          const out = this.worktrees.observe(machine, payload);
          this.recordWorktreeEvents(out.events, actor);
          return json(out.result);
        }
        const m = /^\/commands\/([0-9a-f-]{36})\/result$/.exec(path);
        if (m) {
          const out = this.worktrees.hostResult(machine, m[1], payload);
          this.recordWorktreeEvents(out.events, actor);
          return json({ command: out.command });
        }
        return json({ error: 'not_found' }, 404);
      });
    } catch (e) {
      if (e instanceof WorktreeError || e instanceof IntakeError) return json({ error: e.code, ...(e.detail ?? {}) }, e.status);
      return json({ error: 'internal_error' }, 500);
    }
  }
  async handle(request: Request, authenticate: () => Promise<Auth>): Promise<Response> {
    const json = (data: Obj, status = 200) => Response.json(data, { status, headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' } });
    try {
      const initial = await authenticate();
      const crossOrigin = request.method === 'POST' && request.headers.has('Origin') && request.headers.get('Origin') !== new URL(request.url).origin;
      if (initial.error || crossOrigin) {
        try { await request.body?.cancel(); } catch { /* Body may already be cancelled. */ }
        return json({ error: 'unauthorized_or_forbidden' }, crossOrigin ? 403 : initial.error!.status);
      }
      if (!['GET', 'POST'].includes(request.method)) return json({ error: 'method_not_allowed' }, 405);
      const payload = request.method === 'POST' ? await body(request) : null;
      const auth = await authenticate(); if (auth.error) return json({ error: 'unauthorized_or_forbidden' }, auth.error.status);
      const assignMatch = request.method === 'POST'
        ? /^\/tasks\/([A-Za-z0-9._:-]+)\/assign$/.exec(new URL(request.url).pathname.slice('/api/intake'.length))
        : null;
      if (assignMatch) return await this.assign(assignMatch[1], payload, auth, json);
      return this.tx(() => {
        const p = this.principal(auth); const url = new URL(request.url);
        if (!payload) return json(this.read(url, p));
        identifier(payload.key);
        const path = url.pathname.slice('/api/intake'.length);
        // AC5 triage reviewers may also set AC/project on their requested intakes (never status).
        const triageUpdate = p.worker && p.triage?.has(/^\/tasks\/([^/]+)$/.exec(path)?.[1]) &&
          Object.keys(payload).every(k => ['key', 'expected_version', 'acceptance_criteria', 'project'].includes(k)) &&
          ('acceptance_criteria' in payload || 'project' in payload);
        if (p.worker && !triageUpdate && (!/^\/tasks\/[^/]+\/events$/.test(path) || !['note','report','blocker'].includes(payload.event?.kind))) fail('forbidden', 403);
        if (p.worker && !triageUpdate) payload.event = redactClaimData(payload.event);
        // Do not replay a private response after the caller loses its task/operator grant.
        if (path.startsWith('/rules') || path.startsWith('/grants')) this.operator(p);
        const taskMatch = /^\/tasks\/([^/]+)/.exec(path);
        if (taskMatch) { const t = this.get('tasks', taskMatch[1]); if (!t || !this.allowed(p, t, true)) fail('not_found', 404); }
        if (path === '/tasks' && !this.allowed(p, { project: payload.project, id: '' }, true)) fail('forbidden', 403);
        const id = p.identity + ':' + payload.key;
        const previous = this.get('receipts', id); const fingerprint = canonical({ path, payload });
        if (previous) {
          if (previous.fingerprint !== fingerprint) fail('idempotency_conflict', 409);
          return json(previous.response);
        }
        // Prune before any capacity check: a full receipts table must not
        // wedge every write forever, terminal payloads are archived and retained.
        this.prune(Date.now());
        const response = { ...this.write(path, payload, p), snapshot_version: this.revision() + 1, data_notice: DATA_NOTICE };
        this.sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
        this.put('receipts', { id, fingerprint, response, captured_at: Date.now() }); this.capacity();
        return json(response);
      });
    } catch (e) {
      if (!(e instanceof IntakeError)) return json({ error: 'internal_error' }, 500);
      return json({ error: e.code,
        ...(e.field !== undefined ? { field: e.field } : {}),
        ...(e.field !== undefined && e.maxBytes !== undefined ? { max_bytes: e.maxBytes } : {}),
        ...(e.field === 'limit' ? { min: 1, max: INTAKE_LIMITS.page } : {}),
        ...(e.code === 'invalid_event_kind' ? { valid_kinds: INTAKE_EVENT_KINDS } : {}),
        ...(e.detail ?? {}),
      }, e.status);
    }
  }
}
