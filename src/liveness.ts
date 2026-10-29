/**
 * Heartbeats, liveness and stale alerts for machines, aplexer sessions, hub launches and
 * self-reporting agents (for example Grok Bot). Liveness is derived from timestamps the hub
 * already receives; an unknown host makes everything on it unknown rather than healthy.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { effectiveCheckin } from './registry';
import { accountCapacityAlerts } from './account_capacity';
import { resolveMachineAliases, aliasDetail, projectAliasSessions } from './machine-alias';
import { freshCatalogs } from './catalog-freshness';
import { classifySessionStall, initSessionStalls, orphanFailure } from './session_stall';

export const LIVENESS_SCHEMA = `
CREATE TABLE IF NOT EXISTS agent_heartbeats (actor TEXT PRIMARY KEY, machine_id TEXT, status TEXT NOT NULL, summary TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS liveness_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_kind TEXT NOT NULL, subject TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, opened_at TEXT NOT NULL, resolved_at TEXT, notified_at TEXT, notify_error TEXT, delivered_to TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS liveness_alerts_open ON liveness_alerts (subject_kind, subject, resolved_at);
`;

export type Thresholds = { machineStaleS: number; machineOfflineS: number; agentStallS: number; heartbeatStaleS: number; launchStuckS: number };
export const DEFAULT_THRESHOLDS: Thresholds = { machineStaleS: 180, machineOfflineS: 900, agentStallS: 1800, heartbeatStaleS: 900, launchStuckS: 600 };

/** Read thresholds from Worker vars, ignoring anything that is not a positive number of seconds. */
export function thresholdsFrom(env: any): Thresholds {
  const pick = (name: string, fallback: number) => {
    const value = Number(env?.[name]);
    return Number.isFinite(value) && value > 0 && value <= 7 * 86400 ? value : fallback;
  };
  const t = {
    machineStaleS: pick('HUB_MACHINE_STALE_S', DEFAULT_THRESHOLDS.machineStaleS),
    machineOfflineS: pick('HUB_MACHINE_OFFLINE_S', DEFAULT_THRESHOLDS.machineOfflineS),
    agentStallS: pick('HUB_AGENT_STALL_S', DEFAULT_THRESHOLDS.agentStallS),
    heartbeatStaleS: pick('HUB_HEARTBEAT_STALE_S', DEFAULT_THRESHOLDS.heartbeatStaleS),
    launchStuckS: pick('HUB_LAUNCH_STUCK_S', DEFAULT_THRESHOLDS.launchStuckS),
  };
  if (t.machineOfflineS < t.machineStaleS) t.machineOfflineS = t.machineStaleS;
  return t;
}

/** States that open an alert. `idle`, `working`, `exited` and `drained` are not problems. */
export const ALERT_STATES = new Set(['stale', 'offline', 'stalled', 'stuck', 'uncertain', 'missing', 'launcher_memory_reserve']);

export type Subject = { kind: 'machine' | 'session' | 'launch' | 'agent'; id: string; state: string; detail: string; machine_id?: string | null; age_s?: number | null; alias_of?: string | null; canonical_id?: string | null; resolved_machine_id?: string | null;
  memory_event?: { classification: 'active' | 'historical' | 'superseded'; observed_at: string; retry_id?: string; retry_state?: string; native_id?: string | null; verification_status?: string | null } };

const STATUS = /^[a-z][a-z_-]{0,31}$/;
const ACTOR = /^[^\s\x00-\x1f\x7f]{1,128}$/;

function ageSeconds(at: unknown, now: number): number | null {
  if (typeof at !== 'string') return null;
  const value = Date.parse(at);
  if (!Number.isFinite(value)) return null;
  return Math.max(0, Math.round((now - value) / 1000));
}

export class LivenessService {
  constructor(private sql: SqlStorage, private thresholds: Thresholds = DEFAULT_THRESHOLDS, private now: () => number = Date.now) {}

