/**
 * Independent liveness for the ops orchestrator and the open-intake backlog (intake 500fd60b).
 *
 * The orchestrator session reports each tick and the host ops-watchdog reports every run.
 * This minute-alarm evaluation lives in Hub, so it does not depend on either reporter:
 * a dead session, a dead watchdog or a dead host all show up as stale reports.
 *
 * Backlog check: open intakes with AC and no intake-linked session starting or running for
 * 30 minutes is an idle episode. Hub records why it was idle and starts one intake itself
 * through the canonical queue admission path, at most once per 30 minutes.
 *
 * Alerts (Alexey's rule: only real problems) open only after automatic recovery failed
 * twice in a row: the watchdog reports two consecutive failed recoveries, host-side
 * recovery stays silent for two 30-minute windows, or two idle kicks in a row did not
 * produce a working session.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { nativeObservationFresh, nativeProgress } from './session_stall';

export const OPS_ROLES = ['orchestrator', 'oncall'] as const;
/** Roles that must report: silence counts from Hub's first evaluation even with no row. */
export const EXPECTED_ROLES = new Set(['orchestrator']);
export const OPS_WINDOW_MS = 30 * 60000;
export const OPS_EVAL_INTERVAL_MS = 60000;
const WATCHDOG_STATES = new Set(['ok', 'stalled', 'dead', 'recovering', 'proof_pending', 'proof_failed', 'census_failed', 'restart_failed']);
const ACTIVE_STATES = "('starting','offered','running')";
const NATIVE_STALE_MS = 10 * 60000;
const requestIntake = "json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.intake_task_id')";

/** Per metric, read one overflow sentinel beyond this budget. Never publish
 * a prefix as an exact daily count or use unknown counts to clear an alert. */
export const THROUGHPUT_OBSERVATION_LIMIT = 2048;
const nativeStartedAt = "json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_created_at_ms')";
export const THROUGHPUT_QUERIES = {
  starts: `SELECT ${requestIntake} AS intake_id,actor,
    json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_id') AS native_id
    FROM session_control_sessions INDEXED BY ops_intake_native_started
    WHERE ${nativeStartedAt}>=? AND ${nativeStartedAt}<? LIMIT ${THROUGHPUT_OBSERVATION_LIMIT + 1}`,
  created: `SELECT id FROM intake_task_projection INDEXED BY ops_intake_created
    WHERE json_extract(data,'$.created_at')>=? AND json_extract(data,'$.created_at')<? LIMIT ${THROUGHPUT_OBSERVATION_LIMIT + 1}`,
  closed: `SELECT task_id FROM intake_audit INDEXED BY ops_intake_closed_time
    WHERE captured_at>=? AND captured_at<? AND kind IN ('close','cancel') LIMIT ${THROUGHPUT_OBSERVATION_LIMIT + 1}`,
};

type Obj = Record<string, any>;
export type KickResult = { task_id: string | null; reason: string };
export type OpsLivenessHooks = {
  /** Start one ready intake through canonical admission; returns what happened. */
  kick?: (reason: Obj) => Promise<KickResult>;
  /** Open (detail) or resolve (null) one named alert; returns true when newly opened. */
  alert?: (subject: string, detail: string | null) => Promise<boolean | void>;
  /** Why queued intakes wait, for the idle record. */
  holdReasons?: () => Obj;
  enabled?: () => boolean;
  /** The same live queue budget used by automatic admission. */
  queuePolicy?: () => any;
};

const iso = (ms: number | null | undefined) => typeof ms === 'number' && ms > 0 ? new Date(ms).toISOString() : null;
const age = (now: number, at: number | null | undefined) => typeof at === 'number' && at > 0 ? Math.max(0, Math.round((now - at) / 1000)) : null;

