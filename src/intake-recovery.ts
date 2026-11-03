/** Recovery decisions never substitute a stall observation for native exit proof. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { GOAL_ENGINES } from './goal-engines';
import { modelDisabledByPolicy } from './model-routing';
import { blockingHealthRow } from './engine-health';
import { FleetService, disabledEngines } from './fleet';

const heldReconcilers = new WeakMap<SqlStorage, () => void>();
export function registerHeldReconciler(sql: SqlStorage, fn: () => void): void {
  heldReconcilers.set(sql, fn);
}
export function runHeldReconciler(sql: SqlStorage): void {
  heldReconcilers.get(sql)?.();
}

/** A second canonical row must not bypass protection of the same native worker. */
export function recoveryProtected(sql: SqlStorage, session: { id: string; machine_id?: string; binding_json?: string }): boolean {
  if (sql.exec('SELECT session_id FROM session_control_protections WHERE session_id=?',session.id).toArray().length) return true;
  let binding: any;
  try { binding = JSON.parse(session.binding_json || 'null'); } catch { return false; }
  if (!session.machine_id || !binding?.native_id) return false;
  return sql.exec(`SELECT s.id FROM session_control_sessions s JOIN session_control_protections p ON p.session_id=s.id
    WHERE s.machine_id=? AND json_extract(CASE WHEN json_valid(s.binding_json) THEN s.binding_json ELSE '{}' END,'$.native_id')=? LIMIT 1`,
    session.machine_id,binding.native_id).toArray().length > 0;
}

/** Waiting belongs to the physical worker too, even through an unlinked alias. */
export function recoveryWakePending(sql: SqlStorage, session: { id: string; machine_id?: string; binding_json?: string }, now: number): boolean {
  if (sql.exec("SELECT id FROM session_control_wakes WHERE session_id=? AND state='pending' AND wake_at_ms>? LIMIT 1",session.id,now).toArray().length) return true;
  let binding: any;
  try { binding = JSON.parse(session.binding_json || 'null'); } catch { return false; }
  if (!session.machine_id || !binding?.native_id) return false;
  return sql.exec(`SELECT w.id FROM session_control_sessions s JOIN session_control_wakes w ON w.session_id=s.id
    WHERE s.machine_id=? AND json_extract(s.binding_json,'$.native_id')=? AND w.state='pending' AND w.wake_at_ms>? LIMIT 1`,
    session.machine_id,binding.native_id,now).toArray().length > 0;
}

export type RecoverySession = {
  id: string;
  state: string;
  lifecycle_state?: string;
  stalled?: boolean;
  stall_reason?: string | null;
  waiting_until?: string | null;
};

export function recoveryAction(session: RecoverySession, now: number): 'stop' | 'restart' | 'wait' {
  // A scheduled wake is deliberate waiting, even when an older stall is retained.
  const wake = Date.parse(session.waiting_until ?? '');
  if (Number.isFinite(wake) && wake > now) return 'wait';
  const lifecycle = session.lifecycle_state ?? session.state;
  if (['failed', 'refused', 'cancelled', 'stopped', 'engine_failed', 'engine_oom_killed'].includes(lifecycle)) return 'restart';
  if (lifecycle === 'running' && (session.stalled || session.state === 'stalled') &&
      ['no_native_progress', 'agent_exited_children_running', 'agent_exited'].includes(session.stall_reason ?? '')) return 'stop';
  return 'wait';
}

export type LaunchOutcome = {
  id: string; machine_id: string; engine: string; state: string;
  updated_at: string; created_at?: string; native_id?: string | null; verification_status?: string | null;
  exit_reason?: string | null;
};

/** Count definite launcher failures on one host/engine after its last successful
 * explicit probe. Order attempts by creation, so an older healthy process
 * exiting late cannot erase newer failures. Uncertain dispatch and quota reaps
 * cannot prove a broken launcher. Deterministic ties use the launch id. */
