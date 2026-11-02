import { describe, expect, it, vi } from 'vitest';
// @ts-ignore Vite raw import; no declaration for ?raw
import hostInstallPs1 from '../public/cli/host-install.ps1?raw';
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
  for (const [alias, target] of [['/host/install.ps1', '/cli/host-install.ps1'], ['/host/install.sh', '/cli/host-install.sh']]) {
    it(`${alias} aliases ${target}`, async () => {
      const assets = { fetch: vi.fn(async (r: Request) => new Response(new URL(r.url).pathname, { status: 200 })) };
      const response = await worker.fetch(new Request(`https://hub.test${alias}`), { ASSETS: assets } as any, {} as any);
      expect(response.status).toBe(200);
      expect(await response.text()).toBe(target);
    });
  }
  it('runs the uv installer in a child process, never through Invoke-Expression', () => {
    const script = (hostInstallPs1 as string).split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');
    expect(script).not.toMatch(/Invoke-Expression|\|\s*iex/i);
    expect(script).toMatch(/-File \$UvInstaller/);
  });
  it('keeps unlisted cli assets private', async () => {
    const assets = { fetch: vi.fn() };
    const response = await worker.fetch(new Request('https://hub.test/cli/host.json'), { ASSETS: assets } as any, {} as any);
    expect(response.status).toBe(404);
    expect(assets.fetch).not.toHaveBeenCalled();
  });
});
