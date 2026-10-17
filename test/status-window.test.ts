/** Pure Node unit tests for the win35 expected-offline window.
 * No workerd APIs, no fixtures, no clock faking: the window boundary is
 * exercised directly so a future stale win35 can never hide as expected. */
import { describe, expect, it } from 'vitest';
import {
  EXPECTED_OFFLINE_UNTIL,
  EXPECTED_OFFLINE_UNTIL_MS,
  EXPECTED_OFFLINE_REASON,
  expectedOfflineWindowActive,
  isExpectedOffline,
} from '../src/status_offline';

describe('win35 expected-offline window', () => {
  it('carries the Alexey-requested two-week window', () => {
    expect(EXPECTED_OFFLINE_UNTIL).toBe('2026-10-31T15:23:00Z');
    expect(EXPECTED_OFFLINE_UNTIL_MS).toBe(Date.parse('2026-10-31T15:23:00Z'));
    expect(typeof EXPECTED_OFFLINE_REASON).toBe('string');
  });

  it('classifies stale win35 as expected inside the window', () => {
    const inside = Date.parse('2026-10-17T16:23:00.000Z');
    expect(expectedOfflineWindowActive(inside)).toBe(true);
    expect(isExpectedOffline('win35', 'DESKTOP-FP4OP26', inside)).toBe(true);
    expect(isExpectedOffline('win35', 'renamed-host', inside)).toBe(true);
    expect(isExpectedOffline('other-host', 'DESKTOP-FP4OP26', inside)).toBe(true);
    expect(isExpectedOffline('hetzner', 'hetzner', inside)).toBe(false);
  });

  it('expires at the boundary: after until, win35 is ordinary missing', () => {
    expect(expectedOfflineWindowActive(EXPECTED_OFFLINE_UNTIL_MS)).toBe(true);
    expect(expectedOfflineWindowActive(EXPECTED_OFFLINE_UNTIL_MS + 1)).toBe(false);
    const after = EXPECTED_OFFLINE_UNTIL_MS + 1;
    expect(isExpectedOffline('win35', 'DESKTOP-FP4OP26', after)).toBe(false);
    expect(isExpectedOffline('win35', 'renamed-host', after)).toBe(false);
    expect(isExpectedOffline('other-host', 'DESKTOP-FP4OP26', after)).toBe(false);
    const farFuture = Date.parse('2027-01-01T00:00:00.000Z');
    expect(isExpectedOffline('win35', 'DESKTOP-FP4OP26', farFuture)).toBe(false);
  });
});
