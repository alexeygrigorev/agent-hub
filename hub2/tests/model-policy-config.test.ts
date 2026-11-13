// The committed routing policy must satisfy the operator's model rules on its
// own, so a policy change can never silently reintroduce a banned route.
// Shape rules mirror the /api/model-policy validation in hub2/src/control-do.ts.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { isOrdinaryWorkModel, modelPolicyCandidateViolation } from '../src/model-policy.ts';

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
  assert.equal(modelPolicyCandidateViolation(route({id: 'zai-glm', engine: 'zai', model: 'glm-5.3-flash', account_id: 'shared-zai'})), 'zai_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({id: 'zai-glm', engine: 'zai', model: 'glm-5.3-flash', account_id: 'zai-central'})), null);
  assert.equal(modelPolicyCandidateViolation(route({engine: 'claude', model: 'haiku', effort: 'medium'})), 'haiku_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({engine: 'claude', model: 'opus', effort: 'medium'})), null);
  assert.equal(modelPolicyCandidateViolation(route({engine: 'claude', model: 'opus', effort: 'max'})), 'claude_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({id: 'claude-fable-5-2', engine: 'claude', model: 'fable', effort: 'medium', account_id: 'claude'})), null);
  assert.equal(modelPolicyCandidateViolation(route({engine: 'claude', model: 'fable', effort: 'medium'})), 'claude_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gpt-6-luna', effort: 'medium'})), 'luna_effort_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gpt-6.1-sol', effort: 'max'})), 'bounded_effort_required');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gpt-6-astra'})), 'bounded_effort_required');
  assert.equal(modelPolicyCandidateViolation(route({model: 'gemini-4-argon-medium', effort: 'max'})), 'bounded_effort_required');
});

test('zai appears only as the canonical zai engine on zai-central; astra stays out of the default pool', () => {
  for (const candidate of policy.candidates) {
    assert.ok(!['zcodex', 'goz'].includes(candidate.engine), `candidate ${candidate.id} routes through a legacy zai engine`);
    if (candidate.engine === 'zai') assert.equal(candidate.account_id, 'zai-central');
    assert.ok(!/astra/i.test(candidate.model + ' ' + candidate.id), 'astra is explicit-only, never a default candidate');
  }
});

test('Gemini 4 Hydrogen via Antigravity replaces the flash route for ordinary picks (intake aaeee7b1)', () => {
  const hydrogen = policy.candidates.find(candidate => candidate.id === 'antigravity-hydrogen-medium');
  assert.ok(hydrogen, 'Hydrogen candidate is in the pool');
  assert.equal(hydrogen.engine, 'antigravity');
  assert.equal(hydrogen.model, 'gemini-4-hydrogen-medium');
  assert.equal(hydrogen.effort, 'medium');
  assert.equal(hydrogen.enabled, true);
  assert.deepEqual(hydrogen.capabilities ?? [], [], 'ordinary: not review-only');
  const ordinary = policy.candidates.filter(isOrdinaryWorkModel);
  assert.ok(!ordinary.some(candidate => /flash/i.test(`${candidate.id} ${candidate.model}`)), 'no flash route remains in the ordinary automatic pool');
  assert.equal(modelPolicyCandidateViolation({id: hydrogen.id, engine: 'antigravity', model: hydrogen.model, effort: 'medium', account_id: 'gemini', capabilities: []}), null);
});

test('no haiku anywhere in the pool', () => {
  for (const candidate of policy.candidates) assert.ok(!/haiku/i.test(candidate.model + ' ' + candidate.id));
});

test('claude exposes Sonnet and Opus by default and Fable only under its explicit policy id', () => {
  const claude = policy.candidates.filter(candidate => candidate.engine === 'claude');
  assert.deepEqual(claude.map(candidate => [candidate.id, candidate.model, candidate.effort]), [
    ['claude-sonnet-medium', 'sonnet', 'medium'],
    ['claude-fable-5-2', 'fable', 'medium'],
    ['claude-opus-medium', 'opus', 'medium'],
  ]);
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


test('Astra policy writes require review-only capability for both id and model aliases',()=>{
 const base={id:'review-route',engine:'codex',model:'gpt-6-astra',effort:'medium',account_id:'codex'};
 for(const route of [base,{...base,id:'ASTRA-review',model:'synthetic'}]){
  assert.equal(modelPolicyCandidateViolation(route),'astra_review_only_required');
  assert.equal(modelPolicyCandidateViolation({...route,capabilities:['headless']}),'astra_review_only_required');
  assert.equal(modelPolicyCandidateViolation({...route,capabilities:['review-only']}),null);
 }
});


test('committed GLM uses only the canonical ZAI route and remains outside ordinary work', () => {
  const routes = policy.candidates.filter(candidate => candidate.engine === 'zai');
  assert.deepEqual(routes, [{id: 'zai-glm-5-3-flash', engine: 'zai', model: 'glm-5.3-flash',
    effort: 'medium', account_id: 'zai-central', weight: 1, capabilities: [], enabled: true}]);
  assert.equal(isOrdinaryWorkModel(routes[0]), false);
  assert.deepEqual(policy.candidates.filter(candidate => candidate.engine === 'go').map(candidate => candidate.model),
    ['opencode-go/muse-spark-1.3-contributor']);
  assert(policy.candidates.filter(candidate => /glm/i.test(candidate.model))
    .every(candidate => candidate.engine === 'zai' && candidate.account_id === 'zai-central'));
});
