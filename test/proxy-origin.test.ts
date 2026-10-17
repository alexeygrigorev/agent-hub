import { describe, it, expect } from 'vitest';
import worker from '../src/worker';

describe('registered public proxy origin', () => {
  async function forward(host: string) {
    let observed!: Request;
    const env: any = {
      AUTH_CALLBACK_URL: 'https://hub.dtcdev.click/auth/callback',
      AUTH_CALLBACK_URLS: 'https://hub.dtcdev.click/auth/callback,https://alias.workers.dev/auth/callback',
      HUB: { idFromName: () => 'hub', get: () => ({ fetch: async (r: Request) => {
        observed = r;
        return Response.json({ body: await r.json(), origin: new URL(r.url).origin, csrfOrigin: r.headers.get('Origin') });
      } }) },
    };
    const response = await worker.fetch(new Request('https://alias.workers.dev/api/device/code', {
      method: 'POST', headers: { 'X-Forwarded-Host': host, Origin: 'https://hub.dtcdev.click', 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'human' }),
    }), env, {} as ExecutionContext);
    return { response: await response.json() as any, observed };
  }
  it('preserves the public origin and POST body across the CloudFront hop', async () => {
    const { response } = await forward('hub.dtcdev.click');
    expect(response.origin).toBe('https://hub.dtcdev.click');
    expect(response.csrfOrigin).toBe(response.origin);
    expect(response.body).toEqual({ kind: 'human' });
  });
  it('ignores unregistered and ambiguous forwarded hosts', async () => {
    for (const host of ['attacker.invalid', 'hub.dtcdev.click,attacker.invalid']) {
      expect((await forward(host)).response.origin).toBe('https://alias.workers.dev');
    }
  });
});
