/** Model policy rules that must hold for both operator writes and runtime picks. */
export type ModelPolicyRoute = {
  id: string;
  engine: string;
  model: string;
  effort?: string;
  account_id: string;
  capabilities?: string[];
};

// Z.ai policy writes accept two shapes so a deploy can still prove every
// previously stored policy restorable: the canonical `zai` engine on zai-central,
// and — since GLM 5.4 (t3897u, 2026-11-23) — the `zcodex` engine on zai-central.
// Both are illegal on any other account; shared-zai/zcodex stay retired.
const FORBIDDEN_ZAI_ENGINES = new Set(['goz']);
const FORBIDDEN_ZAI_ACCOUNTS = new Set(['shared-zai', 'zcodex']);
// Standing policy updated for GLM 5.4 (t3897u): the picker may select Z.ai only
// through the `zcodex` engine on zai-central. Every other Z.ai shape (the bare
// `zai`/`goz` engines, the retired shared-zai/zcodex accounts, and zai-central on
// any other engine) stays excluded from the runtime picker.
const PICKER_EXCLUDED_ZAI_ENGINES = new Set(['zai', 'goz']);
const PICKER_EXCLUDED_ZAI_ACCOUNTS = new Set(['shared-zai', 'zcodex']);
const CLAUDE_PICKER_MODELS = new Set(['sonnet', 'opus']);
const CARBON_POLICY_ID = 'antigravity-carbon-medium';
const CARBON_MODEL_ID = 'gemini-4-carbon-medium';

function isCarbonRoute(candidate: Pick<ModelPolicyRoute, 'id' | 'model'>): boolean {
  return /carbon/i.test(`${candidate.id} ${candidate.model}`);
}

function requiresBoundedEffort(model: string): boolean {
  return /^gpt-6(?:\.1)?-(?:sol|astra)$/i.test(model)
    || /^gemini-4-argon(?:-|$)/i.test(model);
}

/** Return a stable error code for a candidate that violates operator policy. */
export function modelPolicyCandidateViolation(candidate: ModelPolicyRoute): string | null {
  const engine = candidate.engine.toLowerCase();
  const model = candidate.model.toLowerCase();
  const id = candidate.id.toLowerCase();
  const account = candidate.account_id.toLowerCase();

  // Carbon must stay tied to the verified model identity. Policy capabilities
  // are operator supplied metadata, so an omitted marker or alias must not
  // turn Carbon into an ordinary automatic route.
  if (isCarbonRoute(candidate) && (
    candidate.id !== CARBON_POLICY_ID
    || candidate.engine !== 'antigravity'
    || candidate.model !== CARBON_MODEL_ID
    || candidate.effort !== 'medium'
    || candidate.account_id !== 'gemini'
    || candidate.capabilities?.length !== 1
    || candidate.capabilities[0] !== 'think-only'
  )) return 'carbon_policy_contract_invalid';

  if (FORBIDDEN_ZAI_ENGINES.has(engine) || FORBIDDEN_ZAI_ACCOUNTS.has(account)
    || account === 'zai-central' && engine !== 'zai' && engine !== 'zcodex'
    || (engine === 'zai' || engine === 'zcodex') && account !== 'zai-central')
    return 'zai_picker_route_forbidden';
  if (account === 'go-orin' || engine === 'go-orin' || /\bgo-orin\b/i.test(id))
    return 'go_orin_picker_route_forbidden';
  if (/haiku/i.test(`${id} ${model}`)) return 'haiku_picker_route_forbidden';
  if (engine === 'claude' && !(
    CLAUDE_PICKER_MODELS.has(model) && candidate.effort === 'medium'
    || model === 'opus' && candidate.effort === 'high'
    || id === 'claude-fable-5-2' && model === 'fable' && candidate.effort === 'medium'
  ))
    return 'claude_picker_route_forbidden';
  if (/luna/i.test(model) && candidate.effort !== 'max') return 'luna_effort_forbidden';
  if (requiresBoundedEffort(model) && candidate.effort !== 'low' && candidate.effort !== 'medium')
    return 'bounded_effort_required';
  if (isExplicitOnlyModel(candidate) && !candidate.capabilities?.includes('review-only'))
    return 'astra_review_only_required';

  return null;
}

