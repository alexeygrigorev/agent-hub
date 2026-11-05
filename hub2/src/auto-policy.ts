// Autonomous queue policy for Hub2 (workstream `autonomy`, 45 deduped member ACs).
// Pure decisions over plain snapshots: no DO state, no I/O, no side effects. The
// coordinator wires each emitted action into ProjectDO/MachineDO; this module only
// decides. Native exit is never acceptance and an idle session is never reaped here.
// Anchors: 85318207 drain+overdue restarts, 2e00e01f merge gate + unmerged requeue,
// 27505f04 idle-with-open-AC nudge/replace, f0b21a52 no false done, 500fd60b 30-minute
// liveness bound, 8edc8f7c dependency DAG, 51e493de no duplicate live session,
// efcfa907/68145244 priority/promote ordering, 167c6178 false-stall exemption, 6b53e801
// zero-session backlog invariant.

export const AUTO_POLICY_DEFAULTS = {
  idleNudgeMs: 1_800_000,   // 500fd60b/27505f04: 30 min without progress opens the idle state
  nudgeRateMs: 3_600_000,   // 27505f04: at most one nudge per 60 min per session
  stallNudges: 2,           // 27505f04: two unanswered nudges trigger replacement
  nudgeAckMs: 1_800_000,    // 27505f04: each nudge gets a 30 min ack/progress window
  unmergedIdleMs: 3_600_000,// 2e00e01f AC2: 60 min with no session activity flags unmerged work
  maxAttempts: 4,           // bounded retries for restarts and finish-merge passes
} as const;
export type AutoPolicyConfig = Partial<typeof AUTO_POLICY_DEFAULTS>;

export type IntakeStatus = 'open' | 'queued' | 'running' | 'completed-awaiting-review' | 'failed'
  | 'needs_attention' | 'waiting_on_prereq' | 'unmerged' | 'accepted' | 'archived';

export interface AutoIntake {
  id: string; project: string; priority: number; status: IntakeStatus;
  blockedBy: string[]; sessionId: string | null; externalLive: boolean; attempts: number;
  createdAt: number; updatedAt: number;
  blockerKey?: string | null; qualifiedFailure?: boolean; restartQueuedAt?: number | null;
  unmergedCommits?: string[]; openAc?: string[]; promotedAt?: number | null;
  finalEvidence?: Record<string, unknown> | null;
}

export interface AutoSession {
  id: string; intakeId: string | null; lastProgressAt: number;
  nudges: { at: number; acked: boolean }[];
  inFlightTool?: { startedAt: number; maxMs: number } | null;
}

export interface AutoCapacity {
  maxInflight: number; maxPerProject: number;
  runningTotal: number; runningByProject: Record<string, number>;
}

export interface AutoState { now: number; capacity: AutoCapacity; intakes: AutoIntake[]; sessions: AutoSession[]; lastActivityAt: number }

export type AutoAction =
  | { kind: 'start'; intake_id: string; reason: 'capacity_fill' | 'prereqs_satisfied' }
  | { kind: 'requeue'; intake_id: string; reason: 'overdue_restart' | 'unmerged_requeue'; mode: 'restart' | 'finish-merge' }
  | { kind: 'recover'; session_id: string; intake_id: string; reason: 'nudges_unanswered' }
  | { kind: 'nudge'; session_id: string; intake_id: string; open_ac: string[]; attempt: number }
  | { kind: 'resume'; intake_id: string; reason: 'prereqs_satisfied' }
  | { kind: 'create_prereq'; parent_ids: string[]; blocker_key: string; priority: number; evidence: string }
  | { kind: 'alert'; reason: string; intake_id?: string; detail?: string };

export interface Classification { session_id: string; intake_id: string | null; state: 'active' | 'idle_with_open_ac' }

export interface AutoPlan {
  actions: AutoAction[]; classifications: Classification[];
  nextDeadlineAt: number | null;
  idle: { reason: 'capacity_full' | 'no_eligible_work'; detail: string } | null;
  capacity_after: AutoCapacity;
}

export interface AutoEvidence {
  acceptanceCriteriaCount: number;
  acMarks: { index: number; met: boolean; evidence: string }[];
  commits: string[]; commitsVerifiedOnMain: string[];
  deployGreenCommit: string | null; reviewApproved: boolean;
  nativeExitConfirmed?: boolean; exitCode?: number;
}

