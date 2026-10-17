/** Expected-offline window for intentionally powered-off hosts (M14).
 * Win35 (DESKTOP-FP4OP26) was shut down 2026-10-17 ~16:23 Berlin while
 * Alexey is away (~2 weeks). Inside the window a stale checkin is
 * expected-offline, never an outage signal. The window EXPIRES: after
 * EXPECTED_OFFLINE_UNTIL a stale win35 is an ordinary missing host again,
 * so future outages are never classified as expected forever.
 * Pure module: no workerd APIs, unit-testable in plain Node. */
export const EXPECTED_OFFLINE_UNTIL = '2026-10-31T15:23:00Z';
export const EXPECTED_OFFLINE_UNTIL_MS = Date.parse(EXPECTED_OFFLINE_UNTIL);
export const EXPECTED_OFFLINE_REASON = 'intentional_shutdown_alexey_away_20261017_2w';

const EXPECTED_OFFLINE_IDS = new Set(['win35']);
const EXPECTED_OFFLINE_HOSTNAMES = new Set(['DESKTOP-FP4OP26']);

export function expectedOfflineWindowActive(nowMs: number): boolean {
  return nowMs <= EXPECTED_OFFLINE_UNTIL_MS;
}

export function isExpectedOffline(id: string, hostname: string | null, nowMs: number): boolean {
  if (!expectedOfflineWindowActive(nowMs)) return false;
  if (EXPECTED_OFFLINE_IDS.has(id)) return true;
  return typeof hostname === 'string' && hostname !== '' && EXPECTED_OFFLINE_HOSTNAMES.has(hostname);
}