/** True when the runtime picker must never select this route. The GLM 5.4
 * zcodex route (t3897u) is the only Z.ai shape the picker may choose. */
export function isPickerExcludedZaiRoute(candidate: Pick<ModelPolicyRoute, 'engine' | 'account_id'>): boolean {
  const engine = candidate.engine.toLowerCase();
  const account = candidate.account_id.toLowerCase();
  return PICKER_EXCLUDED_ZAI_ENGINES.has(engine)
    || PICKER_EXCLUDED_ZAI_ACCOUNTS.has(account)
    || account === 'zai-central' && engine !== 'zcodex';
}

/** Runtime picker eligibility: valid policy data and not an excluded Z.ai route. */
export function isModelPolicyCandidateAllowed(candidate: ModelPolicyRoute): boolean {
  return modelPolicyCandidateViolation(candidate) === null && !isPickerExcludedZaiRoute(candidate);
}

/** Carbon identity stays think-only even if policy metadata omits its marker. */
export function isThinkOnlyModel(candidate: Pick<ModelPolicyRoute, 'id' | 'model' | 'capabilities'>): boolean {
  return isCarbonRoute(candidate) || candidate.capabilities?.includes('think-only') === true;
}

/** Hydrogen replaces Gemini Flash/Argon in ordinary automatic work, without changing other engines. */
export function isOrdinaryWorkModel(candidate: Pick<ModelPolicyRoute, 'id' | 'model' | 'engine'>): boolean {
  return candidate.engine.toLowerCase() !== 'antigravity'
    || !/(?:flash|gemini-4-argon)/i.test(`${candidate.id} ${candidate.model}`);
}

/** Models that stay out of the default pick; only an explicit, reasoned request may select them. */
export function isExplicitOnlyModel(candidate: Pick<ModelPolicyRoute, 'id' | 'model'>): boolean {
  return /astra/i.test(`${candidate.id} ${candidate.model}`);
}

/** Neon variants other than medium (low/high/xhigh, orchestrator) are pinned-only: the picker pool gets Neon medium alone. */
export function isNeonPinnedOnlyModel(candidate: Pick<ModelPolicyRoute, 'id' | 'model' | 'engine'>): boolean {
  return candidate.engine.toLowerCase() === 'antigravity'
    && /neon/i.test(candidate.model) && candidate.model.toLowerCase() !== 'gemini-4-neon-medium';
}

/** Luna needs explicit model/policy intent, never a machine/effort/tier hint. */
export function isLunaModel(candidate: Pick<ModelPolicyRoute, 'id' | 'model'>): boolean {
  return /luna/i.test(candidate.model);
}

/** Catalog's canonical Luna alias; other model selectors retain exact matching. */
export function matchesModelIntent(candidate: Pick<ModelPolicyRoute, 'id' | 'model'>, model: string): boolean {
  if (model === 'luna' || model === 'gpt-6-luna') return candidate.model === 'gpt-6-luna';
  return model === candidate.id || model === candidate.model;
}

