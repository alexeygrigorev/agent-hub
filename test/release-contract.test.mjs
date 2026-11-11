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

test('matched CLI release staging streams tar archive over SSH stdin without heredoc collision', async () => {
  const {chmodSync, mkdirSync, mkdtempSync, readFileSync: readFs, rmSync, writeFileSync} = await import('node:fs');
  const {join} = await import('node:path');
  const {spawnSync} = await import('node:child_process');
  const flow = read('.github/workflows/deploy.yml');
  const stepMatch = flow.match(
    /- name: Stage verified matched CLI release bundle on VM[\s\S]*?run: \|\n((?:          .*\n)+)/
  );
  assert.ok(stepMatch, 'deploy.yml must define the matched CLI staging step');
  assert.match(
    flow,
    /if: always\(\) && steps\.finalize_deploy\.outcome != 'success' && steps\.deploy_vm\.outcome != 'skipped'/,
    'automatic rollback must only run when deploy_vm was attempted'
  );
  const script = stepMatch[1].replace(/^          /gm, '');
  assert.doesNotMatch(script, /<<['"]?STAGE_EOF/, 'ssh staging command must not override tar pipe stdin with a heredoc');

  const tmpRoot = new URL('../.local/tmp', import.meta.url).pathname;
  mkdirSync(tmpRoot, {recursive: true});
  const workDir = mkdtempSync(join(tmpRoot, 'stage-cli-contract-'));
  try {
    const sha = '49bb4ec03f7863ff2742f71144ba24dc6deeefcc';
    const bundleDir = join(workDir, 'bundle');
    const remoteTmp = join(workDir, 'remote-tmp');
    const binDir = join(workDir, 'bin');
    const runnerTemp = join(workDir, 'runner-temp');
    mkdirSync(bundleDir, {recursive: true});
    mkdirSync(join(runnerTemp, 'agent-hub-ssh'), {recursive: true});
    mkdirSync(binDir, {recursive: true});
    writeFileSync(join(runnerTemp, 'agent-hub-ssh', 'hub_vm_key'), 'dummy-key\n');
    writeFileSync(join(bundleDir, 'host-release.json'), JSON.stringify({host_version: sha}));
    writeFileSync(join(bundleDir, `agent_hub_host-2.0.0+g${sha}-py3-none-any.whl`), 'wheel-bytes');

    const mockSsh = join(binDir, 'ssh');
    writeFileSync(
      mockSsh,
      `#!/usr/bin/env bash
set -euo pipefail
remote_cmd="\${!#}"
rewritten="\${remote_cmd//\\/opt\\/agent-hub\\/.local\\/tmp/${remoteTmp}}"
exec bash -c "\$rewritten"
`
    );
    chmodSync(mockSsh, 0o755);

    const res = spawnSync('bash', ['-c', script], {
      env: {
        ...process.env,
        PATH: `${binDir}:${process.env.PATH}`,
        DEPLOY_SHA: sha,
        BUNDLE_DIR: bundleDir,
        RUNNER_TEMP: runnerTemp,
        SSH_TARGET: 'alexey@hub-vm.example.invalid',
      },
      encoding: 'utf8',
    });
    assert.equal(res.status, 0, res.stdout + res.stderr);
    const stagedDir = join(remoteTmp, `ci-cli-release-${sha}`);
    assert.equal(readFs(join(stagedDir, 'host-release.json'), 'utf8'), JSON.stringify({host_version: sha}));
    assert.equal(readFs(join(stagedDir, `agent_hub_host-2.0.0+g${sha}-py3-none-any.whl`), 'utf8'), 'wheel-bytes');
  } finally {
    rmSync(workDir, {recursive: true, force: true});
  }
});

test('policy recovery helper protocol tests pass offline', {timeout: 45000}, async () => {
  const {spawnSync} = await import('node:child_process');
  const result = spawnSync('python3', ['-m', 'unittest', 'discover', '-s', 'test', '-p', 'test_apply_model_policy.py'],
    {cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 40000});
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stdout + result.stderr);
});


test('deployment service credential bootstrap regressions execute in the required release check', {timeout: 45000}, () => {
  const result = spawnSync('python3', ['-m', 'unittest', 'discover', '-s', 'test', '-p', 'test_prepare_model_policy_service_credential.py'],
    {cwd: fileURLToPath(new URL('../', import.meta.url)), encoding: 'utf8', timeout: 40000});
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
