import { describe, expect, it } from 'vitest';
import { autoStartDrawDistribution, mulberry32, weightedDrawIndex } from '../src/draw';
import { drawAutoStart } from '../src/fleet';

describe('weighted auto-start distribution', () => {
  it('weights candidates by remaining quota headroom, not their routing score', () => {
    const headroomWinner = { decision: { score: 2, factors: { remaining_pct: 90 } } };
    const scoreWinner = { decision: { score: 6, factors: { remaining_pct: 10 } } };
    const { eligible, weights, total, probabilities } = autoStartDrawDistribution([headroomWinner, scoreWinner]);

    expect(eligible).toEqual([headroomWinner, scoreWinner]);
    expect(weights).toEqual([90, 10]);
    expect(total).toBe(100);
    expect(probabilities.get(headroomWinner)).toBe(0.9);
    expect(probabilities.get(scoreWinner)).toBe(0.1);
    expect(eligible[weightedDrawIndex(weights, () => 0.5)]).toBe(headroomWinner);
  });

  it('excludes overflow, degraded, depleted and candidates without positive headroom', () => {
    const overflow = { overflow: true, decision: { factors: { remaining_pct: 100 } } };
    const degraded = { degraded: true, decision: { factors: { remaining_pct: 100 } } };
    const depleted = { decision: { factors: { remaining_pct: 100, depleted: true } } };
    const missingHeadroom = { decision: { factors: { depleted: false } } };
    const zeroHeadroom = { decision: { factors: { remaining_pct: 0, depleted: false } } };
    const eligibleCandidate = { decision: { factors: { remaining_pct: 40, depleted: false } } };
    const distribution = autoStartDrawDistribution([
      overflow, degraded, depleted, missingHeadroom, zeroHeadroom, eligibleCandidate,
    ]);

    expect(distribution.eligible).toEqual([eligibleCandidate]);
    expect(distribution.probabilities.get(eligibleCandidate)).toBe(1);
    expect(distribution.probabilities.has(overflow)).toBe(false);
    expect(distribution.probabilities.has(missingHeadroom)).toBe(false);
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

  it('draws by remaining headroom, records probability, and leaves pins deterministic', () => {
    const top: any = {
      decision: { score: 2, reason: 'auto: codex/shared-codex', factors: { remaining_pct: 90 } },
      decision_reason: 'auto: codex/shared-codex',
      selection: { decision_reason: 'auto: codex/shared-codex', decision: { reason: 'auto: codex/shared-codex' } },
    };
    const scoreWinner: any = {
      decision: { score: 6, reason: 'auto: zai/shared-zai', factors: { remaining_pct: 10 } },
      decision_reason: 'auto: zai/shared-zai',
      selection: { decision_reason: 'auto: zai/shared-zai', decision: { reason: 'auto: zai/shared-zai' } },
    };
    const overflow: any = { overflow: true, decision: { score: 100, factors: { remaining_pct: 100 } } };
    const candidates = [top, scoreWinner, overflow];

    const selected = drawAutoStart(candidates, {}, [], {}, () => 0.5);

    expect(selected).toBe(top);
    expect(top.draw_probability).toBe(0.9);
    expect(scoreWinner.draw_probability).toBe(0.1);
    expect(overflow.draw_probability).toBe(0);
    expect(top.decision.reason).toContain('draw_probability=0.900000');
    expect(top.selection.decision_reason).toContain('draw_probability=0.900000');
    expect(top.selection.decision.reason).toContain('draw_probability=0.900000');

    expect(drawAutoStart(candidates, { engine: 'codex' }, [], {}, () => 0.99)).toBe(top);
    expect(drawAutoStart(candidates, {}, [], { tier: 'deep' }, () => 0.99)).toBe(top);
    expect(drawAutoStart(candidates, {}, [], {})).toBe(top);
  });

  it('uses the sole eligible route when the top candidate has no quota headroom', () => {
    const blocked: any = { decision: { factors: { remaining_pct: 0 } } };
    const only: any = { decision: { reason: 'auto: claude/shared', factors: { remaining_pct: 50 } } };

    expect(drawAutoStart([blocked, only], {}, [], {}, () => 0.75)).toBe(only);
  });
});
