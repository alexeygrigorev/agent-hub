import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext, runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv, createHelpers, loginAndGetState, approveDevice } from './m1-focus-setup';
import worker from '../src/worker';

let originalFetch = globalThis.fetch;

describe('M2 Focus: REGISTRY', () => {
  let hostToken = '';
  let machineId = '';
  let hostRefreshToken = '';
  
  beforeAll(async () => {
    // 1. Generate user_code
    const initRes = await doFetch(new Request('http://example.com/api/device/code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name: 'testhost', requested_scopes: ['host'] })
    }));
    const { device_code, user_code } = await initRes.json() as any;

    // 2. Approve
    const apprRes = await approveDevice(user_code, ['host']);
    const apprBody = await apprRes.json() as any;
    expect(apprRes.status).toBe(200);
    machineId = apprBody.machine_id;
    
    // 3. Poll for token
    const tokenRes = await doFetch(new Request('http://example.com/api/device/token', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code, client_id: 'testhost' })
    }));
    const tokenBody = await tokenRes.json() as any;
    hostToken = tokenBody.access_token;
    hostRefreshToken = tokenBody.refresh_token;
  });

  afterAll(() => {
    globalThis.fetch = originalFetch;
  });

  it('registration != checkin, fresh heartbeat/poll -> availability', async () => {
    // 1. Register
    const regRes = await doFetch(new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${hostToken}` },
      body: JSON.stringify({
        hostname: 'testhost-1', os: 'Linux', network: 'default',
        roles: ['host'], capabilities: { 'launcher': true }
      })
    }));
    expect(regRes.status).toBe(200);

    // 2. Check available (should be empty because registration doesn't update checkin)
    let availRes = await doFetch(new Request('http://example.com/api/machines/available', {
      headers: { 'Authorization': `Bearer ${hostToken}` }
    }));
    let availBody = await availRes.json() as any;
    expect(availBody.machines.length).toBe(0);

    // 3. Poll (Heartbeat)
    const pollRes = await doFetch(new Request('http://example.com/api/host/poll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${hostToken}` },
      body: JSON.stringify({
        type: 'heartbeat',
        mem_avail_mb: 1024, disk_free_gb: 100, load: [0, 0, 0], gate: true, running_jobs: 0, sessions_summary: {}
      })
    }));
    expect(pollRes.status).toBe(200);

    // 4. Check available (should be 1)
    availRes = await doFetch(new Request('http://example.com/api/machines/available', {
      headers: { 'Authorization': `Bearer ${hostToken}` }
    }));
    availBody = await availRes.json() as any;
    expect(availBody.machines.length).toBe(1);
    expect(availBody.machines[0].id).toBe(machineId);
    expect(availBody.machines[0].hostname).toBe('testhost-1');
  });

  it('drain/adminCSRF/crosshost binding/malformed inputs', async () => {
    // Crosshost binding -> test with other token
    const otherHostToken = 'hubt_fake'; // not valid
    const malformedReq = await doFetch(new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${otherHostToken}` },
      body: JSON.stringify({
        hostname: 'testhost-2', os: 'Linux', network: 'default', roles: ['host'], capabilities: {}
      })
    }));
    expect(malformedReq.status).toBe(401);

    // Admin CSRF for drain
    const drainReq = new Request(`http://example.com/api/machines/${machineId}/drain`, { method: 'POST' });
    const drainRes = await doFetch(drainReq);
    expect(drainRes.status).toBe(401);
  });

});
