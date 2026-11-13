/** Fail-closed merge evidence for code intakes. GitHub and git inspection is
 * performed by the Hub CLI; Hub validates and journals the evidence, then
 * enforces the same predicate when an intake is accepted. */

export type MergeEvidence = {
  kind: 'merge_gate';
  repository: string;
  pr_url?: string;
  base_ref?: string;
  merged_at?: string | null;
  head_sha?: string;
  checks_head_sha?: string;
  checks_state?: string;
  checks_total?: number;
  checks_successful?: number;
  checks_pending?: number;
  checks_failed?: number;
  merge_commit_sha?: string;
  main_head_sha?: string;
  merge_commit_in_main?: boolean;
  observation_error?: string;
  checked_at: string;
};

export type MergeGateResult = { required: boolean; ok: boolean; evidence?: MergeEvidence; problems: string[] };

const SHA = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i;
const GITHUB_PR = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*(?:\?.*)?$/;

/** API ingestion accepts failed observations too, so they can be shown and
 * escalated. This only protects the journal from unbounded or malformed data. */
export function mergeEvidenceShapeError(value: unknown): string | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return 'merge evidence must be a JSON object';
  const e = value as Record<string, unknown>;
  if (e.kind !== 'merge_gate') return 'evidence kind must be merge_gate';
  if (JSON.stringify(e).length > 12_000) return 'merge evidence is too large';
  if (typeof e.repository !== 'string' || e.repository.length > 200) return 'repository must be bounded text';
  if (e.pr_url !== undefined && (typeof e.pr_url !== 'string' || e.pr_url.length > 500)) return 'pr_url must be bounded text';
  for (const field of ['base_ref','merged_at','head_sha','checks_head_sha','checks_state','merge_commit_sha','main_head_sha','observation_error'] as const)
    if (e[field] !== undefined && e[field] !== null && (typeof e[field] !== 'string' || e[field].length > 1000)) return `${field} must be bounded text`;
  for (const field of ['checks_total','checks_successful','checks_pending','checks_failed'] as const)
    if (e[field] !== undefined && (!Number.isSafeInteger(e[field]) || Number(e[field]) < 0)) return `${field} must be a non-negative integer`;
  if (e.merge_commit_in_main !== undefined && typeof e.merge_commit_in_main !== 'boolean') return 'merge_commit_in_main must be boolean';
  if (typeof e.checked_at !== 'string' || e.checked_at.length > 80 || !Number.isFinite(Date.parse(e.checked_at))) return 'checked_at is required';
  return undefined;
}

export function codeTask(source: unknown): boolean {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return true;
  return (source as Record<string, unknown>).code_task !== false;
}

export function mergeEvidenceError(value: unknown): string | undefined {
  const shape = mergeEvidenceShapeError(value);
  if (shape) return shape;
  const e = value as Record<string, unknown>;
  if (typeof e.repository !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(e.repository)) return 'repository must be owner/name';
  if (typeof e.pr_url !== 'string' || !GITHUB_PR.test(e.pr_url)) return 'pr_url must identify a GitHub pull request';
  if (e.base_ref !== 'main') return 'pull request base_ref must be main';
  if (typeof e.merged_at !== 'string' || !e.merged_at.trim() || !Number.isFinite(Date.parse(e.merged_at))) return 'pull request must be merged';
  for (const name of ['head_sha', 'checks_head_sha', 'merge_commit_sha', 'main_head_sha'] as const)
    if (typeof e[name] !== 'string' || !SHA.test(e[name] as string)) return `${name} must be a full commit SHA`;
  if (String(e.checks_head_sha).toLowerCase() !== String(e.head_sha).toLowerCase()) return 'checks must belong to the exact pull request head';
  if (e.checks_state !== 'success') return 'checks_state must be success';
  for (const name of ['checks_total', 'checks_successful', 'checks_pending', 'checks_failed'] as const)
    if (!Number.isSafeInteger(e[name]) || Number(e[name]) < 0) return `${name} must be a non-negative integer`;
  if (Number(e.checks_total) < 1 || e.checks_successful !== e.checks_total || e.checks_pending !== 0 || e.checks_failed !== 0)
    return 'all checks must be green for the exact head SHA';
  if (e.merge_commit_in_main !== true) return 'merge commit must be present in origin/main';
  if (typeof e.checked_at !== 'string' || !e.checked_at.trim() || !Number.isFinite(Date.parse(e.checked_at))) return 'checked_at is required';
  return undefined;
}

export function mergeGate(required: boolean, value: unknown, expectedRepository?: string): MergeGateResult {
  if (!required) return {required:false,ok:true,problems:[]};
  const shape = mergeEvidenceShapeError(value);
  if (shape) return {required:true,ok:false,problems:[shape]};
  const e = value as Record<string, unknown>, problems: string[] = [];
  if (typeof e.observation_error === 'string' && e.observation_error.trim()) problems.push(e.observation_error.trim());
  if (typeof e.repository !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(e.repository)) problems.push('repository identity is unknown');
  else if (expectedRepository && e.repository.toLowerCase() !== expectedRepository.toLowerCase())
    problems.push(`merge evidence is for ${e.repository}, expected ${expectedRepository}`);
  if (typeof e.pr_url !== 'string' || !GITHUB_PR.test(e.pr_url)) problems.push('a GitHub pull request is not identified');
  if (e.base_ref !== 'main') problems.push('the pull request base is not main');
  if (typeof e.merged_at !== 'string' || !e.merged_at.trim() || !Number.isFinite(Date.parse(e.merged_at))) problems.push('the pull request is not merged');
  if (typeof e.head_sha !== 'string' || !SHA.test(e.head_sha)) problems.push('the pull request head SHA is missing or invalid');
  if (typeof e.checks_head_sha !== 'string' || String(e.checks_head_sha).toLowerCase() !== String(e.head_sha).toLowerCase()) problems.push('checks are not for the exact pull request head');
  if (e.checks_state !== 'success') problems.push('checks are not green for the exact head SHA');
  if (!Number.isSafeInteger(e.checks_total) || Number(e.checks_total) < 1
    || e.checks_successful !== e.checks_total || e.checks_pending !== 0 || e.checks_failed !== 0)
    problems.push('all checks must be green for the exact head SHA');
  if (typeof e.merge_commit_sha !== 'string' || !SHA.test(e.merge_commit_sha)) problems.push('the merge commit SHA is missing or invalid');
  if (typeof e.main_head_sha !== 'string' || !SHA.test(e.main_head_sha)) problems.push('origin/main head SHA is missing or invalid');
  if (e.merge_commit_in_main !== true) problems.push('the merge commit is not present in origin/main');
  if (!problems.length) return {required:true,ok:true,evidence:value as MergeEvidence,problems:[]};
  return {required:true,ok:false,evidence:value as MergeEvidence,problems};
}

export function mergeGaps(result: MergeGateResult): string[] {
  return result.problems.map(problem => `Merge gate: ${problem}. Run hub intake merge-check after updating the PR and CI.`);
}