// Closed projection of the existing model-routing catalog's NON-Luna names.
// The adjacent test checks the catalog projection; no runtime file/config read.
const NON_LUNA_MODEL_NAMES = new Set([
  "antigravity-carbon-medium","antigravity-hydrogen-medium","antigravity-neon-high","antigravity-neon-low",
  "antigravity-neon-medium","antigravity-neon-xhigh","antigravity-orchestrator-neon","argon","argon-gemini",
  "astra","beam-free","claude-fable-5-2","deepseek-flash",
  "deepseek-v4-pro","fable","gemini-4-argon-contributor","gemini-4-argon-medium",
  "gemini-4-carbon-medium","gemini-4-hydrogen-medium","gemini-4-neon-high","gemini-4-neon-low","gemini-4-neon-medium","gemini-4-neon-xhigh","gemini-pro-orchestrator-neon","glm-5-2","glm-5-3",
  "glm-5-3-flash","glm-5.3-flash","glm-5.4","glm-flash","glm-flash-zcodex",
  "gpt-6-astra","gpt-6.1-sol","grok","grok-4-7",
  "grok-4.6","hy3","hy4-preview","kimi-k2-7-code",
  "kimi-k3","longcat-2-0","mimo-v2-5","mimo-v2-5-pro",
  "mimo-v2-6-flash","mimo-v2-6-pro","minimax-m3","minimax-m3-1-flash",
  "muse","muse-spark-1-2-contributor","opencode-go/beam-free","opencode-go/deepseek-v4-pro",
  "opencode-go/deepseek-v4.1-flash","opencode-go/gemini-4-argon-contributor","opencode-go/glm-5.2","opencode-go/glm-5.3",
  "opencode-go/glm-5.3-flash","opencode-go/grok-4.7","opencode-go/hy3","opencode-go/hy4-preview",
  "opencode-go/kimi-k2.7-code","opencode-go/kimi-k3","opencode-go/longcat-2.0","opencode-go/mimo-v2.5",
  "opencode-go/mimo-v2.5-pro","opencode-go/mimo-v2.6-flash","opencode-go/mimo-v2.6-pro","opencode-go/minimax-m3",
  "opencode-go/minimax-m3.1-flash","opencode-go/muse-spark-1.2-contributor","opencode-go/muse-spark-1.3-contributor","opencode-go/qwen3.7-plus",
  "opencode-go/qwen3.8-flash","opencode-go/qwen3.8-max","opencode-go/step-5-preview","qwen3-7-plus",
  "qwen3-8-flash","qwen3-8-max","sol","sonnet",
  "step-5-preview",
]);

/** Resolve the complete selector before authorizing any Luna candidate.
 * A policy-id/model collision with another model cannot silently become Luna.
 * Non-Luna model aliases (including Fable) never convey Luna intent, even if
 * a policy gives a Luna route that ambiguous alias as its id.
 */
export function explicitLunaIntent(candidates: readonly ModelPolicyRoute[], model: string | undefined): boolean {
  if (!model) return false;
  const hint = model.toLowerCase();
  if (NON_LUNA_MODEL_NAMES.has(hint)) return false;
  const matches = candidates.filter(candidate => candidate.id === model || matchesModelIntent(candidate, model));
  return matches.length > 0 && matches.every(candidate => /luna/i.test(candidate.model));
}

/** Deep-work models that a deep/complex tier hint biases toward. */
export function isDeepWorkModel(candidate: Pick<ModelPolicyRoute, 'engine' | 'model'>): boolean {
  return (candidate.engine.toLowerCase() === 'claude' && candidate.model.toLowerCase() === 'opus')
    || /^gpt-6(?:\.1)?-sol$/i.test(candidate.model);
}

/** Strong models a deep/complex/serious tier is restricted to (docs/ROUTING.md deep tier). */
export function isStrongModel(candidate: Pick<ModelPolicyRoute, 'engine' | 'model'>): boolean {
  return isDeepWorkModel(candidate) || /^gemini-4-argon(?:-|$)/i.test(candidate.model);
}

/** Elevated effort (high/max) is never part of an ordinary automatic pick; luna's max is its only mode. */
export function isElevatedEffort(candidate: Pick<ModelPolicyRoute, 'model' | 'effort'>): boolean {
  return candidate.effort === 'high' || candidate.effort === 'max' && !/luna/i.test(candidate.model);
}

/** Default effort per model: Haiku and Luna run at max, every other model at medium (Alexey t2760u). */
export function defaultEffortFor(model: string): 'max' | 'medium' {
  return /haiku|luna/i.test(model) ? 'max' : 'medium';
}

/** True when the candidate's effort is not its model's default, so it needs an explicit request. */
export function isNonDefaultEffort(candidate: Pick<ModelPolicyRoute, 'model' | 'effort'>): boolean {
  return !!candidate.effort && candidate.effort !== defaultEffortFor(candidate.model);
}
