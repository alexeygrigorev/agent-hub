/** Idle-session audit (intake 27d1e27b): a Hub-scheduled pass every 30 minutes
 * over all live sessions (Hub-managed and observed aplexer natives). Each
 * session is scored on last output change, host CPU, git worktree state,
 * intake state and the newest bounded screen capture; a pluggable decider
 * (heuristic by default) picks exactly one action per session: observe,
 * capture, keystroke, nudge, stop or escalate. Every decision lands as a
 * dataset row whose outcome the next run fills in, so a smarter model can
 * later replace the heuristic without touching the scheduler.
 * Safety (AC2): an intake blocked on a human is never acted against; the
 * audit only enqueues ordinary session-control commands and never passes a
 * cleanup flag, so it can never delete a worktree — uncommitted work survives
 * every action it takes. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { freshCatalogs } from './catalog-freshness';
import { nativeProgress } from './session_stall';
import { sanitizeCpuUsage } from './cpu-headroom';

type Obj = Record<string, any>;
const parse = (raw: any): Obj => { try { return JSON.parse(raw || '{}'); } catch { return {}; } };

export const AUDIT_INTERVAL = 1800000; // 30 minutes; matches the */30 Worker cron
const SCREEN_TAIL_CHARS = 4000; // last ~60 terminal lines kept per dataset row
const SCREEN_FRESH_MS = 2 * AUDIT_INTERVAL;
const ACTION_BUDGET = 8; // sends + stops per run, bounded like the intake watchdog restart slice
const CAPTURE_BUDGET = 12;
const ESCALATE_BUDGET = 12;
const MAX_DETAIL = 512;
const RETENTION_MS = 7 * 86400000;
const RETENTION_DELETE = 400;
const TERMINAL_PHASES = new Set(['exited', 'stopped', 'failed', 'pruned', 'forgotten', 'dead']);
const UNOBSERVED_PHASES = new Set(['unknown', 'unobserved']);
const HUMAN_BLOCKED = new Set(['blocked', 'pending_approval']);
const OPEN_INTAKE = new Set(['open', 'in_progress', 'blocked', 'pending_approval']);
const ACTIONABLE = new Set(['keystroke', 'nudge', 'stop']);

/** Screen patterns that mean the agent is waiting on the operator, with the
 * matching keystroke each (AC2 waiting-for-input). First match wins. */
export const INPUT_PROMPTS: Array<{ rule: string; pattern: RegExp; send: string }> = [
  { rule: 'yes_no_prompt', pattern: /\[y\/n\]|\(y\/n\)|\(y\)es|\byes\/no\b/i, send: 'y' },
  { rule: 'approval_prompt', pattern: /\b(do you want|allow|approve|proceed\?|trust)\b/i, send: 'y' },
  { rule: 'update_prompt', pattern: /\bupdate (is )?available\b|\bupdate now\b|\binstall (the )?update\b/i, send: 'y' },
  { rule: 'press_enter', pattern: /\bpress enter\b|\benter to continue\b/i, send: 'continue' },
  { rule: 'continue_prompt', pattern: /\bcontinue\?\s*$/i, send: 'continue' },
];

export function matchInputPrompt(screen: string | null): { rule: string; send: string; matched: string } | null {
  if (!screen) return null;
  for (const p of INPUT_PROMPTS) {
    const m = p.pattern.exec(screen);
    if (m) return { rule: p.rule, send: p.send, matched: m[0].slice(0, 80) };
  }
  return null;
}

export type AuditFeatures = {
  session_id: string | null;
  machine_id: string | null;
  native_id: string | null;
  intake: string | null;
  engine: string | null;
  mode: string | null;
  state: string;
  actor: string | null; // owning credential actor; human:* = operator-started
  bound: boolean; // session carries a machine/native binding
  linked: boolean; // bound, running, and its native is observed live (send/stop possible)
  nativePhase: string | null;
  protected_session: boolean;
  runtime_s: number | null;
  idle_s: number | null; // since the host's last observed native progress; null = unknown
  host_cpu: Obj | null;
  git: Obj | null; // active intake-worktree status: dirty, dirty_count, unpushed, checked_at
  intake_state: string | null;
  screen_tail: string | null;
  screen_age_s: number | null;
  nudges: number; // prior nudge/escalate steps already recorded for this session
  prior: Obj | null; // newest unresolved prior decision for this session
};

