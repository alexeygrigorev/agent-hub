/** Canonical harness/provider/account display labels.
 *
 * Intake 9c6d2c0f (session quota-accounts-harness-provider) is renaming stored
 * account keys (codex-orin -> codex/orin, urgent-muse -> opencode-go, zai ->
 * codex/zai). This module is display-only: stored provider/account keys are
 * never rewritten here, so history rows keyed by old names keep joining new
 * names through this mapping. When the sibling intake lands its renames,
 * update DISPLAY specially: keep old keys as aliases.
 */

/** Display triple for one stored provider key. */
export interface AccountDisplay {
  /** Execution harness, e.g. codex, opencode. */
  harness: string;
  /** Upstream provider, e.g. openai, zai, go. */
  provider: string;
  /** Human account label, e.g. shared-codex, orin, shared-zai, opencode-go. */
  account: string;
}

/** Stored provider key -> display triple. Unknown keys pass through. */
const DISPLAY: Record<string, AccountDisplay> = {
  codex: { harness: 'codex', provider: 'openai', account: 'shared-codex' },
  'codex-orin': { harness: 'codex', provider: 'openai', account: 'orin' },
  zai: { harness: 'codex', provider: 'zai', account: 'shared-zai' },
  go: { harness: 'opencode', provider: 'go', account: 'opencode-go' },
  claude: { harness: 'claude', provider: 'anthropic', account: 'shared-claude' },
  gemini: { harness: 'gemini', provider: 'google', account: 'shared-gemini' },
  grok: { harness: 'grok', provider: 'xai', account: 'shared-grok' },
  copilot: { harness: 'copilot', provider: 'github', account: 'shared-copilot' },
};

/** Aliases for post-rename stored keys (sibling intake 9c6d2c0f). Old rows
 * keep their old keys; new rows may arrive under renamed keys. Both join. */
const ALIASES: Record<string, string> = {
  'codex/orin': 'codex-orin',
  'opencode-go': 'go',
  'codex/zai': 'zai',
  'urgent-muse': 'go',
};

export function canonicalProviderKey(key: string): string {
  return ALIASES[key] || key;
}

export function displayAccount(storedProvider: string, storedLabel?: string): AccountDisplay {
  const key = canonicalProviderKey(storedProvider);
  const known = DISPLAY[key];
  if (known) return { ...known };
  const safe = String(storedLabel || storedProvider || 'unknown').slice(0, 64);
  return { harness: key, provider: key, account: safe };
}

/** "harness/provider account", e.g. "codex/openai orin". */
export function displayAccountLabel(storedProvider: string, storedLabel?: string): string {
  const d = displayAccount(storedProvider, storedLabel);
  return `${d.harness}/${d.provider} ${d.account}`;
}
