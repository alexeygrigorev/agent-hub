import { describe, it, expect } from 'vitest';
import { resolveMachineAliases, normalizeHostname, canonicalMachineId, aliasDetail, projectAliasSessions } from '../src/machine-alias';

describe('machine aliasing', () => {
  it('normalizes hostnames case-insensitively and rejects empties', () => {
    expect(normalizeHostname('RMTHZ')).toBe('RMTHZ');
    expect(normalizeHostname('rmthz')).toBe('RMTHZ');
    expect(normalizeHostname('  RmThZ  ')).toBe('RMTHZ');
    expect(normalizeHostname('')).toBeNull();
    expect(normalizeHostname('   ')).toBeNull();
    expect(normalizeHostname(null)).toBeNull();
    expect(normalizeHostname(42)).toBeNull();
  });

  it('aliases the stale hetzner record to the live urgent-hetzner host', () => {
    const now = Date.now();
    const resolution = resolveMachineAliases([
      { id: 'hetzner', hostname: 'RMTHZ', drained: 0, last_seen_ms: now - 220 * 3600000 },
      { id: 'urgent-hetzner', hostname: 'rmthz', drained: 0, last_seen_ms: now },
    ]);
    expect(resolution.aliasOf.get('hetzner')).toBe('urgent-hetzner');
    expect(resolution.aliasOf.has('urgent-hetzner')).toBe(false);
    expect(canonicalMachineId('hetzner', resolution)).toBe('urgent-hetzner');
    expect(canonicalMachineId('urgent-hetzner', resolution)).toBe('urgent-hetzner');
    expect(aliasDetail('hetzner', 'urgent-hetzner', 'RMTHZ')).toContain('urgent-hetzner');
  });

  it('prefers the non-drained member even when its check-in is older', () => {
    const now = Date.now();
    const resolution = resolveMachineAliases([
      { id: 'new-drained', hostname: 'RMTHZ', drained: 1, last_seen_ms: now },
      { id: 'old-live', hostname: 'RMTHZ', drained: 0, last_seen_ms: now - 3600000 },
    ]);
    expect(resolution.aliasOf.get('new-drained')).toBe('old-live');
  });

  it('never aliases records with empty hostnames', () => {
    const resolution = resolveMachineAliases([
      { id: 'a', hostname: '', drained: 0, last_seen_ms: 1 },
      { id: 'b', hostname: null, drained: 0, last_seen_ms: 2 },
    ]);
    expect(resolution.aliasOf.size).toBe(0);
  });

  it('keeps distinct physical hosts separate', () => {
    const now = Date.now();
    const resolution = resolveMachineAliases([
      { id: 'hetzner', hostname: 'RMTHZ', drained: 0, last_seen_ms: now },
      { id: 'win35', hostname: 'DESKTOP-FP4OP26', drained: 0, last_seen_ms: now },
    ]);
    expect(resolution.aliasOf.size).toBe(0);
  });
});

 it('never turns an old alias observation into a live session without matching native evidence', () => {
   const now = Date.now(), alias = new Map([['old','live']]);
   const old = {machine_id:'old',native_id:'n1',phase:'running',agent_state:'working',observed_at:new Date(now-86400000).toISOString()};
   expect(projectAliasSessions([old],alias,now)[0].phase).toBe('unknown');
   const live = {...old,machine_id:'live',phase:'exited',observed_at:new Date(now).toISOString()};
   expect(projectAliasSessions([old,live],alias,now)[0].phase).toBe('exited');
   expect(old.phase).toBe('running');
 });
