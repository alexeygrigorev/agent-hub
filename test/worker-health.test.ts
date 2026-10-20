import { describe, it, expect, vi } from 'vitest';
import worker from '../src/worker';

describe('separate Worker liveness and authenticated Hub readiness', () => {
  const ctx = {} as ExecutionContext;

  it('serves only constant GET/HEAD liveness even when the Hub binding is unavailable', async () => {
    const get = vi.fn(() => { throw new Error('private platform detail'); });
    const env: any = { HUB: { idFromName: get } };
    for (const method of ['GET', 'HEAD', 'POST']) {
      const response = await worker.fetch(new Request('https://hub.test/livez', { method }), env, ctx);
      expect(response.status).toBe(method === 'POST' ? 405 : 200);
      expect(response.headers.get('Cache-Control')).toBe('no-store');
      if (method === 'GET') expect(await response.json()).toEqual({ status: 'ok', component: 'hub-worker' });
      else expect(await response.text()).toBe('');
    }
    expect(get).not.toHaveBeenCalled();
  });

  for (const path of ['/healthz', '/api/healthz']) {
    it(`preserves credential and method failures on ${path}`, async () => {
      for (const status of [200, 401, 403, 405, 503]) {
        const forward = vi.fn(async (request: Request) => {
          expect(new URL(request.url).pathname).toBe('/api/healthz');
          expect(request.headers.get('Authorization')).toBe('Bearer test-probe');
          return Response.json({ original: true }, { status, headers: { Allow: 'GET' } });
        });
        const env: any = { HUB: { idFromName: () => 'hub', get: () => ({ fetch: forward }) } };
        const response = await worker.fetch(new Request(`https://hub.test${path}`, {
          headers: { Authorization: 'Bearer test-probe' },
        }), env, ctx);
        expect(response.status).toBe(status);
        expect(response.headers.get('Cache-Control')).toBe('no-store');
        expect(response.headers.get('Allow')).toBe('GET');
        expect(await response.json()).toEqual({ original: true });
        expect(forward).toHaveBeenCalledTimes(1);
      }
    });

    it(`reports thrown DO failures as uncached sanitized 503 without replay on ${path}`, async () => {
      const forward = vi.fn(async () => { throw new Error('private platform detail'); });
      const env: any = { HUB: { idFromName: () => 'hub', get: () => ({ fetch: forward }) } };
      const response = await worker.fetch(new Request(`https://hub.test${path}`), env, ctx);
      expect(response.status).toBe(503);
      expect(response.headers.get('Retry-After')).toBe('5');
      expect(response.headers.get('Cache-Control')).toBe('no-store');
      expect(await response.json()).toEqual({ status: 'unavailable', component: 'hub-do' });
      expect(forward).toHaveBeenCalledTimes(1);
    });
  }
});
