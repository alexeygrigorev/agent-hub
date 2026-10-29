import { describe, expect, it, vi } from 'vitest';
import worker from '../src/worker';

describe('host installer public delivery', () => {
  for (const path of ['/cli/host-install.sh', '/cli/host-install.ps1']) {
    it(`${path} serves actual assets without authentication`, async () => {
      const assets = { fetch: vi.fn(async () => new Response('installer', { status: 200 })) };
      const env = { ASSETS: assets } as any;
      const response = await worker.fetch(new Request(`https://hub.test${path}`), env, {} as any);
      expect(response.status).toBe(200);
      expect(await response.text()).toBe('installer');
      expect(response.headers.get('X-Content-Type-Options')).toBe('nosniff');
      const denied = await worker.fetch(new Request(`https://hub.test${path}`, { method: 'POST' }), env, {} as any);
      expect(denied.status).toBe(405);
      expect(assets.fetch).toHaveBeenCalledTimes(1);
    });
  }
  it('keeps unlisted cli assets private', async () => {
    const assets = { fetch: vi.fn() };
    const response = await worker.fetch(new Request('https://hub.test/cli/host.json'), { ASSETS: assets } as any, {} as any);
    expect(response.status).toBe(404);
    expect(assets.fetch).not.toHaveBeenCalled();
  });
});
