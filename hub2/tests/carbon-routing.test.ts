import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { eligibleCandidates } from '../src/routing-pool.ts';

const root = resolve(import.meta.dirname, '../..');
const policy = JSON.parse(readFileSync(resolve(root, 'config/model-policy.json'), 'utf8'));
const carbonId = 'antigravity-carbon-medium';
const carbonModel = 'gemini-4-carbon-medium';

test('Carbon stays out of automatic implementation, ordinary and review pools', () => {
  for (const input of [
    {interactive: false},
    {interactive: false, purpose: 'fix'},
    {interactive: false, purpose: 'implement'},
    {interactive: false, purpose: 'review'},
    {interactive: false, purpose: 'think'},
    {interactive: false, purpose: 'think', engine: 'antigravity'},
  ]) {
    const candidates = eligibleCandidates(policy, input as any);
    assert.ok(!candidates.some(candidate => candidate.id === carbonId));
  }
  assert.ok(eligibleCandidates(policy, {interactive: false})
    .some(candidate => candidate.id === 'antigravity-hydrogen-medium'), 'Hydrogen remains in ordinary picks');
});

test('Carbon can be pinned only by exact model id in a thinking session', () => {
  for (const model of [carbonModel, carbonId]) {
    const candidates = eligibleCandidates(policy, {interactive: false, purpose: 'think', model});
    assert.deepEqual(candidates.map(candidate => candidate.id), [carbonId]);
  }
  for (const purpose of [undefined, 'fix', 'implement', 'review']) {
    assert.throws(
      () => eligibleCandidates(policy, {interactive: false, purpose: purpose as any, model: carbonModel}),
      (error: any) => error.code === 'think_only_model_requires_think_purpose' && error.status === 400,
    );
  }
});