export interface Acceptance {
  decision: 'accept' | 'refuse'; refusals: string[];
  unmetAc: number[]; unverifiedCommits: string[]; missingDeploy: string | null;
}

const GUARDABLE_STATUSES: IntakeStatus[] = ['open', 'queued', 'running', 'waiting_on_prereq', 'needs_attention', 'failed', 'unmerged'];
const byQueue = (a: AutoIntake, b: AutoIntake): number =>
  (b.promotedAt ?? -Infinity) - (a.promotedAt ?? -Infinity) || a.priority - b.priority || b.createdAt - a.createdAt || a.id.localeCompare(b.id);

/** 8edc8f7c AC1: cycles are rejected and named, never silently queued. */
export function validateDependencyGraph(intakes: AutoIntake[]): { cycles: string[][]; errors: string[] } {
  const byId = new Map(intakes.map(i => [i.id, i]));
  const errors: string[] = [];
  for (const i of intakes) for (const d of i.blockedBy) if (!byId.has(d)) errors.push(`unknown_dependency:${i.id}->${d}`);
  const state = new Map<string, 0 | 1 | 2>(), stack: string[] = [], seen = new Set<string>(), cycles: string[][] = [];
  const visit = (id: string): void => {
    state.set(id, 1); stack.push(id);
    for (const d of byId.get(id)!.blockedBy) {
      const s = state.get(d) ?? 0;
      if (s === 1) {
        const members = [...new Set(stack.slice(stack.indexOf(d)).concat(d))].sort();
        const key = members.join(',');
        if (!seen.has(key)) { seen.add(key); cycles.push(members); errors.push(`dependency_cycle:${key}`); }
      } else if (s === 0 && byId.has(d)) visit(d);
    }
    stack.pop(); state.set(id, 2);
  };
  for (const i of intakes) if ((state.get(i.id) ?? 0) === 0) visit(i.id);
  return { cycles, errors };
}

/** 8edc8f7c/efcfa907/51e493de: who may be dispatched now, and why everyone else may not. */
export function eligibleIntakes(intakes: AutoIntake[], config: AutoPolicyConfig = {}): { eligible: AutoIntake[]; blocked: { id: string; reason: string }[] } {
  const cfg = { ...AUTO_POLICY_DEFAULTS, ...config };
  const cyclic = new Set(validateDependencyGraph(intakes).cycles.flat());
  const byId = new Map(intakes.map(i => [i.id, i]));
  const satisfied = (i: AutoIntake) => i.blockedBy.every(d => byId.get(d)?.status === 'accepted');
  const eligible: AutoIntake[] = [], blocked: { id: string; reason: string }[] = [];
  for (const i of intakes) {
    let reason: string | null = null;
    if (i.externalLive) reason = 'linked_live_external_session';
    else if (cyclic.has(i.id)) reason = 'dependency_cycle';
    else if (i.attempts >= cfg.maxAttempts) reason = 'attempt_cap_reached';
    else if (i.status !== 'open' && i.status !== 'waiting_on_prereq') reason = 'not_dispatchable';
    else if (!satisfied(i)) reason = 'prereq_not_accepted';
    if (reason) blocked.push({ id: i.id, reason }); else eligible.push(i);
  }
  eligible.sort(byQueue);
  return { eligible, blocked };
}

/** Evidence-based acceptance (f0b21a52, 2e00e01f AC1): native exit alone never closes an intake. */
export function evaluateAcceptance(evidence: AutoEvidence): Acceptance {
  const refusals = new Set<string>(), unmetAc: number[] = [];
  const marks = new Map(evidence.acMarks.map(m => [m.index, m]));
  for (let index = 0; index < evidence.acceptanceCriteriaCount; index++) {
    const mark = marks.get(index);
    if (!mark || !mark.met) unmetAc.push(index);
    if (!mark || !String(mark.evidence ?? '').trim()) refusals.add('ac_evidence_missing');
  }
  if (unmetAc.length) refusals.add('ac_marks_missing');
  const unverified = evidence.commits.filter(sha => !evidence.commitsVerifiedOnMain.includes(sha));
  if (unverified.length) refusals.add('unverified_commits');
  const deployOk = !!evidence.deployGreenCommit && evidence.commitsVerifiedOnMain.includes(evidence.deployGreenCommit);
  if (!deployOk) refusals.add('deploy_not_green');
  if (!evidence.reviewApproved) refusals.add('review_not_approved');
  if (!evidence.nativeExitConfirmed && evidence.acMarks.length === 0 && evidence.commits.length === 0) refusals.add('no_evidence');
  return {
    decision: refusals.size === 0 && evidence.commits.length > 0 ? 'accept' : 'refuse',
    refusals: [...refusals], unmetAc, unverifiedCommits: unverified,
    missingDeploy: deployOk ? null : (evidence.deployGreenCommit ?? null),
  };
}

