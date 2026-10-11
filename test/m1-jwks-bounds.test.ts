import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { JwksCache } from '../src/auth/dtc';
import { Env } from '../src/env';

describe('JwksCache bounds and limits', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const dummyEnv = { AUTH_JWKS_URL: 'http://fake.local/jwks' } as Env;

  it('enforces 5000ms timeout via AbortSignal', async () => {
    const cache = new JwksCache();
    let aborted = false;
    globalThis.fetch = vi.fn().mockImplementation(async (url, init) => {
      return new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => { 
          aborted = true; 
          reject(new Error('AbortError')); 
        });
      });
    });

    const promise = expect(cache.getKeys(dummyEnv)).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(5001);
    
    await promise;
    expect(aborted).toBe(true);
  });

  it('rejects endless streams and exact size boundaries', async () => {
    const cache = new JwksCache();
    let cancelCalled = false;
    
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array(256 * 1024));
          controller.enqueue(new Uint8Array(1));
        },
        cancel() {
          cancelCalled = true;
        }
      }), {
        headers: { 'content-type': 'application/json' }
      });
    });

    await expect(cache.getKeys(dummyEnv)).rejects.toThrow('JWKS stream exceeded 256KiB');
    expect(cancelCalled).toBe(true);
  });

  it('deduplicates inflight requests', async () => {
    const cache = new JwksCache();
    let fetchCalls = 0;
    let resolveFetch: any;
    
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      fetchCalls++;
      return new Promise((resolve) => {
        resolveFetch = resolve;
      });
    });

    const p1 = cache.getKeys(dummyEnv);
    const p2 = cache.getKeys(dummyEnv);
    expect(fetchCalls).toBe(1);

    resolveFetch(new Response(JSON.stringify({ keys: [{ kty: 'RSA', alg: 'RS256', use: 'sig', kid: '1', n: 'a', e: 'b' }] }), {
      headers: { 'content-type': 'application/json' }
    }));

    await Promise.all([p1, p2]);
  });

  it('prevents failure storm by measuring cooldown from completion', async () => {
    const cache = new JwksCache();
    let fetchCalls = 0;
    
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      fetchCalls++;
      await new Promise(r => setTimeout(r, 2000));
      return new Response('Not Found', { status: 404 });
    });

    const p1 = cache.getKeys(dummyEnv, true).catch(() => {});
    await vi.advanceTimersByTimeAsync(2000);
    await p1;
    
    expect(fetchCalls).toBe(1);

    const p2 = cache.getKeys(dummyEnv, true);
    const keys = await p2;
    expect(keys).toEqual([]);
    expect(fetchCalls).toBe(1);

    await vi.advanceTimersByTimeAsync(10000);
    
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      fetchCalls++;
      return new Response(JSON.stringify({ keys: [{ kty: 'RSA', alg: 'RS256', use: 'sig', kid: '1', n: 'a', e: 'b' }] }), {
        headers: { 'content-type': 'application/json' }
      });
    });

    const keys2 = await cache.getKeys(dummyEnv, true);
    expect(fetchCalls).toBe(2);
    expect(keys2.length).toBe(1);
  });

  it('rejects payload with more than 32 keys', async () => {
    const cache = new JwksCache();
    const keys = Array.from({ length: 33 }, (_, i) => ({ kty: 'RSA', alg: 'RS256', use: 'sig', kid: String(i), n: 'a', e: 'b' }));
    
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      return new Response(JSON.stringify({ keys }), {
        headers: { 'content-type': 'application/json' }
      });
    });

    await expect(cache.getKeys(dummyEnv)).rejects.toThrow('Invalid JWKS');
  });

  it('rejects malformed keys', async () => {
    const cache = new JwksCache();
    
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      return new Response(JSON.stringify({ keys: [{ kty: 'RSA' }] }), {
        headers: { 'content-type': 'application/json' }
      });
    });

    await expect(cache.getKeys(dummyEnv)).rejects.toThrow('Invalid JWKS');
  });

});
