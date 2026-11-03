import { describe, expect, it } from 'vitest';
import { bindingMoveAllowed } from '../src/session-binding-move';

describe('workspace binding move authorization', () => {
  const now = Date.now();
  const prior = { native_id: 'native', tag: 'tag', workspace: '/launch', native_created_at_ms: 123 };
  const next = { ...prior, workspace: '/tree' };
  const observed = { native_id: 'native', tag: 'tag', workspace: '/tree', created_at_ms: 123,
    phase: 'running', observed_at: new Date(now).toISOString() };
  const tree = { task_id: 'intake', machine: 'host', path: '/tree', state: 'active' };
  const allowed = (p: any = prior, n: any = next, o: any = observed, t: any = tree) =>
    bindingMoveAllowed(p, n, o, t, 'intake', 'host', now);
  it('accepts a fresh same-incarnation move to the same intake worktree', () => {
    expect(allowed()).toBe(true);
  });
  it.each([
    { native_id: 'other' }, { tag: 'reused' }, { native_created_at_ms: 124 },
  ])('rejects native identity replacement %j', change => {
    expect(allowed(prior, { ...next, ...change })).toBe(false);
  });
  it.each([
    { created_at_ms: 124 }, { created_at_ms: null }, { phase: 'exited' },
    { observed_at: new Date(now - 120001).toISOString() }, { observed_at: 'invalid' },
    { workspace: '/foreign' },
  ])('rejects unproven native catalog %j', change => {
    expect(allowed(prior, next, { ...observed, ...change })).toBe(false);
  });
  it.each([{ task_id: 'foreign' }, { machine: 'foreign' }, { state: 'removed' }, { path: '/foreign' }])(
    'rejects foreign or inactive worktree %j', change => {
      expect(allowed(prior, next, observed, { ...tree, ...change })).toBe(false);
    });
});