export type AuditDecision = {
  rule: string;
  action: 'observe' | 'capture' | 'keystroke' | 'nudge' | 'stop' | 'escalate';
  detail: string;
  send_text?: string;
};

export interface SessionDecider { decide(features: AuditFeatures): AuditDecision; }

/** Ordered explicit heuristic (AC2); the first matching rule wins. */
export function makeHeuristicDecider(idleThresholdS: number): SessionDecider {
  const t = idleThresholdS;
  const detail = (f: AuditFeatures): string =>
    `idle ${f.idle_s === null ? 'unknown' : Math.round(f.idle_s) + 's'} (threshold ${t}s)`;
  return {
    decide(f: AuditFeatures): AuditDecision {
      if (f.protected_session)
        return { rule: 'protected_session', action: 'observe', detail: 'operator protection set; audit never acts' };
      if (f.actor !== null && f.actor.startsWith('human:'))
        return { rule: 'operator_started', action: 'observe', detail: `operator-started session (${f.actor}); audit never acts on it` };
      if (f.intake_state !== null && HUMAN_BLOCKED.has(f.intake_state))
        return { rule: 'human_blocked_intake', action: 'observe', detail: `intake ${f.intake} is ${f.intake_state} on a human; audit never acts on it` };
      if (f.idle_s === null || f.idle_s < t)
        return { rule: 'recent_progress', action: 'observe', detail: f.idle_s === null ? 'no progress observation yet' : detail(f) };
      if (!f.bound && f.state === 'observed')
        return { rule: 'unlinked_idle_native', action: 'escalate', detail: `observed aplexer native without a Hub binding; no control channel (${detail(f)})` };
      if (f.state !== 'running')
        return { rule: 'intent_not_running', action: 'observe', detail: `session state ${f.state}; only running sessions are audited for action` };
      if (!f.bound)
        return { rule: 'unlinked_idle_native', action: 'escalate', detail: `running Hub session never bound a native; no control channel (${detail(f)})` };
      if (!f.linked) {
        if (UNOBSERVED_PHASES.has(f.nativePhase ?? ''))
          return { rule: 'observation_unknown', action: 'observe', detail: `host census phase ${f.nativePhase}; never act on stale evidence` };
        return { rule: 'stale_binding_stop', action: 'stop', detail: `binding native is ${f.nativePhase} in a fresh census; ${detail(f)}` };
      }
      if (f.intake_state !== null && !OPEN_INTAKE.has(f.intake_state))
        return { rule: 'finished_intake_closed', action: 'stop', detail: `intake ${f.intake} is ${f.intake_state}; ${detail(f)}; files stay on disk` };
      if (f.intake_state === null && f.git && f.git.dirty === false && (f.git.unpushed ?? 0) === 0 && f.idle_s! >= 2 * t)
        return { rule: 'finished_committed_idle', action: 'stop', detail: `no linked intake, clean pushed tree; ${detail(f)}` };
      if (f.prior?.action === 'stop' && !f.prior.outcome)
        return { rule: 'stop_pending', action: 'observe', detail: 'stop command still resolving; verified next audit' };
      const prompt = f.screen_age_s !== null && f.screen_age_s * 1000 <= SCREEN_FRESH_MS ? matchInputPrompt(f.screen_tail) : null;
      if (prompt)
        return { rule: 'waiting_for_input:' + prompt.rule, action: 'keystroke', send_text: prompt.send, detail: `screen matches ${prompt.rule} ("${prompt.matched}"); ${detail(f)}` };
      if (f.nudges >= 2)
        return { rule: 'dead_after_nudge', action: 'stop', detail: `no progress through nudge and escalation; ${detail(f)}; tree is never deleted` };
      if (f.prior?.action === 'capture' && !f.prior.outcome)
        return { rule: 'capture_pending', action: 'observe', detail: 'screen capture still resolving; classified next audit' };
      if (f.nudges === 1)
        return { rule: 'stuck_escalate', action: 'escalate', detail: `nudge produced no progress; ${detail(f)}` };
      if (f.git && (f.git.dirty === true || (f.git.unpushed ?? 0) > 0))
        return { rule: 'stuck_uncommitted', action: 'nudge', send_text: 'continue, commit, report', detail: `uncommitted work (${f.git.dirty_count ?? '?'} dirty, ${f.git.unpushed ?? 0} unpushed); ${detail(f)}` };
      if (!f.screen_tail || (f.screen_age_s !== null && f.screen_age_s * 1000 > SCREEN_FRESH_MS))
        return { rule: 'missing_screen', action: 'capture', detail: `no fresh screen evidence to classify; capturing (${detail(f)})` };
      return { rule: 'observe_idle', action: 'observe', detail: `idle past threshold with no matching rule; ${detail(f)}` };
    },
  };
}