export function launchFailureStreak(outcomes: LaunchOutcome[], machine: string, engine: string,
  probeSucceededAt: string | null = null): number {
  const probe = probeSucceededAt === null ? -Infinity : Date.parse(probeSucceededAt);
  if (Number.isNaN(probe)) return 0;
  const recent = outcomes.filter(o => o.machine_id === machine && o.engine === engine && Date.parse(o.updated_at) > probe)
    .sort((a, b) => Date.parse(b.created_at ?? b.updated_at) - Date.parse(a.created_at ?? a.updated_at) || b.id.localeCompare(a.id));
  let streak = 0;
  for (const o of recent) {
    if (o.native_id && ['running', 'exited'].includes(o.state)) break;
    if (o.state !== 'failed' || !['not_started','verified'].includes(o.verification_status ?? '')) continue;
    if (!['launcher_command_failed', 'dispatch_local_command_failed', 'dispatch_claim_refused'].includes(o.exit_reason ?? '')) break;
    streak++;
  }
  return streak;
}

/** Eligibility is supplied by canonical admission, including operator engine
 * policy and account quota gates. Ordering alone never grants launch authority. */
export function recoveryEngineOrder(eligible: string[], previous?: string): string[] {
  const preferred = ['claude', 'codex', 'opencode', 'zcodex', 'antigravity'];
  const unique = [...new Set(eligible)];
  const rank = (engine: string) => {
    const index = preferred.indexOf(engine);
    return index < 0 ? preferred.length : index;
  };
  return unique.sort((a, b) => Number(a === previous) - Number(b === previous) || rank(a) - rank(b) || a.localeCompare(b));
}

const parseJson = (raw: any, fallback: any = {}): any => {
  try { return JSON.parse(raw || 'null') ?? fallback; } catch { return fallback; }
};

export function isIntakeRecoverySession(row: any, req?: any): boolean {
  const parsedReq = req ?? parseJson(row?.request_json, {});
  const reason = typeof parsedReq?.engine_override_reason === 'string' ? parsedReq.engine_override_reason.trim() : '';
  if (reason.startsWith('Intake recovery')) return true;
  return row?.actor === 'hub:service:intake-watchdog' && Boolean(parsedReq?.engine || row?.engine);
}

export const REPLACEABLE_RECOVERY_REJECTIONS = new Set([
  'zcodex_session_failed',
  'engine_already_failed_for_intake',
  'intake_engine_failed',
  'account_blocked',
  'account_unmapped',
  'account_unknown',
  'account_unavailable',
  'provider_disabled',
  'model_disabled_by_policy',
  'model_unsupported',
  'tier_unavailable',
  'goal_unsupported',
  'mode_unavailable',
  'engine_unavailable',
  'capability_unavailable',
  'launch_failures_quarantined',
  'engine_auth_expired',
  'engine_tool_permission_refused',
  'admission_unconfigured',
  'credential_invalid',
  'credential_rejected',
  'not_logged_in',
  'worker_silent_start',
  'quota_exhausted',
  'quota_limit_reached',
  'quota_window_exhausted',
  'quota_reserve',
  'quota_unavailable',
  'quota_unknown',
  'quota_stale',
  'no_quota_evidence',
  'reserve',
  'limit_reached',
  'cooldown_active',
]);

export function isReplaceableRecoveryRejection(reason: unknown): boolean {
  return typeof reason === 'string' && REPLACEABLE_RECOVERY_REJECTIONS.has(reason);
}

const TERMINAL_FAILED_STATES = new Set([
  'failed', 'refused', 'cancelled', 'engine_failed', 'engine_oom_killed', 'stopped', 'dead', 'exited',
]);

const NON_ENGINE_CANCEL_REASONS = new Set([
  'superseded_by_newer_session', 'held_expired', 'intake_task_closed',
]);

