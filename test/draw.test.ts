import { describe, expect, it } from 'vitest';
import { autoStartDrawDistribution, mulberry32, weightedDrawIndex } from '../src/draw';
import { drawAutoStart } from '../src/fleet';

describe('weighted auto-start distribution', () => {
  it('weights candidates by the composite routing score, not raw remaining quota', () => {
    const highHeadroom = { decision: { score: 2, factors: { remaining_pct: 90, burn_factor: 0.05 } } };
    const highScore = { decision: { score: 6, factors: { remaining_pct: 10, burn_factor: 1 } } };
    const { eligible, weights, total, probabilities } = autoStartDrawDistribution([highHeadroom, highScore]);

    expect(eligible).toEqual([highHeadroom, highScore]);
    expect(weights).toEqual([2, 6]);
    expect(total).toBe(8);
    expect(probabilities.get(highHeadroom)).toBe(0.25);
    expect(probabilities.get(highScore)).toBe(0.75);
    expect(eligible[weightedDrawIndex(weights, () => 0.5)]).toBe(highScore);
  });

  it('excludes overflow, degraded, depleted and candidates without a positive score', () => {
    const overflow = { overflow: true, decision: { score: 3, factors: { remaining_pct: 100 } } };
    const degraded = { degraded: true, decision: { score: 3, factors: { remaining_pct: 100 } } };
    const depleted = { decision: { score: 3, factors: { remaining_pct: 100, depleted: true } } };
    const missingScore = { decision: { factors: { remaining_pct: 100, depleted: false } } };
    const zeroScore = { decision: { score: 0, factors: { remaining_pct: 100, depleted: false } } };
    const eligibleCandidate = { decision: { score: 0.4, factors: { remaining_pct: 40, depleted: false } } };
    const distribution = autoStartDrawDistribution([
      overflow, degraded, depleted, missingScore, zeroScore, eligibleCandidate,
    ]);

    expect(distribution.eligible).toEqual([eligibleCandidate]);
    expect(distribution.probabilities.get(eligibleCandidate)).toBe(1);
    expect(distribution.probabilities.has(overflow)).toBe(false);
    expect(distribution.probabilities.has(missingScore)).toBe(false);
  });

  it('keeps the weighted distribution stable for a seeded stream', () => {
    const rand = mulberry32(0x9647e6d5);
    let preferred = 0;
    for (let i = 0; i < 1000; i++) {
      if (weightedDrawIndex([1, 3], rand) === 1) preferred++;
    }
    expect(preferred).toBeGreaterThanOrEqual(700);
    expect(preferred).toBeLessThanOrEqual(800);
  });

  it('draws by composite score, records probability, and leaves pins deterministic', () => {
    const top: any = {
      decision: { score: 6, reason: 'auto: codex/shared-codex', factors: { remaining_pct: 10 } },
      decision_reason: 'auto: codex/shared-codex',
      selection: { decision_reason: 'auto: codex/shared-codex', decision: { reason: 'auto: codex/shared-codex' } },
    };
    const scoreWinner: any = {
      decision: { score: 2, reason: 'auto: zai/shared-zai', factors: { remaining_pct: 90 } },
      decision_reason: 'auto: zai/shared-zai',
      selection: { decision_reason: 'auto: zai/shared-zai', decision: { reason: 'auto: zai/shared-zai' } },
    };
    const overflow: any = { overflow: true, decision: { score: 100, factors: { remaining_pct: 100 } } };
    const candidates = [top, scoreWinner, overflow];

    const selected = drawAutoStart(candidates, {}, [], {}, () => 0.5);

    expect(selected).toBe(top);
    expect(top.draw_probability).toBe(0.75);
    expect(scoreWinner.draw_probability).toBe(0.25);
    expect(overflow.draw_probability).toBe(0);
    expect(top.decision.reason).toContain('draw_probability=0.750000');
    expect(top.selection.decision_reason).toContain('draw_probability=0.750000');
    expect(top.selection.decision.reason).toContain('draw_probability=0.750000');

    expect(drawAutoStart(candidates, { engine: 'codex' }, [], {}, () => 0.99)).toBe(top);
    expect(drawAutoStart(candidates, {}, [], { tier: 'deep' }, () => 0.99)).toBe(top);
    expect(drawAutoStart(candidates, {}, [], {})).toBe(top);
  });

  it('uses the sole eligible route when the top candidate has no positive score', () => {
    const blocked: any = { decision: { score: 0, factors: { remaining_pct: 50 } } };
    const only: any = { decision: { score: 0.5, reason: 'auto: claude/shared', factors: { remaining_pct: 50 } } };

    expect(drawAutoStart([blocked, only], {}, [], {}, () => 0.75)).toBe(only);
  });
});
