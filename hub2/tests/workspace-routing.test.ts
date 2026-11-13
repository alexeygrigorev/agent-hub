import {test} from 'node:test';
import assert from 'node:assert/strict';
import {buildRoutingPool, type RoutingSnapshot} from '../src/routing-pool.ts';

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

test('placement retries exclude every machine already attempted', () => {
  const result = buildRoutingPool(fixture(), {interactive: false, workspace: 'home', exclude_machine_ids: ['desktop']});
  assert.ok(result.options.length > 0);
  assert.ok(result.options.every(row => row.value.machine_id === 'linux'));
});
