export const MAX_ACTIVE_SESSIONS = 50;
export const LIVE_SNAPSHOT_TTL_MS = 90_000;
export const SESSION_IDLE_MS = 6 * 60 * 60_000;
export const SESSION_REAP_INTERVAL_MS = 3 * 60_000;
export const CLAIMED_WITHOUT_NATIVE_GRACE_MS = 2 * 60_000;

export type LiveSession = {
  session_id: string;
  native_id: string | null;
  phase: string;
  last_activity_at: number | null;
  observed_at: number;
  containment_empty?: boolean;
};

function recent(timestamp: number | null, now: number): boolean {
  return Number.isSafeInteger(timestamp) && Number(timestamp) <= now + 5_000
    && now - Number(timestamp) <= SESSION_IDLE_MS;
}

export function isLiveActiveSession(session: LiveSession, now = Date.now()): boolean {
  return !!session.native_id
    && !['exited', 'missing', 'dead', 'stopped'].includes(session.phase)
    && session.containment_empty !== true
    && Number.isSafeInteger(session.observed_at) && session.observed_at <= now + 5_000
    && now - session.observed_at <= LIVE_SNAPSHOT_TTL_MS
    && recent(session.last_activity_at, now);
}

export function inactiveReason(session: LiveSession, now = Date.now()): 'dead' | 'idle' | null {
  if (!session.native_id || ['exited', 'missing', 'dead', 'stopped'].includes(session.phase)) return 'dead';
  if (Number.isSafeInteger(session.last_activity_at) && now - Number(session.last_activity_at) > SESSION_IDLE_MS) return 'idle';
  return null;
}