export function idleThresholdFrom(env?: any): number {
  const n = Number(env?.HUB_SESSION_AUDIT_IDLE_S);
  return Number.isFinite(n) && n >= 300 && n <= 604800 ? n : 1800;
}

export type SessionAuditHooks = {
  /** Internal session-control call; returns parsed JSON and status. */
  request?: (path: string, init: { method: string; body?: string }) => Promise<{ status: number; body: Obj }>;
  /** Liveness alert sink (same shape as the intake watchdog). */
  notify?: (kind: string, subject: string, detail: string | null) => Promise<boolean | void>;
};

/** Key a decision/prior is tracked by across runs. */
const sessionKey = (machineId: any, nativeId: any, sessionId: any): string =>
  (machineId ?? '') + '/' + (nativeId ?? sessionId ?? '');

export class SessionAuditService {
  constructor(private sql: SqlStorage, private hooks: SessionAuditHooks = {}, private now = Date.now,
    private decider: (idleThresholdS: number) => SessionDecider = makeHeuristicDecider,
    private env?: any, private interval = AUDIT_INTERVAL) {}

  init() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS session_audit_meta (id INTEGER PRIMARY KEY CHECK(id=1), last_tick INTEGER, last_source TEXT, last_result TEXT)');
    this.sql.exec('INSERT OR IGNORE INTO session_audit_meta(id) VALUES(1)');
    this.sql.exec(`CREATE TABLE IF NOT EXISTS session_audit_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, source TEXT NOT NULL,
      scanned INTEGER NOT NULL, counts_json TEXT NOT NULL, alert TEXT)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS session_audit_decisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, at TEXT NOT NULL,
      session_id TEXT, machine_id TEXT, native_id TEXT, intake TEXT, engine TEXT, mode TEXT,
      idle_s INTEGER, rule TEXT NOT NULL, action TEXT NOT NULL, detail TEXT,
      action_key TEXT, outcome TEXT, outcome_at TEXT,
      features_json TEXT NOT NULL, screen_tail TEXT)`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS session_audit_decisions_session ON session_audit_decisions(session_id, at)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS session_audit_decisions_run ON session_audit_decisions(run_id)');
  }

  status(): Obj {
    const m: any = this.rows('SELECT last_tick,last_source,last_result FROM session_audit_meta WHERE id=1')[0] ?? {};
    return { last_run_at: m.last_tick ?? null, source: m.last_source ?? null, state: parse(m.last_result).state ?? 'not_run', threshold_s: idleThresholdFrom(this.env) };
  }

  private rows(query: string, ...args: any[]): Obj[] { return this.sql.exec(query, ...args).toArray() as Obj[]; }

  /** Recent runs plus their decisions (CLI/UI action log, AC4). */
  view(last: number): Obj {
    const runs = this.rows('SELECT * FROM session_audit_runs ORDER BY id DESC LIMIT ?', Math.min(50, Math.max(1, last)));
    const ids = runs.map(r => r.id);
    const decisions = ids.length ? this.rows(
      `SELECT * FROM session_audit_decisions WHERE run_id IN (${ids.map(() => '?').join(',')}) ORDER BY id`, ...ids) : [];
    return { audit: this.status(), runs: runs.map(r => this.runView(r)), decisions: decisions.map(d => this.decisionView(d)) };
  }

  /** Full dataset export for offline decider training (AC3). */
  exportRows(limit: number): Obj {
    const capped = Math.min(20000, Math.max(1, limit));
    return {
      exported_at: new Date(this.now()).toISOString(),
      runs: this.rows('SELECT * FROM session_audit_runs ORDER BY id DESC LIMIT ?', capped).map(r => this.runView(r)),
      decisions: this.rows('SELECT * FROM session_audit_decisions ORDER BY id DESC LIMIT ?', capped)
        .map(d => ({ ...this.decisionView(d), features: parse(d.features_json), screen_tail: d.screen_tail })),
    };
  }

