/** Workspace moves preserve native identity; only an intake-owned worktree
 * and a fresh host catalog can authorize updating the Hub binding. */
export function bindingMoveAllowed(prior: any, next: any, observed: any,
  worktree: any, intakeId: unknown, machineId: string, now: number): boolean {
  if (!prior || !next || !observed || !worktree) return false;
  const created = prior.native_created_at_ms;
  const seen = Date.parse(observed.observed_at);
  return typeof intakeId === 'string' && !!intakeId &&
    worktree.task_id === intakeId && worktree.machine === machineId &&
    worktree.state === 'active' && worktree.path === next.workspace &&
    prior.workspace !== next.workspace &&
    prior.native_id === next.native_id && prior.tag === next.tag &&
    Number.isSafeInteger(created) && created > 0 &&
    created === next.native_created_at_ms && created === observed.created_at_ms &&
    observed.native_id === next.native_id && observed.tag === next.tag &&
    observed.workspace === next.workspace &&
    ['running', 'idle', 'waiting', 'busy'].includes(observed.phase) &&
    Number.isFinite(seen) && seen <= now + 60000 && seen >= now - 120000;
}
