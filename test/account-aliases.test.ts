import { describe, it, expect } from 'vitest';
import { policy } from '../src/model-routing';
import { LEGACY_ACCOUNT_LABELS } from '../src/quota';

// Single source of truth for relabel tolerance: config/model-routing.json
// `account_aliases` (canonical -> [legacy...]) must mirror the server ingest
// map, so host _route() and server subscription joins agree in both orders.
describe('account aliases', () => {
  it('model-routing.json mirrors LEGACY_ACCOUNT_LABELS', () => {
    const aliases = (policy as any).account_aliases;
    expect(aliases).toBeTypeOf('object');
    for (const [legacy, canonical] of Object.entries(LEGACY_ACCOUNT_LABELS)) {
      expect(Array.isArray(aliases[canonical]), `canonical ${canonical} in policy`).toBe(true);
      expect(aliases[canonical]).toContain(legacy);
    }
    const inverted: Record<string, string> = {};
    for (const [canonical, legacies] of Object.entries(aliases) as [string, string[]][]) {
      expect(canonical).toMatch(/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/);
      expect(Array.isArray(legacies) && legacies.length > 0).toBe(true);
      for (const legacy of legacies) {
        expect(legacy).toMatch(/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/);
        expect(legacy).not.toBe(canonical);
        inverted[legacy] = canonical;
      }
    }
    expect(inverted).toEqual(LEGACY_ACCOUNT_LABELS);
  });
});