  init() {
    initSessionStalls(this.sql);
    for (const stmt of LIVENESS_SCHEMA.split(';')) if (stmt.trim()) this.sql.exec(stmt);
    const columns = this.sql.exec('PRAGMA table_info(liveness_alerts)').toArray().map((c: any) => c.name);
    if (!columns.includes('delivered_to')) this.sql.exec("ALTER TABLE liveness_alerts ADD COLUMN delivered_to TEXT NOT NULL DEFAULT '[]'");
  }

  machineState(machine: any, now = this.now(), label: string = machine.id): Subject {
    const age = ageSeconds(effectiveCheckin(machine.id, machine.last_checkin_at), now);
    let state: string;
    if (machine.drained) state = 'drained';
    else if (age === null) state = 'never';
    else if (age <= this.thresholds.machineStaleS) state = 'online';
    else if (age <= this.thresholds.machineOfflineS) state = 'stale';
    else state = 'offline';
    const detail = age === null ? 'no heartbeat received yet' : `last heartbeat ${age}s ago`;
    return { kind: 'machine', id: label, state, detail, machine_id: machine.id, age_s: age };
  }

  private pressureProjection?: () => Subject[];
  setPressureProjection(project: () => Subject[]) { this.pressureProjection = project; }

  /** Every subject the hub can judge, with its current liveness state. */
  evaluate(): { thresholds: Thresholds; subjects: Subject[] } {
    const now = this.now(); const t = this.thresholds; const subjects: Subject[] = [];
    const machines = new Map<string, Subject>();
    const rows = this.sql.exec('SELECT id, name, hostname, last_checkin_at, drained FROM machines ORDER BY id').toArray() as any[];
    // Show enrolled names; fall back to the opaque id when a name is missing or shared.
    const counts = new Map<string, number>();
    for (const m of rows) if (m.name) counts.set(m.name, (counts.get(m.name) || 0) + 1);
    const names = new Map<string, string>(rows.map(m => [m.id, m.name && counts.get(m.name) === 1 ? m.name : m.id]));
    const label = (id: string) => names.get(id) || id;
    // Machine aliasing (mark, never delete): records sharing a normalized
    // hostname are one physical host. The stale id (e.g. hetzner, offline
    // ~220h) aliases to the live canonical host (e.g. urgent-hetzner, same
    // host RMTHZ); sessions on the alias resolve to the canonical host and
    // report the canonical host's real state instead of unknown.
    let aliasOf = new Map<string, string>();
    const hostnames = new Map<string, string>();
    try {
      const resolution = resolveMachineAliases(rows.map((m: any) => {
        const best = effectiveCheckin(String(m.id), m.last_checkin_at);
        const seen = typeof best === 'string' ? Date.parse(best) : NaN;
        if (typeof m.hostname === 'string' && m.hostname.trim()) hostnames.set(String(m.id), m.hostname.trim());
        return { id: String(m.id), hostname: m.hostname, drained: m.drained, last_seen_ms: Number.isFinite(seen) ? seen : null };
      }));
      aliasOf = resolution.aliasOf;
    } catch { aliasOf = new Map(); }
    const canonical = (id: string) => aliasOf.get(id) ?? id;
    for (const m of rows) {
      const s = this.machineState(m, now, label(m.id)); machines.set(m.id, s);
      const alias = aliasOf.get(String(m.id)) ?? null;
      subjects.push(alias
        ? { ...s, detail: `${s.detail}; ${aliasDetail(String(m.id), alias, hostnames.get(String(m.id)))}`, alias_of: alias, canonical_id: alias }
        : { ...s, canonical_id: String(m.id) });
    }
    const hostKnown = (id: string) => machines.get(canonical(id))?.state === 'online';

    const reported = new Set<string>();
    let sessionRows: any[] = [];
    try { sessionRows = this.sql.exec('SELECT * FROM fleet_sessions ORDER BY machine_id, tag').toArray() as any[]; } catch { sessionRows = []; }
    const catalogSeen = freshCatalogs(this.sql, now);
    for (const s of projectAliasSessions(sessionRows, aliasOf, now, catalogSeen)) {
      reported.add(s.machine_id + '/' + s.native_id);
      // A late catalog may already report under the canonical id while older
      // rows still name the alias; either binding proves the session live.
      if (s.alias_observation !== 'no_current_native_observation') reported.add(canonical(s.machine_id) + '/' + s.native_id);
      const activity = ageSeconds(s.last_activity_at, now);
      const resolved = canonical(s.machine_id);
      const alias = aliasOf.get(String(s.machine_id)) ?? null;
      const stall = classifySessionStall(this.sql, s, now, t.agentStallS, hostKnown(s.machine_id));
      let state: string; let detail: string;
      if (!hostKnown(s.machine_id)) { state = 'unknown'; detail = `machine ${label(resolved)} is not reporting`; }
      else if (s.alias_observation === 'no_current_native_observation') { state = 'unknown'; detail = 'no current native observation on canonical host'; }
      else if (stall.stalled) { state = 'stalled'; detail = `${stall.reason}; no native progress for ${stall.idle_for ?? 'unknown'}s (threshold ${t.agentStallS}s)`; }
      else if (s.phase === 'unobserved' || s.phase === 'unknown') { state = 'unknown'; detail = 'no positive current native state'; }
      else if ((s.phase === 'exited' || s.phase === 'dead') && orphanFailure(s)) { state = 'stalled'; detail = 'headless agent exited while children remained; workload exit is now confirmed'; }
      else if (s.phase === 'exited' || s.phase === 'dead') { state = 'exited'; detail = `phase ${s.phase}`; }
      else if (s.phase !== 'running') { state = ['starting','exiting','uncertain','broken'].includes(s.phase) ? s.phase : 'unknown'; detail = `phase ${s.phase}`; }
      else if (s.agent_state === 'working') { state = 'working'; detail = activity === null ? 'working' : `active ${activity}s ago`; }
      else { state = s.agent_state ? 'idle' : 'running'; detail = `${s.agent_state || 'no agent state'}${activity === null ? '' : `, active ${activity}s ago`}`; }
      if (alias) detail += ` (same host ${hostnames.get(String(s.machine_id)) ?? 'unknown'}; reporting via ${label(resolved)})`;
      subjects.push({ kind: 'session', id: `${label(resolved)}/${s.tag}`, state, detail: `${detail} (${s.engine || 'shell'} in ${s.workspace})`, machine_id: s.machine_id, resolved_machine_id: resolved, alias_of: alias, canonical_id: resolved, age_s: activity });
    }

    let launchRows: any[] = [];
    try { launchRows = this.sql.exec("SELECT id, machine_id, mode, state, native_id, updated_at, summary FROM fleet_launches WHERE state IN ('offered','starting','running','uncertain') ORDER BY created_at").toArray() as any[]; } catch { launchRows = []; }
    for (const l of launchRows) {
      const age = ageSeconds(l.updated_at, now);
      const resolved = l.machine_id ? canonical(l.machine_id) : l.machine_id;
      const alias = l.machine_id ? (aliasOf.get(String(l.machine_id)) ?? null) : null;
      let state: string; let detail: string;
      if (l.state === 'uncertain') { state = 'uncertain'; detail = 'launch outcome unknown; reconcile before retrying'; }
      else if ((l.state === 'offered' || l.state === 'starting') && age !== null && age > t.launchStuckS) { state = 'stuck'; detail = `${l.state} for ${age}s`; }
      else if (alias && l.native_id && !sessionRows.some(s => s.native_id === l.native_id && canonical(s.machine_id) === resolved && !['unobserved','unknown'].includes(s.phase) && (catalogSeen.has(s.machine_id) || (Number.isFinite(Date.parse(s.observed_at)) && now - Date.parse(s.observed_at) <= 180000)))) { state = 'unknown'; detail = 'no current native observation on canonical host'; }
      else if (l.machine_id && !hostKnown(l.machine_id)) { state = 'unknown'; detail = `machine ${label(resolved)} is not reporting`; }
      else if (l.state === 'running' && l.native_id && !this.sessionReported(l.machine_id, l.native_id, reported) && !this.sessionReported(resolved, l.native_id, reported)) { state = 'missing'; detail = 'session no longer reported by its machine'; }
      else { state = l.state === 'running' ? 'working' : l.state; detail = `${l.state}${age === null ? '' : ` since ${age}s`}`; }
      subjects.push({ kind: 'launch', id: l.id, state, detail: `${l.summary}: ${detail}`, machine_id: l.machine_id, resolved_machine_id: resolved, alias_of: alias, canonical_id: resolved || null, age_s: age });
    }

    // Only a queued attempt is a current hold. Exited predecessors retain
    // their refusal evidence, but a retry link never proves retry success.
    // A proven pre-spawn refusal alerts immediately; planned holds after 5m.
    for (const h of this.sql.exec("SELECT h.*, l.state FROM fleet_memory_holds h JOIN fleet_launches l ON l.id = h.launch_id WHERE l.state = 'queued' OR (l.state = 'exited' AND h.retry_id IS NOT NULL AND h.since >= ?)", new Date(now - 86400000).toISOString()).toArray() as any[]) {
      const age = ageSeconds(h.since, now) ?? 0;
      if (!h.refused && age <= 300) continue;
      let reasons:any[]=[]; try {reasons=JSON.parse(h.rejected_json);} catch {}
      const reason=typeof reasons[0]?.reason==='string' ? reasons[0].reason : 'placement_hold';
      if (h.state === 'exited') {
        const retry: any = this.sql.exec('SELECT state FROM fleet_launches WHERE id = ?', h.retry_id).toArray()[0];
        subjects.push({ kind: 'launch', id: h.launch_id, state: 'historical',
          detail: `Historical ${reason} refusal at ${h.since}; attempt exited, superseded by retry ${h.retry_id} (${retry?.state ?? 'unknown'}); not a current hold. Refusal evidence: ${h.rejected_json}`,
          age_s: age, memory_event: { classification: 'superseded', observed_at: h.since, retry_id: h.retry_id, retry_state: retry?.state ?? 'unknown' } });
        continue;
      }
      subjects.push({ kind: 'launch', id: h.launch_id, state: reason,
        detail: `${reason} hold for ${age}s: ${h.rejected_json}`, age_s: age,
        memory_event: { classification: 'active', observed_at: h.since } });
    }
    // A terminal refusal is failure history, not evidence of current pressure.
    // Never attach today's healthy numbers to yesterday's failure receipt.
    for (const l of this.sql.exec("SELECT id, updated_at, finished_at, machine_id, native_id, verification_status FROM fleet_launches WHERE state = 'failed' AND exit_reason = 'launcher_memory_reserve' AND updated_at >= ?", new Date(now - 86400000).toISOString()).toArray() as any[]) {
      const at = l.finished_at ?? l.updated_at;
      subjects.push({ kind: 'launch', id: l.id, state: 'historical', detail:
        `Historical launcher_memory_reserve failure at ${at}; verification=${l.verification_status ?? 'unknown'}, native_id=${l.native_id ?? 'none'}. Failure remains recorded; current capacity and retry outcome are separate observations.`, machine_id: l.machine_id,
        memory_event: { classification: 'historical', observed_at: at, native_id: l.native_id ?? null, verification_status: l.verification_status ?? null } });
    }

    subjects.push(...accountCapacityAlerts(this.sql, now));
    subjects.push(...(this.pressureProjection?.() || []));
    for (const h of this.sql.exec('SELECT * FROM agent_heartbeats ORDER BY actor').toArray() as any[]) {
      const age = ageSeconds(h.at, now);
      const state = age === null || age > t.heartbeatStaleS ? 'stale' : h.status;
      subjects.push({ kind: 'agent', id: h.actor, state, detail: `${h.status}${h.summary ? ': ' + h.summary : ''}; last heartbeat ${age ?? 'unknown'}s ago`, machine_id: h.machine_id, age_s: age });
    }
    return { thresholds: t, subjects };
  }

