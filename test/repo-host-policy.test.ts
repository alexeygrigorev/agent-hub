import { describe, expect, it } from 'vitest';
import { advertisedRepoPaths, allowedMachinesValid, repoAllowsMachine, repoPathKey } from '../src/repo-host-policy';

describe('repository host policy (ce1fecdd)', () => {
  it('defaults to every host and supports an explicit empty restriction or reset', () => {
    expect(repoAllowsMachine(undefined, 'tabo')).toBe(true);
    expect(repoAllowsMachine(null, 'tabo')).toBe(true);
    expect(repoAllowsMachine([], 'tabo')).toBe(false);
    expect(repoAllowsMachine(['tabo'], 'tabo')).toBe(true);
    expect(repoAllowsMachine(['hetzner'], 'tabo')).toBe(false);
    for (const invalid of ['tabo', ['tabo', 'tabo'], ['../tabo'], [4], {}, Array(257).fill('tabo')]) {
      expect(allowedMachinesValid(invalid)).toBe(false);
      expect(repoAllowsMachine(invalid, 'tabo')).toBe(false);
    }
  });
  it('recognizes Windows checkout paths from workspace maps and lists', () => {
    expect(advertisedRepoPaths({workspaces: {'agent-hub': 'C:\\repos\\Agent-Hub'}}, 'agent-hub'))
      .toEqual(['C:\\repos\\Agent-Hub']);
    expect(advertisedRepoPaths({workspaces: {'agent-hub': 'D:\\development\\hub'}}, 'agent-hub'))
      .toEqual(['D:\\development\\hub']);
    expect(advertisedRepoPaths({workspaces: ['/srv/repos/agent-hub', 'C:/repos/agent-hub']}, 'agent-hub'))
      .toEqual(['/srv/repos/agent-hub', 'C:/repos/agent-hub']);
    expect(repoPathKey('C:\\repos\\Agent-Hub\\')).toBe(repoPathKey('c:/repos/agent-hub'));
    expect(repoPathKey('/srv/Agent-Hub')).not.toBe(repoPathKey('/srv/agent-hub'));
  });
  it('does not invent a checkout from a workspace root or accept traversal', () => {
    expect(advertisedRepoPaths({workspaces: ['/srv/repos'], workspace_roots: ['/srv/repos']}, 'agent-hub')).toEqual([]);
    expect(advertisedRepoPaths({workspace_roots: ['C:\\repos\\agent-hub']}, 'agent-hub')).toEqual(['C:\\repos\\agent-hub']);
    expect(advertisedRepoPaths({workspace_roots: ['C:\\repos']}, 'agent-hub')).toEqual([]);
    expect(advertisedRepoPaths({workspaces: ['agent-hub', '/srv/../agent-hub', 'C:\\repos\\.\\agent-hub']}, 'agent-hub')).toEqual([]);
  });
});
