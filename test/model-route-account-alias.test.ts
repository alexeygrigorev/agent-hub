import { describe, expect, it } from 'vitest';
import { modelCandidates } from '../src/model-routing';

const now = Date.parse('2026-10-26T08:00:00Z');
const input = { tier: 'daily', model: 'muse', preferred_runner: 'opencode' };
const route = { provider: 'go', engine: 'opencode', profile: null, account: 'urgent-muse' };
const ad = { engine: 'opencode', profile: null, account: 'shared-go', model: 'opencode-go/muse-spark-1.3-contributor', effort: null };
const quota = (remaining = 80) => ({ status: 'ok', observed_at: new Date(now).toISOString(), details: { limit_reached: false },
  windows: { '5h': { present: true, percent_remaining: remaining, reset_at: new Date(now + 3600000).toISOString() } } });

describe('capability routing during a canonical account rename', () => {
  it.each([['urgent-muse', 'shared-go'], ['shared-go', 'urgent-muse']])(
    'places the same real Go account for route %s and advertisement %s', (account, advertised) => {
      const candidates = modelCandidates(input, { ...route, account }, [{ ...ad, account: advertised }], quota(), now);
      expect(candidates.map(c => c.id)).toEqual(['muse']);
    });

  it('keeps distinct accounts, model, effort, profile and engine boundaries', () => {
    for (const different of [{ account: 'another-go-account' }, { account: null },
      { model: 'unconfigured-model' }, { effort: 'high' }, { profile: 'another-profile' }, { engine: 'codex' }]) {
      expect(modelCandidates(input, route, [{ ...ad, ...different }], quota(), now)).toEqual([]);
    }
  });

  it('retains quota floors and explicit engine health refusal for an alias match', () => {
    expect(modelCandidates(input, route, [ad], quota(0), now)).toEqual([]);
    expect(modelCandidates(input, route, [ad], quota(), now, undefined, new Set(['opencode']))).toEqual([]);
  });
});
