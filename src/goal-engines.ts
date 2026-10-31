// Engines whose native CLI starts from a '/goal ...' prompt. Claude Code
// supports /goal as well as the Codex family (intake bf0863c4); providers
// report the same capability as supports_goal. Host mirror: fleet.GOAL_ENGINES.
export const GOAL_ENGINES = new Set(['codex', 'zcodex', 'claude']);
export const GOAL_PROVIDERS = new Set(['codex', 'zai', 'claude']);