export class OpsLiveness {
  constructor(private sql: SqlStorage, private hooks: OpsLivenessHooks = {}, private now = Date.now) {}

  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS ops_liveness(role TEXT PRIMARY KEY, tick_at INTEGER, tick_summary TEXT,
      watchdog_at INTEGER, watchdog_state TEXT, recovery_failures INTEGER NOT NULL DEFAULT 0, detail TEXT, updated_at INTEGER NOT NULL)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS ops_backlog_idle(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL,
      resolved_at INTEGER, open_with_ac INTEGER NOT NULL, reason_json TEXT NOT NULL, kicks INTEGER NOT NULL DEFAULT 0,
      failed_kicks INTEGER NOT NULL DEFAULT 0, last_kick_at INTEGER, last_kick_json TEXT)`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS ops_backlog_idle_open ON ops_backlog_idle(resolved_at, id)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS ops_liveness_meta(id INTEGER PRIMARY KEY CHECK(id=1), last_eval INTEGER, expected_since INTEGER)');
    this.sql.exec('INSERT OR IGNORE INTO ops_liveness_meta(id,last_eval,expected_since) VALUES(1,NULL,?)', this.now());
    const cols = (this.sql.exec('PRAGMA table_info(ops_liveness)').toArray() as any[]).map(c => c.name);
    if (!cols.includes('first_seen_at')) this.sql.exec('ALTER TABLE ops_liveness ADD COLUMN first_seen_at INTEGER');
    // A one-row projection captures future work ends without adding an
    // index over retained sessions. Its bootstrap time gives a conservative
    // one-window grace: unseen pre-bootstrap work cannot be newer than it.
    this.sql.exec('CREATE TABLE IF NOT EXISTS ops_work_observation(id INTEGER PRIMARY KEY CHECK(id=1),started_at INTEGER NOT NULL,last_terminal_at INTEGER NOT NULL)');
    const endedAt = "ROUND((julianday(NEW.updated_at)-2440587.5)*86400000)";
    const linked = requestIntake.replaceAll('request_json','NEW.request_json');
    for (const event of ['INSERT','UPDATE OF state,updated_at']) {
      const name = event === 'INSERT' ? 'ops_work_ended_insert' : 'ops_work_ended_update';
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS ${name} AFTER ${event} ON session_control_sessions
        WHEN NEW.state NOT IN ${ACTIVE_STATES} AND ${linked} IS NOT NULL AND ${endedAt} IS NOT NULL
        BEGIN UPDATE ops_work_observation SET last_terminal_at=MAX(last_terminal_at,${endedAt})
          WHERE id=1 AND last_terminal_at<${endedAt}; END`);
    }
    this.sql.exec('INSERT OR IGNORE INTO ops_work_observation VALUES(1,?,?)',this.now(),this.now());
    this.sql.exec("CREATE INDEX IF NOT EXISTS ops_intake_created ON intake_task_projection(json_extract(data,'$.created_at'))");
    this.sql.exec("CREATE INDEX IF NOT EXISTS ops_intake_closed_time ON intake_audit(captured_at,task_id) WHERE kind IN ('close','cancel')");
    this.sql.exec(`CREATE INDEX IF NOT EXISTS ops_intake_native_started ON session_control_sessions(
      json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_created_at_ms'))`);

  }

  /** One report from the orchestrator tick (kind=tick) or the host watchdog (kind=watchdog). */
  record(input: any): { ok: true } | { error: string } {
    if (!input || typeof input !== 'object' || Array.isArray(input)) return { error: 'invalid_ops_heartbeat' };
    const allowed = ['role', 'kind', 'state', 'recovery_failures', 'tick_at', 'summary'];
    if (!Object.keys(input).every(k => allowed.includes(k))) return { error: 'invalid_ops_heartbeat' };
    const { role, kind } = input;
    if (!OPS_ROLES.includes(role)) return { error: 'invalid_role' };
    if (kind !== 'tick' && kind !== 'watchdog') return { error: 'invalid_kind' };
    if (input.summary !== undefined && (typeof input.summary !== 'string' || input.summary.length > 240 || /[\x00-\x1f\x7f]/.test(input.summary))) return { error: 'invalid_summary' };
    const now = this.now();
    // A relayed tick time is the tick log's age as the watchdog saw it; never from the future.
    let relayed: number | null = null;
    if (input.tick_at !== undefined && input.tick_at !== null) {
      const t = typeof input.tick_at === 'string' ? Date.parse(input.tick_at) : NaN;
      if (!Number.isFinite(t) || t > now + 120000) return { error: 'invalid_tick_at' };
      relayed = Math.min(t, now);
    }
    this.sql.exec('INSERT OR IGNORE INTO ops_liveness(role,updated_at,first_seen_at) VALUES(?,?,?)', role, now, now);
    if (kind === 'tick') {
      if (input.state !== undefined || input.recovery_failures !== undefined) return { error: 'invalid_ops_heartbeat' };
      this.sql.exec('UPDATE ops_liveness SET tick_at=?, tick_summary=?, updated_at=? WHERE role=?', relayed ?? now, input.summary ?? null, now, role);
      return { ok: true };
    }
    const state = input.state ?? 'ok';
    if (!WATCHDOG_STATES.has(state)) return { error: 'invalid_state' };
    const failures = input.recovery_failures ?? 0;
    if (!Number.isInteger(failures) || failures < 0 || failures > 1000) return { error: 'invalid_recovery_failures' };
    this.sql.exec(`UPDATE ops_liveness SET watchdog_at=?, watchdog_state=?, recovery_failures=?, detail=?, updated_at=?,
      tick_at=CASE WHEN ? IS NOT NULL AND (tick_at IS NULL OR ? > tick_at) THEN ? ELSE tick_at END WHERE role=?`,
      now, state, failures, input.summary ?? null, now, relayed, relayed, relayed, role);
    return { ok: true };
  }

  private rows(q: string, ...a: any[]): Obj[] { return this.sql.exec(q, ...a).toArray() as Obj[]; }

  private expectedSince(): number | null {
    return this.rows('SELECT expected_since FROM ops_liveness_meta WHERE id=1')[0]?.expected_since ?? null;
  }

  /** Classification of one role; also used by the read endpoint. A reporter that never
   * reported is silent since the role was first seen, or since Hub started expecting it. */
  roleStatus(role: string, r: Obj | undefined, now = this.now()): Obj {
    const since = r?.first_seen_at ?? r?.updated_at ?? (EXPECTED_ROLES.has(role) ? this.expectedSince() : null);
    if (!r && since === null) return { state: 'never_reported', alert: null };
    const tickAge = age(now, r?.tick_at), watchdogAge = age(now, r?.watchdog_at);
    const tickSilent = age(now, r?.tick_at ?? since) ?? 0, watchdogSilent = age(now, r?.watchdog_at ?? since) ?? 0;
    const tickStale = tickSilent * 1000 > OPS_WINDOW_MS, watchdogStale = watchdogSilent * 1000 > OPS_WINDOW_MS;
    const state = tickStale && watchdogStale ? 'tick_and_watchdog_stale' : tickStale ? 'tick_stale' : watchdogStale ? 'watchdog_stale' : 'ok';
    const last = tickAge === null ? 'never' : tickAge + 's ago';
    let alert: string | null = null;
    if (!watchdogStale && (r?.recovery_failures ?? 0) >= 2)
      alert = `automatic recovery failed ${r!.recovery_failures} times in a row (watchdog state ${r!.watchdog_state}); last tick ${last}. Inspect journalctl --user -u ops-watchdog.service on the host.`;
    else if (watchdogSilent * 1000 > 2 * OPS_WINDOW_MS)
      alert = `host ops-watchdog ${watchdogAge === null ? 'never reported' : 'silent'} for ${watchdogSilent}s (two 30-minute recovery windows missed); last tick ${last}. Check ops-watchdog.timer and the host.`;
    return { state: r ? state : 'never_reported_' + state, tick_at: iso(r?.tick_at), tick_age_s: tickAge, tick_summary: r?.tick_summary ?? null,
      watchdog_at: iso(r?.watchdog_at), watchdog_age_s: watchdogAge, watchdog_state: r?.watchdog_state ?? null,
      recovery_failures: r?.recovery_failures ?? 0, alert };
  }

  /** Bounded backlog view: open intakes with AC, and intake sessions actually working.
   * Work needs fresh evidence on an open/in-progress intake: an offered/starting row updated
   * within the window, or a running row whose bound native session is observed live on a
   * fresh host AND made host-sampled progress within the window. A fresh observation of an
   * idle process is not work. `last_work_at` is the newest such evidence (including recent
   * session ends), so the idle clock starts at the last useful work, not at detection. */
  backlog(now = this.now()): { open_with_ac: number; active_sessions: number; last_work_at: number | null; truncated?: boolean; open_with_ac_truncated?: boolean } {
    // Deployed indexes provide bounded raw candidates; AC and intake-link
    // postfilters cannot force scans of retained closed/unlinked history.
    const openRows = this.rows(`SELECT data FROM intake_task_projection INDEXED BY intake_watchdog_open
      WHERE json_extract(data,'$.status') IN ('open','in_progress','blocked','pending_approval')
      AND json_extract(data,'$.archived_at') IS NULL ORDER BY id LIMIT 1001`);
    const open = openRows.slice(0,1000).filter(row => {
      const task = JSON.parse(row.data);
      return ['open','in_progress'].includes(task.status) && String(task.acceptance_criteria ?? '').trim().length > 0;
    }).length;
    const raw: Obj[] = [];
    let activeTruncated = false;
    for (const state of ['starting','offered','running']) {
      const page = this.rows(`SELECT state,updated_at,machine_id,${requestIntake} AS intake_id,
        CASE WHEN json_valid(binding_json) THEN json_extract(binding_json,'$.native_id') END AS native_id
        FROM session_control_sessions INDEXED BY session_control_state WHERE state=? ORDER BY rowid LIMIT 68`,state);
      if (page.length > 67) activeTruncated = true;
      raw.push(...page.slice(0,67));
    }
    const candidates = raw.filter(row => row.intake_id && this.rows(`SELECT 1 FROM intake_task_projection
      WHERE id=? AND json_extract(data,'$.status') IN ('open','in_progress')`,row.intake_id).length);
    const cache = new Map();
    let active = 0, lastWork: number | null = null;
    const saw = (t: number | null) => { if (t !== null && Number.isFinite(t) && t <= now) lastWork = lastWork === null ? t : Math.max(lastWork, t); };
    for (const c of candidates) {
      if (c.state !== 'running') {
        const at = Date.parse(c.updated_at) || 0;
        saw(at);
        if (now - at <= OPS_WINDOW_MS) active++;
        continue;
      }
      if (!c.native_id) continue;
      const native = this.rows(`SELECT * FROM fleet_sessions WHERE machine_id=? AND native_id=?
        AND phase NOT IN ('exited','stopped','failed','dead','pruned','forgotten') LIMIT 1`, c.machine_id, c.native_id)[0];
      if (!native || now - (Date.parse(native.observed_at) || 0) > NATIVE_STALE_MS || !nativeObservationFresh(this.sql, native, now, cache)) continue;
      const progress = nativeProgress(native);
      saw(progress);
      if (progress !== null && now - progress <= OPS_WINDOW_MS) active++;
    }
    // Creation-ordered raw candidates are bounded before terminal/intake
    // filters. A prefix cannot prove that no recent linked work exists.
    const endedCandidates = this.rows(`SELECT state,updated_at,${requestIntake} AS intake_id
      FROM session_control_sessions INDEXED BY session_control_created ORDER BY created_at DESC,id DESC LIMIT 33`);
    for (const r of endedCandidates.slice(0,32)) if (!['starting','offered','running'].includes(r.state) && r.intake_id) saw(Date.parse(r.updated_at) || null);
    const observation = this.rows('SELECT started_at,last_terminal_at FROM ops_work_observation WHERE id=1')[0];
    const endedPartial = endedCandidates.length > 32;
    // Small complete registries preserve exact historical evidence. For a
    // large registry, the projection proves all post-bootstrap ends; after
    // one idle window, omitted older history cannot justify another delay.
    if (endedPartial && observation) saw(Number(observation.last_terminal_at));
    const endedUnknown = endedPartial && (!observation || now - Number(observation.started_at) < OPS_WINDOW_MS);
    return { open_with_ac: open, active_sessions: active, last_work_at: lastWork,
      ...(activeTruncated || endedUnknown ? { truncated: true } : {}),
      ...(openRows.length > 1000 ? { open_with_ac_truncated: true } : {}) };

  }

  /** Rolling [now-24h, now) counts. A native binding proves a start; queue creation
   * is not execution. Verified closes and cancellations come from immutable audit,
   * excluding archive duplicates and operator-unverified DONE transitions. */
  throughput(now = this.now()): Obj {
    try {
      const since = now - 86400000;
      const starts = this.rows(THROUGHPUT_QUERIES.starts, since, now);
      const creations = this.rows(THROUGHPUT_QUERIES.created, since, now);
      const closes = this.rows(THROUGHPUT_QUERIES.closed, since, now);
      const startsComplete = starts.length <= THROUGHPUT_OBSERVATION_LIMIT;
      const created = creations.length <= THROUGHPUT_OBSERVATION_LIMIT ? creations.length : null;
      const closed = closes.length <= THROUGHPUT_OBSERVATION_LIMIT ? new Set(closes.map(r => r.task_id)).size : null;
      const started = new Set(), automatic = new Set();
      if (startsComplete) for (const row of starts) {
        if (row.native_id == null || row.intake_id == null) continue;
        started.add(row.intake_id);
        if (row.actor === 'hub:service:intake-watchdog') automatic.add(row.intake_id);
      }
      const incomplete = [!startsComplete && 'started_24h', !startsComplete && 'automatic_started_24h',
        created === null && 'created_24h', closed === null && 'closed_24h',
        (created === null || closed === null) && 'net_24h'].filter(Boolean);
      const policy = this.hooks.queuePolicy?.();
      const capacity = policy?.capacity;
      const slots = capacity && policy.enabled && policy.valid
        ? Math.max(0, Math.min(capacity.available_slots, policy.max_inflight - capacity.active)) : null;
      const active = capacity?.active ?? null;
      const alertEvaluable = slots !== null && created !== null && closed !== null;
      const alert = alertEvaluable && slots > 0 && created > closed
        ? `intake backlog grew over 24h (created ${created} > closed ${closed}) with ${slots} automatic queue slot(s) available.` : null;
      return { window_start: iso(since), window_end: iso(now), window_s: 86400,
        started_24h: startsComplete ? started.size : null, automatic_started_24h: startsComplete ? automatic.size : null,
        created_24h: created, closed_24h: closed, net_24h: created !== null && closed !== null ? created - closed : null,
        complete: incomplete.length === 0, incomplete_metrics: incomplete, observation_limit: THROUGHPUT_OBSERVATION_LIMIT,
        alert_evaluable: alertEvaluable,
        active_vs_capacity: { active_sessions: active, effective_capacity: policy?.max_inflight ?? null,
          idle_capacity: slots, source: capacity?.source ?? 'unknown' },
        holds_by_reason: this.safeHolds(), alert };
    } catch { return { error: 'throughput_unavailable' }; }
  }

  status(now = this.now()): Obj {
    const roles: Obj = {};
    const byRole = new Map(this.rows('SELECT * FROM ops_liveness').map(r => [r.role, r]));
    for (const role of OPS_ROLES) roles[role] = this.roleStatus(role, byRole.get(role), now);
    const open = this.rows('SELECT * FROM ops_backlog_idle WHERE resolved_at IS NULL ORDER BY id DESC LIMIT 1')[0];
    const recent = this.rows('SELECT * FROM ops_backlog_idle ORDER BY id DESC LIMIT 10').map(e => this.episode(e, now));
    let backlog: Obj = {};
    try { backlog = this.backlog(now); } catch { backlog = { error: 'backlog_unavailable' }; }
    const meta = this.rows('SELECT last_eval FROM ops_liveness_meta WHERE id=1')[0];
    return { evaluated_at: iso(meta?.last_eval), window_s: OPS_WINDOW_MS / 1000, roles, backlog, throughput: this.throughput(now),
      idle: open ? this.episode(open, now) : null, recent_idle: recent };
  }

  private episode(e: Obj, now: number): Obj {
    let reason: Obj = {}, kick: Obj | null = null;
    try { reason = JSON.parse(e.reason_json); } catch {}
    try { kick = e.last_kick_json ? JSON.parse(e.last_kick_json) : null; } catch {}
    return { id: e.id, started_at: iso(e.started_at), idle_for_s: age(e.resolved_at ?? now, e.started_at), resolved_at: iso(e.resolved_at),
      open_with_ac: e.open_with_ac, why_idle: reason, kicks: e.kicks, failed_kicks: e.failed_kicks, last_kick_at: iso(e.last_kick_at), last_kick: kick };
  }

  /** Minute-alarm evaluation; throttled, never throws into the alarm. */
  async tick(): Promise<Obj> {
    const now = this.now();
    const meta = this.rows('SELECT last_eval FROM ops_liveness_meta WHERE id=1')[0];
    if (meta?.last_eval && now - meta.last_eval < OPS_EVAL_INTERVAL_MS) return { state: 'already_checked' };
    this.sql.exec('UPDATE ops_liveness_meta SET last_eval=? WHERE id=1', now);
    if (this.hooks.enabled && !this.hooks.enabled()) return { state: 'disabled' };
    const out: Obj = { state: 'checked', roles: {} };
    const byRole = new Map(this.rows('SELECT * FROM ops_liveness').map(r => [r.role, r]));
    for (const role of OPS_ROLES) {
      const s = this.roleStatus(role, byRole.get(role), now);
      out.roles[role] = s.state;
      if (s.state !== 'never_reported') await this.hooks.alert?.('ops-' + role, s.alert);
    }
    out.throughput = this.throughput(now);
    if (out.throughput.alert_evaluable) await this.hooks.alert?.('ops-backlog-throughput', out.throughput.alert);
    out.idle = await this.idleTick(now, byRole);
    return out;
  }

  private async idleTick(now: number, byRole: Map<any, Obj>): Promise<Obj> {
    const b = this.backlog(now);
    if ((b.truncated && b.active_sessions === 0 && b.open_with_ac > 0) || (b.open_with_ac_truncated && b.open_with_ac === 0))
      return { state: 'observation_incomplete', ...b };
    const open = this.rows('SELECT * FROM ops_backlog_idle WHERE resolved_at IS NULL ORDER BY id DESC LIMIT 1')[0];
    if (b.open_with_ac === 0 || b.active_sessions > 0) {
      if (open) {
        this.sql.exec('UPDATE ops_backlog_idle SET resolved_at=? WHERE id=?', now, open.id);
        await this.hooks.alert?.('ops-backlog-idle', null);
      }
      return { state: b.open_with_ac === 0 ? 'no_backlog' : 'working', open_with_ac: b.open_with_ac, active_sessions: b.active_sessions };
    }
    const orchestrator = this.roleStatus('orchestrator', byRole.get('orchestrator'), now);
    const why = { open_with_ac: b.open_with_ac, active_sessions: 0, last_work_at: iso(b.last_work_at), orchestrator: orchestrator.state,
      orchestrator_tick_age_s: orchestrator.tick_age_s ?? null, watchdog_state: orchestrator.watchdog_state ?? null,
      watchdog_age_s: orchestrator.watchdog_age_s ?? null, queue_holds: this.safeHolds() };
    if (!open) {
      // Idle since the last useful work; fresh observations of idle processes never reset it.
      // Work older than one window means it is already proven idle: kick on this tick.
      const since = b.last_work_at ?? now;
      const id = this.sql.exec('INSERT INTO ops_backlog_idle(started_at,open_with_ac,reason_json) VALUES(?,?,?) RETURNING id', since, b.open_with_ac, JSON.stringify(why)).one().id;
      if (now - since < OPS_WINDOW_MS) return { state: 'idle_started', idle_for_s: age(now, since), open_with_ac: b.open_with_ac, active_sessions: 0 };
      return this.kick(now, this.rows('SELECT * FROM ops_backlog_idle WHERE id=?', id)[0], b, why);
    }
    this.sql.exec('UPDATE ops_backlog_idle SET open_with_ac=?, reason_json=? WHERE id=?', b.open_with_ac, JSON.stringify(why), open.id);
    const since = open.last_kick_at ?? open.started_at;
    if (now - since < OPS_WINDOW_MS) return { state: 'idle', idle_for_s: age(now, open.started_at), open_with_ac: b.open_with_ac, active_sessions: 0 };
    return this.kick(now, open, b, why);
  }

  private async kick(now: number, open: Obj, b: { open_with_ac: number }, why: Obj): Promise<Obj> {
    // Still idle a full window after the previous kick: that kick did not produce work.
    const failed = open.last_kick_at ? open.failed_kicks + 1 : open.failed_kicks;
    let kick: KickResult = { task_id: null, reason: 'kick_unavailable' };
    if (this.hooks.kick) {
      try { kick = await this.hooks.kick(why); }
      catch (e: any) { kick = { task_id: null, reason: 'kick_error:' + String(e?.message || e).slice(0, 120) }; }
    }
    this.sql.exec('UPDATE ops_backlog_idle SET kicks=kicks+1, failed_kicks=?, last_kick_at=?, last_kick_json=? WHERE id=?',
      failed, now, JSON.stringify({ ...kick, at: iso(now) }), open.id);
    if (failed >= 2) await this.hooks.alert?.('ops-backlog-idle',
      `${b.open_with_ac} open intakes with AC and no intake session starting or running for ${age(now, open.started_at)}s; ${failed} automatic starts in a row did not produce a working session (last: ${kick.task_id ?? 'none'} ${kick.reason}). Why idle: orchestrator ${why.orchestrator}, tick age ${why.orchestrator_tick_age_s ?? 'unknown'}s.`);
    return { state: 'kicked', kick, failed_kicks: failed, open_with_ac: b.open_with_ac, active_sessions: 0 };
  }

  private safeHolds(): Obj {
    try { return this.hooks.holdReasons?.() ?? {}; } catch { return { error: 'hold_reasons_unavailable' }; }
  }
}
