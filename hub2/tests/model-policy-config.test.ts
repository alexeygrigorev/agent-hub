// The committed routing policy must satisfy the operator's model rules on its
// own, so a policy change can never silently reintroduce a banned route.
// Shape rules mirror the /api/model-policy validation in hub2/src/control-do.ts.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { isModelPolicyCandidateAllowed, isOrdinaryWorkModel, isThinkOnlyModel, modelPolicyCandidateViolation } from '../src/model-policy.ts';

const root = resolve(import.meta.dirname, '../..');
const policy = JSON.parse(readFileSync(resolve(root, 'config/model-policy.json'), 'utf8')) as {
  candidates: {
    id: string; engine: string; model: string; effort?: string;
    account_id: string; weight: number; capabilities: string[]; enabled: boolean;
  }[];
};
const routing = JSON.parse(readFileSync(resolve(root, 'config/model-routing.json'), 'utf8')) as {
  models: {id:string; model:string; provider:string; engine:string; effort:string|null}[];
  tiers: Record<string,string[]>;
  rules: {auto_start_models:string[]};
};

const FIELD_PATTERN = /^[A-Za-z0-9_.:/-]+$/;
const EFFORTS = new Set(['low', 'medium', 'high', 'max']);

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
  // GLM 5.4 (t3897u): the zcodex engine is writable only on zai-central; the
  // retired shared-zai/zcodex accounts and every other account stay forbidden.
  assert.equal(modelPolicyCandidateViolation(route({id: 'glm-flash-zcodex', engine: 'zcodex', model: 'glm-5.4', account_id: 'zai-central'})), null);
  assert.equal(modelPolicyCandidateViolation(route({id: 'zai-central', engine: 'zcodex', model: 'glm-flash', account_id: 'shared-zai'})), 'zai_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({id: 'zai-central', engine: 'zcodex', model: 'glm-flash', account_id: 'zai-other'})), 'zai_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({id: 'zai-glm', engine: 'zai', model: 'glm-5.3-flash', account_id: 'shared-zai'})), 'zai_picker_route_forbidden');
  // Standing policy: "No zai-central in the Hub picker" applies to every shape
  // except the GLM 5.4 zcodex route. Writes still accept the previously deployed
  // zai-engine zai-central shape so a deploy can prove v22 restorable, but the
  // runtime picker never selects it.
  const central = route({id: 'zai-glm', engine: 'zai', model: 'glm-5.3-flash', account_id: 'zai-central'});
  const glm54 = route({id: 'glm-flash-zcodex', engine: 'zcodex', model: 'glm-5.4', effort: 'medium', account_id: 'zai-central'});
  assert.equal(modelPolicyCandidateViolation(central), null);
  assert.equal(isModelPolicyCandidateAllowed(central), false);
  assert.equal(modelPolicyCandidateViolation(glm54), null);
  assert.equal(isModelPolicyCandidateAllowed(glm54), true);
  assert.equal(modelPolicyCandidateViolation(route({id: 'goz-central', engine: 'goz', model: 'glm-5.4', account_id: 'zai-central'})), 'zai_picker_route_forbidden');
  // zai-central never pairs with an unrelated engine (review of 10fade21f): the
  // account is reachable only through the zai and zcodex Z.ai engines.
  assert.equal(modelPolicyCandidateViolation(route({id: 'bad-zai-account', engine: 'codex', model: 'glm-5.4', effort: 'medium', account_id: 'zai-central'})), 'zai_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation(route({id: 'bad-zai-account', engine: 'claude', model: 'glm-5.4', effort: 'medium', account_id: 'zai-central'})), 'zai_picker_route_forbidden');
  assert.equal(isModelPolicyCandidateAllowed(route({id: 'sneaky', engine: 'codex', model: 'gpt-6.1-sol', effort: 'medium', account_id: 'zai-central'})), false);
  assert.equal(modelPolicyCandidateViolation(route({id: 'zai-glm', engine: 'zai', model: 'glm-5.3-flash', account_id: 'zai-other'})), 'zai_picker_route_forbidden');
  assert.equal(isModelPolicyCandidateAllowed(route({id: 'sol', engine: 'codex', model: 'gpt-6.1-sol', effort: 'medium', account_id: 'codex'})), true);
  const zaiRoutes = policy.candidates.filter(c => c.account_id === 'zai-central' || c.engine === 'zai');
  assert.deepEqual(zaiRoutes.map(c => [c.id, c.engine, c.model, c.account_id]),
    [['glm-flash-zcodex', 'zcodex', 'glm-5.4', 'zai-central']],
    'the committed policy carries exactly the GLM 5.4 zcodex route');
  assert.equal(modelPolicyCandidateViolation(route({id: 'go-orin-muse', engine: 'go', model: 'opencode-go/muse-spark-1.3-contributor', account_id: 'go-orin'})), null);
  assert.equal(modelPolicyCandidateViolation(route({id: 'opencode-go', engine: 'go-orin', model: 'opencode-go/muse-spark-1.3-contributor', account_id: 'shared-go'})), 'go_orin_picker_route_forbidden');
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

test('Z.ai reaches the picker only through the GLM 5.4 zcodex route on zai-central', () => {
  const zai = policy.candidates.filter(candidate => ['zai', 'zcodex', 'goz'].includes(candidate.engine)
    || ['zai-central', 'shared-zai', 'zcodex'].includes(candidate.account_id));
  assert.deepEqual(zai.map(candidate => [candidate.id, candidate.engine, candidate.model, candidate.effort, candidate.account_id]),
    [['glm-flash-zcodex', 'zcodex', 'glm-5.4', 'medium', 'zai-central']],
    'exactly one Z.ai route exists: the GLM 5.4 zcodex candidate');
  for (const candidate of policy.candidates) {
    assert.ok(candidate.engine !== 'zai' && candidate.engine !== 'goz', `candidate ${candidate.id} uses a bare zai engine`);
    assert.ok(!['shared-zai', 'zcodex'].includes(candidate.account_id), `candidate ${candidate.id} uses a retired zai account`);
    if (candidate.account_id === 'zai-central') assert.equal(candidate.engine, 'zcodex', 'zai-central is picker-reachable only via zcodex');
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
  assert.ok(!ordinary.some(candidate => candidate.engine === 'antigravity' && /flash|argon/i.test(`${candidate.id} ${candidate.model}`)), 'no antigravity flash/argon route remains in the ordinary automatic pool');
  assert.ok(ordinary.filter(candidate => candidate.account_id === 'zai-central')
    .every(candidate => candidate.id === 'glm-flash-zcodex' && candidate.engine === 'zcodex' && candidate.model === 'glm-5.4'),
    'zai-central reaches the ordinary pool only through the GLM 5.4 zcodex route');
  assert.equal(modelPolicyCandidateViolation({id: hydrogen.id, engine: 'antigravity', model: hydrogen.model, effort: 'medium', account_id: 'gemini', capabilities: []}), null);
});

test('Gemini 4 Carbon is allowlisted for pinned thinking only, outside every automatic tier', () => {
  const carbon = policy.candidates.find(candidate => candidate.id === 'antigravity-carbon-medium');
  assert.ok(carbon, 'Carbon candidate is in the Hub pool');
  assert.equal(carbon.engine, 'antigravity');
  assert.equal(carbon.model, 'gemini-4-carbon-medium');
  assert.equal(carbon.effort, 'medium');
  assert.equal(carbon.account_id, 'gemini');
  assert.deepEqual(carbon.capabilities, ['think-only']);
  assert.equal(carbon.enabled, true);
  assert.equal(modelPolicyCandidateViolation(carbon), null);

  const localCarbon = routing.models.find(model => model.id === 'antigravity-carbon-medium');
  assert.deepEqual(localCarbon, {
    id: 'antigravity-carbon-medium', model: 'gemini-4-carbon-medium', provider: 'gemini',
    engine: 'antigravity', profile: null, effort: 'medium',
  });
  assert.ok(Object.values(routing.tiers).every(members => !members.includes('antigravity-carbon-medium')),
    'Carbon must not join daily, review, deep or quick defaults');
  assert.ok(!routing.rules.auto_start_models.includes('antigravity-carbon-medium'),
    'Carbon must not join automatic starts');
  assert.ok(policy.candidates.some(candidate => candidate.id === 'antigravity-hydrogen-medium'
    && candidate.model === 'gemini-4-hydrogen-medium'), 'Hydrogen remains the ordinary Gemini candidate');
});

test('Carbon identity requires the exact think-only policy contract', () => {
  const canonical = policy.candidates.find(candidate => candidate.id === 'antigravity-carbon-medium')!;
  const malformed = [
    {...canonical, capabilities: []},
    {...canonical, id: 'generic-route', capabilities: []},
    {...canonical, id: 'generic-carbon-alias'},
    {...canonical, model: 'gemini-4-carbon-alias'},
    {...canonical, engine: 'codex'},
    {...canonical, effort: 'low'},
    {...canonical, account_id: 'codex'},
    {...canonical, capabilities: ['think-only', 'review-only']},
  ];
  for (const candidate of malformed) {
    assert.equal(isThinkOnlyModel(candidate), true, `${candidate.id} remains think-only by identity`);
    assert.equal(modelPolicyCandidateViolation(candidate), 'carbon_policy_contract_invalid');
  }
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
    ['claude-opus-high', 'opus', 'high'],
  ]);
});