  private sessionReported(machineId: string, nativeId: string, reported: Set<string>) {
    return reported.has(machineId + '/' + nativeId);
  }

  /** Mandatory host monitoring, independent of user-configured checks.
   * Stable machine ids retain incident identity across display-name changes.
   * Only a fresh checkin proves recovery; draining a host does not.
   */
  watchHosts(): any[] {
    const notifications: any[] = [];
    for (const m of this.sql.exec('SELECT id,name,last_checkin_at,registered_at,drained FROM machines').toArray() as any[]) {
      const checkin = effectiveCheckin(String(m.id), m.last_checkin_at);
      const age = ageSeconds(checkin ?? m.registered_at, this.now());
      let existing: any = this.sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind='host_watch' AND subject=? AND resolved_at IS NULL", m.id).toArray()[0];
      // Adopt an already-notified legacy machine incident without resending it.
      if (!existing) {
        existing = this.sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind='machine' AND subject IN (?,?) AND resolved_at IS NULL ORDER BY id LIMIT 1", m.id, m.name || m.id).toArray()[0];
        if (existing) this.sql.exec("UPDATE liveness_alerts SET subject_kind='host_watch',subject=? WHERE id=?", m.id, existing.id);
      }
      if (!m.drained && (age === null || age > 900)) {
        const opened = this.setAlert('host_watch', m.id, { state: 'offline', detail: `${m.name || m.id}: last_checkin_at=${checkin ?? 'never'}; no checkin for ${age ?? 'unknown'}s` });
        if (opened) notifications.push(opened);
      } else if (existing && checkin && age !== null && age <= 900) {
        this.setAlert('host_watch', m.id, null);
        const recovered = this.setAlert('host_recovery', String(existing.id), { state: 'RECOVERED', detail: `${m.name || m.id}: last_checkin_at=${checkin}; incident ${existing.id}` });
        if (recovered) notifications.push(recovered);
      }
    }
    return notifications;
  }

