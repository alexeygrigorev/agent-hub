/** Forgotten-work projection and durable restart queue. Canonical session/Fleet admission
 * remains the only launcher; notes never count as liveness or restart authority. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { currentHistory, intakeCompletionHoldReason, intakeHasDoneEvidence, type IntakeService } from './intake';
import { freshCatalogs } from './catalog-freshness';
import { decodeRepoWorkspace, encodeRepoWorkspace, resolveLabelUnderRoots } from './fleet';
import { parseNativeActor } from './work_claim';
import { nativeTerminalError, sessionTerminalError, nativeProgress, orphanFailure } from './session_stall';
import { externalSessionInCensus } from './intake-external';
import { latestTriageVerdict } from './worker-access';
import { launchFailureStreak, recoveryEngineOrder, recoveryProtected, recoveryWakePending } from './intake-recovery';
import { ENGINE_HEALTH_RETRY_MS, RETRIABLE_ENGINE_HOLDS } from './fleet';
type Obj = Record<string, any>;
type QueueSnapshot = { native:Obj[]; catalogs:Map<string,number>; sessions:Obj[]; launches:Map<string,Obj>;
  holds:Map<string,Obj>; claims:Obj[]; queue:Map<string,Obj>; wakes:Obj[]; wakedNatives:Set<string>; overflow:boolean };
const ACTIVE = new Set(['queued', 'held', 'offered', 'starting', 'running', 'uncertain']);
const FAILED = new Set(['failed', 'refused', 'cancelled', 'engine_failed', 'engine_oom_killed']);
const OPEN = new Set(['open', 'in_progress', 'blocked', 'pending_approval']);
const DISPATCHABLE = new Set(['open', 'in_progress']);
export const WATCHDOG_INTERVAL = 60000;
export const QUEUE_RETRY_LIMIT = 3;
export const QUEUE_STALL_MS = 30 * 60000;
export const QUEUE_RETRY_BASE_MS = 15 * 60000;
export const QUEUE_RETRY_MAX_MS = 60 * 60000;
const retryDelay = (attempt: number) => Math.min(QUEUE_RETRY_MAX_MS, QUEUE_RETRY_BASE_MS * 2 ** Math.max(0, attempt - 1));
export const QUEUE_BATCH_SIZE = 64;
/** Bounded hold lifetime (intake 0841a966 AC2): a queued restart that cannot
 * progress for this long escalates with a visible reason instead of waiting
 * forever. Two hours matches the forgotten-grace window. */
export const QUEUE_HOLD_TTL_MS = 2 * 60 * 60000;
/** Rows inspected per tick by the bounded queue sweep. */
export const QUEUE_SWEEP_LIMIT = 64;
const CONTEXT_LIMIT = 512;
const requestIntake = "json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.intake_task_id')";
const OWNED = "actor='hub:service:intake-watchdog' AND json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.with_goal')=1";
export type QueuePolicy = { enabled: boolean; valid: boolean; max_inflight: number; max_per_project: number; projects: string[] | null };
export function queuePolicyFrom(env: Record<string, unknown> = {}, capacity = 0): QueuePolicy {
  const enabled = env.HUB_INTAKE_QUEUE_ENABLED ?? 'false';
  // Automatic limits may be zero: absent capacity evidence must not create slots.
  const automatic = env.HUB_INTAKE_QUEUE_MAX_INFLIGHT === 'auto';
  const measured = Number.isInteger(capacity) && capacity >= 0 ? Math.min(32, capacity) : 0;
  const global = automatic ? measured : Number(env.HUB_INTAKE_QUEUE_MAX_INFLIGHT ?? 2);
  const autoProject = env.HUB_INTAKE_QUEUE_MAX_PER_PROJECT === 'auto' ||
    (automatic && env.HUB_INTAKE_QUEUE_MAX_PER_PROJECT === undefined);
  const project = autoProject ? global : Number(env.HUB_INTAKE_QUEUE_MAX_PER_PROJECT ?? 1);
  const scope = env.HUB_INTAKE_QUEUE_PROJECTS;
  const projects = typeof scope === 'string' && scope.length <= 64 * 129 ? scope.split(',').map(p => p.trim()) : [];
  const all = projects.length === 1 && projects[0] === '*';
  const scoped = projects.length > 0 && projects.length <= 64 && new Set(projects).size === projects.length &&
    projects.every(p => /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(p));
  const valid = ['true', 'false'].includes(String(enabled)) && Number.isInteger(global) && global >= (automatic ? 0 : 1) && global <= 32 &&
    Number.isInteger(project) && project >= (autoProject ? 0 : 1) && project <= global &&
    (scope === undefined || all || scoped) && (String(enabled) !== 'true' || all || scoped);
  return { enabled: valid && String(enabled) === 'true', valid, max_inflight: valid ? global : 2,
    max_per_project: valid ? project : 1, projects: valid && all ? null : valid && scoped ? projects : [] };
}
/** Intake priority (intake 68145244): high runs before normal before low; a
 * missing or unknown value reads as normal so legacy rows keep their place. */
export const INTAKE_PRIORITY_RANK: Record<string, number> = { high: 0, normal: 1, low: 2 };
const promotedAt = (t: Obj): number => typeof t?.promoted_at === 'number' && t.promoted_at > 0 ? t.promoted_at : 0;
/** Queue candidate order (intake 68145244 AC3): promoted intakes first — newest
 * promotion first, ahead of every priority — then priority high > normal > low,
 * and within one priority the newest intake first. id is the stable tiebreak. */
export function queueCandidateOrder(a: Obj, b: Obj): number {
  const pa = promotedAt(a), pb = promotedAt(b);
  if ((pa > 0) !== (pb > 0)) return pa > 0 ? -1 : 1;
  if (pa > 0 && pa !== pb) return pb - pa;
  const ra = INTAKE_PRIORITY_RANK[a?.priority] ?? 1, rb = INTAKE_PRIORITY_RANK[b?.priority] ?? 1;
  if (ra !== rb) return ra - rb;
  return ms(b.created_at) - ms(a.created_at) || a.id.localeCompare(b.id);
}
const bindingNative = "json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_id')";
const DAY = 86400000;
/** Admission holds an automatic restart may clear by moving to another
 * engine/account (intake b527e905 AC2). Holds only a person can clear are
 * handled by alertHumanHolds instead; watchdog gates stay in automationHold. */
const RECOVERABLE_HOLDS = new Set(['model_unsupported', 'tier_unavailable', 'launcher_memory_reserve',
  'quota_exhausted', 'reserve', 'limit_reached', 'cooldown_active', 'launch_failures_quarantined',
  'engine_auth_expired', 'provider_disabled', 'goal_unsupported', 'account_unavailable',
  'admission_unconfigured', 'engine_tool_permission_refused', 'quota_unavailable', 'quota_unknown', 'quota_stale', 'no_quota_evidence']);
/** Recovery grace (intake b527e905 AC1/AC5): an open intake without a live or
 * queued session becomes forgotten after 25 minutes, so the restart + alert
 * path lands well inside the 30-minute idle invariant. */
export const INTAKE_IDLE_GRACE = 1500000;
/** Canonical session states that end a restart episode: the submitted row is
 * spent history, not a live restart intent (AC2: canonical_offered/held drain). */
const SPENT_SESSION_STATES = new Set(['failed', 'refused', 'cancelled', 'engine_failed', 'engine_oom_killed', 'stopped']);
/** Hold reasons exempt from the TTL escalation: rollout/config states that are the
 * designed idle pool, and completion holds owned by the post-DONE verifier. */
const HOLD_TTL_EXEMPT = new Set(['watchdog_disabled', 'watchdog_configuration_invalid', 'watchdog_project_not_enabled',
  'reported_completion_requires_operator_review', 'done_evidence_requires_operator_review']);
/** Hold reasons only a person can clear (AC8, intake d8d509d2): each (intake, reason)
 * pair is alerted exactly once instead of waiting silently on an operator. */
export const HUMAN_HOLD_REASONS = new Set(['workspace_configuration_required',
  'reported_completion_requires_operator_review', 'done_evidence_requires_operator_review']);
const HUMAN_ALERT_LIST = 30;
const parse = (raw: any): Obj => { try { return JSON.parse(raw || '{}'); } catch { return {}; } };
const ms = (v: any): number => typeof v === 'number' ? v : (Date.parse(v) || 0);
/** A bound intent's hold/rejection is recoverable when admission can still pick
 * another engine or account. A bound session has already spent its admission. */
const recoveryHoldReason = (s: Obj): string | null => {
  if (s.binding_json) return null;
  // A host-proven no-start refusal creates a queued Fleet retry, not a held
  // control row. Never cancel a retry that Fleet has since claimed/promoted.
  if (s.state === 'queued' && (s.recovery_fleet_state !== 'queued' || s.recovery_native_id || s.recovery_launcher_task_id)) return null;
  const h = parse(s.hold_json);
  const fleetRejected = parse(s.recovery_rejected_json);
  const rejected = [...(Array.isArray(h.rejected) ? h.rejected : []), ...(Array.isArray(fleetRejected) ? fleetRejected : [])];
  return [h.reason, ...rejected.map((r: Obj) => r.reason)]
    .find(r => typeof r === 'string' && RECOVERABLE_HOLDS.has(r)) ?? null;
};
/** Exact native incarnation match: a census row for a newer/older incarnation
 * of the same native id is not evidence about this session's worker. */
const boundNative = (binding: Obj, n: Obj | undefined): boolean => !!n &&
  Number.isInteger(binding.native_created_at_ms) && binding.native_created_at_ms > 0 &&
  n.created_at_ms === binding.native_created_at_ms && n.tag === binding.tag && n.workspace === binding.workspace;
/** Local mirror of fleet's workspaceCapable prefix probe: an exact workspace id or a
 * boundary-safe descendant of an advertised root, following the advertised separator
 * style (intake 0841a966). A divergence here only misjudges a workspace as
 * unconfigured, which keeps the fail-safe hold. */
