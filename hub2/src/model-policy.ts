/** Model policy rules that must hold for both operator writes and runtime picks. */
export type ModelPolicyRoute = {
  id: string;
  engine: string;
  model: string;
  effort?: string;
  account_id: string;
};

const FORBIDDEN_ZAI_ENGINES = new Set(['zai', 'zcodex', 'goz']);
const FORBIDDEN_ZAI_ACCOUNTS = new Set(['zai-central', 'shared-zai', 'zcodex']);

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

  if (FORBIDDEN_ZAI_ENGINES.has(engine) || FORBIDDEN_ZAI_ACCOUNTS.has(account) || id.includes('zai-central'))
    return 'zai_picker_route_forbidden';
  if (/haiku/i.test(`${id} ${model}`)) return 'haiku_picker_route_forbidden';
  if (engine === 'claude' && (model !== 'sonnet' || candidate.effort !== 'medium'))
    return 'claude_picker_route_forbidden';
  if (/luna/i.test(model) && candidate.effort !== 'max') return 'luna_effort_forbidden';
  if (requiresBoundedEffort(model) && candidate.effort !== 'low' && candidate.effort !== 'medium')
    return 'bounded_effort_required';

  return null;
}

export function isModelPolicyCandidateAllowed(candidate: ModelPolicyRoute): boolean {
  return modelPolicyCandidateViolation(candidate) === null;
}
