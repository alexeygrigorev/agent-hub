// Canonical release contract for the Node runtime: no Cloudflare toolchain,
// hub2 entry points intact, dispatch-only production deploys, SSH-only secrets.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const read = path => readFileSync(new URL('../' + path, import.meta.url), 'utf8');

test('schema rollback and transaction regressions execute in the required release check', () => {
  const result = spawnSync('python3', ['-m', 'unittest', 'discover', '-s', 'test', '-p', 'test_schema_*.py', '-v'], {
    cwd: fileURLToPath(new URL('../', import.meta.url)), encoding: 'utf8', timeout: 120_000,
  });
  assert.equal(result.status, 0, result.stderr || String(result.error));
});

test('hub2 entry and production manifest select the Node runtime only', () => {
  const production = read('hub2/production-worker.ts');
  assert.match(production, /from '.\/src\/worker'/);
  for (const cls of ['ControlDO', 'ProjectDO', 'MachineDO', 'AccountDO', 'MailboxDO'])
    assert.match(production, new RegExp('export \{ ' + cls + ' \}'), cls + ' must stay exported for the Node container map');
  assert.doesNotMatch(read('hub2/package.json') + read('package.json'), /"wrangler"/, 'wrangler must stay out of the dependency tree');
  assert.doesNotMatch(read('package.json'), /miniflare/, 'no Cloudflare runtime may come back through dev tooling');
});

test('production deploy is dispatch-gated, SSH-only and health verified', () => {
  const flow = read('.github/workflows/deploy.yml');
  assert.match(flow, /uses: \.\/\.github\/workflows\/hub2\.yml/);
  assert.match(flow, /github\.event_name == 'workflow_dispatch'/);
  assert.match(flow, /needs: \[hub2, distribution\]/);
  assert.doesNotMatch(flow, /CLOUDFLARE_API_TOKEN|CLOUDFLARE_ACCOUNT_ID/);
  assert.doesNotMatch(flow, /wrangler|gcloud|GOOGLE_APPLICATION_CREDENTIALS/);
  assert.match(flow, /AGENT_HUB_VM_DEPLOY_KEY/);
  assert.equal((flow.match(/printf '%s\\n' "\$VM_DEPLOY_KEY"/g) ?? []).length, 2,
    'both deploy and rollback key installs must terminate the private-key file with a newline');
  assert.equal((flow.match(/ssh-keygen -y -P '' -f/g) ?? []).length, 2,
    'both deploy and rollback must parse-check the private key before SSH');
  assert.match(flow, /agent-hub-hub2\.service/);
  assert.match(flow, /hub\.dtcdev\.click\/health/);
});

test('rollback validation stays read-only and Cloudflare-free', () => {
  const rollback = read('.github/workflows/rollback.yml');
  assert.doesNotMatch(rollback, /CLOUDFLARE_API_TOKEN|versions deploy/);
  assert.match(rollback, /contents: read/);
});

test('policy recovery helper protocol tests pass offline', {timeout: 45000}, async () => {
  const {spawnSync} = await import('node:child_process');
  const result = spawnSync('python3', ['-m', 'unittest', 'discover', '-s', 'test', '-p', 'test_apply_model_policy.py'],
    {cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 40000});
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