/** Collect every engine that has already terminally failed or been refused for this intake. */
export function intakeFailedEngines(
  sql: SqlStorage,
  taskId: string | null | undefined,
  opts?: { excludeSessionId?: string; extraFailed?: Iterable<[string, string]> },
): Map<string, string> {
  const failed = new Map<string, string>();
  if (opts?.extraFailed) {
    for (const [eng, reason] of opts.extraFailed) {
      if (eng && !failed.has(eng)) failed.set(eng, reason || 'engine_already_failed_for_intake');
    }
  }
  if (!taskId || typeof taskId !== 'string') return failed;
  try {
    const hasSessions = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_sessions'").toArray().length > 0;
    if (!hasSessions) return failed;
    const rows = sql.exec(
      "SELECT id, engine, state, fleet_launch_id, binding_json, request_json, hold_json FROM session_control_sessions WHERE json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END, '$.intake_task_id') = ? LIMIT 128",
      taskId,
    ).toArray() as any[];
    const seenIds = new Set(rows.map(r => String(r.id)));
    try {
      const hasIntake = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_tasks'").toArray().length > 0;
      if (hasIntake) {
        const tRow: any = sql.exec('SELECT data FROM intake_tasks WHERE id = ?', taskId).toArray()[0];
        const tData = parseJson(tRow?.data, {});
        const linkIds = (Array.isArray(tData?.history) ? tData.history : [])
          .map((e: any) => e?.hub_session_id)
          .filter((id: any): id is string => typeof id === 'string' && !!id && !seenIds.has(id));
        if (linkIds.length) {
          for (const s of sql.exec(
            'SELECT id, engine, state, fleet_launch_id, binding_json, request_json, hold_json FROM session_control_sessions WHERE id IN (SELECT value FROM json_each(?))',
            JSON.stringify(linkIds.slice(0, 128)),
          ).toArray() as any[]) {
            rows.push(s);
            seenIds.add(String(s.id));
          }
        }
      }
    } catch { /* optional intake table */ }
    const hasLaunches = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_launches'").toArray().length > 0;
    for (const s of rows) {
      if (opts?.excludeSessionId && s.id === opts.excludeSessionId) continue;
      if (!TERMINAL_FAILED_STATES.has(String(s.state))) continue;
      const req = parseJson(s.request_json, {});
      const hold = parseJson(s.hold_json, {});
      if (s.state === 'cancelled' && !s.fleet_launch_id && !s.binding_json && NON_ENGINE_CANCEL_REASONS.has(String(hold?.reason))) {
        continue;
      }
      let launchEngine: string | null = null;
      let launchReason: string | null = null;
      if (hasLaunches && s.fleet_launch_id) {
        try {
          const l: any = sql.exec('SELECT engine, exit_reason FROM fleet_launches WHERE id = ?', s.fleet_launch_id).toArray()[0];
          if (typeof l?.engine === 'string' && l.engine) launchEngine = l.engine;
          if (typeof l?.exit_reason === 'string' && l.exit_reason) launchReason = l.exit_reason;
        } catch { /* ignore */ }
      }
      const eng = (typeof s.engine === 'string' && s.engine) ? s.engine
        : (typeof req?.engine === 'string' && req.engine) ? req.engine
        : launchEngine;
      if (!eng) continue;
      const rawHoldReason = hold?.rejected?.[0]?.reason || hold?.reason || launchReason;
      const specificReason = typeof rawHoldReason === 'string' && rawHoldReason &&
        !['no_eligible_route', 'placement_held', 'placement_refused'].includes(rawHoldReason) &&
        !rawHoldReason.startsWith('no_eligible_route(')
        ? rawHoldReason
        : 'engine_already_failed_for_intake';
      if (!failed.has(eng)) failed.set(eng, specificReason);
    }
  } catch { /* best-effort */ }
  return failed;
}

export function formatRecoveryHoldReason(rejected: { engine?: string; reason?: string }[], prefix = 'no_eligible_route'): string {
  const byEngine = new Map<string, string>();
  for (const r of rejected) {
    const eng = typeof r?.engine === 'string' && r.engine ? r.engine : '';
    const reason = typeof r?.reason === 'string' && r.reason ? r.reason : 'ineligible';
    if (eng && !byEngine.has(eng)) byEngine.set(eng, reason);
  }
  if (!byEngine.size) return prefix;
  return `${prefix}(${[...byEngine.entries()].map(([e, r]) => `${e}:${r}`).join(',')})`;
}

