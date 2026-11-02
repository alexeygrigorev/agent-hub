import { describe, expect, it } from 'vitest';
import { DRIVER_SCOPE, DRIVER_SCOPES, driverIntakeMutationAllowed } from '../src/driver-access';

describe('Hub driver access bounds', () => {
  it('uses a distinct bounded role instead of human or host credentials', () => {
    expect(DRIVER_SCOPE).toBe('driver:orchestrate');
    expect(DRIVER_SCOPES).toEqual(['bus:read', 'fleet:launch', DRIVER_SCOPE]);
    expect(DRIVER_SCOPES).not.toContain('admin');
    expect(DRIVER_SCOPES).not.toContain('host');
    expect(DRIVER_SCOPES).not.toContain('worker:own-intake');
  });

  it('allows repair notes, acceptance criteria and linked assignments but denies terminal and operator decisions', () => {
    expect(driverIntakeMutationAllowed('/tasks', {
      key: 'repair', project: 'agent-hub', title: 'Repair', text: 'Fix a breakage', acceptance_criteria: 'Evidence is recorded',
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks', {
      key: 'repair-without-ac', project: 'agent-hub', title: 'Repair', text: 'Fix a breakage',
    })).toBe(false);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'note', expected_version: 2, event: { kind: 'note', text: 'DONE with proof' },
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks/item/assign', {
      key: 'link-session', expected_version: 2, session: { hub_session_id: 'session-id' },
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'scope', expected_version: 2, event: { kind: 'scope_decision', verdict: 'in_scope' },
    })).toBe(false);
    expect(driverIntakeMutationAllowed('/tasks/item', {
      key: 'close', expected_version: 2, status: 'done',
    })).toBe(false);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'archive', expected_version: 2, event: { kind: 'archive' },
    })).toBe(false);
  });
});