  private runView(r: Obj): Obj {
    return { id: r.id, at: r.at, source: r.source, scanned: r.scanned, counts: parse(r.counts_json), alert: r.alert ?? null };
  }

  private decisionView(d: Obj): Obj {
    return { id: d.id, run_id: d.run_id, at: d.at, session_id: d.session_id, machine_id: d.machine_id,
      native_id: d.native_id, intake: d.intake, engine: d.engine, mode: d.mode, idle_s: d.idle_s,
      rule: d.rule, action: d.action, detail: d.detail, outcome: d.outcome, outcome_at: d.outcome_at };
  }

  private intakeStatus(taskId: string): string | null {
    const row = this.rows('SELECT data FROM intake_task_projection WHERE id = ?', taskId)[0];
    const status = row ? parse(row.data).status : null;
    return typeof status === 'string' ? status : null;
  }

  private linkedIntakeId(session: Obj): string | null {
    const row = this.rows(`SELECT i.id FROM intake_task_projection i, json_each(i.links) e
      WHERE json_extract(e.value, '$.kind') = 'link' AND
      (json_extract(e.value, '$.hub_session_id') = ? OR json_extract(e.value, '$.native_id') = ?) LIMIT 1`,
      session.id, parse(session.binding_json)?.native_id ?? '')[0];
    return row?.id ?? null;
  }

  private screenFor(sessionId: string): { tail: string | null; ageS: number | null } {
    const row = this.rows(`SELECT ack_json, updated_at FROM session_control_commands
      WHERE session_id = ? AND kind = 'capture' AND state = 'acked' ORDER BY updated_at DESC LIMIT 1`, sessionId)[0];
    if (!row) return { tail: null, ageS: null };
    const capture = parse(row.ack_json).capture;
    const at = Date.parse(row.updated_at);
    const age = Number.isFinite(at) ? Math.max(0, (this.now() - at) / 1000) : null;
    return { tail: typeof capture === 'string' && capture ? capture.slice(-SCREEN_TAIL_CHARS) : null, ageS: age };
  }

  /** Newest unresolved actionable decision per session (drives sequencing). */
  private pendingPrior(): Map<string, Obj> {
    const out = new Map<string, Obj>();
    for (const d of this.rows(`SELECT * FROM session_audit_decisions
        WHERE outcome IS NULL AND action IN ('keystroke','nudge','stop','capture') ORDER BY id`)) {
      out.set(sessionKey(d.machine_id, d.native_id, d.session_id), d);
    }
    return out;
  }

  private priorSteps(): Map<string, number> {
    const out = new Map<string, number>();
    for (const d of this.rows(`SELECT machine_id, native_id, session_id FROM session_audit_decisions
        WHERE action IN ('nudge','escalate')`)) {
      const key = sessionKey(d.machine_id, d.native_id, d.session_id);
      out.set(key, (out.get(key) ?? 0) + 1);
    }
    return out;
  }

  /** Outcome of the previous run's actions, evaluated on current evidence (AC2
   * "verified gone, not just rc 0"; AC3 outcome after next audit). */
  private resolveOutcomes(pending: Map<string, Obj>, natives: Map<string, Obj>, catalogs: Map<string, number>, now: number) {
    for (const [key, d] of pending) {
      const native = d.machine_id && d.native_id ? natives.get(d.machine_id + '/' + d.native_id) : null;
      const fresh = !!d.machine_id && catalogs.has(d.machine_id);
      const terminal = !!native && TERMINAL_PHASES.has(native.phase);
      const cmd = d.action_key ? this.rows('SELECT * FROM session_control_commands WHERE id = ?', d.action_key)[0] : null;
      const acked = cmd?.state === 'acked';
      const age = now - Date.parse(d.at);
      let outcome: string | null = null;
      if (d.action === 'stop') {
        if (acked || (terminal && fresh)) outcome = native && fresh && !terminal ? 'stop_unverified' : 'stopped_verified';
        else if (age > 2 * this.interval) outcome = 'stop_timeout';
      } else if (d.action === 'capture') {
        const screen = d.session_id ? this.screenFor(d.session_id) : { tail: null, ageS: null };
        const screenAt = screen.ageS !== null ? now - screen.ageS * 1000 : null;
        if (screen.tail && screenAt !== null && screenAt >= Date.parse(d.at) - 60000) outcome = 'screen_available';
        else if (acked) outcome = screen.tail ? 'screen_available' : 'capture_empty';
        else if (age > 2 * this.interval) outcome = 'capture_timeout';
      } else if (d.action === 'keystroke' || d.action === 'nudge') {
        const progress = native ? nativeProgress(native) : null;
        if (progress !== null && progress > Date.parse(d.at)) outcome = 'progressed';
        else if (acked) outcome = 'delivered';
        else if (age > 2 * this.interval) outcome = 'send_timeout';
      }
      if (outcome) {
        this.sql.exec('UPDATE session_audit_decisions SET outcome = ?, outcome_at = ? WHERE id = ? AND outcome IS NULL',
          outcome, new Date(now).toISOString(), d.id);
        d.outcome = outcome;
      }
    }
  }