export function candidateInputForEngine(baseInput: Record<string, any>, engine: string, sql?: SqlStorage): Record<string, any> {
  const next: Record<string, any> = { ...baseInput, engine };
  if (engine === 'claude') {
    if (!next.tier) next.tier = 'review';
  } else if (baseInput.engine === 'claude' && next.tier === 'review') {
    delete next.tier;
  }
  if (engine === 'codex' || engine === 'zcodex') {
    let goalSupported = true;
    if (sql) {
      try {
        const hasRoutes = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_routes'").toArray().length > 0;
        if (hasRoutes) {
          const routes = sql.exec('SELECT machine_id, account, profile FROM fleet_routes WHERE engine = ?', engine).toArray() as any[];
          if (routes.length > 0) {
            goalSupported = routes.some(route => {
              const m: any = sql.exec('SELECT capabilities FROM machines WHERE id = ?', route.machine_id).toArray()[0];
              const caps = parseJson(m?.capabilities, {});
              return Array.isArray(caps.fleet_goal_routes) && caps.fleet_goal_routes.some((gr: any) =>
                gr.engine === engine && gr.account === route.account && (gr.profile ?? null) === (route.profile ?? null));
            });
          }
        }
      } catch { goalSupported = true; }
    }
    if (goalSupported && (next.brief || next.prompt)) {
      next.mode = 'interactive';
      next.with_goal = true;
    } else {
      next.mode = 'headless';
      delete next.with_goal;
    }
  } else if (!GOAL_ENGINES.has(engine)) {
    next.mode = 'headless';
    delete next.with_goal;
  }
  return next;
}

const ENGINE_PROVIDERS: Record<string, string[]> = {
  codex: ['codex', 'codex-orin'],
  opencode: ['go', 'opencode'],
  zcodex: ['zai'],
  antigravity: ['gemini'],
  claude: ['claude'],
};

/** Evaluate canonical recovery engine order at creation time, skipping engines
 * that already failed for the intake, have account_blocked, provider_disabled,
 * or are unavailable in Fleet. */