const rootCapable = (roots: any, request: any): boolean => {
  if (!Array.isArray(roots) || typeof request !== 'string' || !request) return false;
  for (const w of roots) {
    if (typeof w !== 'string' || !w) continue;
    if (w === request) return true;
    const sep = w.includes('\\') ? '\\' : '/';
    const prefix = w.endsWith('/') || w.endsWith('\\') ? w : w + sep;
    if (request.startsWith(prefix)) return true;
  }
  return false;
};
export type WatchdogHooks = {
  policy?: () => QueuePolicy;
  start?: (input: Obj) => Promise<{ status: number; body: Obj }>;
  /** Resolve an intake project alias through Hub's canonical repo registry. */
  repoForProject?: (project: string) => string | null;
  command?: (sessionId: string, action: 'retry' | 'stop', key: string) => Promise<{ status: number; body: Obj }>;
  /** Canonical stop for any open-intake session, including intents this
   * watchdog did not create (intake b527e905 AC1). Fenced server-side by
   * protection/admission/waiting rechecks; never fabricates an exit receipt. */
  stop?: (sessionId: string, key: string) => Promise<unknown>;
  /** Host census stall threshold in seconds (session_stall.stallThreshold). */
  stallThresholdSeconds?: number;
  /** Enabled only after the operator permits Claude recovery launches; unset
   * keeps the standing no-new-Claude policy (operator rule 2026-10-17). */
  claudeRecoveryEnabled?: boolean;
  observe?: (sessionId: string) => Promise<{ status: number; body: Obj }>;
  note?: (task: Obj, text: string, key: string) => Promise<boolean>;
  notify?: (kind: string, subject: string, detail: string | null) => Promise<boolean | void>;
  /** Ask ledger pass (intake 55c10deb) run on this tick, and its digest line. */
  asks?: (now: number) => Promise<Obj>;
  asksDigest?: () => string | null;
};
export class IntakeWatchdog {
  constructor(private sql: SqlStorage, private intake: IntakeService,
    private tx: <T>(fn: () => T) => T, private hooks: WatchdogHooks = {}, private now = Date.now) {}
  init() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_watchdog_meta(id INTEGER PRIMARY KEY CHECK(id=1),last_tick INTEGER,nonzero_since INTEGER,digest_day TEXT,last_source TEXT,last_result TEXT)');
    this.sql.exec('INSERT OR IGNORE INTO intake_watchdog_meta(id) VALUES(1)');
    this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_restart_queue(task_id TEXT PRIMARY KEY, state TEXT NOT NULL,
      reason TEXT NOT NULL, request_key TEXT, request_json TEXT, session_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      next_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)`);
    const cols = (this.sql.exec('PRAGMA table_info(intake_restart_queue)').toArray() as any[]).map(c => c.name);
    if (!cols.includes('held_since')) this.sql.exec('ALTER TABLE intake_restart_queue ADD COLUMN held_since INTEGER');
    // Existing in-flight holds enter the TTL clock at their last update.
    this.sql.exec('UPDATE intake_restart_queue SET held_since=updated_at WHERE held_since IS NULL');
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_human_alerts(task_id TEXT NOT NULL, reason TEXT NOT NULL, alerted_at INTEGER NOT NULL, PRIMARY KEY(task_id, reason))');
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_movement(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at INTEGER NOT NULL)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_queue_movement_task ON intake_queue_movement(task_id,seq)');
    this.sql.exec("CREATE INDEX IF NOT EXISTS intake_queue_due ON intake_restart_queue(next_at,task_id) WHERE state='queued'");
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_due_cursor(id INTEGER PRIMARY KEY CHECK(id=1),next_at INTEGER NOT NULL,task_id TEXT NOT NULL)');
    this.sql.exec("INSERT OR IGNORE INTO intake_queue_due_cursor VALUES(1,-1,'')");
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_cursor(id INTEGER PRIMARY KEY CHECK(id=1),after_id TEXT NOT NULL,forgotten_seen INTEGER NOT NULL DEFAULT 0)');
    this.sql.exec("INSERT OR IGNORE INTO intake_queue_cursor(id,after_id) VALUES(1,'')");
    const cursorCols = (this.sql.exec('PRAGMA table_info(intake_queue_cursor)').toArray() as any[]).map(c => c.name);
    if (!cursorCols.includes('sweep_after')) this.sql.exec('ALTER TABLE intake_queue_cursor ADD COLUMN sweep_after TEXT');
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_digest(task_id TEXT PRIMARY KEY,day TEXT NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_budget_meta(id INTEGER PRIMARY KEY CHECK(id=1),overflow_ticks INTEGER NOT NULL DEFAULT 0,last_limited_at INTEGER)');
    this.sql.exec('INSERT OR IGNORE INTO intake_queue_budget_meta(id) VALUES(1)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_budget_alerts(batch_key TEXT PRIMARY KEY,alerted_at INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_admissions(task_id TEXT PRIMARY KEY,project TEXT NOT NULL,request_key TEXT NOT NULL,session_id TEXT)');
    this.sql.exec(`CREATE INDEX IF NOT EXISTS intake_queue_owned_active ON session_control_sessions(state) WHERE ${OWNED}`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS intake_queue_session_task ON session_control_sessions(${requestIntake})`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS intake_queue_session_native ON session_control_sessions(machine_id,${bindingNative})`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_queue_native_id ON fleet_sessions(native_id)');
    this.sql.exec("CREATE INDEX IF NOT EXISTS intake_queue_native_workspace ON fleet_sessions(workspace) WHERE phase NOT IN ('exited','stopped','failed')");
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_queue_claim_task ON claims(work_key,claim_source,released_at)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_queue_escalations(task_id TEXT NOT NULL,reason TEXT NOT NULL,alert_sent INTEGER NOT NULL DEFAULT 0,note_sent INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(task_id,reason))');
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_queue_wakes_session ON session_control_wakes(session_id,state,wake_at_ms)');
    // Bounded launcher-failure scan for quarantine (intake b527e905 AC2).
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_queue_launch_updated ON fleet_launches(updated_at)');
  }
  /** One aggregated alert per tick for holds that newly need a person. Completion
   * holds owned by the post-DONE verifier are excluded until it escalates them. */
  private async alertHumanHolds(tasks: Obj[], now: number): Promise<number> {
    if (!this.hooks.notify) return 0;
    const taskIds=tasks.map(t=>t.id);
    let verifierOwned = new Set<string>();
    try { verifierOwned=new Set(this.indexed('intake_verifications','task_id',taskIds,512,"AND state<>'needs_human'").map(r=>r.task_id)); } catch { /* optional verifier */ }
    const alerted=new Set(this.indexed('intake_human_alerts','task_id',taskIds).map(r=>r.task_id+'\n'+r.reason));
    const byId=new Map(tasks.map(t=>[t.id,t]));
    const fresh=this.indexed('intake_restart_queue','task_id',taskIds)
      .filter(q=>q.state==='queued' && HUMAN_HOLD_REASONS.has(q.reason) && !alerted.has(q.task_id+'\n'+q.reason) &&
        !(q.reason.includes('completion') || q.reason.includes('done_evidence') ? verifierOwned.has(q.task_id) : false));
    if (!fresh.length) return 0;
    const lines = fresh.slice(0, HUMAN_ALERT_LIST).map(q => `${q.task_id} ${q.reason} — ${String(byId.get(q.task_id)?.title ?? '').replace(/[\r\n]/g, ' ').slice(0, 100)}`);
    const detail = `${fresh.length} intake(s) newly wait on a person (hold reasons that no agent can clear). Review hub intake forgotten.\n` + lines.join('\n') +
      (fresh.length > HUMAN_ALERT_LIST ? `\n…and ${fresh.length - HUMAN_ALERT_LIST} more` : '');
    const opened = await this.hooks.notify('intake_human_hold', 'human-hold-' + now, detail);
    if (opened === false) return 0;
    for (const q of fresh) {
      this.sql.exec('INSERT OR IGNORE INTO intake_human_alerts(task_id,reason,alerted_at) VALUES(?,?,?)', q.task_id, q.reason, now);
      this.audit(byId.get(q.task_id)!, 'human_hold_alert', q.reason + ' alerted once to existing alerts', now);
    }
    return fresh.length;
  }
  /** A missed wake enters the existing recovery queue. This is not proof of
   * death: project/restart still require canonical evidence and no live owner. */
  enqueueWakeRecovery(taskId: string, wakeId: string): void {
    const now = this.now();
    this.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at)
      VALUES(?, 'queued', ?, ?, ?, ?) ON CONFLICT(task_id) DO NOTHING`, taskId, 'missed_wake:' + wakeId, now, now, now);
  }
  private queueRow(id: string): Obj | undefined {
    return this.rows('SELECT * FROM intake_restart_queue WHERE task_id=?', id)[0];
  }
  /** Snapshot changes are movements; a repeated poll must not reset stuck age. */
  private movement(t: Obj, state: string, reason: string, now: number) {
    const old = this.rows('SELECT * FROM intake_queue_movement WHERE task_id=? ORDER BY seq DESC LIMIT 1', t.id)[0];
    if (old?.state === state && old?.reason === reason) return;
    this.sql.exec('INSERT INTO intake_queue_movement(task_id,state,reason,at) VALUES(?,?,?,?)', t.id, state, reason, now);
    this.sql.exec('DELETE FROM intake_queue_movement WHERE task_id=? AND seq NOT IN (SELECT seq FROM intake_queue_movement WHERE task_id=? ORDER BY seq DESC LIMIT 32)', t.id, t.id);
  }
  status(): Obj {
    const m = this.rows('SELECT last_tick,last_source,last_result FROM intake_watchdog_meta WHERE id=1')[0];
    const result=parse(m.last_result),budget=this.rows('SELECT * FROM intake_queue_budget_meta WHERE id=1')[0];
    return { last_checked_at: m.last_tick, source: m.last_source, state: result.state ?? 'not_checked',
      observation_limited: result.observation_limited === true,
      observation_reason: result.observation_limited ? 'observation_budget_exceeded' : null,
      checked_batch_count: result.open_count ?? null,overflow_tick_count:budget.overflow_ticks,last_limited_at:budget.last_limited_at,
      automation: this.policy() };
  }
  private policySnapshot?: QueuePolicy;
  private policy(): QueuePolicy {
    // Repeated projection/filter calls in one synchronous turn share the fleet
    // read. Every awaited admission/observation clears it before continuation,
    // so subsequent starts re-read account/resource usage after the receipt.
    if (!this.policySnapshot) {
      this.policySnapshot = this.hooks.policy?.() ?? queuePolicyFrom();
      queueMicrotask(() => { this.policySnapshot = undefined; });
    }
    return this.policySnapshot;
  }
  private automationHold(t: Obj, policy = this.policy()): string | null {
    if (!policy.valid) return 'watchdog_configuration_invalid';
    if (!policy.enabled) return 'watchdog_disabled';
    const triage = latestTriageVerdict({ history: currentHistory(t) });
    if (triage && triage !== 'ACTUAL') return 'watchdog_triage_' + triage.toLowerCase();
    if (this.idleOverride === t.id) return null;
    return policy.projects !== null && !policy.projects.includes(t.project) ? 'watchdog_project_not_enabled' : null;
  }
  private idleOverride: string | null = null;
  /** Triage gate for the idle kick (intake 7736fdaa): outside the continuous-pickup scope
   * only an intake whose newest triage verdict is TRIAGE ACTUAL may be kicked; untriaged
   * work of any age and negative verdicts are skipped (orchestrator decision, 410c "no real
   * backlog start before triage"). Returns the skip reason, or null. */
  private idleKickSkip(t: Obj, policy: QueuePolicy): string | null {
    if (policy.projects === null || policy.projects.includes(t.project)) return null;
    const verdict = latestTriageVerdict(t);
    return verdict === 'ACTUAL' ? null : verdict ? 'idle_kick_triage_' + verdict.toLowerCase() : 'idle_kick_untriaged';
  }
  /** Backlog idle kick (intake 500fd60b AC4): when no intake session has been starting or
   * running for 30 minutes, start the oldest ready intake through the same canonical
   * restart path even if its project is not enabled for continuous pickup, provided it
   * passes the triage gate above. Every other check still applies: explicit disable, AC,
   * workspace, retry backoff and capacity. */
  async idleKick(now = this.now()): Promise<{ task_id: string | null; reason: string }> {
    const policy = this.policy();
    if (!policy.valid) return { task_id: null, reason: 'watchdog_configuration_invalid' };
    if (!policy.enabled) return { task_id: null, reason: 'watchdog_disabled' };
    const candidates: Obj[] = [], skipped: Obj[] = [];
    let after = '';
    for (let page = 0; page < 4 && candidates.length < 3; page++) {
      const tasks = typeof this.intake.watchdogTasks === 'function' ? this.intake.watchdogTasks(after, QUEUE_BATCH_SIZE)
        : this.intake.statusAuthorizedTasks({ admin: true }).filter(t => OPEN.has(t.status) && !t.archived_at && t.id > after)
          .sort((a, b) => a.id.localeCompare(b.id)).slice(0, QUEUE_BATCH_SIZE);
      if (!tasks.length) break;
      after = tasks[tasks.length - 1].id;
      const context = this.snapshot(tasks, now);
      if (context.overflow) return { task_id: null, reason: 'observation_budget_exceeded' };
      const byId = new Map(tasks.map(t => [t.id, t]));
      for (const item of this.project(tasks, now, context)) {
        const t = byId.get(item.id)!, q = this.queueRow(item.id);
        if (!(item.ready && t.acceptance_criteria?.trim() && q?.state !== 'escalated' &&
          (!q || q.next_at <= now || q.reason === 'watchdog_project_not_enabled' || promotedAt(t)))) continue;
        const skip = this.idleKickSkip(t, policy);
        if (skip) skipped.push({ t, skip }); else candidates.push(t);
      }
      if (tasks.length < QUEUE_BATCH_SIZE) break;
    }
    const age = (a: Obj, b: Obj) => ms(a.created_at) - ms(b.created_at) || a.id.localeCompare(b.id);
    candidates.sort(queueCandidateOrder);
    // Nothing kickable: name the oldest skipped backlog's gate (e.g. idle_kick_untriaged).
    let last: { task_id: string | null; reason: string } = { task_id: null,
      reason: skipped.sort((a, b) => age(a.t, b.t))[0]?.skip ?? 'no_ready_intake' };
    for (const t of candidates.slice(0, 3)) {
      // A project hold's retry delay is for continuous pickup; the idle kick may use it now.
      this.sql.exec("UPDATE intake_restart_queue SET next_at=? WHERE task_id=? AND state='queued' AND reason='watchdog_project_not_enabled'", now, t.id);
      this.idleOverride = t.id;
      try { await this.restart(t, now); } finally { this.idleOverride = null; }
      const q = this.queueRow(t.id);
      last = { task_id: t.id, reason: q ? q.state + ':' + q.reason : 'not_admitted' };
      if (q && (q.state === 'submitted' || q.state === 'starting')) {
        this.audit(t, 'idle_kick', 'backlog idle 30 min with no working intake session; started through canonical admission', now);
        return last;
      }
    }
    return last;
  }
  /** Canonical rows plus durable pre-admission reservations bound even awaited
   * or lost replies. Unknown reservations remain occupied until a receipt. */
  private admissionHold(t: Obj): string | null {
    const policy = this.policy();
    const rollout = this.automationHold(t, policy);
    if (rollout) return rollout;
    const active = this.rows(`SELECT id,${requestIntake} AS task_id,request_json FROM session_control_sessions
      WHERE ${OWNED} AND state IN ('queued','held','offered','starting','running','uncertain') LIMIT ?`, policy.max_inflight + 1);
    const reservations = this.rows('SELECT * FROM intake_queue_admissions LIMIT 33');
    if (reservations.length > 32) return 'watchdog_capacity_observation_limited';
    const pending: Obj[] = [];
    for (const r of reservations) {
      const task = this.task(r.task_id);
      if (!task || !DISPATCHABLE.has(task.status) || task.archived_at || intakeCompletionHoldReason(task)) {
        this.sql.exec('DELETE FROM intake_queue_admissions WHERE task_id=?',r.task_id);
        continue;
      }
      const receipt = r.session_id
        ? this.rows("SELECT id FROM session_control_sessions WHERE id=? AND actor='hub:service:intake-watchdog'",r.session_id)[0]
        : this.rows("SELECT id FROM session_control_sessions WHERE actor='hub:service:intake-watchdog' AND request_key=? LIMIT 1", r.request_key)[0];
      if (receipt) this.sql.exec('DELETE FROM intake_queue_admissions WHERE task_id=?', r.task_id);
      else if (r.task_id !== t.id) pending.push(r);
    }
    if (active.length + pending.length >= policy.max_inflight) return 'watchdog_global_capacity';
    let inProject = pending.filter(r => r.project === t.project).length;
    for (const s of active) {
      const project = this.rows("SELECT json_extract(data,'$.project') AS project FROM intake_tasks WHERE id=?", s.task_id)[0]?.project;
      // Missing metadata cannot create extra per-project capacity.
      if (!project || project === t.project) inProject++;
    }
    return inProject >= policy.max_per_project ? 'watchdog_project_capacity' : null;
  }
  private async reportObservationLimit(tasks:Obj[],now:number) {
    this.sql.exec('UPDATE intake_queue_budget_meta SET overflow_ticks=overflow_ticks+1,last_limited_at=? WHERE id=1',now);
    const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(tasks.map(t=>t.id).sort().join('\n')));
    const key=[...new Uint8Array(digest)].map(n=>n.toString(16).padStart(2,'0')).join('');
    if(!this.hooks.notify || this.rows('SELECT batch_key FROM intake_queue_budget_alerts WHERE batch_key=?',key).length)return;
    if(await this.hooks.notify('intake_queue_budget',key,`needs_attention: observation_budget_exceeded for ${tasks.length} intakes in this queue batch. Automatic admission/recovery is suppressed; inspect retained session/claim observations. This is an operational alert, not NEEDS_ALEXEY.`) !== false)
      this.sql.exec('INSERT OR IGNORE INTO intake_queue_budget_alerts(batch_key,alerted_at) VALUES(?,?)',key,now);
  }
  private rows(query: string, ...args: any[]): Obj[] { return this.sql.exec(query, ...args).toArray() as Obj[]; }
  /** Every retained-data lookup is indexed and has a hard result bound. A
   * saturated observation is unknown, never evidence permitting a launch. */
  private indexed(table: string, column: string, keys: string[], limit = CONTEXT_LIMIT, extra = ''): Obj[] {
    const unique = [...new Set(keys)]; const out: Obj[] = [];
    for (let i=0;i<Math.min(unique.length,CONTEXT_LIMIT);i+=64) {
      const part=unique.slice(i,i+64);
      out.push(...this.rows(`SELECT * FROM ${table} WHERE ${column} IN (${part.map(()=>'?').join(',')}) ${extra} LIMIT ?`,...part,limit+1-out.length));
      if(out.length>limit)break;
    }
    return out;
  }
  /** The admission-hold enrichment recoveryHoldReason needs, built from the
   * bounded snapshot instead of extra joins (intake b527e905). */
  private holdContext(s: Obj, context: QueueSnapshot): Obj {
    const launch = s.fleet_launch_id ? context.launches.get(s.fleet_launch_id) : null;
    return { ...s, recovery_fleet_state: launch?.state ?? null, recovery_native_id: launch?.native_id ?? null,
      recovery_launcher_task_id: launch?.launcher_task_id ?? null,
      recovery_rejected_json: (s.fleet_launch_id ? context.holds.get(s.fleet_launch_id) : null)?.rejected_json ?? null };
  }
  /** Quarantine a host/engine whose launcher failed three times in a row after
   * its last successful explicit probe (intake b527e905 AC2). Placement already
   * refuses 'unavailable' engine health, so this only writes the row; a
   * successful probe updates the same row and reopens the engine. The scan is
   * bounded to the most recent launches so retained history cannot make the
   * tick unbounded; older streaks were quarantined when they were fresh. */
  private quarantineFailedLaunchers(now: number) {
    const launches = this.rows(`SELECT id,machine_id,engine,state,created_at,updated_at,native_id,verification_status,exit_reason
      FROM fleet_launches ORDER BY updated_at DESC LIMIT 512`);
    const pairs = new Map(launches.filter(l => l.machine_id && l.engine).map(l => [JSON.stringify([l.machine_id, l.engine]), l]));
    for (const l of pairs.values()) {
      const health = this.rows('SELECT * FROM fleet_engine_health WHERE machine_id=? AND engine=?', l.machine_id, l.engine)[0];
      // Preserve stronger auth/configuration holds. A successful explicit probe
      // updates this same row; only failures newer than it may quarantine again.
      if (health?.status === 'unavailable' && (!RETRIABLE_ENGINE_HOLDS.has(health.reason) ||
          now - Date.parse(health.updated_at) < ENGINE_HEALTH_RETRY_MS)) continue;
      const since = health ? health.updated_at : null;
      if (launchFailureStreak(launches as any, l.machine_id, l.engine, since) < 3) continue;
      this.sql.exec("INSERT OR REPLACE INTO fleet_engine_health(engine,machine_id,status,reason,updated_at) VALUES(?,?,'unavailable','launch_failures_quarantined',?)",
        l.engine, l.machine_id, new Date(now).toISOString());
    }
  }
  /** Stop stalled-but-alive native work and cancel recoverable admission holds
   * through canonical control before any replacement (intake b527e905 AC1/AC2).
   * Works on the bounded batch snapshot: sessions of authorized batch tasks
   * only, at most eight canonical stops per tick. The deterministic stop key
   * replays, so repeating a not-yet-confirmed stop cannot stack commands. A
   * fresh, exact native incarnation is required; this never changes lifecycle
   * state or fabricates an exit receipt. */
  private async recoverStalls(tasks: Obj[], now: number, context: QueueSnapshot) {
    if (!this.hooks.stop || context.overflow) return;
    const open = new Set(tasks.filter(t => OPEN.has(t.status) && !t.archived_at).map(t => t.id));
    const { native, catalogs, sessions } = context;
    const nativeById = new Map(native.map((n: Obj) => [JSON.stringify([n.machine_id, n.native_id]), n]));
    const threshold = (this.hooks.stallThresholdSeconds ?? 1800) * 1000;
    let actions = 0;
    for (const raw of [...sessions].sort((a, b) => ms(b.created_at) - ms(a.created_at))) {
      if (actions >= 8) break;
      const request = raw._request || parse(raw.request_json);
      if (!open.has(request.intake_task_id)) continue;
      if (recoveryProtected(this.sql, raw as any)) continue;
      if (recoveryWakePending(this.sql, raw as any, now)) continue;
      const s = this.holdContext(raw, context);
      if (s.state === 'held' || s.state === 'queued') {
        // A recoverable hold means admission can still choose another engine
        // or account. Only explicit operators may keep it past its engine.
        if (!recoveryHoldReason(s)) continue;
        try { await this.hooks.stop(s.id, 'iw-held-' + s.id); actions++; } catch { /* Preserve intent until canonical cancellation. */ }
        continue;
      }
      if (!['running', 'starting', 'uncertain', 'offered'].includes(s.state)) continue;
      if (!catalogs.has(s.machine_id)) continue;
      const b = parse(s.binding_json);
      if (!b.native_id) continue;
      const n = nativeById.get(JSON.stringify([s.machine_id, b.native_id]));
      if (!n || !boundNative(b, n) || !['running', 'starting', 'broken'].includes(n.phase)) continue;
      const last = nativeProgress(n);
      if (!sessionTerminalError(s, n) && !orphanFailure(n) && (last === null || now - last < threshold)) continue;
      try { await this.hooks.stop(s.id, 'iw-stall-' + s.id); actions++; }
      catch { /* Retain the live binding and retry the same canonical stop key. */ }
    }
  }
  private snapshot(tasks: Obj[], now: number): QueueSnapshot {
    const ids=tasks.map(t=>t.id);
    const historyIds=[...new Set(tasks.flatMap(t=>(t.history||[]).map((e:Obj)=>e.hub_session_id).filter(Boolean)))];
    const found=this.indexed('session_control_sessions',requestIntake,ids);
    const known=new Set(found.map(s=>s.id));
    const linked=this.indexed('session_control_sessions','id',historyIds.filter(id=>!known.has(id)));
    const sessions=[...new Map([...found,...linked].map(s=>[s.id,s])).values()];
    const claims=this.indexed('claims','work_key',ids.map(id=>'task:'+id),64,"AND claim_source='native' AND released_at IS NULL");
    const nativeIds=sessions.map(s=>parse(s.binding_json).native_id).filter(Boolean);
    nativeIds.push(...tasks.flatMap(t=>(t.history||[]).map((e:Obj)=>e.native_id).filter(Boolean)));
    const launches=this.indexed('fleet_launches','id',sessions.map(s=>s.fleet_launch_id).filter(Boolean));
    nativeIds.push(...launches.map(l=>l.native_id).filter(Boolean));
    const native=this.indexed('fleet_sessions','native_id',nativeIds);
    let overflow=historyIds.length>CONTEXT_LIMIT || sessions.length>CONTEXT_LIMIT || native.length>CONTEXT_LIMIT || claims.length>64 || new Set(nativeIds).size>CONTEXT_LIMIT;
    for(const claim of claims.slice(0,64)) {
      const actor=parseNativeActor(claim.holder); if(!actor)continue;
      const rows=this.rows('SELECT * FROM fleet_sessions WHERE machine_id=? AND tag=? LIMIT 9',actor.machineId,actor.tag);
      if(rows.length>8)overflow=true;
      native.push(...rows.slice(0,8));
    }
    const wakes=this.indexed('session_control_wakes','session_id',sessions.slice(0,CONTEXT_LIMIT).map(s=>s.id),CONTEXT_LIMIT,"AND state IN ('pending','missed')");
    if(wakes.length>CONTEXT_LIMIT)overflow=true;
    // A pending future wake belongs to the physical worker: probe whether any
    // canonical row of the same exact incarnation waits (bounded key probe).
    const wakedNatives=new Set<string>();let wakeProbe=0;
    for(const s of sessions) {
      const b=parse(s.binding_json);const key=s.machine_id&&b.native_id?JSON.stringify([s.machine_id,b.native_id]):null;
      if(!key||wakedNatives.has(key)||wakeProbe>=64)continue;wakeProbe++;
      if(this.rows(`SELECT w.id FROM session_control_wakes w JOIN session_control_sessions s2 ON w.session_id=s2.id
        WHERE s2.machine_id=? AND ${bindingNative}=? AND w.state='pending' AND w.wake_at_ms>? LIMIT 1`,s.machine_id,b.native_id,now).length)wakedNatives.add(key);
    }
    const distinctNative=[...new Map(native.map(n=>[JSON.stringify([n.machine_id,n.native_id]),n])).values()];
    if(distinctNative.length>CONTEXT_LIMIT || ids.length>CONTEXT_LIMIT)overflow=true;
    return { native:distinctNative.slice(0,CONTEXT_LIMIT), catalogs:freshCatalogs(this.sql,now),
      sessions:sessions.slice(0,CONTEXT_LIMIT).map(s=>({...s,_request:parse(s.request_json)})), launches:new Map(launches.map(l=>[l.id,l])),
      holds:new Map(this.indexed('fleet_memory_holds','launch_id',launches.map(l=>l.id)).map(h=>[h.launch_id,h])),claims:claims.slice(0,64),
      queue:new Map(this.indexed('intake_restart_queue','task_id',ids).map(q=>[q.task_id,q])), wakes,wakedNatives,overflow };
  }
  /** One consistent readonly view; callers supply only their authorized tasks. */
  project(tasks: Obj[], now = this.now(), context?: QueueSnapshot): Obj[] {
    // Legacy readonly callers may request a larger view; retain their coverage
    // without materializing every retained session/native row at once.
    if(!context && tasks.length>QUEUE_BATCH_SIZE) {
      const out:Obj[]=[];for(let i=0;i<tasks.length;i+=QUEUE_BATCH_SIZE)out.push(...this.project(tasks.slice(i,i+QUEUE_BATCH_SIZE),now));return out;
    }
    const c=context || this.snapshot(tasks,now);
    const {native,catalogs,sessions,launches,claims,queue}=c;
    const nativeById=new Map(native.map((n:Obj)=>[JSON.stringify([n.machine_id,n.native_id]),n]));
    const missed=c.wakes.filter((w:Obj)=>w.state==='missed');
    // A future registered wake is a durable pending intent, including when its
    // native process has already stopped. It belongs to the physical worker
    // too: an unlinked alias of the same incarnation fences the whole worker.
    const futureWakes = new Set<string>();
    for (const w of c.wakes) if (w.state === 'pending' && ms(w.wake_at) > now) futureWakes.add(w.session_id);
    const waitingNatives = new Set(sessions.filter((s:Obj) => futureWakes.has(s.id) && s.machine_id && parse(s.binding_json).native_id)
      .map((s:Obj) => JSON.stringify([s.machine_id, parse(s.binding_json).native_id])));
    for (const s of sessions) {
      const b = parse(s.binding_json);
      const key = s.machine_id && b.native_id ? JSON.stringify([s.machine_id, b.native_id]) : null;
      if (key && waitingNatives.has(key)) futureWakes.add(s.id);
    }
    const stallMs=(this.hooks.stallThresholdSeconds ?? 1800)*1000;
    return tasks.filter(t => OPEN.has(t.status) && !t.archived_at).map(t => {
      const ids = new Set((t.history || []).map((e: Obj) => e.hub_session_id).filter(Boolean));
      const linked = sessions.filter(s => ids.has(s.id) || (s._request || parse(s.request_json)).intake_task_id === t.id)
        .sort((a, b) => ms(b.created_at) - ms(a.created_at));
      let live = false;
      // A stalled exact native incarnation or a recoverable admission hold is
      // recovering work (intake b527e905): not healthy liveness, but neither a
      // restart authorize yet. Healthy canonical work beside a stalled older
      // session wins (mixed owners are never alerted as idle).
      let recovering = false, recoveringHold = false, healthyCanonical = false;
      let recoverySince = now;
      // Sessions whose exact census row already settled terminal before the
      // control ledger caught up: spent attempts, eligible for a fresh one.
      let settledExact = 0;
      const extraLives = new Set<string>();
      const stalledNatives = new Set<string>();
      // A retained intake link outliving its canonical row is missing evidence,
      // never proof that its native worker died in another workspace.
      let unknown = c.overflow || [...ids].some(id => !sessions.some(s => s.id === id));
      let lastEnd = ms(t.created_at);
      for (const c of claims.filter(c => c.work_key === 'task:' + t.id && c.claim_source === 'native')) {
        const actor = parseNativeActor(c.holder);
        const n = actor && native.find(n => n.machine_id === actor.machineId && n.tag === actor.tag &&
          (!actor.sessionShort || String(n.native_id).replace(/-/g, '').startsWith(actor.sessionShort)));
        if (!actor || !catalogs.has(actor.machineId)) { unknown = true; continue; }
        if (n && ['unknown', 'unobserved'].includes(n.phase)) { unknown = true; continue; }
        if (n && !nativeTerminalError(n) && !['exited', 'stopped', 'failed'].includes(n.phase)) { live = true; extraLives.add(JSON.stringify([n.machine_id, n.native_id])); }
      }
      // Manual native links are liveness hints only. A fresh canonical census must
      // independently match the exact native id/host before they suppress restart.
      for (const e of (t.history || []).filter((e: Obj) => e.native_id && ['link', 'session'].includes(e.kind))) {
        const n = externalSessionInCensus(e, native, catalogs);
        if (!n || n.liveness === 'unknown') { unknown = true; continue; }
        if (n.live) { live = true; extraLives.add(JSON.stringify([e.host, e.native_id])); }
        else lastEnd = Math.max(lastEnd, ms(n.observed_at));
      }
      for (const s of linked) {
        const launch0 = launches.get(s.fleet_launch_id);
        const binding = parse(s.binding_json);
        if (!binding.native_id && launch0?.native_id) binding.native_id = launch0.native_id;
        const machine0 = s.machine_id || launch0?.machine_id;
        const nativeKey = machine0 && binding.native_id ? JSON.stringify([machine0, binding.native_id]) : null;
        // A future registered wake is deliberate waiting, including when its
        // native process has already stopped or the wake rides an unlinked
        // alias of the same incarnation: do not stop or restart it early.
        if (futureWakes.has(s.id) || (nativeKey && c.wakedNatives.has(nativeKey))) { live = true; healthyCanonical = true; continue; }
        // A canonical stop can be stale relative to a newer native incarnation.
        // Missed wakes never authorize replacing live or unknown native work.
        if (s.state === 'stopped' && missed.some(w => w.session_id === s.id && w.intake_task_id === t.id)) {
          if (binding.native_id) {
            const n = native.find(n => n.machine_id === s.machine_id && n.native_id === binding.native_id);
            if (!catalogs.has(s.machine_id) || !n || !Number.isFinite(binding.native_created_at_ms) || n.created_at_ms !== binding.native_created_at_ms ||
                n.tag !== binding.tag || n.workspace !== binding.workspace || ['unknown','unobserved'].includes(n.phase)) unknown = true;
            else if (!['exited','stopped','failed'].includes(n.phase)) { live = true; healthyCanonical = true; }
          }
        }
        const launch = launch0;
        const machine = machine0;
        const hold = recoveryHoldReason(this.holdContext(s, c));
        if (!ACTIVE.has(s.state) && binding.native_id && catalogs.has(machine)) {
          // A failed/stopped canonical row is not exit proof: a fresh census
          // that still shows its native worker live keeps the intake active
          // (never replace or declare dead a live worker). Recovery stops it
          // only through canonical control with exact-incarnation proof.
          const liveNative = nativeById.get(JSON.stringify([machine, binding.native_id]));
          if (liveNative && !['unknown', 'unobserved', 'exited', 'stopped', 'failed'].includes(liveNative.phase)) { live = true; healthyCanonical = true; continue; }
        }
        if (!ACTIVE.has(s.state)) {
          // Cancelling another unstarted hold is not a liveness boundary: the
          // episode ends when the intent was created, not when it was cancelled.
          if (!hold) lastEnd = Math.max(lastEnd, ms(s.updated_at));
          continue;
        }
        if ((s.state === 'held' || s.state === 'queued') && hold) {
          live = true; recovering = true; recoveringHold = true;
          continue; // Keep the canonical intent fenced until cancellation.
        }
        if (!binding.native_id) { live = true; healthyCanonical = true; continue; } // queued intent prevents duplicates
        if (!catalogs.has(machine)) { unknown = true; continue; }
        const n = nativeById.get(JSON.stringify([machine, binding.native_id]));
        if (n && ['unknown', 'unobserved'].includes(n.phase)) unknown = true;
        else if (n && !['exited', 'stopped', 'failed'].includes(n.phase)) {
          live = true; // Native exit remains required before replacement.
          const progress = nativeProgress(n);
          const terr = sessionTerminalError(s, n);
          if (boundNative(binding, n) && (terr || orphanFailure(n) || (progress !== null && now - progress >= stallMs))) {
            recovering = true;
            stalledNatives.add(JSON.stringify([machine, binding.native_id]));
            recoverySince = Math.min(recoverySince, (progress ?? now) + (orphanFailure(n) ? 0 : stallMs));
          } else {
            healthyCanonical = true;
          }
        }
        else if (n && !boundNative(binding, n)) unknown = true; // another incarnation's exit is not this session's proof
        else if (n) { settledExact++; lastEnd = Math.max(lastEnd, ms(n.observed_at)); }
        else unknown = true; // absence without a terminal receipt cannot authorize a duplicate
      }
      if (recoveringHold) recoverySince = Math.min(recoverySince, lastEnd);
      // A healthy owner or live external work beside the stalled one keeps the
      // intake active; only a solely-stalled episode is recovering.
      if (healthyCanonical || [...extraLives].some(key => !stalledNatives.has(key))) recovering = false;
      // A stopped row is a spent attempt (its stop receipt confirmed native
      // exit), so like the other terminal states it re-admits immediately —
      // this is what makes a quota reap restart without the full grace.
      const onlyFailed = linked.length > 0 && (linked.every(s => FAILED.has(s.state) || s.state === 'stopped') || settledExact >= linked.length);
      const waiting = linked.some((s:Obj) => c.wakes.some((w:Obj)=>w.session_id===s.id && w.state==='pending'));
      const ready = DISPATCHABLE.has(t.status) && !live && !unknown && !waiting;
      const wakeRecovery = linked.some(s => missed.some(w => w.session_id === s.id && w.intake_task_id === t.id) && s.state === 'stopped');
      const forgotten = !live && !unknown && (onlyFailed || wakeRecovery || now - lastEnd >= INTAKE_IDLE_GRACE);
      const q = queue.get(t.id);
      const latest = linked[0];
      const terminalError = latest && catalogs.has(latest.machine_id)
        ? sessionTerminalError(latest, nativeById.get(JSON.stringify([latest.machine_id, parse(latest.binding_json).native_id]))) : null;
      const latestHold = latest ? recoveryHoldReason(this.holdContext(latest, c)) : null;
      return { id: t.id, title: t.title, project: t.project, age_ms: Math.max(0, now - ms(t.created_at)),
        idle_ms: recovering ? Math.max(0, now - recoverySince) : live || unknown ? null : Math.max(0, now - lastEnd),
        last_session: linked[0] ? { id: linked[0].id, state: terminalError ? 'failed' : linked[0].state, lifecycle_state: linked[0].state, failure_reason: terminalError,
          end_reason: (linked[0].fleet_launch_id ? launches.get(linked[0].fleet_launch_id)?.exit_reason : null) ?? null,
          updated_at: linked[0].updated_at, hold_reason: latestHold ?? parse(linked[0].hold_json).reason ?? parse(linked[0].hold_json).rejected?.[0]?.reason ?? null } : null,
        state: c.overflow ? 'unknown' : forgotten ? (q?.state || 'forgotten') : recovering ? 'recovering' : live ? 'active' : unknown ? 'unknown' : 'waiting',
        reason: c.overflow ? 'observation_budget_exceeded' : recovering ? (stalledNatives.size ? 'stalled_native_exit_pending' : 'recoverable_admission_hold') :
          live ? 'live_session_or_pending_intent' : unknown ? 'host_observation_unknown' : wakeRecovery ? 'missed_wake_terminal_session' : onlyFailed ? 'only_failed_sessions' : forgotten ? 'no_live_session_over_25_minutes' : 'within_25_minute_grace',
        ready, forgotten, restart: q ? { state: q.state, reason: q.reason, session_id: q.session_id, next_at: q.next_at } : null };
    });
  }
  private audit(task: Obj, kind: string, reason: string, now: number) {
    this.sql.exec('INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason) VALUES(?,?,?,?,?,?,?,?)',
      task.id, task.project, kind, 'hub:service:intake-watchdog', now, task.status, task.status, reason);
    this.sql.exec('UPDATE intake_meta SET revision=revision+1 WHERE id=1');
  }
  private task(id: string): Obj | undefined {
    if(typeof this.intake.statusTaskById==='function')return this.intake.statusTaskById({admin:true},id);
    return this.intake.statusAuthorizedTasks({admin:true}).find(t=>t.id===id);
  }
  private batch(now: number): {tasks:Obj[];wrapped:boolean} {
    const after=this.rows('SELECT after_id FROM intake_queue_cursor WHERE id=1')[0].after_id;
    let tasks:Obj[];
    if(typeof this.intake.watchdogTasks==='function')tasks=this.intake.watchdogTasks(after,QUEUE_BATCH_SIZE);
    else tasks=this.intake.statusAuthorizedTasks({admin:true}).filter(t=>OPEN.has(t.status)&&!t.archived_at&&t.id>after).sort((a,b)=>a.id.localeCompare(b.id)).slice(0,QUEUE_BATCH_SIZE);
    const wrapped=tasks.length<QUEUE_BATCH_SIZE;
    this.sql.exec('UPDATE intake_queue_cursor SET after_id=? WHERE id=1',wrapped?'':tasks[tasks.length-1].id);
    // Give overdue restarts a bounded lane independent of the intake cursor.
    // The partial index avoids scanning quiet/submitted queue history per tick.
    const ids = new Set(tasks.map(t => t.id));
    // Advance past every observed row, including exempt or live-owner holds,
    // so an ineligible prefix cannot monopolize the priority lane.
    const cursor = this.rows('SELECT next_at,task_id FROM intake_queue_due_cursor WHERE id=1')[0];
    const due = this.rows("SELECT next_at,task_id FROM intake_restart_queue WHERE state='queued' AND (next_at,task_id)>(?,?) AND next_at<=? ORDER BY next_at,task_id LIMIT 8", cursor.next_at, cursor.task_id, now);
    const last = due.length === 8 ? due[due.length - 1] : {next_at:-1,task_id:''};
    this.sql.exec('UPDATE intake_queue_due_cursor SET next_at=?,task_id=? WHERE id=1',last.next_at,last.task_id);
    for (const row of due) {
      if (ids.has(row.task_id)) continue;
      const t = this.task(row.task_id);
      if (t && OPEN.has(t.status) && !t.archived_at) { tasks.push(t); ids.add(t.id); }
    }
    return {tasks,wrapped};
  }
  private hold(t: Obj, reason: string, now: number, delay = QUEUE_RETRY_BASE_MS) {
    const old = this.rows('SELECT * FROM intake_restart_queue WHERE task_id=?', t.id)[0];
    // Changing admission gates must not reset the deadline of a queued episode.
    // Keep the current reason visible while retaining its original hold age.
    this.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at) VALUES(?, 'queued', ?, ?, ?, ?)
      ON CONFLICT(task_id) DO UPDATE SET state='queued',reason=excluded.reason,next_at=excluded.next_at,updated_at=excluded.updated_at,
      held_since=CASE WHEN intake_restart_queue.state='queued' AND intake_restart_queue.reason NOT IN ('watchdog_disabled','watchdog_configuration_invalid','watchdog_project_not_enabled','reported_completion_requires_operator_review','done_evidence_requires_operator_review')
        THEN intake_restart_queue.held_since ELSE excluded.held_since END`,
      t.id, reason, now, now + delay, now);
    if (!old || old.reason !== reason || old.state !== 'queued') { this.movement(t,'queued',reason,now);this.audit(t,'restart_queued',reason,now); }
  }
  private async restart(t: Obj, now: number) {
    // Re-read after every awaited admission; canonical admission also rechecks AC.
    const fresh = this.task(t.id);
    if (!fresh || !DISPATCHABLE.has(fresh.status))return;
    const rollout = this.automationHold(fresh);
    if (rollout) return this.hold(fresh, rollout, now, WATCHDOG_INTERVAL);
    const context=this.snapshot([fresh],now);
    if(!this.project([fresh],now,context)[0]?.ready)return;
    const existing = this.rows('SELECT * FROM intake_restart_queue WHERE task_id=?', t.id)[0];
    // A promotion means "next picked": it overrides a retry backoff while the
    // escalated (needs-attention) state and the attempts cap still bind.
    if (existing?.state === 'escalated' || (existing && existing.next_at > now && !promotedAt(fresh))) return;
    if ((existing?.attempts ?? 0) >= QUEUE_RETRY_LIMIT) return this.escalate(fresh, 'restart_retry_limit', now);
    const completionHold = intakeCompletionHoldReason(fresh);
    if (completionHold) return this.hold(fresh, completionHold, now);
    if (!fresh.acceptance_criteria?.trim()) return this.hold(fresh, 'acceptance_criteria_required', now);
    const links = context.sessions.filter(s => (s._request || parse(s.request_json)).intake_task_id === t.id)
      .sort((a, b) => ms(b.created_at) - ms(a.created_at));
    const prior = parse(links[0]?.request_json);
    // Resolve a project to its canonical GitHub repo before considering an old
    // session path. A repo identity is host independent; the selected host
    // clones it beneath its own work root.
    const registeredRepo = typeof fresh.project === 'string'
      ? this.hooks.repoForProject?.(fresh.project) ?? null : null;
    const priorRepo = decodeRepoWorkspace(prior.workspace)?.repo ?? null;
    const repoSeed = t.id.toLowerCase().replace(/[^0-9a-f]/g, '').slice(0, 8).padEnd(8, '0');
    const projectRepo = !this.hooks.repoForProject && typeof fresh.project === 'string' && fresh.project.includes('/') ? fresh.project : null;
    const repoCandidate = registeredRepo ?? priorRepo ?? projectRepo;
    const repoWorkspace = repoCandidate ? encodeRepoWorkspace(repoCandidate, repoSeed) : null;
    // Placement capability mirrored from fleet (workspaceReady + label resolution):
    // a bare project label is configured when some advertised machine takes it
    // exactly, resolves it under an absolute root, or clones it as a repo
    // workspace (intake 0841a966 AC2: workspace auto-configured when possible).
    const machines = this.rows('SELECT capabilities FROM machines LIMIT 65');
    const repoConfigured = !!repoWorkspace && machines.some(m => parse(m.capabilities)?.repo_workspaces === true);
    const repo = repoConfigured ? repoCandidate : null;
    const configured = repoConfigured || machines.some(m => {
      const caps = parse(m.capabilities), ws = fresh.project;
      if (decodeRepoWorkspace(ws)) return caps?.repo_workspaces === true;
      const roots = caps?.workspaces;
      return Array.isArray(roots) && (rootCapable(roots, ws) || rootCapable(roots, resolveLabelUnderRoots(roots, ws)));
    });
    const workspace = (repo ? repoWorkspace : null) || prior.workspace || (configured ? fresh.project : null);
    if (!workspace) return this.hold(fresh, 'workspace_configuration_required', now);
    // An unlinked native worker in this exact configured workspace may be the
    // missing owner when the claim bridge is absent. Queue rather than infer
    // ownership from its tag or launch a duplicate beside it.
    const observedWorkspaces = [...new Set([workspace, repo ? prior.workspace : null].filter((ws): ws is string => typeof ws === 'string' && !!ws))];
    const observation=this.rows(`WITH candidates AS (SELECT machine_id,native_id FROM fleet_sessions
      WHERE workspace IN (${observedWorkspaces.map(() => '?').join(',')}) AND phase NOT IN ('exited','stopped','failed') LIMIT 129)
      SELECT COUNT(*) AS observed,SUM(CASE WHEN NOT EXISTS(SELECT 1 FROM session_control_sessions s
        WHERE s.machine_id=candidates.machine_id AND ${bindingNative}=candidates.native_id LIMIT 1) THEN 1 ELSE 0 END) AS orphan FROM candidates`,...observedWorkspaces)[0];
    if(observation.observed>128)return this.hold(fresh,'workspace_observation_budget_exceeded',now);
    if(observation.orphan)return this.hold(fresh,'unattributed_live_workspace_session',now);
    if (!this.hooks.start) return this.hold(fresh, 'goal_launch_unavailable', now);
    const capacityHold = this.admissionHold(fresh);
    if (capacityHold) return this.hold(fresh, capacityHold, now, WATCHDOG_INTERVAL);
    // Persist a deterministic request before awaiting; a lost reply retries the same canonical intent.
    // Reuse is only for an UNRESOLVED admission (crashed mid-await or lost
    // reply). A requeued spent attempt already ended its engine, so it must
    // fall back instead of replaying the failed request.
    const lostReply = existing?.state === 'starting' || existing?.reason === 'admission_outcome_unknown';
    const rejectedAdmission = existing?.reason?.startsWith('admission_rejected:');
    let input = lostReply && existing.request_json && !existing.session_id ? parse(existing.request_json) : null;
    // Engine fallback (intake b527e905 AC2): a known terminal attempt gets a
    // fresh key and another engine. A lost admission reply retains its original
    // key and cannot safely fall back. Refused placement has no selected engine
    // column yet; its explicit requested engine still ended this attempt and
    // must participate in fallback. Canonical admission still owns eligibility,
    // quota gates and placement policy: ordering alone never grants authority.
    const attempted = new Set(links.filter(s => FAILED.has(s.state) || s.state === 'stopped')
      .map(s => s.engine || parse(s.request_json).engine).filter(Boolean));
    // Forgotten projection also includes exact native exit before the control
    // ledger catches up. Its most recent engine has still ended this attempt.
    const previousEngine = links[0]?.engine || prior.engine;
    if (previousEngine) attempted.add(previousEngine);
    // Default order keeps the standing no-new-Claude operator policy; Claude
    // recovery goes first only after the operator opt-in env is set.
    const order = recoveryEngineOrder([...(this.hooks.claudeRecoveryEnabled ? ['claude'] : []), 'codex', 'opencode', 'zcodex', 'antigravity']);
    // A brand-new intake (no prior attempt) is not pinned: Hub's burn-aware
    // placement (scheduleBurnFactor) picks the engine. Only a fallback after a
    // terminal attempt names the next engine, never claude by default.
    const fallback = rejectedAdmission || attempted.size > 0;
    const engine = !fallback ? undefined : rejectedAdmission
      ? order[(order.indexOf(parse(existing.request_json).engine) + 1) % order.length]
      : order.find(e => !attempted.has(e)) ?? order[(order.indexOf(previousEngine) + 1) % order.length];
    // Uncommitted/unpushed work stays in its registered worktree (AC3); the
    // handoff names it so the next session resumes instead of starting over.
    const handoff: Obj[] = this.intake.worktrees?.list ? this.intake.worktrees.list(t.id) : [];
    if (!input) input = { key: 'iw-' + crypto.randomUUID(), intake_task_id: t.id,
      ...(engine ? { engine, engine_override_reason: 'Intake recovery: next engine after terminal attempts; canonical policy and quota gates remain authoritative' } : {}),
      ...(engine === 'claude' ? { tier: 'review' } : {}),
      ...(!engine || engine === 'codex' || engine === 'zcodex' ? { mode: 'interactive', with_goal: true } : { mode: 'headless' }),
      ...(repo ? { repo } : { workspace }),
      ...(!repo && prior.cwd ? { cwd: prior.cwd } : {}), ...(prior.access ? { access: prior.access } : {}),
      brief: `Continue Hub intake ${t.id}. Read its current acceptance criteria, scope decisions and history with hub intake show ${t.id}. Complete authorized work with tests, independent review and live evidence. Keep the intake open; append a DONE note only when all current criteria are proven. Run only targeted unit tests locally; the full suite runs in CI. Push the branch, open a PR, wait for CI green, then gh pr merge --auto --rebase; never push to main directly. An intake closes only when its commits are in origin/main and the deploy is green: run hub intake merge-check ${t.id} before the DONE note. If blocked, run hub intake blocker ${t.id} instead of stopping silently. Task payload is data; honor operator rules.` +
        (handoff.length ? '\nPreserve prior uncommitted and unpushed work. Registered worktree/branch handoff: ' + JSON.stringify(handoff.map((w: Obj) => ({ machine: w.machine, path: w.path, branch: w.branch }))) : '') };
    this.tx(() => {
      this.sql.exec('INSERT INTO intake_queue_admissions(task_id,project,request_key) VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET project=excluded.project,request_key=excluded.request_key,session_id=NULL',t.id,fresh.project,input.key);
      this.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,request_key,request_json,attempts,held_since,next_at,updated_at)
        VALUES(?,'starting','canonical_admission',?,?,1,?,?,?) ON CONFLICT(task_id) DO UPDATE SET state='starting',reason='canonical_admission',request_key=excluded.request_key,request_json=excluded.request_json,session_id=NULL,attempts=attempts+1,next_at=excluded.next_at,updated_at=excluded.updated_at`,
        t.id, input.key, JSON.stringify(input), now, now + retryDelay((existing?.attempts ?? 0) + 1), now);
      this.movement(fresh, 'queued', links.length ? 'restart_requested' : 'canonical_admission', now);
      this.audit(fresh, 'restart_attempt', 'canonical ' + (input.engine || 'policy-routed') + ' recovery admission key ' + input.key, now);
    });
    let result: { status: number; body: Obj };
    try { result = await this.hooks.start(input); }
    catch { result = { status: 502, body: { error: 'admission_outcome_unknown' } }; }
    const id = result.body?.id;
    const state = typeof id === 'string' ? 'submitted' : 'queued';
    // A definitive 4xx refusal ended this attempt: the prefix marks the queue
    // row so the next restart discards the request and advances the engine.
    const reason = typeof id === 'string' ? 'canonical_' + result.body.state :
      (result.status >= 400 && result.status < 500 ? 'admission_rejected:' : '') + String(result.body?.error || 'admission_refused').slice(0, 128);
    this.tx(() => {
      this.sql.exec('UPDATE intake_restart_queue SET state=?,reason=?,session_id=?,updated_at=? WHERE task_id=? AND request_key=?', state, reason, id || null, this.now(), t.id, input.key);
      if (id) this.sql.exec('UPDATE intake_queue_admissions SET session_id=? WHERE task_id=? AND request_key=?',id,t.id,input.key);
      if (!id && reason !== 'admission_outcome_unknown') this.sql.exec('DELETE FROM intake_queue_admissions WHERE task_id=? AND request_key=?',t.id,input.key);
      this.audit(fresh, 'restart_result', JSON.stringify({ state, reason, session_id: id || null, status: result.status }), this.now());
    });
  }
  /** Authorized callers pass only visible tasks; positions are relative to that view. */
  queue(tasks: Obj[], now = this.now(), includeHistory = true, context?:QueueSnapshot, projection?:Obj[]): Obj[] {
    const c=context||this.snapshot(tasks,now);
    const projections = projection||this.project(tasks, now,c);
    const byId = new Map(tasks.map(t => [t.id, t]));
    const queueRows = c.queue;
    // One capacity projection per read, rather than one fleet scan per intake.
    const policy = this.policy();
    const items = projections.map(p => {
      const t = byId.get(p.id)!, q = queueRows.get(p.id);
      const s = p.last_session;
      const done = intakeHasDoneEvidence(t);
      const completion = intakeCompletionHoldReason(t);
      let state = done ? 'done' : !DISPATCHABLE.has(t.status) ? 'needs_attention' : q?.state === 'escalated' ? 'escalated' : completion ? 'needs_attention' :
        p.state === 'unknown' || p.state === 'recovering' || (p.state === 'active' && s && !ACTIVE.has(s.state)) ? 'needs_attention' : (s ? s.state === 'running' : p.state === 'active') ? 'running' : 'queued';
      let reason = done ? 'done_note_with_live_evidence' : !DISPATCHABLE.has(t.status) ? 'intake_' + t.status : completion ?? s?.failure_reason ?? q?.reason ?? p.reason;
      if (state === 'running') reason = s ? 'canonical_' + s.state : 'live_native_owner';
      if (state === 'queued' && s?.hold_reason) reason = s.hold_reason;
      else if (state === 'queued' && s && q?.state !== 'recovering') reason = 'canonical_' + s.state;
      if (state === 'queued') reason = this.automationHold(t, policy) ?? reason;
      const history = this.rows('SELECT state,reason,at FROM intake_queue_movement WHERE task_id=? ORDER BY seq DESC LIMIT ?', p.id, includeHistory ? 32 : 1).reverse();
      const last = history[history.length - 1];
      const at = last?.at ?? ms(t.created_at);
      return { id: p.id, title: p.title, project: p.project, priority: byId.get(p.id)?.priority ?? 'normal',
        promoted_at: promotedAt(byId.get(p.id)!) || null,
        state, reason, session: s, attempts: q?.attempts ?? 0,
        retry_limit: QUEUE_RETRY_LIMIT, next_at: q?.next_at ?? now, last_movement_at: at,
        stuck: !done && now - at >= QUEUE_STALL_MS, history, queue_position: null as number | null,
        completion_evidence: done ? 'reported_live_proof_not_independently_verified' : null };
    }).sort((a,b) => Number(a.next_at > now) - Number(b.next_at > now) ||
      (queueRows.get(a.id)?.updated_at ?? 0) - (queueRows.get(b.id)?.updated_at ?? 0) || a.id.localeCompare(b.id));
    let position = 0;
    // Visible positions: due items lead, then the candidate order
    // (promoted > priority > newest) so the queue reads as it will run.
    const dueFirst = (item: Obj) => Number((queueRows.get(item.id)?.next_at ?? now) > now);
    for (const item of [...items].sort((a,b) => dueFirst(a) - dueFirst(b) ||
      queueCandidateOrder(byId.get(a.id)!, byId.get(b.id)!)))
      if (item.state === 'queued' && !this.automationHold(byId.get(item.id)!, policy)) item.queue_position = ++position;
    return items;
  }
  private async escalate(t: Obj, reason: string, now: number) {
    const q = this.queueRow(t.id);
    this.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at) VALUES(?, 'escalated', ?, ?, ?, ?)
      ON CONFLICT(task_id) DO UPDATE SET next_at=excluded.next_at,updated_at=CASE WHEN state<>'escalated' OR reason<>excluded.reason THEN excluded.updated_at ELSE updated_at END,state='escalated',reason=excluded.reason`,
      t.id, reason, now, now + QUEUE_RETRY_BASE_MS, now);
    this.movement(t, 'escalated', reason, now);
    if (q?.state !== 'escalated') this.audit(t, 'needs_attention', reason, now);
    // Idempotent hook retries delivery; neither an alert nor a note approves a prod gate.
    this.sql.exec('INSERT OR IGNORE INTO intake_queue_escalations(task_id,reason) VALUES(?,?)',t.id,reason);
    const sent = this.rows('SELECT * FROM intake_queue_escalations WHERE task_id=? AND reason=?',t.id,reason)[0];
    if (!sent.alert_sent && this.hooks.notify && await this.hooks.notify('intake_queue', t.id, `needs_attention: intake ${t.id}: ${reason}. Retry cap ${QUEUE_RETRY_LIMIT}; inspect canonical session evidence. Operational escalation, not NEEDS_ALEXEY.`) !== false)
      this.sql.exec('UPDATE intake_queue_escalations SET alert_sent=1 WHERE task_id=? AND reason=?',t.id,reason);
    if (!sent.note_sent && this.hooks.note && await this.hooks.note(t, `needs_attention: autonomous queue exhausted bounded recovery (${reason}; cap ${QUEUE_RETRY_LIMIT}). Intake remains OPEN; inspect session evidence and repair routing/runtime. NEEDS_ALEXEY is reserved for production approvals, secrets, deletions or 2FA.`, 'queue-escalated-' + t.id + '-' + reason))
      this.sql.exec('UPDATE intake_queue_escalations SET note_sent=1 WHERE task_id=? AND reason=?',t.id,reason);
  }
  private waiting(sessionId: string): boolean {
    return this.rows("SELECT id FROM session_control_wakes WHERE session_id=? AND state='pending' LIMIT 1",sessionId).length > 0;
  }
  private async reconcile(tasks: Obj[], now: number, context:QueueSnapshot) {
    if(context.overflow || !this.policy().enabled)return;
    const {catalogs,native,sessions}=context;
    const queueRows=context.queue;
    const ordered = tasks.filter(t => DISPATCHABLE.has(t.status) && !t.archived_at && !this.automationHold(t)).sort((a,b) =>
      (queueRows.get(a.id)?.updated_at ?? 0) - (queueRows.get(b.id)?.updated_at ?? 0) || a.id.localeCompare(b.id));
    let actions = 0, observations = 0;
    for (const t of ordered) {
      if (actions >= 8) break;
      if (intakeCompletionHoldReason(t)) continue;
      const q = queueRows.get(t.id);
      const links = sessions.filter(s => (s._request || parse(s.request_json)).intake_task_id === t.id)
        .sort((a,b) => ms(b.created_at) - ms(a.created_at));
      const s = links[0];
      if (!s || (!ACTIVE.has(s.state) && !s.binding_json)) {
        if (q?.state === 'escalated' && q.next_at <= now) { actions++; await this.escalate(t,q.reason,now); }
        continue;
      }
      const binding = parse(s.binding_json), n = native.find(n => n.machine_id === s.machine_id && n.native_id === binding.native_id);
      const progress = nativeProgress(n);
      const owned = s.actor === 'hub:service:intake-watchdog' && parse(s.request_json).with_goal === true;
      const matches = n && n.tag === binding.tag && n.workspace === binding.workspace && n.created_at_ms === binding.native_created_at_ms;
      const healthy = s.state === 'running' && matches && catalogs.has(s.machine_id) && !sessionTerminalError(s,n) &&
        !['unknown','unobserved','exited','stopped','failed'].includes(n.phase) && (n.agent_state === 'working' || (progress !== null && now - progress < QUEUE_STALL_MS));
      if (q && owned && healthy && (q.attempts > 0 || q.state === 'escalated')) {
        if (q.state === 'escalated') await this.hooks.notify?.('intake_queue',t.id,null);
        this.sql.exec("UPDATE intake_restart_queue SET state='submitted',reason='canonical_recovered',session_id=?,attempts=0,next_at=?,updated_at=? WHERE task_id=?",s.id,now + QUEUE_RETRY_BASE_MS,now,t.id);
        this.movement(t,'running','canonical_recovered',now);
        continue;
      }
      if (q && q.next_at > now) continue;
      if (q?.state === 'escalated') { actions++; await this.escalate(t, q.reason, now); continue; }
      if (context.wakes.some((w:Obj)=>w.session_id===s.id && w.state==='pending')) continue;
      const terminal = matches && s.mode==='interactive' && s.engine===n.engine && nativeTerminalError(n);
      const sampled = parse(n?.progress_json);
      const sampledProgress = typeof sampled.last_progress_at === 'string' ? Date.parse(sampled.last_progress_at) : NaN;
      const idleCandidate = n?.agent_state === 'idle' && Number.isFinite(sampledProgress) && now - sampledProgress > QUEUE_STALL_MS;
      const gapCandidate = owned && ['uncertain','starting','running','offered'].includes(s.state) && now - ms(s.updated_at) > QUEUE_STALL_MS &&
        (s.state !== 'running' || !binding.native_id || !matches || !catalogs.has(s.machine_id) || ['unknown','unobserved'].includes(n?.phase));
      let observed: Obj | null = null;
      if (owned && (idleCandidate || gapCandidate || FAILED.has(s.state)) && this.hooks.observe) {
        if(observations>=8)continue;observations++;
        try { const r = await this.hooks.observe(s.id); if (r.status === 200) observed = r.body; } catch { /* unknown is not a stop */ }
      }
      if (observed?.waiting_until || observed?.stall_reason === 'waiting_until') continue;
      const stalled = owned && (['running','starting','offered','uncertain'].includes(s.state) || FAILED.has(s.state)) && matches && catalogs.has(s.machine_id) &&
        !['unknown','unobserved','exited','stopped','failed'].includes(n.phase) &&
        (terminal || observed?.stall_reason==='agent_exited_children_running' || (idleCandidate && ["no_native_progress", "hung_tool_call"].includes(observed?.stall_reason as string) && (observed?.stalled === true || s.state !== 'running')));
      const held = owned && s.state === 'held' && !binding.native_id;
      // An unproven launch is observed with backoff, never stopped on age.
      // Missing receipts cannot authorize replacing a possibly live worker.
      if (gapCandidate && !held && !stalled) {
        actions++;
        const attempts = q?.attempts ?? 0;
        if (attempts >= QUEUE_RETRY_LIMIT) { await this.escalate(t,'launch_observation_retry_limit',now);continue; }
        this.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,session_id,attempts,held_since,next_at,updated_at)
          VALUES(?,'recovering','awaiting_host_receipt',?,1,?,?,?) ON CONFLICT(task_id) DO UPDATE SET state='recovering',reason='awaiting_host_receipt',session_id=excluded.session_id,attempts=attempts+1,next_at=excluded.next_at,updated_at=excluded.updated_at`,
          t.id,s.id,now,now+retryDelay(attempts+1),now);
        this.audit(t,'queue_observation','canonical launch/native receipt still unproven; no stop or replacement',now);
        this.movement(t,'recovering','awaiting_host_receipt',now);
        continue;
      }
      if (!held && !stalled) continue;
      if (!this.hooks.command) continue;
      actions++;
      const attempts = q?.attempts ?? 0;
      if (attempts >= QUEUE_RETRY_LIMIT) { await this.escalate(t, held ? 'held_retry_limit' : 'stall_retry_limit', now); continue; }
      const latestTask=this.task(t.id);
      if(!latestTask || !DISPATCHABLE.has(latestTask.status) || this.automationHold(latestTask) || this.waiting(s.id))continue;
      const action = held ? 'retry' : 'stop';
      const key = 'iw-' + action + '-' + s.id;
      this.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,session_id,attempts,held_since,next_at,updated_at)
        VALUES(?,'recovering',?,?,1,?,?,?) ON CONFLICT(task_id) DO UPDATE SET state='recovering',reason=excluded.reason,session_id=excluded.session_id,attempts=attempts+1,next_at=excluded.next_at,updated_at=excluded.updated_at`,
        t.id, action === 'stop' ? 'awaiting_confirmed_stop' : 'rechecking_admission', s.id, now, now + retryDelay(attempts + 1), now);
      try {
        const result = await this.hooks.command(s.id, action, key);
        this.audit(t, 'queue_recovery', JSON.stringify({ action, session_id:s.id, status:result.status }), now);
      } catch { this.audit(t, 'queue_recovery', 'canonical_' + action + '_outcome_unknown', now); }
      this.movement(t, 'recovering', action === 'stop' ? 'stop_requested_waiting_for_exit' : 'admission_rechecked', now);
    }
  }
  /** Every restart row ends in progress or a visible failure (intake 0841a966
   * AC1/AC2). A submitted row describes its admission-time session: once that
   * session is terminal the row is spent, so it requeues for a fresh bounded
   * admission instead of reporting canonical_offered/held forever. Rows whose
   * task no longer accepts restarts close with a reason; a queued hold older
   * than QUEUE_HOLD_TTL_MS escalates instead of waiting indefinitely. */
  private async sweepQueue(tasks: Obj[], now: number, context: QueueSnapshot) {
    if (context.overflow) return;
    const byId = new Map(tasks.map(t => [t.id, t]));
    let actions = 0;
    for (const q of context.queue.values()) {
      if (actions >= QUEUE_SWEEP_LIMIT) break;
      const t = byId.get(q.task_id);
      if (!t || t.archived_at) continue; // out-of-batch rows: table sweep below
      if (q.state === 'submitted' && q.session_id) {
        if (!DISPATCHABLE.has(t.status)) { actions++; this.closeRestart(t, q, 'intake_' + t.status, now); continue; }
        const s = context.sessions.find(s => s.id === q.session_id);
        if (s && !SPENT_SESSION_STATES.has(s.state)) continue;
        actions++;
        // Immediately eligible again: the spent session already consumed one
        // bounded admission; the attempts cap still bounds repeat failures.
        const reason = s ? 'previous_session_' + s.state : 'previous_session_missing';
        this.sql.exec(`UPDATE intake_restart_queue SET state='queued',reason=?,held_since=?,next_at=?,updated_at=?,session_id=NULL
          WHERE task_id=? AND state='submitted'`, reason, now, now, now, t.id);
        this.movement(t, 'queued', reason, now);
        this.audit(t, 'restart_requeued', reason, now);
      } else if (q.state !== 'escalated' && !DISPATCHABLE.has(t.status)) {
        actions++; this.closeRestart(t, q, 'intake_' + t.status, now);
      } else if (q.state === 'queued' && !HOLD_TTL_EXEMPT.has(q.reason) &&
        (q.held_since ?? q.updated_at) <= now - QUEUE_HOLD_TTL_MS) {
        // In-batch holds age out too: the escalation is the bounded, visible
        // failure for work the queue could not progress (AC2).
        actions++;
        await this.escalate(t, 'hold_ttl_' + q.reason, now);
      }
    }
    // Rows for tasks outside the batch (done or archived intakes) never
    // reappear in a cursor range. The scan is a raw keyset window over the queue
    // PK: at most QUEUE_SWEEP_LIMIT*2 rows examined per tick regardless of how
    // many are actionable (review F2 of intake 0841a966 — an OR predicate here
    // would defeat every index and walk the table on quiet ticks). The cursor
    // advances over raw task_id order, so every row is examined exactly once per
    // ceil(N/window) ticks and nothing hides behind non-matching rows (F1).
    const windowSize = QUEUE_SWEEP_LIMIT * 2;
    const sweepAfter = (): string => this.rows('SELECT sweep_after FROM intake_queue_cursor WHERE id=1')[0]?.sweep_after ?? '';
    const fetchWindow = (from: string): Obj[] => this.rows(`SELECT q.task_id,q.state,q.reason,q.held_since,q.updated_at,
        json_extract(p.data,'$.status') AS status,json_extract(p.data,'$.archived_at') AS archived_at,json_extract(p.data,'$.project') AS project
      FROM intake_restart_queue q LEFT JOIN intake_task_projection p ON p.id=q.task_id
      WHERE q.task_id > ? ORDER BY q.task_id LIMIT ?`, from, windowSize);
    const page = fetchWindow(sweepAfter());
    const batchIds = new Set(tasks.map(t => t.id));
    let escalations = 0, lastExamined: string | null = null;
    for (const r of page) {
      if (actions >= QUEUE_SWEEP_LIMIT) break; // unprocessed rows stay ahead of the cursor
      lastExamined = r.task_id;
      if (batchIds.has(r.task_id)) continue; // the batch part owns its own rows
      if (!r.status || r.archived_at || !OPEN.has(r.status)) {
        actions++;
        const t = { id: r.task_id, project: r.project ?? 'unknown', status: r.status ?? 'missing', title: '' };
        this.closeRestart(t, r, r.archived_at ? 'archived' : 'intake_' + (r.status ?? 'missing'), now);
        continue;
      }
      if (r.state !== 'queued' || HOLD_TTL_EXEMPT.has(r.reason)) continue;
      if ((r.held_since ?? r.updated_at) > now - QUEUE_HOLD_TTL_MS) continue;
      if (!DISPATCHABLE.has(r.status)) { actions++; this.closeRestart({ id: r.task_id, project: r.project ?? 'unknown', status: r.status, title: '' }, r, 'intake_' + r.status, now); continue; }
      if (escalations >= 8) continue;
      const t = this.task(r.task_id);
      if (!t || !DISPATCHABLE.has(t.status) || t.archived_at) continue;
      actions++; escalations++;
      await this.escalate(t, 'hold_ttl_' + r.reason, now);
    }
    // A short window ends the cycle: restart from the top next tick. A break on
    // the actions cap leaves the cursor behind the unprocessed rows.
    this.sql.exec('UPDATE intake_queue_cursor SET sweep_after=? WHERE id=1',
      page.length < windowSize ? '' : (lastExamined ?? sweepAfter()));
  }
  private closeRestart(t: Obj, q: Obj, why: string, now: number) {
    this.movement(t, 'closed', q.reason + ' closed: ' + why, now);
    this.audit(t, 'restart_closed', `${q.state}:${q.reason} closed (${why})`, now);
    this.sql.exec('DELETE FROM intake_restart_queue WHERE task_id=?', t.id);
  }
  digest(items: Obj[]): string {    const buckets = ['<1d', '1–3d', '3–7d', '>7d'];
    return buckets.map((bucket, index) => {
      const group = items.filter(t => (t.age_ms < DAY ? 0 : t.age_ms < 3 * DAY ? 1 : t.age_ms <= 7 * DAY ? 2 : 3) === index);
      return bucket + ' (' + group.length + ')\n' + group.map(t => `${t.forgotten ? '[FORGOTTEN] ' : ''}${t.id} ${String(t.title).replace(/[\r\n]/g, ' ')} — ${Math.floor(t.age_ms / 3600000)}h; ${t.state}; ${t.reason}`).join('\n');
    }).join('\n\n');
  }
  async tick(source = 'scheduled') {
    const began=performance.now(),now=this.now();
    const acquired=this.tx(()=>{
      const m=this.rows('SELECT * FROM intake_watchdog_meta WHERE id=1')[0];
      if(m.last_tick!==null && now-m.last_tick<WATCHDOG_INTERVAL)return false;
      this.sql.exec('UPDATE intake_watchdog_meta SET last_tick=?,last_source=? WHERE id=1',now,source);return true;
    });
    if(!acquired)return {state:'already_checked'};
    const {tasks,wrapped}=this.batch(now);
    const context=this.snapshot(tasks,now);
    if(context.overflow)await this.reportObservationLimit(tasks,now);
    const items=this.project(tasks,now,context),forgotten=items.filter(i=>i.forgotten);
    const meta=this.rows('SELECT * FROM intake_watchdog_meta WHERE id=1')[0];
    const seen=this.rows('SELECT forgotten_seen FROM intake_queue_cursor WHERE id=1')[0].forgotten_seen || forgotten.length>0;
    const since=forgotten.length ? (meta.nonzero_since??now) : wrapped && !seen ? null : meta.nonzero_since;
    this.sql.exec('UPDATE intake_watchdog_meta SET nonzero_since=? WHERE id=1',since);
    this.sql.exec('UPDATE intake_queue_cursor SET forgotten_seen=? WHERE id=1',wrapped?0:Number(!!seen));
    if(this.hooks.notify) {
      const detail=forgotten.length && since!==null && now-since>DAY ? `${forgotten.length} forgotten intakes observed in this bounded batch for >24h. Review hub intake forgotten. IDs: `+forgotten.slice(0,20).map(t=>t.id).join('; ') : null;
      const opened=await this.hooks.notify('intake_watchdog','forgotten-over-24h',detail);
      if(detail && opened!==false)for(const item of forgotten)this.audit(tasks.find(t=>t.id===item.id)!,'forgotten_alert','sustained forgotten work observed in bounded batch',now);
      // Live invariant (intake b527e905 AC5): an open intake with no live or
      // queued session for >30 minutes is a real violation. The alert names the
      // intake, its last session end reason and why no restart happened.
      const violations=items.filter(t=>(t.forgotten||t.state==='recovering')&&(t.idle_ms??0)>1800000);
      if(violations.length) {
        const idleDetail=`${violations.length} open intake(s) without a live or queued session for >30 min. `+violations.slice(0,20).map(t=>
          `${t.id}; last session=${t.last_session?.id??'none'} state=${t.last_session?.state??'none'} end_reason=${t.last_session?.end_reason??'unknown'} hold_reason=${t.last_session?.hold_reason??'none'}; restart=${t.restart?.reason??t.reason}`).join('\n');
        const idleOpened=await this.hooks.notify('intake_watchdog','idle-over-30m',idleDetail);
        if(idleOpened!==false)for(const item of violations)this.audit(tasks.find(t=>t.id===item.id)!,'forgotten_alert','idle >30 minutes; routed to existing alerts',now);
      }
      const day=new Date(now).toISOString().slice(0,10);
      const done=new Set(this.indexed('intake_queue_digest','task_id',tasks.map(t=>t.id)).filter(r=>r.day===day).map(r=>r.task_id));
      const due=items.filter(t=>!done.has(t.id));
      if(due.length) {
        const unmerged=this.intake.mergeGate?.unmerged(20)??[];
        // The ask ledger line rides on the day's first digest batch only.
        const asksLine=meta.digest_day!==day ? this.hooks.asksDigest?.() : null;
        const text='Daily open intake digest '+day+' (bounded batch)\n'+(asksLine ? asksLine+'\n\n' : '')+this.digest(due)+(unmerged.length?'\n\nUNMERGED intakes ('+this.intake.mergeGate.counts().total_unmerged+'): work not in origin/main or not deployed\n'+unmerged.map(m=>`${m.task_id} ${m.state}; requeues ${m.requeues}; ${m.problems.join('; ')}`).join('\n'):'');
        const chunks:string[]=[];
        for(const line of text.split('\n')) {
          const tail=chunks.length-1;
          if(tail>=0 && chunks[tail].length+line.length+1<=1800)chunks[tail]+='\n'+line;
          else for(let offset=0;offset<line.length;offset+=1800)chunks.push(line.slice(offset,offset+1800));
        }
        for(let i=0;i<chunks.length;i++)await this.hooks.notify('intake_digest',day+'-'+due[0].id+'-part-'+(i+1),chunks[i]);
        for(const item of due) {
          this.sql.exec('INSERT INTO intake_queue_digest(task_id,day) VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET day=excluded.day',item.id,day);
          this.audit(tasks.find(t=>t.id===item.id)!,'daily_digest','daily bounded-batch digest recorded; receipt not confirmed',now);
        }
        this.sql.exec('UPDATE intake_watchdog_meta SET digest_day=? WHERE id=1',day);
      } else if(meta.digest_day!==day) {
        // Nothing due today: the ask ledger line still goes out once.
        const asksLine=this.hooks.asksDigest?.();
        if(asksLine)await this.hooks.notify('intake_digest',day+'-asks','Daily open intake digest '+day+'\n'+asksLine);
        this.sql.exec('UPDATE intake_watchdog_meta SET digest_day=? WHERE id=1',day);
      }
    }
    // Persist one projection before awaited mutations. Their specific movement
    // events follow it; a new intake/session is observed on the next cursor turn.
    for(const item of this.queue(tasks,now,false,context,items)) {
      if(item.state==='running' && item.session?.state==='running') {
        const q=context.queue.get(item.id),reason='canonical_running:'+item.session.id;
        if(q?.session_id===item.session.id && this.rows(`SELECT id FROM session_control_sessions WHERE id<>? AND ${requestIntake}=? LIMIT 1`,item.session.id,item.id).length &&
          !this.rows("SELECT seq FROM intake_queue_movement WHERE task_id=? AND state='restarted' AND reason=? LIMIT 1",item.id,reason).length)this.movement(item,'restarted',reason,now);
      }
      this.movement(item,item.state,item.reason,now);
    }
    // Quarantine proven launcher-failure streaks and stop stalled-but-alive
    // native work first (intake b527e905 AC1/AC2): the projection above is
    // pre-stop, so a just-stopped session restarts on the next cursor turn,
    // well inside the 30-minute invariant.
    this.quarantineFailedLaunchers(now);
    await this.recoverStalls(tasks, now, context);
    await this.reconcile(tasks,now,context);
    await this.sweepQueue(tasks,now,context);
    const dispatchRows=new Map(tasks.map(t=>[t.id,this.queueRow(t.id)]));
    const ordered=items.filter(i=>{
      const q=dispatchRows.get(i.id);
      const task=tasks.find(t=>t.id===i.id);
      // Recovering episodes are candidates too: the canonical stop or hold
      // cancellation above may have settled this tick, and restart re-checks
      // readiness against a fresh snapshot before admitting anything. A
      // promoted intake overrides a retry backoff (never an escalation);
      // candidate order is promoted > priority > newest (queueCandidateOrder).
      return task && !this.automationHold(task) && (i.ready||i.state==='recovering') && q?.state!=='escalated' &&
        (!q || q.next_at<=now || promotedAt(task));
    }).sort((a,b)=>queueCandidateOrder(tasks.find(t=>t.id===a.id)!,tasks.find(t=>t.id===b.id)!));
    for(const item of ordered.slice(0,8))await this.restart(tasks.find(t=>t.id===item.id)!,now);
    const human_hold_alerts=await this.alertHumanHolds(tasks,now);
    let asks:Obj|undefined;
    if(this.hooks.asks) {
      try { asks=await this.hooks.asks(now); }
      catch(e:any) { asks={state:'error',error:String(e?.message||e).slice(0,200)}; }
    }
    const result={state:'checked',checked_at:now,forgotten_count:forgotten.length,open_count:items.length,human_hold_alerts,...(asks?{asks}:{}),
      observation_scope:'cursor_batch',batch_limit:QUEUE_BATCH_SIZE,restart_priority_limit:8,observed_sessions:context.sessions.length,observed_native:context.native.length,observation_limited:context.overflow,elapsed_ms:performance.now()-began};
    this.sql.exec('UPDATE intake_watchdog_meta SET last_result=? WHERE id=1',JSON.stringify(result));return result;
  }
}
