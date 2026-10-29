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
import { nativeObservationFresh } from './session_stall';

export const OPS_ROLES = ['orchestrator', 'oncall'] as const;
/** Roles that must report: silence counts from Hub's first evaluation even with no row. */
export const EXPECTED_ROLES = new Set(['orchestrator']);
export const OPS_WINDOW_MS = 30 * 60000;
export const OPS_EVAL_INTERVAL_MS = 60000;
const WATCHDOG_STATES = new Set(['ok', 'stalled', 'dead', 'recovering', 'proof_pending', 'proof_failed', 'census_failed', 'restart_failed']);
const ACTIVE_STATES = "('starting','offered','running')";
const NATIVE_STALE_MS = 10 * 60000;
const requestIntake = "json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.intake_task_id')";

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

  /** Bounded backlog view: open intakes with AC, and intake sessions with fresh evidence of
   * work: offered/starting rows updated within the window, running rows whose bound native
   * session is observed live. A retained row alone, or one for a closed intake, is not work. */
  backlog(now = this.now()): { open_with_ac: number; active_sessions: number } {
    const open = this.rows(`SELECT COUNT(*) AS n FROM (SELECT 1 FROM intake_task_projection
      WHERE json_extract(data,'$.status') IN ('open','in_progress','blocked','pending_approval') AND json_extract(data,'$.archived_at') IS NULL
      AND json_extract(data,'$.status') IN ('open','in_progress')
      AND length(trim(coalesce(json_extract(data,'$.acceptance_criteria'),'')))>0 LIMIT 1001)`)[0]?.n ?? 0;
    const candidates = this.rows(`SELECT s.state, s.updated_at, s.machine_id,
        CASE WHEN json_valid(s.binding_json) THEN json_extract(s.binding_json,'$.native_id') END AS native_id
      FROM session_control_sessions s JOIN intake_task_projection p ON p.id=${requestIntake}
      WHERE s.state IN ${ACTIVE_STATES} AND json_extract(p.data,'$.status') IN ('open','in_progress') LIMIT 201`);
    const cache = new Map();
    let active = 0;
    for (const c of candidates) {
      if (c.state !== 'running') { if (now - (Date.parse(c.updated_at) || 0) <= OPS_WINDOW_MS) active++; continue; }
      if (!c.native_id) continue;
      const native = this.rows(`SELECT machine_id, native_id, observed_at FROM fleet_sessions WHERE machine_id=? AND native_id=?
        AND phase NOT IN ('exited','stopped','failed') LIMIT 1`, c.machine_id, c.native_id)[0];
      if (native && now - (Date.parse(native.observed_at) || 0) <= NATIVE_STALE_MS && nativeObservationFresh(this.sql, native, now, cache)) active++;
    }
    return { open_with_ac: open, active_sessions: active };
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
    return { evaluated_at: iso(meta?.last_eval), window_s: OPS_WINDOW_MS / 1000, roles, backlog,
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
    out.idle = await this.idleTick(now, byRole);
    return out;
  }

  private async idleTick(now: number, byRole: Map<any, Obj>): Promise<Obj> {
    const b = this.backlog(now);
    const open = this.rows('SELECT * FROM ops_backlog_idle WHERE resolved_at IS NULL ORDER BY id DESC LIMIT 1')[0];
    if (b.open_with_ac === 0 || b.active_sessions > 0) {
      if (open) {
        this.sql.exec('UPDATE ops_backlog_idle SET resolved_at=? WHERE id=?', now, open.id);
        await this.hooks.alert?.('ops-backlog-idle', null);
      }
      return { state: b.open_with_ac === 0 ? 'no_backlog' : 'working', ...b };
    }
    const orchestrator = this.roleStatus('orchestrator', byRole.get('orchestrator'), now);
    const why = { open_with_ac: b.open_with_ac, active_sessions: 0, orchestrator: orchestrator.state,
      orchestrator_tick_age_s: orchestrator.tick_age_s ?? null, watchdog_state: orchestrator.watchdog_state ?? null,
      watchdog_age_s: orchestrator.watchdog_age_s ?? null, queue_holds: this.safeHolds() };
    if (!open) {
      this.sql.exec('INSERT INTO ops_backlog_idle(started_at,open_with_ac,reason_json) VALUES(?,?,?)', now, b.open_with_ac, JSON.stringify(why));
      return { state: 'idle_started', ...b };
    }
    this.sql.exec('UPDATE ops_backlog_idle SET open_with_ac=?, reason_json=? WHERE id=?', b.open_with_ac, JSON.stringify(why), open.id);
    const since = open.last_kick_at ?? open.started_at;
    if (now - since < OPS_WINDOW_MS) return { state: 'idle', idle_for_s: age(now, open.started_at), ...b };
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
    return { state: 'kicked', kick, failed_kicks: failed, ...b };
  }

  private safeHolds(): Obj {
    try { return this.hooks.holdReasons?.() ?? {}; } catch { return { error: 'hold_reasons_unavailable' }; }
  }
}