  private collect(now: number): { audits: AuditFeatures[] } {
    const sessions = this.rows(`SELECT * FROM session_control_sessions
      WHERE state IN ('queued','held','offered','starting','running','uncertain')`);
    const nativeRows = this.rows('SELECT * FROM fleet_sessions');
    const natives = new Map<string, Obj>(nativeRows.map(n => [n.machine_id + '/' + n.native_id, n]));
    const catalogs = freshCatalogs(this.sql, now);    const cpuByMachine = new Map<string, Obj>();
    for (const m of this.rows('SELECT id, last_heartbeat FROM machines')) {
      const usage = sanitizeCpuUsage(parse(m.last_heartbeat).cpu_usage);
      if (usage) cpuByMachine.set(m.id, { busy_percent: usage.busy_percent, busy_cores: usage.busy_cores, cores: usage.cores, observed_at: usage.observed_at });
    }
    const worktrees = new Map<string, Obj>();
    for (const w of this.rows("SELECT machine, path, status_json, status_at FROM intake_worktrees WHERE state = 'active'")) {
      worktrees.set(w.machine + '|' + w.path, { ...parse(w.status_json), checked_at: w.status_at ? new Date(Number(w.status_at)).toISOString() : null });
    }
    const protectedIds = new Set(this.rows('SELECT session_id FROM session_control_protections').map(r => r.session_id));
    const boundNatives = new Set(sessions
      .filter((s: Obj) => s.machine_id && parse(s.binding_json)?.native_id)
      .map((s: Obj) => s.machine_id + '/' + parse(s.binding_json).native_id));
    const pending = this.pendingPrior();
    const steps = this.priorSteps();
    const audits: AuditFeatures[] = [];
    for (const s of sessions) {
      const request = parse(s.request_json);
      const binding = s.binding_json ? parse(s.binding_json) : null;
      const native = binding?.native_id && s.machine_id ? natives.get(s.machine_id + '/' + binding.native_id) : null;
      const bound = !!(binding?.native_id && s.machine_id);
      const linked = !!(native && !TERMINAL_PHASES.has(native.phase) && !UNOBSERVED_PHASES.has(native.phase) && s.state === 'running');
      const last = native ? nativeProgress(native) : null;
      const started = binding?.native_created_at_ms ?? native?.created_at_ms ?? Date.parse(s.created_at);
      const workspace = binding?.workspace || request.workspace || native?.workspace || null;
      const wt = s.machine_id && workspace ? worktrees.get(s.machine_id + '|' + workspace) ?? null : null;
      const intake = request.intake_task_id ?? this.linkedIntakeId(s) ?? null;
      const screen = s.state === 'running' && bound ? this.screenFor(s.id) : { tail: null, ageS: null };
      const key = sessionKey(s.machine_id, binding?.native_id, s.id);
      audits.push({
        session_id: s.id, machine_id: s.machine_id ?? null, native_id: binding?.native_id ?? null,
        intake, engine: s.engine ?? null, mode: s.mode ?? null, state: s.state,
        actor: typeof s.actor === 'string' ? s.actor : null,
        bound, linked, protected_session: protectedIds.has(s.id),
        runtime_s: Number.isFinite(started) ? Math.max(0, (now - started) / 1000) : null,
        idle_s: last === null ? null : Math.max(0, (now - last) / 1000),
        host_cpu: s.machine_id ? cpuByMachine.get(s.machine_id) ?? null : null,
        git: wt ? { dirty: wt.dirty ?? null, dirty_count: wt.dirty_count ?? null, unpushed: wt.unpushed ?? null, checked_at: wt.checked_at ?? null } : null,
        intake_state: intake ? this.intakeStatus(intake) : null,
        screen_tail: screen.tail, screen_age_s: screen.ageS,
        nudges: steps.get(key) ?? 0,
        prior: pending.get(key) ?? null,
        nativePhase: native?.phase ?? null,
      });
    }
    // Unlinked live aplexer natives (observed, never Hub-bound): escalate-only.
    for (const n of nativeRows) {
      const key = n.machine_id + '/' + n.native_id;
      if (!catalogs.has(n.machine_id) || !['running', 'starting'].includes(n.phase) || boundNatives.has(key)) continue;
      const last = nativeProgress(n);
      const wt = n.workspace ? worktrees.get(n.machine_id + '|' + n.workspace) ?? null : null;
      audits.push({
        session_id: null, machine_id: n.machine_id, native_id: n.native_id, intake: null,
        engine: n.engine ?? null, mode: null, state: 'observed', bound: false, linked: false,
        actor: null, protected_session: false,
        runtime_s: Number.isFinite(n.created_at_ms) ? Math.max(0, (now - Number(n.created_at_ms)) / 1000) : null,
        idle_s: last === null ? null : Math.max(0, (now - last) / 1000),
        host_cpu: cpuByMachine.get(n.machine_id) ?? null,
        git: wt ? { dirty: wt.dirty ?? null, dirty_count: wt.dirty_count ?? null, unpushed: wt.unpushed ?? null, checked_at: wt.checked_at ?? null } : null,
        intake_state: null, screen_tail: null, screen_age_s: null,
        nudges: steps.get(key) ?? 0, prior: pending.get(key) ?? null, nativePhase: n.phase,
      });
    }
    return { audits };
  }

