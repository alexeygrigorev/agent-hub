import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/worker';

const admin = { id: 'human:settings', kind: 'operator', scopes: ['operator:admin'] };
const agent = { id: 'agent:settings', kind: 'agent', scopes: [] };
function request(floor: string | undefined, actor = admin, method = 'GET', authenticated = true) {
  const env = {
    HUB2_DISK_FLOOR_GB: floor,
    CONTROL: {
      idFromName: (name: string) => name,
      get: () => ({ fetch: async (r: Request) => {
        const path = new URL(r.url).pathname;
        if (path === '/_auth/cookie') return new Response(null, { status: 401 });
        assert.equal(path, '/_verify');
        return Response.json({ actor });
      } }),
    },
  } as unknown as Parameters<typeof worker.fetch>[1];
  const headers = authenticated ? { Authorization: 'Bearer ' + 'fixture-token'.repeat(3) } : undefined;
  return worker.fetch(new Request('https://hub.test/api/resource-settings', { method, headers }), env);
}

test('only an administrator can read the configured disk floor, without caching', async () => {
  const response = await request('27.5');
  assert.equal(response.status, 200);
  assert.match(response.headers.get('Cache-Control') || '', /no-store/);
  assert.deepEqual(await response.json(), { resource_settings: { disk_floor_gb: 27.5 } });
  assert.equal((await request('27.5', admin, 'GET', false)).status, 401);
  assert.equal((await request('27.5', agent)).status, 403);
  assert.equal((await request('27.5', admin, 'PUT')).status, 405);
});

test('missing or malformed placement settings cannot produce a default or a cached value', async () => {
  const missing = await request(undefined);
  assert.equal(missing.status, 503);
  assert.deepEqual(await missing.json(), { error: 'resource_settings_unconfigured' });
  for (const value of ['', '0', '0x10', '1e1', 'NaN', 'Infinity', '1048577']) {
    const invalid = await request(value);
    assert.equal(invalid.status, 503);
    assert.match(invalid.headers.get('Cache-Control') || '', /no-store/);
    assert.deepEqual(await invalid.json(), { error: 'invalid_resource_settings' });
  }
});
