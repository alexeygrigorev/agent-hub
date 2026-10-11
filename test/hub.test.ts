import { env, createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { describe, it, expect } from 'vitest';
import worker from '../src/worker';

describe('Hub Worker', () => {
  it('responds with ok to /healthz', async () => {
    const request = new Request('http://example.com/healthz');
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as any, ctx);
    
    await waitOnExecutionContext(ctx);
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ status: 'ok', component: 'worker' });
  });

  it('routes /api/healthz to Hub DO', async () => {
    const request = new Request('http://example.com/api/healthz');
    const ctx = createExecutionContext();
    const response = await worker.fetch(request, env as any, ctx);
    
    await waitOnExecutionContext(ctx);
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ status: 'ok', component: 'hub-do' });
  });
});