  /** Self-reported heartbeat from an agent credential. Status is a short word, summary is plain text. */
  recordHeartbeat(actor: string, machineId: string | null, input: any): { ok: true } | { error: string } {
    if (!ACTOR.test(actor)) return { error: 'invalid_actor' };
    if (input === null || typeof input !== 'object' || Array.isArray(input) || !Object.keys(input).every(k => ['status', 'summary'].includes(k))) return { error: 'invalid_heartbeat' };
    const status = input.status ?? 'working';
    if (typeof status !== 'string' || !STATUS.test(status) || ALERT_STATES.has(status)) return { error: 'invalid_status' };
    if (input.summary !== undefined && (typeof input.summary !== 'string' || input.summary.length > 240 || /[\x00-\x1f\x7f]/.test(input.summary))) return { error: 'invalid_summary' };
    this.sql.exec('INSERT INTO agent_heartbeats (actor, machine_id, status, summary, at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(actor) DO UPDATE SET machine_id = excluded.machine_id, status = excluded.status, summary = excluded.summary, at = excluded.at',
      actor, machineId, status, input.summary ?? null, new Date(this.now()).toISOString());
    return { ok: true };
  }

  /**
   * Open an alert for each subject that newly entered a problem state and resolve alerts whose
   * subject recovered or disappeared. Returns only newly opened alerts, so callers notify once.
   */
  reconcileAlerts(subjects: Subject[], kinds: string[] = ['machine', 'session', 'launch', 'agent']): any[] {
    const at = new Date(this.now()).toISOString(); const opened: any[] = [];
    const problems = new Map(subjects.filter(s => ALERT_STATES.has(s.state)).map(s => [s.kind + ':' + s.id, s]));
    const unknown = new Set(subjects.filter(s => s.kind === 'session' && s.state === 'unknown').map(s => s.kind + ':' + s.id));
    const open = this.sql.exec(`SELECT * FROM liveness_alerts WHERE resolved_at IS NULL AND subject_kind IN (${kinds.map(() => '?').join(',')})`, ...kinds).toArray() as any[];
    const openKeys = new Set<string>();
    for (const alert of open) {
      const key = alert.subject_kind + ':' + alert.subject; const current = problems.get(key);
      if (alert.state === 'stalled' && unknown.has(key)) continue;
      if (!current) this.sql.exec('UPDATE liveness_alerts SET resolved_at = ? WHERE id = ?', at, alert.id);
      else {
        openKeys.add(key);
        if (current.state !== alert.state || current.detail !== alert.detail) this.sql.exec('UPDATE liveness_alerts SET state = ?, detail = ? WHERE id = ?', current.state, current.detail, alert.id);
      }
    }
    for (const [key, s] of problems) {
      if (openKeys.has(key)) continue;
      const row: any = this.sql.exec('INSERT INTO liveness_alerts (subject_kind, subject, state, detail, opened_at) VALUES (?, ?, ?, ?, ?) RETURNING *', s.kind, s.id, s.state, s.detail, at).one();
      opened.push(row);
    }
    return opened;
  }

