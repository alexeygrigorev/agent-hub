// OpenCode (go) device-login export lives in the central store (intake 5814e109 AC3).
import { describe, expect, it } from 'vitest';
import { validBundle } from '../src/provisioning';
import { CREDENTIAL_PATHS } from '../src/credentials';

const MODEL = ['--model', 'opencode-go/muse-spark-1.3-contributor'];
const base = { label: 'shared-go', provider: 'go', version: '1.19.3', files: {}, env: {}, args: MODEL };

describe('go credential bundle', () => {
  it('accepts no file or exactly the Hub-held device export', () => {
    expect(validBundle(base)).toBe(true);
    expect(validBundle({ ...base, files: { [CREDENTIAL_PATHS.go[0]]: '[{"id":"x"}]' } })).toBe(true);
  });
  it('rejects other files and any env secret', () => {
    expect(validBundle({ ...base, files: { '.local/share/opencode/auth.json': '{}' } })).toBe(false);
    expect(validBundle({ ...base, env: { OPENCODE_API_KEY: 'x' } })).toBe(false);
  });
});
