import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { describe, it, expect } from 'vitest';
import worker from '../src/worker';

describe('Hub Worker', () => {
  it('protects /healthz with authentication', async () => {
    const request = new Request('http://example.com/healthz');
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as any, ctx);
    
    await waitOnExecutionContext(ctx);
    expect(response.status).toBe(401);
  });

  it('protects /api/healthz with authentication', async () => {
    const request = new Request('http://example.com/api/healthz');
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as any, ctx);
    
    await waitOnExecutionContext(ctx);
    expect(response.status).toBe(401);
  });
});