  private async act(session: AuditFeatures, d: AuditDecision, key: string): Promise<{ actionKey: string | null; error: string | null }> {
    if (!this.hooks.request) return { actionKey: null, error: 'no_channel' };
    const path = ({ keystroke: 'send', nudge: 'send', capture: 'capture', stop: 'stop' } as Record<string, string>)[d.action];
    const body: Obj = { key };
    if (d.action === 'keystroke' || d.action === 'nudge') body.body = d.send_text ?? 'continue';
    if (d.action === 'capture') { body.mode = 'screen'; body.max_bytes = 8192; }
    try {
      const res = await this.hooks.request(`/api/sessions/${session.session_id}/${path}`, { method: 'POST', body: JSON.stringify(body) });
      if (res.status >= 400) return { actionKey: null, error: String(res.body?.error || `status_${res.status}`).slice(0, 120) };
      return { actionKey: res.body?.command_id ?? res.body?.id ?? null, error: null };
    } catch (e: any) {
      return { actionKey: null, error: String(e?.message || 'request_failed').slice(0, 120) };
    }
  }

  /** One audit pass. `manual` (admin endpoint) bypasses the 30-minute gate for
   * live proof; scheduled ticks keep the interval. */
  async tick(source = 'scheduled'): Promise<Obj> {
    const now = this.now();
    const meta: any = this.rows('SELECT last_tick FROM session_audit_meta WHERE id=1')[0] ?? {};
    if (source !== 'manual' && meta.last_tick !== null && meta.last_tick !== undefined && now - Number(meta.last_tick) < this.interval)
      return { state: 'already_audited', last_run_at: meta.last_tick };
    this.sql.exec('UPDATE session_audit_meta SET last_tick=?, last_source=? WHERE id=1', now, source);

    const thresholdS = idleThresholdFrom(this.env);
    const decider = this.decider(thresholdS);
    const natives = new Map(this.rows('SELECT * FROM fleet_sessions').map(n => [n.machine_id + '/' + n.native_id, n]));
    const catalogs = freshCatalogs(this.sql, now);
    this.resolveOutcomes(this.pendingPrior(), natives, catalogs, now);

    const { audits } = this.collect(now);
    const runAt = new Date(now).toISOString();
    this.sql.exec('INSERT INTO session_audit_runs(at,source,scanned,counts_json) VALUES (?,?,?,?)', runAt, source, audits.length, '{}');
    const runId = Number(this.rows('SELECT id FROM session_audit_runs ORDER BY id DESC LIMIT 1')[0].id);
    const counts: Obj = { observed: 0, capture: 0, keystroke: 0, nudge: 0, stop: 0, escalate: 0, enqueue_failed: 0, skipped: 0 };
    let actionBudget = ACTION_BUDGET, captureBudget = CAPTURE_BUDGET;
    const escalated: string[] = [];
    let seq = 0;

    for (const f of audits) {
      const d = decider.decide(f);
      if (d.action === 'observe') counts.observed++;
      let actionKey: string | null = null;
      let failure: string | null = null;
      if (d.action === 'escalate') {
        if (escalated.length < ESCALATE_BUDGET) escalated.push(`${f.session_id ?? f.native_id} ${f.machine_id ?? ''} ${d.rule}: ${d.detail}`.slice(0, 200));
        counts.escalate++;
      } else if (d.action === 'capture') {
        if (captureBudget <= 0) counts.skipped++;
        else { captureBudget--; counts.capture++; const r = await this.act(f, d, `sa-r${runId}-${++seq}`); actionKey = r.actionKey; failure = r.error; }
      } else if (ACTIONABLE.has(d.action)) {
        if (actionBudget <= 0) counts.skipped++;
        else {
          actionBudget--; counts[d.action]++;
          const r = await this.act(f, d, `sa-r${runId}-${++seq}`);
          actionKey = r.actionKey; failure = r.error;
        }
      }
      if (failure) counts.enqueue_failed++;
      this.sql.exec(`INSERT INTO session_audit_decisions
        (run_id, at, session_id, machine_id, native_id, intake, engine, mode, idle_s, rule, action, detail, action_key, features_json, screen_tail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
        runId, runAt, f.session_id, f.machine_id, f.native_id, f.intake, f.engine, f.mode,
        f.idle_s === null ? null : Math.round(f.idle_s), d.rule,
        failure ? d.action + '_failed' : d.action, String(d.detail).slice(0, MAX_DETAIL), actionKey,
        JSON.stringify({ ...f, screen_tail: undefined }), f.screen_tail);
    }

    let alert: string | null = null;
    if (escalated.length && this.hooks.notify) {
      alert = escalated.slice(0, 10).join('; ');
      try { await this.hooks.notify('session_audit', 'idle-actions', alert); } catch { /* alert retry elsewhere */ }
    }
    // Unverified stops stay visible until a host census proves the exit (AC2).
    const unverified = this.rows(`SELECT session_id, machine_id, native_id FROM session_audit_decisions
      WHERE action = 'stop' AND outcome IN ('stop_unverified','stop_timeout') AND run_id = ?`, runId);
    if (unverified.length && this.hooks.notify) {
      const detail = 'stop not verified by host census: ' + unverified.slice(0, 10).map(u => `${u.session_id ?? u.native_id} on ${u.machine_id}`).join('; ');
      try { await this.hooks.notify('session_audit', 'stop-unverified', detail); } catch { /* alert retry elsewhere */ }
      alert = alert ? alert + ' | ' + detail : detail;
    }
    const result = { state: 'audited', run_id: runId, at: runAt, scanned: audits.length, counts, threshold_s: thresholdS };
    this.sql.exec('UPDATE session_audit_runs SET counts_json=?, alert=? WHERE id=?', JSON.stringify(counts), alert, runId);
    this.sql.exec('UPDATE session_audit_meta SET last_result=? WHERE id=1', JSON.stringify(result));
    this.prune(now);
    return result;
  }

  /** Dataset stays bounded: decisions and runs older than 7 days go first
   * (a decision's run carries the same at, so both age out together). */
  private prune(now: number) {
    const cutoff = new Date(now - RETENTION_MS).toISOString();
    this.sql.exec(`DELETE FROM session_audit_decisions WHERE id IN (SELECT id FROM session_audit_decisions WHERE at < ? ORDER BY id LIMIT ?)`, cutoff, RETENTION_DELETE);
    this.sql.exec(`DELETE FROM session_audit_runs WHERE id IN (SELECT id FROM session_audit_runs WHERE at < ? ORDER BY id LIMIT ?)`, cutoff, RETENTION_DELETE);
  }
}