  /** Open a single named alert (used by scheduled checks), or resolve it when `problem` is null. */
  setAlert(kind: string, subject: string, problem: { state: string; detail: string } | null): any | null {
    const at = new Date(this.now()).toISOString();
    const existing: any = this.sql.exec('SELECT * FROM liveness_alerts WHERE resolved_at IS NULL AND subject_kind = ? AND subject = ?', kind, subject).toArray()[0];
    if (!problem) { if (existing) this.sql.exec('UPDATE liveness_alerts SET resolved_at = ? WHERE id = ?', at, existing.id); return null; }
    if (existing) { this.sql.exec('UPDATE liveness_alerts SET state = ?, detail = ? WHERE id = ?', problem.state, problem.detail, existing.id); return null; }
    return this.sql.exec('INSERT INTO liveness_alerts (subject_kind, subject, state, detail, opened_at) VALUES (?, ?, ?, ?, ?) RETURNING *', kind, subject, problem.state, problem.detail, at).one();
  }

  /** Targets that already received this alert; a retry skips them. */
  deliveredTo(alert: any): string[] {
    try { const v = JSON.parse(alert.delivered_to || '[]'); return Array.isArray(v) ? v : []; } catch { return []; }
  }

  markNotified(id: number, error: string | null, delivered: string[] = []) {
    this.sql.exec('UPDATE liveness_alerts SET notified_at = ?, notify_error = ?, delivered_to = ? WHERE id = ?', new Date(this.now()).toISOString(), error, JSON.stringify(delivered), id);
  }

