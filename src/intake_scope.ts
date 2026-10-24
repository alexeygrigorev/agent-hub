/** F2 Hub scope rule: project registry + classifier hint + single start gate.
 * Pure module (no SQL, no worker-only imports): imported by src/intake.ts and
 * src/session_control.ts so HTTP, CLI and MCP go through one check (both ride
 * the same HTTP gates).
 *
 * Policy is versioned. Tasks created before F2 have no `scope` field and are
 * grandfathered (existing intakes untouched). Tasks with a stale
 * policy_version fail closed until a fresh operator decision exists.
 */

import { sha256Sync } from './conversation-followthrough';

export const SCOPE_POLICY_VERSION = 1;

export interface ScopeProject {
  scope_terms: string[];
  not_terms: string[];
}

/** Registry adjusted per F1 AC1 (t1405u-t1410u):
 * - Hub may control ANY session: the old "control of sessions Hub did not
 *   start without explicit operator claim" item is gone from the Hub NOT-list.
 * - In PocketShell, features Hub already does are REMOVED (not frozen), and
 *   there is no pocketshell-mcp / business MCP namespace in either product. */
export const SCOPE_PROJECTS: Record<string, ScopeProject> = {
  'agent-hub': {
    scope_terms: [
      'intakes and acceptance criteria',
      'operator rules and grants',
      'routing tiers',
      'quota admission and pacing',
      'engine-account provisioning',
      'machine registry for execution eligibility',
      'managed sessions with receipts, custody, lineage, cleanup',
      'bus and conversations',
      'work alerts and metrics',
      'hub-mcp',
      'hub web UI including phone browser',
      'bounded capture snapshots and task artifacts',
    ],
    not_terms: [
      'live terminal transport',
      'pty streams',
      'raw keystrokes',
      'resize proxy',
      'pty',
      'xterm',
      'attach proxy',
      'ssh tunnel',
      'nat relay',
      'port forward',
      'sftp file browser',
      'sftp',
      'host file browser',
      'port forwarding',
      'ssh tunnels',
      'nat relays',
      'ssh keys',
      'vault sync',
      'settings sync',
      'gateway tokens',
      'broker tokens',
      'pocketshell login',
      'native apps',
      'voice ime',
      'voice input',
      'ime',
      'human input device',
      'aplexer internals',
      'session runtime internals',
      'native identity',
      'engine profile definitions',
      'state directory logic',
      'path logic',
      'runtime dependency on pocketshell',
      'generic saas connector',
      'multiuser saas',
      'pocketshell-mcp',
      'business mcp namespace',
      'business mcp',
      'dataops mcp',
      'dapier',
      'aisl agent tools',
    ],
  },
  pocketshell: {
    scope_terms: [
      'e2e ssh and terminal',
      'human input including voice ime',
      'host trust, keys, vault, zero-knowledge sync',
      'gateway nat tunnel and device enrollment',
      'sftp files and port forwards as connected-machine tools',
      'per-host sessions and workspaces',
      'manual start and kill on own hosts',
      'display-only quse',
      'clients and site',
    ],
    not_terms: [
      'model tier routing',
      'model routing',
      'tier routing',
      'routing tiers',
      'quota admission',
      'quota queues',
      'retries of agent work',
      'task approval ledger',
      'task ledger',
      'ac ledger',
      'approval ledger',
      'cards',
      'checklists',
      'acceptance criteria ledger',
      'cards checklists',
      'fleet placement',
      'engine-account provisioning',
      'agent-to-agent orchestration',
      'quota history',
      'reset timers',
      'quota capture',
      'work notifications',
      'continue agent',
      'handoff export',
      'work push',
      'engine profile catalog',
      'engine profile definitions',
      'business mcp',
      'pocketshell-mcp',
      'dataops mcp',
      'dapier',
      'aisl tools',
      'requiring hub',
      'storing hub tokens',
      'hub credentials',
      'hub login',
      'silent fallback after hub refusal',
      'mcp session write',
      'psc automation',
      'cli push',
      'usage capture',
      'scheduler hooks',
      'conversations handoff',
      'engine catalog',
    ],
  },
};

/** A research request may mention forbidden features without requesting them.
 * A research keyword anywhere is not an exemption for mixed implementation. */
