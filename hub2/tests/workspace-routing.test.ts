import {test} from 'node:test';
import assert from 'node:assert/strict';
import {buildRoutingPool, type RoutingSnapshot} from '../src/routing-pool.ts';
import {normalizeRepositoryOrigin, qualifiedRepositoryWorkspace} from '../src/repository-identity.ts';

function fixture(): RoutingSnapshot {
  const now = Date.now();
  const candidate = (engine: string) => ({id: engine, engine, model: engine === 'codex' ? 'gpt-6.1-sol' : 'gemini-4-hydrogen-medium',
    effort: 'medium', account_id: engine, weight: 1, capabilities: [], enabled: true});
  return {observed_at: now, policy: {version: 1, candidates: [candidate('codex'), candidate('antigravity')]},
    accounts: new Map(['codex', 'antigravity'].map(id => [id, {account: {enabled: true}, credential_usable: true,
      quota: {remaining: 60, captured_at: now, valid_until: now + 900000}, hosts: []}])),
    machines: new Map([
      ['linux', {connected: true, os: 'linux', engines: ['codex', 'antigravity'], capabilities: ['headless', 'effort:codex', 'effort:antigravity', 'workspace:home']}],
      ['desktop', {connected: true, os: 'windows', engines: ['antigravity'],
        capabilities: ['headless', 'effort:codex', 'effort:antigravity', 'browser:orin-chrome', 'tool:agent-browser', 'workspace:home']}],
    ])};
}

const request = {interactive: false, workspace: 'home',
  required_capabilities: ['os:windows', 'browser:orin-chrome', 'tool:agent-browser']};

test('documented desktop needs and workspace select the same compatible engine/host pair', () => {
  const result = buildRoutingPool(fixture(), request);
  assert.ok(result.options.length > 0);
  assert.ok(result.options.every(row => row.value.machine_id === 'desktop' && row.value.engine === 'antigravity'));
  assert.ok(!result.diagnostics.advisories?.some(row => row.reason === 'capability_not_advertised'));
});

test('workspace advertisement prevents a capable host with the wrong label winning', () => {
  const snapshot = fixture();
  snapshot.machines.set('wrong-workspace', {...snapshot.machines.get('desktop')!,
    capabilities: ['headless', 'effort:codex', 'effort:antigravity', 'browser:orin-chrome', 'tool:agent-browser', 'workspace:other']});
  const result = buildRoutingPool(snapshot, {...request, workspace: 'HOME'});
  assert.ok(result.options.every(row => row.value.machine_id === 'desktop'));
});

test('missing workspace observations preserve the advisory fallback without claiming compatibility', () => {
  const snapshot = fixture();
  snapshot.machines.get('desktop')!.capabilities = ['headless', 'effort:codex', 'effort:antigravity', 'browser:orin-chrome', 'tool:agent-browser'];
  const result = buildRoutingPool(snapshot, request);
  assert.ok(result.options.length > 0);
  assert.equal(result.diagnostics.pool_degraded, true);
  assert.ok(result.diagnostics.advisories?.some(row => row.reason === 'capability_not_advertised'));
});

test('absolute paths do not require a workspace label tag', () => {
  const result = buildRoutingPool(fixture(), {...request, workspace: 'C:\\Users\\Alexey'});
  assert.ok(result.options.every(row => row.value.machine_id === 'desktop'));
  assert.ok(!result.diagnostics.advisories?.some(row => row.reason === 'capability_not_advertised'));
});

test('repository identity normalizes registry HTTPS and checkout SSH to one stable bounded token', async () => {
  const registry = 'https://github.com/AI-Shipping-Labs/Website.git';
  const checkout = 'git@github.com:ai-shipping-labs/website.git';
  assert.equal(normalizeRepositoryOrigin(registry), 'github.com/ai-shipping-labs/website');
  assert.equal(normalizeRepositoryOrigin(checkout), normalizeRepositoryOrigin(registry));
  assert.equal(await qualifiedRepositoryWorkspace(registry), 'repo-948832775e82e294b99d190121d47ac86ec4986811a36b2c505e3a6f802');
  assert.equal((await qualifiedRepositoryWorkspace(registry))?.length, 64);
  assert.equal(normalizeRepositoryOrigin('https://user:secret@github.com/owner/repo.git'), 'github.com/owner/repo');
  assert.equal(normalizeRepositoryOrigin('https://github.com/owner/repo?token=secret'), undefined);
});

test('qualified repository requirement rejects old hosts that forge its token through a label', () => {
  const token = 'repo-948832775e82e294b99d190121d47ac86ec4986811a36b2c505e3a6f802';
  const snapshot = fixture();
  snapshot.machines.set('qualified', {...snapshot.machines.get('linux')!,
    capabilities: ['headless', 'effort:codex', 'effort:antigravity', 'repo-identity:v1', `workspace:${token}`]});
  snapshot.machines.set('old-host', {...snapshot.machines.get('desktop')!,
    // An old host can forge the token through a same-named directory/alias,
    // but supported old configuration cannot emit the new protocol marker.
    capabilities: ['headless', 'effort:antigravity', `workspace:${token}`]});
  const qualified = buildRoutingPool(snapshot, {interactive: false, workspace: token});
  assert.ok(qualified.options.length > 0);
  assert.ok(qualified.options.every(row => row.value.machine_id === 'qualified'));
  assert.ok(qualified.options.every(row => row.value.required_capabilities.includes('repo-identity:v1')));

  snapshot.machines.delete('qualified');
  assert.throws(() => buildRoutingPool(snapshot, {interactive: false, workspace: token}));
});