/** f0b21a52 AC3-4: re-audit closed intakes; claimed shas that left origin/main reopen them. */
export function auditDone(intakes: AutoIntake[], verifySha: (sha: string) => boolean): { reopen: { intake_id: string; missing_commits: string[] }[] } {
  const reopen: { intake_id: string; missing_commits: string[] }[] = [];
  for (const intake of intakes) {
    if (intake.status !== 'accepted' && intake.status !== 'completed-awaiting-review') continue;
    const raw = intake.finalEvidence?.commits;
    const commits = Array.isArray(raw) ? raw.filter((v): v is string => typeof v === 'string') : [];
    const missing = commits.filter(sha => !verifySha(sha));
    if (missing.length) reopen.push({ intake_id: intake.id, missing_commits: missing });
  }
  return { reopen };
}

/** One pure planning pass: overdue restarts, prerequisites, unmerged requeue, liveness, capacity fill. */
export function planAutoQueue(state: AutoState, config: AutoPolicyConfig = {}): AutoPlan {
  const cfg = { ...AUTO_POLICY_DEFAULTS, ...config }, now = state.now;
  const actions: AutoAction[] = [], classifications: Classification[] = [], deadlines: number[] = [];
  const byId = new Map(state.intakes.map(i => [i.id, i]));
  const satisfied = (i: AutoIntake) => i.blockedBy.every(d => byId.get(d)?.status === 'accepted');

  // 85318207 R3 / 0841a966: restarts queued in the past execute now, oldest due first, bounded by the attempt cap.
  for (const intake of state.intakes.filter(i => i.restartQueuedAt !== undefined && i.restartQueuedAt !== null)
    .sort((a, b) => (a.restartQueuedAt as number) - (b.restartQueuedAt as number) || a.id.localeCompare(b.id))) {
    if (intake.restartQueuedAt as number <= now) {
      if (intake.attempts < cfg.maxAttempts) actions.push({ kind: 'requeue', intake_id: intake.id, reason: 'overdue_restart', mode: 'restart' });
      else actions.push({ kind: 'alert', reason: 'attempt_cap_reached', intake_id: intake.id, detail: `attempts=${intake.attempts}` });
    } else deadlines.push(intake.restartQueuedAt as number);
  }

  // 8edc8f7c AC2/AC4: one shared prerequisite per blocker key, at the highest parent priority.
  const waiting = state.intakes.filter(i => i.status === 'waiting_on_prereq' && !satisfied(i) && i.blockerKey);
  const parentIds = new Set(waiting.map(i => i.id));
  const groups = new Map<string, AutoIntake[]>();
  for (const w of waiting) {
    const key = w.blockerKey as string;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(w);
  }
  for (const [key, parents] of groups) {
    if (state.intakes.some(o => o.blockerKey === key && !parentIds.has(o.id))) continue;
    actions.push({
      kind: 'create_prereq', parent_ids: parents.map(p => p.id).sort(), blocker_key: key,
      priority: Math.min(...parents.map(p => p.priority)), evidence: `blocker:${key}`,
    });
  }

  // 8edc8f7c AC3: satisfied parents re-enter the queue.
  const resumed = new Set<string>();
  for (const intake of state.intakes) {
    if (intake.status !== 'waiting_on_prereq' || !satisfied(intake)) continue;
    actions.push({ kind: 'resume', intake_id: intake.id, reason: 'prereqs_satisfied' });
    resumed.add(intake.id);
  }

  // 2e00e01f AC2-4: unmerged work requeues a finish-merge pass after 60 idle minutes;
  // exhausted passes raise a merge_blocked prerequisite instead of looping.
  for (const intake of state.intakes) {
    if (intake.status !== 'completed-awaiting-review' || !intake.unmergedCommits?.length) continue;
    const dueIn = cfg.unmergedIdleMs - (now - intake.updatedAt);
    if (dueIn > 0) { deadlines.push(now + dueIn); continue; }
    if (intake.attempts < cfg.maxAttempts) actions.push({ kind: 'requeue', intake_id: intake.id, reason: 'unmerged_requeue', mode: 'finish-merge' });
    else actions.push({
      kind: 'create_prereq', parent_ids: [intake.id], blocker_key: 'merge_blocked',
      priority: intake.priority, evidence: `unmerged_commits:${intake.unmergedCommits.join(',')}`,
    });
  }

  // 500fd60b / 27505f04 / 167c6178: classify every linked session; nudge once per hour,
  // replace after two unanswered nudges; a bounded in-flight tool call is not a stall.
  for (const s of state.sessions) {
    const intake = s.intakeId ? byId.get(s.intakeId) : undefined;
    if (!intake || !GUARDABLE_STATUSES.includes(intake.status)) {
      classifications.push({ session_id: s.id, intake_id: s.intakeId, state: 'active' }); continue;
    }
    const tool = s.inFlightTool;
    if (tool && now < tool.startedAt + tool.maxMs) {
      deadlines.push(tool.startedAt + tool.maxMs);
      classifications.push({ session_id: s.id, intake_id: s.intakeId, state: 'active' }); continue;
    }
    if (now - s.lastProgressAt < cfg.idleNudgeMs) {
      deadlines.push(s.lastProgressAt + cfg.idleNudgeMs);
      classifications.push({ session_id: s.id, intake_id: s.intakeId, state: 'active' }); continue;
    }
    classifications.push({ session_id: s.id, intake_id: s.intakeId, state: 'idle_with_open_ac' });
    const nudges = [...s.nudges].sort((a, b) => a.at - b.at), last = nudges[nudges.length - 1];
    const unacked = nudges.filter(n => !n.acked).length;
    if (last && now - last.at < cfg.nudgeRateMs) {
      if (unacked >= cfg.stallNudges) {
        deadlines.push(last.at + cfg.nudgeAckMs);
        if (now >= last.at + cfg.nudgeAckMs) actions.push({ kind: 'recover', session_id: s.id, intake_id: intake.id, reason: 'nudges_unanswered' });
      } else deadlines.push(last.at + cfg.nudgeRateMs);
    } else {
      actions.push({ kind: 'nudge', session_id: s.id, intake_id: intake.id, open_ac: [...(intake.openAc ?? [])], attempt: unacked + 1 });
      deadlines.push(now + cfg.nudgeAckMs);
    }
  }

  // 85318207 R1 / efcfa907 / 68145244: fill free capacity across every real project,
  // highest priority first, promoted intakes first, newest first within a priority.
  const { eligible } = eligibleIntakes(state.intakes, config);
  const running = { ...state.capacity.runningByProject };
  let total = state.capacity.runningTotal;
  for (const intake of eligible) {
    if (total >= state.capacity.maxInflight) break;
    if ((running[intake.project] ?? 0) >= state.capacity.maxPerProject) continue;
    total++; running[intake.project] = (running[intake.project] ?? 0) + 1;
    actions.push({ kind: 'start', intake_id: intake.id, reason: resumed.has(intake.id) ? 'prereqs_satisfied' : 'capacity_fill' });
  }

  // 500fd60b AC4 / 6b53e801: a kick from a zero-session backlog records why it was idle.
  const started = actions.filter(a => a.kind === 'start').length;
  if (started > 0 && state.capacity.runningTotal === 0 && now - state.lastActivityAt >= cfg.idleNudgeMs)
    actions.push({ kind: 'alert', reason: 'idle_backlog_kick', detail: `started=${started} idle_ms=${now - state.lastActivityAt}` });

  let idle: AutoPlan['idle'] = null;
  if (started === 0) {
    if (eligible.length > 0) idle = { reason: 'capacity_full', detail: `eligible=${eligible.length} running=${state.capacity.runningTotal}` };
    else if (state.intakes.some(i => !['accepted', 'archived', 'running', 'completed-awaiting-review'].includes(i.status)))
      idle = { reason: 'no_eligible_work', detail: `intakes=${state.intakes.length}` };
  }

  const future = deadlines.filter(d => d > now);
  return {
    actions, classifications,
    nextDeadlineAt: future.length ? Math.min(...future) : null, idle,
    capacity_after: { ...state.capacity, runningTotal: total, runningByProject: running },
  };
}
