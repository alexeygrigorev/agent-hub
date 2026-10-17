import { SqlStorage } from "@cloudflare/workers-types";

/**
 * R5 R2: stable server-owned host enrollment authority.
 *
 * Operator approval is the sole origin of enrollment authority: each approval
 * for a machine mints a strictly increasing per-machine integer sequence,
 * independent of pairing creation order, token issuance/consumption order,
 * client body content and wall-clock timestamps (same-clock ties are
 * impossible by construction). Issuance and registration enforce it; refresh
 * preserves it without upgrade.
 *
 * NULL means pre-migration legacy or not-yet-approved: enforcement is skipped
 * while either side is NULL so existing enrolled hosts keep working until
 * their next approval establishes authority (safe migration).
 */

export function approvalAuthorityMatches(
  credentialSeq: unknown,
  machineSeq: unknown
): boolean {
  if (credentialSeq === null || credentialSeq === undefined) return machineSeq === null || machineSeq === undefined;
  if (machineSeq === null || machineSeq === undefined) return true;
  return credentialSeq === machineSeq;
}

/** Next authority value for a machine. Must run inside the approval transaction. */
export function nextApprovalSeq(sql: SqlStorage, machineId: string): number {
  const row = sql
    .exec('SELECT approval_seq FROM machines WHERE id = ?', machineId)
    .toArray()[0] as { approval_seq?: unknown } | undefined;
  const cur = typeof row?.approval_seq === 'number' ? row.approval_seq : 0;
  return cur + 1;
}

/**
 * Assign fresh server authority to a machine and return it. Must run inside
 * the same synchronous approval transaction that marks the pairing approved.
 */
export function assignApprovalAuthority(sql: SqlStorage, machineId: string): number {
  const seq = nextApprovalSeq(sql, machineId);
  sql.exec('UPDATE machines SET approval_seq = ? WHERE id = ?', seq, machineId);
  return seq;
}
