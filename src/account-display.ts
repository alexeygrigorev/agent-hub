/** History display reuses the canonical quota account/provider mapping. */
import { canonicalQuotaProvider, canonicalHarness, canonicalAccountLabel } from './quota';
export interface AccountDisplay { harness: string; provider: string; account: string }
const engines: Record<string,string> = {go:'opencode',zai:'zcodex'};
export const canonicalProviderKey = canonicalQuotaProvider;
export function displayAccount(storedProvider: string, storedLabel?: string): AccountDisplay {
  const provider = canonicalQuotaProvider(storedProvider);
  const account = canonicalAccountLabel(storedLabel || storedProvider);
  return {harness: canonicalHarness(engines[provider] || provider),provider,account};
}
export function displayAccountLabel(storedProvider: string, storedLabel?: string): string {
  const d = displayAccount(storedProvider,storedLabel);
  return `${d.harness}/${d.provider} ${d.account}`;
}
