import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/worker';

const token = 'synthetic-session-route-token-0000000000';
const sessionId = '82872611-39d0-4710-9369-d831845d23e4';

function fixture() {
  const forwarded: Request[] = [];
  const control = {
    idFromName: (name: string) => name,
    get: () => ({
      fetch: async (input: string | Request, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(input, init);
        assert.equal(new URL(request.url).pathname, '/_actors/register');
        return Response.json({ registered: true });
      },
    }),
  };
  const machines = {
    idFromName: (name: string) => name,
    get: () => ({
      fetch: async (request: Request) => {
        forwarded.push(request);
        return Response.json({ forwarded: true });
      },
    }),
  };
  return {
    forwarded,
    env: { CONTROL: control, MACHINES: machines, HUB2_DEV_OPERATOR_TOKEN: token } as any,
  };
}

test('public session stop route reaches the selected machine unchanged', async () => {
  const f = fixture();
  const response = await worker.fetch(new Request(
    `https://hub.test/api/sessions/${sessionId}/stop?machine_id=hetzner`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
        Origin: 'https://hub.test',
      },
      body: JSON.stringify({ key: 'session-stop-82872611-20261110' }),
    },
  ), f.env);

  assert.equal(response.status, 200, 'stop route response');
  assert.deepEqual(await response.json(), { forwarded: true });

  assert.equal(f.forwarded.length, 1);
  const request = f.forwarded[0];
  assert.equal(request.method, 'POST');
  assert.equal(new URL(request.url).pathname, `/api/sessions/${sessionId}/stop`);
  assert.equal(new URL(request.url).searchParams.get('machine_id'), 'hetzner');
  assert.deepEqual(await request.json(), { key: 'session-stop-82872611-20261110' });
  assert.equal(JSON.parse(request.headers.get('x-hub2-actor') || '{}').id, 'human:dev');
});
