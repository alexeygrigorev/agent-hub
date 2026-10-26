import { describe, it, expect } from 'vitest';
import { policy } from '../src/model-routing';

// Single source of truth: config/model-routing.json.
// AC2 forbids duplicate model ids; every tier member must resolve to a model.
// AC1 keeps one canonical account definition per account (harness, provider,
// account label, aplexer profile, credential location/mode, quota probe).
const LABEL = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

describe('model routing policy', () => {
  it('has unique model ids', () => {
    const ids = (policy.models as any[]).map(m => m.id);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('resolves every tier member to exactly one model', () => {
    const byId = new Map((policy.models as any[]).map(m => [m.id, m]));
    for (const [tier, members] of Object.entries(policy.tiers as Record<string, string[]>)) {
      expect(Array.isArray(members) && members.length > 0, `tier ${tier} non-empty`).toBe(true);
      for (const id of members) {
        expect(byId.has(id), `tier ${tier} member ${id} resolves`).toBe(true);
      }
    }
  });

  it('validates the model entry schema', () => {
    for (const m of policy.models as any[]) {
      expect(m.id, 'model id label').toMatch(LABEL);
      for (const field of ['model', 'provider', 'engine']) {
        expect(typeof m[field], `model ${m.id}.${field}`).toBe('string');
        expect(m[field].length).toBeGreaterThan(0);
      }
      expect(m.profile === null || (typeof m.profile === 'string' && LABEL.test(m.profile)), `model ${m.id}.profile`).toBe(true);
      expect(m.effort === null || typeof m.effort === 'string', `model ${m.id}.effort`).toBe(true);
    }
  });

  it('defines every account once with harness, provider, profile, credential and probe', () => {
    const accounts = (policy as any).accounts;
    expect(Array.isArray(accounts) && accounts.length > 0).toBe(true);
    const labels = accounts.map((a: any) => a.account);
    expect(new Set(labels).size).toBe(labels.length);
    for (const a of accounts) {
      expect(a.account, 'account label').toMatch(LABEL);
      expect(typeof a.harness === 'string' && LABEL.test(a.harness), `account ${a.account}.harness`).toBe(true);
      expect(typeof a.provider === 'string' && a.provider.length > 0, `account ${a.account}.provider`).toBe(true);
      expect(a.aplexer_profile === null || (typeof a.aplexer_profile === 'string' && /^hub-[a-z0-9-]+$/.test(a.aplexer_profile)),
        `account ${a.account}.aplexer_profile`).toBe(true);
      expect(a.credential && typeof a.credential === 'object', `account ${a.account}.credential`).toBe(true);
      if (a.credential.mode === 'managed-bundle') {
        expect(typeof a.credential.bundle === 'string' && LABEL.test(a.credential.bundle), `account ${a.account}.credential.bundle`).toBe(true);
      } else if (a.credential.mode === 'host-home') {
        expect(typeof a.credential.path === 'string' && a.credential.path.startsWith('~/'), `account ${a.account}.credential.path`).toBe(true);
      } else {
        expect(`account ${a.account} credential mode ${a.credential?.mode}`, 'known credential mode').toBe('managed-bundle or host-home');
      }
      expect(Array.isArray(a.quota_probe) && a.quota_probe.length > 0, `account ${a.account}.quota_probe`).toBe(true);
      expect(a.quota_probe[0], `account ${a.account} probe binary`).toBe('quse');
      for (const arg of a.quota_probe) expect(typeof arg === 'string' && arg.length > 0).toBe(true);
    }
  });

  it('covers every alias canonical label with an account definition', () => {
    const accounts = new Set(((policy as any).accounts as any[]).map(a => a.account));
    const canonicals = Object.keys((policy as any).account_aliases ?? {});
    expect(canonicals.length).toBeGreaterThan(0);
    for (const c of canonicals) expect(accounts.has(c), `alias canonical ${c} has an account`).toBe(true);
  });
});
