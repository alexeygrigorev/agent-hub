// The committed routing policy must satisfy the operator's model rules on its
// own, so a policy change can never silently reintroduce a banned route.
// Shape rules mirror the /api/model-policy validation in hub2/src/control-do.ts.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { modelPolicyCandidateViolation } from '../src/model-policy.ts';

const root = resolve(import.meta.dirname, '../..');
const policy = JSON.parse(readFileSync(resolve(root, 'config/model-policy.json'), 'utf8')) as {
  candidates: {
    id: string; engine: string; model: string; effort?: string;
    account_id: string; weight: number; capabilities: string[]; enabled: boolean;
  }[];
};

const FIELD_PATTERN = /^[A-Za-z0-9_.:/-]+$/;
const EFFORTS = new Set(['low', 'medium', 'max']);

test('policy candidates pass the model-policy wire validation', () => {
  assert.ok(Array.isArray(policy.candidates) && policy.candidates.length > 0);
  assert.ok(policy.candidates.length <= 32, 'policy stays inside the wire bound');
  const ids = new Set<string>();
  for (const candidate of policy.candidates) {
    assert.deepEqual(
      Object.keys(candidate).filter(key => !['id', 'engine', 'model', 'effort', 'account_id', 'weight', 'capabilities', 'enabled'].includes(key)),
      [], `candidate ${candidate.id} has unknown fields`);
    for (const key of ['id', 'engine', 'model', 'account_id'] as const) {
      assert.equal(typeof candidate[key], 'string', `${key} must be a string`);
      assert.match(candidate[key]!, FIELD_PATTERN, `${candidate.id}: ${key} must match the wire pattern`);
      assert.ok(candidate[key]!.length >= 1 && candidate[key]!.length <= 120);
    }
    assert.equal(typeof candidate.weight, 'number');
    assert.ok(Number.isFinite(candidate.weight) && candidate.weight > 0 && candidate.weight <= 1_000_000);
    assert.equal(typeof candidate.enabled, 'boolean');
    assert.ok(candidate.enabled, 'committed policy ships only enabled candidates');
    assert.ok(Array.isArray(candidate.capabilities) && candidate.capabilities.length <= 32);
    if (candidate.effort !== undefined) assert.ok(EFFORTS.has(candidate.effort), `effort ${candidate.effort} is outside the wire enum`);
    assert.ok(!ids.has(candidate.id), 'candidate ids are unique');
    ids.add(candidate.id);
  }
});

test('the Hub policy validator accepts the committed pool and rejects banned candidates', () => {
  for (const candidate of policy.candidates) {
    assert.equal(modelPolicyCandidateViolation(candidate), null, `${candidate.id} must remain eligible`);
  }
  const route = (overrides: Partial<(typeof policy.candidates)[number]>) => ({
    id: 'test-candidate', engine: 'codex', model: 'gpt-6.1-sol', account_id: 'codex', weight: 1,
    capabilities: [], enabled: true, ...overrides,
  });
  assert.equal(modelPolicyCandidateViolation(route({id: 'zai-central', engine: 'zcodex', model: 'glm-flash', account_id: 'zai-central'})), 'zai_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({engine: 'claude', model: 'haiku', effort: 'medium'})), 'haiku_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({engine: 'claude', model: 'opus', effort: 'medium'})), 'claude_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gpt-6-luna', effort: 'medium'})), 'luna_effort_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gpt-6.1-sol', effort: 'max'})), 'bounded_effort_required');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gpt-6-astra'})), 'bounded_effort_required');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gemini-4-argon-medium', effort: 'max'})), 'bounded_effort_required');
});

test('no zai-central: the picker never spends the shared zai route', () => {
  for (const candidate of policy.candidates) {
    assert.notEqual(candidate.account_id, 'zai-central');
    assert.ok(!['zai', 'zcodex', 'goz'].includes(candidate.engine), `candidate ${candidate.id} routes through zai`);
  }
});

test('no haiku anywhere in the pool', () => {
  for (const candidate of policy.candidates) assert.ok(!/haiku/i.test(candidate.model + ' ' + candidate.id));
});

test('claude appears only as sonnet at medium effort', () => {
  const claude = policy.candidates.filter(candidate => candidate.engine === 'claude');
  assert.ok(claude.length > 0, 'claude sonnet must be part of the pool');
  for (const candidate of claude) {
    assert.equal(candidate.model, 'sonnet');
    assert.equal(candidate.effort, 'medium');
  }
});

test('luna is max-only and one option among many, never the default', () => {
  const luna = policy.candidates.filter(candidate => /luna/i.test(candidate.model));
  assert.ok(luna.length > 0, 'luna must be part of the pool');
  for (const candidate of luna) assert.equal(candidate.effort, 'max');
  const others = policy.candidates.filter(candidate => !/luna/i.test(candidate.model));
  assert.ok(others.length >= 3, 'luna must not dominate the pool');
  const lunaWeight = luna.reduce((sum, candidate) => sum + candidate.weight, 0);
  const otherWeight = others.reduce((sum, candidate) => sum + candidate.weight, 0);
  assert.ok(lunaWeight <= otherWeight, 'luna weight must not exceed the rest of the pool combined');
});

test('sol, argon and astra are never routed at high effort', () => {
  for (const candidate of policy.candidates) {
    if (!/^gpt-6(\.1)?-(sol|astra)$/.test(candidate.model) && candidate.model !== 'gemini-4-argon-medium') continue;
    assert.ok(candidate.effort !== undefined && candidate.effort !== 'max', `${candidate.id} must stay at a bounded effort`);
  }
});

test('the pool spreads across several engines and quota accounts', () => {
  const engines = new Set(policy.candidates.map(candidate => candidate.engine));
  const accounts = new Set(policy.candidates.map(candidate => candidate.account_id));
  assert.ok(engines.size >= 3, `pool needs >=3 engines, has ${[...engines].join(',')}`);
  assert.ok(accounts.size >= 3, `pool needs >=3 quota accounts, has ${[...accounts].join(',')}`);
});
