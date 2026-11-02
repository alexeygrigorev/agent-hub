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

  it('allows repair notes, task creation criteria and linked assignments but denies AC edits and operator decisions', () => {
    expect(driverIntakeMutationAllowed('/tasks', {
      key: 'repair', project: 'agent-hub', title: 'Repair', text: 'Fix a breakage', acceptance_criteria: 'Evidence is recorded',
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks', {
      key: 'repair-without-ac', project: 'agent-hub', title: 'Repair', text: 'Fix a breakage',
    })).toBe(false);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'note', expected_version: 2, event: { kind: 'note', text: 'DONE with proof' },
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'mcp-note', expected_version: 2, note: { text: 'DONE with proof', source: 'agent' },
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'cli-note', expected_version: 2, note: 'DONE with proof',
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'text-note', expected_version: 2, text: 'DONE with proof',
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks/item/events', {
      key: 'conflicting-note', expected_version: 2, note: 'note', text: 'different note',
    })).toBe(false);
    expect(driverIntakeMutationAllowed('/tasks/item/assign', {
      key: 'link-session', expected_version: 2,
      session: { workspace: 'agent-hub', mode: 'interactive' }, provenance: 'unknown',
    })).toBe(true);
    expect(driverIntakeMutationAllowed('/tasks/item', {
      key: 'clear-ac', expected_version: 2, acceptance_criteria: null,
    })).toBe(false);
    expect(driverIntakeMutationAllowed('/tasks/item', {
      key: 'replace-ac', expected_version: 2, acceptance_criteria: 'replaced criteria',
    })).toBe(false);
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