export function isResearchHint(text: string): boolean {
  const normalized = text.replace(/\b(?:no|without)\s+(?:implementation|coding|deployment)\b/gi, '')
    .replace(/\b(?:do not|don't|must not|never)\s+(?:implement|build|add|deploy|ship)\b/gi, '');
  return /(?:^|\n)\s*(?:research|survey|discuss|investigate|audit|compare|study|report on|write (?:a )?report)\b/i.test(text)
    && !/\b(?:implement(?:ation)?|build|add|deploy|ship|integrate|enable)\b/i.test(normalized);
}

export interface ScopeFlag {
  flag: 'in_scope' | 'needs_decision';
  matched_terms: string[];
}

/** Classifier only hints, operator decides. Research merely discussing a
 * forbidden topic is not blocked: a research hint forces in_scope. */
export function classifyScope(project: string, title: string, text: string, acceptanceCriteria: string | null): ScopeFlag {
  const entry = Object.hasOwn(SCOPE_PROJECTS, project) ? SCOPE_PROJECTS[project] : undefined;
  if (!entry) return { flag: 'in_scope', matched_terms: [] };
  const combined = (title + '\n' + text + '\n' + (acceptanceCriteria ?? '')).toLowerCase();
  if (isResearchHint(combined)) return { flag: 'in_scope', matched_terms: [] };
  const words = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const normalized = ' ' + words(combined) + ' ';
  const matched = entry.not_terms.filter(term => normalized.includes(' ' + words(term) + ' '));
  return matched.length ? { flag: 'needs_decision', matched_terms: matched } : { flag: 'in_scope', matched_terms: [] };
}

type Obj = Record<string, any>;

/** Unambiguous, collision-resistant binding. Revision prevents an edit followed
 * by restoration from reviving an old operator decision. */
export function scopeBasisHash(task: Obj): string {
  return sha256Sync(JSON.stringify([
    task.project ?? '', task.title ?? '',
    Array.isArray(task.history) ? task.history.filter((e: Obj) => e?.kind === 'intake').map((e: Obj) => e.text ?? '') : [],
    task.acceptance_criteria ?? null, task.scope?.basis_version ?? 1,
  ]));
}

/** Only called on newly created tasks or scope-relevant mutations. */
export function scopeHint(task: Obj, basisVersion = 1): Obj {
  const texts = task.history.filter((e: Obj) => e?.kind === 'intake').map((e: Obj) => e.text ?? '').join('\n');
  const hint = classifyScope(task.project, task.title ?? '', texts, task.acceptance_criteria ?? null);
  return { policy_version: SCOPE_POLICY_VERSION, basis_version: basisVersion, ...hint };
}

export const SCOPE_ALLOW_VERDICTS = ['in_scope', 'gray_adr'];
export const SCOPE_DENY_VERDICTS = ['out_of_scope', 'wrong_project'];
export const SCOPE_VERDICTS = [...SCOPE_ALLOW_VERDICTS, ...SCOPE_DENY_VERDICTS];

export interface ScopeDecisionSummary {
  verdict: string;
  actor: string;
  captured_at: number;
  basis_hash: string;
  policy_version: number;
  valid: boolean;
}

/** Latest scope_decision event in history order, if any. */
export function latestScopeDecision(task: Obj): Obj | null {
  if (!Array.isArray(task.history)) return null;
  for (let i = task.history.length - 1; i >= 0; i--) {
    const e = task.history[i];
    if (e?.kind === 'scope_decision' && e.authority === 'operator_record') return e;
  }
  return null;
}

export function describeScopeDecision(task: Obj): ScopeDecisionSummary | null {
  const e = latestScopeDecision(task);
  if (!e) return null;
  const current = scopeBasisHash(task);
  return {
    verdict: e.verdict,
    actor: e.actor,
    captured_at: e.captured_at,
    basis_hash: e.basis_hash,
    policy_version: e.policy_version,
    valid: e.policy_version === SCOPE_POLICY_VERSION && e.basis_hash === current,
  };
}

export interface ScopeStatus {
  /** Null for legacy tasks without a scope field (grandfathered, untouched). */
  scope: { policy_version: number; flag: string; matched_terms: string[] } | null;
  decision: ScopeDecisionSummary | null;
  clear: boolean;
  /** Present when blocked: which refusal the gates must return. */
  block_code: 'scope_decision_required' | 'scope_out_of_bounds' | null;
  stale_policy: boolean;
}

/** The single check used by intake assign and linked session starts. */
export function getScopeStatus(task: Obj): ScopeStatus {
  const scope = task.scope ?? null;
  if (!scope || typeof scope !== 'object') {
    return { scope: null, decision: null, clear: true, block_code: null, stale_policy: false };
  }
  const stale_policy = scope.policy_version !== SCOPE_POLICY_VERSION;
  const decision = describeScopeDecision(task);
  const decisionClear = !!decision && decision.valid && (SCOPE_ALLOW_VERDICTS as string[]).includes(decision.verdict);
  const decisionDeny = !!decision && decision.valid && (SCOPE_DENY_VERDICTS as string[]).includes(decision.verdict);
  // Live flag: stored flag, but a stale policy version re-hints from current text.
  let flag = scope.flag;
  if (stale_policy) {
    const texts = Array.isArray(task.history)
      ? task.history.filter((e: Obj) => e?.kind === 'intake').map((e: Obj) => (typeof e.text === 'string' ? e.text : '')).join('\n')
      : '';
    flag = classifyScope(task.project ?? '', task.title ?? '', texts, typeof task.acceptance_criteria === 'string' ? task.acceptance_criteria : null).flag;
  }
  if (decisionClear) {
    return { scope, decision, clear: true, block_code: null, stale_policy };
  }
  if (decisionDeny) {
    return { scope, decision, clear: false, block_code: 'scope_out_of_bounds', stale_policy };
  }
  if (flag === 'in_scope' && !stale_policy && !decision) {
    return { scope, decision, clear: true, block_code: null, stale_policy };
  }
  return { scope, decision, clear: false, block_code: 'scope_decision_required', stale_policy };
}

/** Returns the refusal error code, or null when start/assign may proceed. */
export function getScopeBlock(task: Obj): 'scope_decision_required' | 'scope_out_of_bounds' | null {
  return getScopeStatus(task).block_code;
}
