import assert from 'node:assert/strict';
import { registerTypeScriptResolveHook } from '../node/compat.ts';
registerTypeScriptResolveHook();
const { default: worker } = await import('../src/worker.ts');
const { default: production } = await import('../production-worker.ts');
let assets = 0;
const env = {
  ASSETS: { fetch: async () => { assets++; return new Response('artifact'); } },
  CONTROL: { idFromName: () => 'global', get: () => ({ fetch: async () => Response.json({ error: 'unauthorized' }, { status: 401 }) }) },
};
const names = ['host-release.json', 'hub.pyz', 'hub.pyz.sha256', 'manifest.json',
  'host-install.sh', 'hub2-self-update.py', 'hub2-enroll.py',
  'agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer',
  `agent_hub_host_v2-2.0.0+g${'a'.repeat(40)}-py3-none-any.whl`, 'websockets-16.0-py3-none-any.whl'];
for (const name of names) {
  for (const method of ['GET', 'HEAD']) {
    const before = assets;
    const response = await worker.fetch(new Request(`https://hub.example/cli/${name}`, { method }), env);
    assert.equal(response.status, 200, `${method} ${name}`);
    assert.equal(assets, before + 1);
    const bootstrap = await production.fetch(new Request(`https://hub.example/cli/${name}`, { method }),
      { ...env, HUB2_RELEASE_MODE: 'bootstrap' });
    assert.equal(bootstrap.status, 200, `bootstrap ${method} ${name}`);
    assert.equal(assets, before + 2);
  }
}
for (const path of ['/cli/private.env', '/cli/%2e%2e/private.env', '/cli/other-1.0.whl', '/cli/agent_hub_host_v2-malformed.whl', '/api/sessions']) {
  const before = assets;
  assert.notEqual((await worker.fetch(new Request('https://hub.example' + path), env)).status, 200);
  assert.equal(assets, before, path);
}
const before = assets;
assert.notEqual((await worker.fetch(new Request('https://hub.example/cli/host-release.json', { method: 'POST' }), env)).status, 200);
assert.equal(assets, before);
assert.equal((await production.fetch(new Request('https://hub.example/cli/host-release.json', { method: 'POST' }),
  { ...env, HUB2_RELEASE_MODE: 'bootstrap' })).status, 503);
assert.equal(assets, before);
console.log('Release assets: public GET/HEAD only; arbitrary files, API and writes remain authenticated.');
