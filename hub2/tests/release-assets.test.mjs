import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rename, rm, symlink, unlink, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { registerTypeScriptResolveHook } from '../node/compat.ts';
import { assetFetcher } from '../node/assets.ts';
registerTypeScriptResolveHook();
const { default: worker } = await import('../src/worker.ts');
const { default: production } = await import('../production-worker.ts');
let assets = 0;
const env = {
  ASSETS: { fetch: async () => { assets++; return new Response('artifact'); } },
  CONTROL: { idFromName: () => 'global', get: () => ({ fetch: async () => Response.json({ error: 'unauthorized' }, { status: 401 }) }) },
};
const names = ['host-release.json', 'hub.pyz', 'hub.pyz.sha256', 'manifest.json',
  'install.sh', 'install.ps1', 'host-install.sh', 'hub2-self-update.py', 'hub2-enroll.py',
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

const scratch = resolve(import.meta.dirname, '../../.local/tmp');
await mkdir(scratch, { recursive: true });
const testDir = await mkdtemp(join(scratch, 'release-assets-'));
try {
  const publicDir = join(testDir, 'public');
  const releaseRoot = join(testDir, 'managed');
  const first = `releases/${'a'.repeat(40)}`;
  const second = `releases/${'b'.repeat(40)}`;
  await mkdir(join(publicDir, 'cli'), { recursive: true });
  await writeFile(join(publicDir, 'index.html'), 'dashboard');
  await writeFile(join(publicDir, 'cli/manifest.json'), 'legacy');
  await writeFile(join(publicDir, 'cli/install.sh'), 'legacy installer');
  for (const [target, content] of [[first, 'first'], [second, 'second release']]) {
    await mkdir(join(releaseRoot, target), { recursive: true });
    await writeFile(join(releaseRoot, target, 'manifest.json'), content);
  }
  const current = join(releaseRoot, 'current');
  const runtime = { ...env, ASSETS: assetFetcher(publicDir, releaseRoot), HUB2_RELEASE_MODE: 'active' };
  const get = path => production.fetch(new Request(`https://hub.example${path}`), runtime);
  const select = async target => {
    const next = join(releaseRoot, 'next');
    await symlink(target, next, 'dir');
    await rename(next, current);
  };
  assert.equal(await (await get('/cli/manifest.json')).text(), 'legacy');
  await select(first);
  assert.equal(await (await get('/cli/manifest.json')).text(), 'first');
  assert.equal((await get('/cli/install.sh')).status, 404, 'active release does not mix in legacy assets');
  assert.equal(await (await get('/')).text(), 'dashboard');
  await select(second);
  const asset = await get('/cli/manifest.json');
  assert.equal(await asset.text(), 'second release');
  assert.equal(asset.headers.get('Content-Length'), '14');
  await select(first);
  assert.equal(await (await get('/cli/manifest.json')).text(), 'first');
  await select('../public/cli');
  assert.equal((await get('/cli/manifest.json')).status, 404, 'unmanaged pointer fails closed');
  await unlink(current);
  assert.equal(await (await get('/cli/manifest.json')).text(), 'legacy');
} finally {
  await rm(testDir, { recursive: true, force: true });
}
console.log('Release assets: public GET/HEAD only; arbitrary files, API and writes remain authenticated.');