export function evaluateRecoveryEngineEligibility(
  sql: SqlStorage,
  tx: <T>(fn: () => T) => T,
  now: () => number,
  order: string[],
  opts: {
    taskId?: string | null;
    failedForIntake?: Map<string, string>;
    workspace?: string | null;
    cwd?: string | null;
    project?: string | null;
    brief?: string | null;
  },
): {
  selectedEngine: string | null;
  mode: 'interactive' | 'headless';
  with_goal: boolean;
  rejected: { machine: string; engine: string; reason: string }[];
} {
  const failed = opts.failedForIntake ?? intakeFailedEngines(sql, opts.taskId);
  const disabled = disabledEngines();
  const rejected: { machine: string; engine: string; reason: string }[] = [];
  let hasLiveFleet = false;
  try {
    const hasRoutes = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_routes'").toArray().length > 0;
    const hasMachines = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='machines'").toArray().length > 0;
    if (hasRoutes && hasMachines && opts.workspace) {
      const routeCount = Number((sql.exec('SELECT COUNT(*) AS n FROM fleet_routes').one() as any)?.n || 0);
      if (routeCount > 0) {
        const nowMs = now();
        const mRows = sql.exec('SELECT last_checkin_at, drained FROM machines WHERE revoked_at IS NULL LIMIT 64').toArray() as any[];
        hasLiveFleet = mRows.some(m => !m.drained && typeof m.last_checkin_at === 'string' && Math.abs(nowMs - Date.parse(m.last_checkin_at)) <= 180000);
      }
    }
  } catch { hasLiveFleet = false; }
  const fleet = hasLiveFleet ? new FleetService(sql, tx, now) : null;

  for (const eng of order) {
    if (failed.has(eng)) {
      rejected.push({ machine: '', engine: eng, reason: failed.get(eng)! });
      continue;
    }
    if (disabled.has(eng) || modelDisabledByPolicy(eng)) {
      rejected.push({ machine: '', engine: eng, reason: 'provider_disabled' });
      continue;
    }
    const cand = candidateInputForEngine({
      workspace: opts.workspace || opts.project || 'agent-hub',
      ...(opts.cwd ? { cwd: opts.cwd } : {}),
      ...(opts.project ? { project: opts.project } : {}),
      prompt: opts.brief || 'Continue Hub intake',
      brief: opts.brief || 'Continue Hub intake',
    }, eng, sql);

    if (fleet && opts.workspace) {
      const planInput: any = {
        mode: cand.mode,
        engine: eng,
        ...(cand.tier ? { tier: cand.tier } : {}),
        ...(cand.with_goal ? { with_goal: true } : {}),
        workspace: opts.workspace,
        ...(opts.cwd ? { cwd: opts.cwd } : {}),
        ...(opts.project ? { project: opts.project } : {}),
        prompt: opts.brief || 'Continue Hub intake',
      };
      let plan: any = fleet.plan(planInput);
      if (!plan?.selected && planInput.with_goal && Array.isArray(plan?.rejected) && plan.rejected.length > 0 &&
          plan.rejected.every((r: any) => ['goal_unsupported', 'mode_unavailable'].includes(r.reason))) {
        plan = fleet.plan({ ...planInput, mode: 'headless', with_goal: undefined });
        if (plan?.selected) {
          return { selectedEngine: eng, mode: 'headless', with_goal: false, rejected };
        }
      }
      if (plan && !plan.error && plan.selected) {
        return {
          selectedEngine: eng,
          mode: plan.request?.mode === 'interactive' ? 'interactive' : 'headless',
          with_goal: plan.request?.with_goal === true,
          rejected,
        };
      }
      const planRej = Array.isArray(plan?.rejected) && plan.rejected.length
        ? plan.rejected.map((r: any) => ({ machine: String(r.machine || ''), engine: String(r.engine || eng), reason: String(r.reason || 'no_eligible_route') }))
        : [{ machine: '', engine: eng, reason: String(plan?.error || 'no_eligible_route') }];
      rejected.push(...planRej);
      continue;
    }

    // Narrow fixture fallback when fleet_routes is empty: check fleet_engine_health and fleet_accounts directly.
    try {
      const hasHealth = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_engine_health'").toArray().length > 0;
      if (hasHealth) {
        const hRows = sql.exec("SELECT engine, machine_id, status, reason, updated_at FROM fleet_engine_health WHERE engine = ? AND status = 'unavailable'", eng).toArray() as any[];
        const blocking = blockingHealthRow(hRows, cand.mode, now());
        if (blocking) {
          rejected.push({ machine: String(blocking.machine_id || ''), engine: eng, reason: String(blocking.reason || 'engine_auth_expired') });
          continue;
        }
      }
    } catch { /* optional table */ }
    try {
      const hasAccounts = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_accounts'").toArray().length > 0;
      if (hasAccounts) {
        const providers = ENGINE_PROVIDERS[eng] || [eng];
        const acctRows = sql.exec(
          `SELECT enabled FROM fleet_accounts WHERE provider IN (${providers.map(() => '?').join(',')})`,
          ...providers,
        ).toArray() as any[];
        if (acctRows.length > 0 && acctRows.every(a => !a.enabled)) {
          rejected.push({ machine: '', engine: eng, reason: 'account_blocked' });
          continue;
        }
      }
    } catch { /* optional table */ }

    return {
      selectedEngine: eng,
      mode: cand.mode === 'interactive' ? 'interactive' : 'headless',
      with_goal: cand.with_goal === true,
      rejected,
    };
  }

  return { selectedEngine: null, mode: 'headless', with_goal: false, rejected };
}
