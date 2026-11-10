/** Model policy rules that must hold for both operator writes and runtime picks. */
export type ModelPolicyRoute = {
  id: string;
  engine: string;
  model: string;
  effort?: string;
  account_id: string;
};

// Z.ai GLM is a pool member only through the canonical `zai` engine on the zai-central account.
const FORBIDDEN_ZAI_ENGINES = new Set(['zcodex', 'goz']);
const FORBIDDEN_ZAI_ACCOUNTS = new Set(['shared-zai', 'zcodex']);
const CLAUDE_PICKER_MODELS = new Set(['sonnet', 'opus']);

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

  if (FORBIDDEN_ZAI_ENGINES.has(engine) || FORBIDDEN_ZAI_ACCOUNTS.has(account)
    || (engine === 'zai' && account !== 'zai-central'))
    return 'zai_picker_route_forbidden';
  if (/haiku/i.test(`${id} ${model}`)) return 'haiku_picker_route_forbidden';
  if (engine === 'claude' && !(
    CLAUDE_PICKER_MODELS.has(model) && candidate.effort === 'medium'
    || id === 'claude-fable-5-2' && model === 'fable' && candidate.effort === 'medium'
  ))
    return 'claude_picker_route_forbidden';
  if (/luna/i.test(model) && candidate.effort !== 'max') return 'luna_effort_forbidden';
  if (requiresBoundedEffort(model) && candidate.effort !== 'low' && candidate.effort !== 'medium')
    return 'bounded_effort_required';

  return null;
}

export function isModelPolicyCandidateAllowed(candidate: ModelPolicyRoute): boolean {
  return modelPolicyCandidateViolation(candidate) === null;
}

/** Models that stay out of the default pick; only an explicit, reasoned request may select them. */
export function isExplicitOnlyModel(candidate: Pick<ModelPolicyRoute, 'id' | 'model'>): boolean {
  return /astra/i.test(`${candidate.id} ${candidate.model}`);
}

/** Deep-work models that a deep/complex tier hint biases toward. */
export function isDeepWorkModel(candidate: Pick<ModelPolicyRoute, 'engine' | 'model'>): boolean {
  return (candidate.engine.toLowerCase() === 'claude' && candidate.model.toLowerCase() === 'opus')
    || /^gpt-6(?:\.1)?-sol$/i.test(candidate.model);
}