test('Opus is the only Claude route allowed at high effort (intake 10a76faa)', () => {
  const route = {id: 'r', engine: 'claude', account_id: 'claude', capabilities: []};
  assert.equal(modelPolicyCandidateViolation({...route, model: 'opus', effort: 'high'}), null);
  assert.equal(modelPolicyCandidateViolation({...route, model: 'sonnet', effort: 'high'}), 'claude_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation({...route, id: 'claude-fable-5-2', model: 'fable', effort: 'high'}), 'claude_picker_route_forbidden');
  assert.equal(modelPolicyCandidateViolation({...route, engine: 'codex', account_id: 'codex', model: 'gpt-6.1-sol', effort: 'high'}), 'bounded_effort_required');
  assert.equal(modelPolicyCandidateViolation({...route, engine: 'antigravity', account_id: 'gemini', model: 'gemini-4-argon-medium', effort: 'high'}), 'bounded_effort_required');
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
    assert.ok(candidate.effort === 'low' || candidate.effort === 'medium', `${candidate.id} must stay at a bounded effort`);
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


test('The committed Z.ai route is the GLM 5.4 zcodex candidate and Go stays on Muse', () => {
  assert.deepEqual(policy.candidates.filter(candidate => /glm/i.test(candidate.model)),
    [{id: 'glm-flash-zcodex', engine: 'zcodex', model: 'glm-5.4', effort: 'medium', account_id: 'zai-central', weight: 1, capabilities: [], enabled: true}]);
  assert.deepEqual(policy.candidates.filter(candidate => candidate.engine === 'go').map(candidate => [candidate.account_id, candidate.model]),
    [['shared-go', 'opencode-go/muse-spark-1.3-contributor'], ['go-orin', 'opencode-go/muse-spark-1.3-contributor']]);
});