  /**
   * Open alerts to (re)notify: delivery failed or had no target at least `retryAfterS` ago, or
   * delivery was never attempted (for example the run was interrupted after the alert opened).
   */
  undelivered(retryAfterS: number) {
    const cutoff = new Date(this.now() - retryAfterS * 1000).toISOString();
    return this.sql.exec('SELECT * FROM liveness_alerts WHERE resolved_at IS NULL AND ((notify_error IS NOT NULL AND notified_at <= ?) OR (notified_at IS NULL AND opened_at <= ?)) ORDER BY id LIMIT 20', cutoff, cutoff).toArray();
  }

  alerts(limit = 100, subject?: string) {
    if (subject !== undefined) return this.sql.exec('SELECT * FROM liveness_alerts WHERE subject = ? ORDER BY resolved_at IS NOT NULL, opened_at DESC LIMIT ?', subject, limit).toArray();
    return this.sql.exec('SELECT * FROM liveness_alerts ORDER BY resolved_at IS NOT NULL, opened_at DESC LIMIT ?', limit).toArray();
  }

  prune(keepDays = 30) {
    const cutoff = new Date(this.now() - keepDays * 86400000).toISOString();
    this.sql.exec('DELETE FROM liveness_alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?', cutoff);
  }
}

/** Human-readable one-line alert text; contains only hub-side labels, never transcripts. */
export function alertText(alert: any): string {
  return `[agent-hub] ${alert.subject_kind} ${alert.subject} is ${alert.state}: ${alert.detail}`;
}
