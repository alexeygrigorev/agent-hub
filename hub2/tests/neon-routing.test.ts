import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { eligibleCandidates } from '../src/routing-pool.ts';
import { modelPolicyCandidateViolation } from '../src/model-policy.ts';

const root = resolve(import.meta.dirname, '../..');
const policy = JSON.parse(readFileSync(resolve(root, 'config/model-policy.json'), 'utf8'));
const NEON = ['gemini-4-neon-low', 'gemini-4-neon-medium', 'gemini-4-neon-high', 'gemini-4-neon-xhigh', 'gemini-pro-orchestrator-neon'];

test('every Neon model is a valid antigravity/gemini policy candidate', () => {
  for (const model of NEON) {
    const candidate = policy.candidates.find((c: any) => c.model === model);
    assert.ok(candidate, model);
    assert.equal(candidate.engine, 'antigravity');
    assert.equal(candidate.account_id, 'gemini');
    assert.equal(modelPolicyCandidateViolation(candidate), null);
  }
});

test('explicit Neon requests resolve by model and by policy id', () => {
  for (const model of NEON) {
    const id = policy.candidates.find((c: any) => c.model === model).id;
    for (const requested of [model, id]) {
      const out = eligibleCandidates(policy, {interactive: false, engine: 'antigravity', model: requested} as any);
      assert.deepEqual(out.map(c => c.model), [model]);
    }
  }
});

test('picker pool gets Neon medium only', () => {
  const ids = eligibleCandidates(policy, {interactive: false} as any).map(c => c.model);
  assert.ok(ids.includes('gemini-4-neon-medium'));
  for (const model of NEON.filter(m => m !== 'gemini-4-neon-medium')) assert.ok(!ids.includes(model), model);
  const engine = eligibleCandidates(policy, {interactive: false, engine: 'antigravity'} as any).map(c => c.model);
  assert.ok(engine.includes('gemini-4-neon-medium'));
  assert.ok(!engine.includes('gemini-pro-orchestrator-neon'));
});

test('unknown Neon model is still unavailable', () => {
  assert.throws(() => eligibleCandidates(policy, {interactive: false, engine: 'antigravity', model: 'gemini-4-neon-ultra'} as any),
    (e: any) => e.code === 'model_policy_unavailable');
});

test('every ordinary automatic pick uses the model default effort (medium; Haiku/Luna max)', async () => {
  const { defaultEffortFor } = await import('../src/model-policy.ts');
  assert.equal(defaultEffortFor('gemini-4-neon-medium'), 'medium');
  assert.equal(defaultEffortFor('claude-haiku-4-5'), 'max');
  assert.equal(defaultEffortFor('gpt-6-luna'), 'max');
  for (const c of eligibleCandidates(policy, {interactive: false} as any)) {
    if (c.effort) assert.equal(c.effort, defaultEffortFor(c.model), c.id);
  }
  const ex = eligibleCandidates(policy, {interactive: false, engine: 'antigravity', model: 'gemini-4-neon-xhigh'} as any);
  assert.deepEqual(ex.map(c => c.model), ['gemini-4-neon-xhigh']);
});
