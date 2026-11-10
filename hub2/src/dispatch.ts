import type { Hub2Env } from './types';

export type DispatchIntent = {
  state: 'pending_machine' | 'staged';
  operation_id: string; attempt_operation_id: string; dispatch_id: string; attempt: number;
  session_id: string; machine_id: string; account_id: string | null; epoch: number | null;
  request_digest: string; project_id: string; intake_id: string;
  title: string; acceptance_criteria: unknown; launch: unknown;
};
export type AdvanceOutcome = { ok: true; state: 'staged'; epoch: number } | { ok: true; state: 'offered' } | { ok: false; reason: string };

/** Dispatch an accepted session without host-slot, quota, or credential reservations. */
export async function advanceDispatch(env: Hub2Env, saga: DispatchIntent): Promise<AdvanceOutcome> {
  const machine = env.MACHINES.get(env.MACHINES.idFromName(saga.machine_id));
  if (saga.state === 'pending_machine') {
    const response = await machine.fetch('https://machine.hub2.internal/_internal/reserve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        operation_id: saga.attempt_operation_id, dispatch_id: saga.dispatch_id, attempt: saga.attempt,
        ...(saga.attempt > 0 ? { retry_epoch: saga.epoch } : {}), session_id: saga.session_id,
        project_id: saga.project_id, intake_id: saga.intake_id, machine_id: saga.machine_id,
        request_digest: saga.request_digest, title: saga.title, acceptance_criteria: saga.acceptance_criteria,
        account_id: saga.account_id, launch: saga.launch,
      }),
    });
    const payload = await response.json() as { epoch?: number; state?: string; reason?: string; error?: string };
    if ((response.status !== 201 && response.status !== 200) || !Number.isSafeInteger(payload.epoch) || payload.state !== 'staged')
      return { ok: false, reason: payload.reason || payload.error || 'dispatch_retrying' };
    return { ok: true, state: 'staged', epoch: Number(payload.epoch) };
  }
  const response = await machine.fetch('https://machine.hub2.internal/_internal/activate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ operation_id: saga.attempt_operation_id, session_id: saga.session_id, epoch: saga.epoch }),
  });
  const result = await response.json() as { state?: string; error?: string };
  if (!response.ok || !['offered', 'claimed', 'running'].includes(String(result.state)))
    return { ok: false, reason: result.error || 'dispatch_retrying' };
  return { ok: true, state: 'offered' };
}
