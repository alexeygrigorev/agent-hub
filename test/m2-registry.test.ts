import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { env, createExecutionContext, waitOnExecutionContext, runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv, createHelpers, approveDevice, loginAndGetState } from './m1-focus-setup';
import { Hub } from '../src/hub';
import { updateHeartbeat } from '../src/registry';
import { sha256 } from '../src/auth/crypto';

let originalFetch = globalThis.fetch;

async function seedHostCredential(sql: any, token: string, machineId: string, hostname: string, network: string = 'home') {
  const tokenHash = await sha256(token);
  const now = new Date().toISOString();
  const expires = new Date(Date.now() + 3600000).toISOString();
  sql.exec(`INSERT INTO machines (id, name, hostname, network, registered_at) VALUES (?, ?, ?, ?, ?)`,
    machineId, hostname, hostname, network, now);
  sql.exec(`INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    'cred_' + machineId, 'host', tokenHash, 'fam_' + machineId, hostname, machineId, JSON.stringify(['host']), 'admin-1', now, expires);
}

let stableHelpersPromise: Promise<any> | null = null;
function getStableHelpers() {
  if (!stableHelpersPromise) stableHelpersPromise = createHelpers();
  return stableHelpersPromise;
}

async function approveDeviceStable(user_code: string, scopes: string[]) {
  const { loginCookie, state, nonce } = await loginAndGetState();
  const helpers = await getStableHelpers();
  const now = Math.floor(Date.now() / 1000);
  const idToken = await helpers.signJwt({
    iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
    nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
  });
  
  const savedFetch = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL) => {
    if (input.toString().includes('/oauth2/token')) {
      return new Response(JSON.stringify({ id_token: idToken }), { status: 200 });
    }
    if (input.toString().includes('/.well-known/jwks.json')) {
      return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return savedFetch(input);
  };
  
  const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, {
    headers: { 'Cookie': loginCookie }, redirect: 'manual'
  }));
  const sessionCookie = cbRes.headers.get('Set-Cookie')!;
  globalThis.fetch = savedFetch;

  const csrfReq = new Request('http://example.com/auth/me', { headers: { 'Cookie': sessionCookie } });
  const meRes = await doFetch(csrfReq);
  const csrfToken = ((await meRes.json()) as any).csrf_token;

  const approveReq = new Request(`http://example.com/api/device/approve/${user_code}`, {
    method: 'POST',
    headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
    body: JSON.stringify({ granted_scopes: scopes })
  });
  return await doFetch(approveReq);
}

describe('M2 Adverse: Registry Runtime & Persistence Cadence', () => {
  let hostToken = '';
  let machineId = '';
  let stub: any;

  beforeAll(async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    stub = defaultEnv.HUB.get(id);

    // 1. Generate user_code for host pairing
    const initRes = await doFetch(new Request('http://example.com/api/device/code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name: 'adverse-host-1', hostname: 'host-adv-1', requested_scopes: ['host'] })
    }));
    const { device_code, user_code } = await initRes.json() as any;

    // 2. Approve device
    const apprRes = await approveDeviceStable(user_code, ['host']);
    const apprBody = await apprRes.json() as any;
    expect(apprRes.status).toBe(200);
    machineId = apprBody.machine_id;

    // 3. Obtain tokens
    const tokenRes = await doFetch(new Request('http://example.com/api/device/token', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code, client_id: 'adverse-host-1' })
    }));
    const tokenBody = await tokenRes.json() as any;
    hostToken = tokenBody.access_token;
  });

  afterAll(() => {
    globalThis.fetch = originalFetch;
  });

  // Test 1: Persistence Cadence Starvation under Stable 60s Heartbeats
  it('REPRO / CONTRACT: stable 60s heartbeats must persist periodically to SQLite', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const sql = instance.sql;
      const initialTime = 1700000000000;

      const stableHb = {
        type: 'heartbeat',
        mem_avail_mb: 4096,
        disk_free_gb: 120,
        load: [0.1, 0.1, 0.1],
        gate: { launch_allowed: true, reason: 'ok' },
        running_jobs: 0,
        sessions_summary: {}
      };

      const origNow = Date.now;
      try {
        Date.now = () => initialTime;
        updateHeartbeat(machineId, stableHb, sql);

        let writesCount = 1;

        // Simulate 16 minutes of stable heartbeats arriving every 60 seconds (t = 1min to t = 16min)
        for (let m = 1; m <= 16; m++) {
          const currentTime = initialTime + m * 60 * 1000;
          Date.now = () => currentTime;
          updateHeartbeat(machineId, stableHb, sql);

          const row = sql.exec('SELECT last_checkin_at FROM machines WHERE id = ?', machineId).toArray()[0] as any;
          const checkin = Date.parse(row.last_checkin_at);
          if (checkin === currentTime) {
            writesCount++;
          }
        }

        // Under SPEC 5-minute persistence cadence, SQL should be updated at t=0, ~5m, ~10m, ~15m (>= 3 writes).
        // Actual in registry.ts: writesCount is 1 because machineLastHb resets ts=now every 60s, starving persistence!
        expect(writesCount).toBeGreaterThanOrEqual(3);
      } finally {
        Date.now = origNow;
      }
    });
  });

  // Test 2: Active Host False Unavailability past 900s boundary under stable 60s beats
  it('REPRO / CONTRACT: active host sending stable 60s beats must remain available past 900s boundary', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const sql = instance.sql;
      const initialTime = 1700000000000;

      const stableHb = {
        type: 'heartbeat',
        mem_avail_mb: 4096,
        disk_free_gb: 120,
        load: [0.1, 0.1, 0.1],
        gate: { launch_allowed: true, reason: 'ok' },
        running_jobs: 0,
        sessions_summary: {}
      };

      const origNow = Date.now;
      try {
        Date.now = () => initialTime;
        updateHeartbeat(machineId, stableHb, sql);

        // Simulate 16 minutes of beats (up to 960s > 900s)
        for (let m = 1; m <= 16; m++) {
          const currentTime = initialTime + m * 60 * 1000;
          Date.now = () => currentTime;
          updateHeartbeat(machineId, stableHb, sql);
        }

        // At t = 16 minutes (960s):
        const t16m = initialTime + 16 * 60 * 1000;
        Date.now = () => t16m;

        // Query available machines
        const availReq = new Request('http://example.com/api/machines/available', {
          headers: { 'Authorization': `Bearer ${hostToken}` }
        });
        const availRes = await instance.fetch(availReq);
        const availBody = await availRes.json() as any;

        // SPEC §9 & §13: Active host reporting every 60 seconds MUST BE available.
        // In registry.ts: Since SQL was starved, last_checkin_at in SQL is 960s old, so available drops the machine!
        const found = availBody.machines.find((m: any) => m.id === machineId);
        expect(found).toBeDefined();
      } finally {
        Date.now = origNow;
      }
    });
  });

  // Test 3: Whole-object comparison vs timestamp/fluctuating telemetry write amplification
  it('REPRO / CONTRACT: heartbeat with timestamp or minor fluctuating telemetry must not write SQL on every beat', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const sql = instance.sql;
      const initialTime = 1700100000000;

      const origNow = Date.now;
      try {
        let writesCount = 0;

        // Host sends heartbeat containing 'at' timestamp (per SPEC §6 & §7.2: {type: 'heartbeat', at, ...})
        for (let m = 0; m < 5; m++) {
          const currentTime = initialTime + m * 60 * 1000;
          Date.now = () => currentTime;
          const hbWithAt = {
            type: 'heartbeat',
            at: new Date(currentTime).toISOString(),
            mem_avail_mb: 4096,
            disk_free_gb: 120,
            load: [0.1, 0.1, 0.1],
            gate: { launch_allowed: true, reason: 'ok' },
            running_jobs: 0,
            sessions_summary: {}
          };
          updateHeartbeat(machineId, hbWithAt, sql);

          const row = sql.exec('SELECT last_checkin_at FROM machines WHERE id = ?', machineId).toArray()[0] as any;
          if (Date.parse(row.last_checkin_at) === currentTime) {
            writesCount++;
          }
        }

        // Over 5 beats (4 minutes), persistence cadence should NOT write on every single beat!
        // Because state hasn't meaningfully changed. Only 1 write should occur in < 5 minutes.
        // In registry.ts: JSON.stringify(last.hb) !== JSON.stringify(hbPayload) is true every beat, writing all 5 times!
        expect(writesCount).toBe(1);
      } finally {
        Date.now = origNow;
      }
    });
  });

  // Test 4: Heartbeat Payload Validation & Forbidden Field Sanitization
  it('REPRO / CONTRACT: heartbeat must validate fields and strip quota balances, raw tokens, and attacker fields', async () => {
    const maliciousHb = {
      type: 'heartbeat',
      mem_avail_mb: 2048,
      disk_free_gb: 50,
      load: [0.2, 0.2, 0.2],
      gate: { launch_allowed: true, reason: 'ok' },
      running_jobs: 0,
      sessions_summary: {},
      quota_balances: { claude_credits: 500, codex_credits: 1000 },
      raw_token: 'hubt_super_secret_raw_token_that_must_never_be_stored',
      transcript: 'confidential conversation data',
      attacker_payload: 'DROP TABLE machines;',
      launcher_resources: { active_mem_mb: 4500, observed_at: '2026-10-24T12:00:00Z', raw_token: 'withheld' }
    };

    const pollRes = await doFetch(new Request('http://example.com/api/host/poll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${hostToken}` },
      body: JSON.stringify(maliciousHb)
    }));
    expect(pollRes.status).toBe(200);

    await runInDurableObject(stub, async (instance: Hub) => {
      const row = instance.sql.exec('SELECT last_heartbeat FROM machines WHERE id = ?', machineId).toArray()[0] as any;
      expect(row.last_heartbeat).toBeDefined();
      const parsedHb = JSON.parse(row.last_heartbeat);

      // SPEC §7.2: "the hub never sees quota balances, only launch_allowed + reason."
      // Review requirement: "No quota balances, raw tokens, transcripts or arbitrary attacker fields persisted from heartbeat."
      expect(parsedHb.quota_balances).toBeUndefined();
      expect(parsedHb.raw_token).toBeUndefined();
      expect(parsedHb.transcript).toBeUndefined();
      expect(parsedHb.attacker_payload).toBeUndefined();
      expect(parsedHb.launcher_resources).toEqual({ active_mem_mb: 4500, observed_at: '2026-10-24T12:00:00Z' });
    });
  });

  // Test 5: Unbounded request stream BEFORE request.json()
  it('REPRO / CONTRACT: registry endpoints must bound request stream before JSON allocation', async () => {
    // 64KB JSON payload
    const largeCap: Record<string, string> = {};
    for (let i = 0; i < 2000; i++) {
      largeCap[`key_${i}`] = 'value_padding_for_size';
    }
    const largeBody = JSON.stringify({
      hostname: 'testhost-large',
      os: 'Linux',
      network: 'default',
      roles: ['host'],
      capabilities: largeCap
    });

    const res = await doFetch(new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${hostToken}`
      },
      body: largeBody
    }));

    // Bounded stream input before request.json must return 400 (malformed_json) like safeJson in hub.ts
    // In registry.ts: request.json() is called directly, allocating the entire body before checking capJson.length > 4096
    const body = await res.json() as any;
    expect(body.error).toBe('malformed_json');
  });

  // Test 6: machines.py compatible output shape and no fabricated SSH for home hosts
  it('REPRO / CONTRACT: GET /api/machines/available must return machines.py shape and not fabricate inbound SSH for home hosts', async () => {
    // Register machine as network='home'
    await doFetch(new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${hostToken}` },
      body: JSON.stringify({
        hostname: 'win35-desktop', os: 'Windows', network: 'home', roles: ['work', 'desktop'], capabilities: { launcher: 'v0.1' }
      })
    }));

    // Send checkin heartbeat so it is available
    await doFetch(new Request('http://example.com/api/host/poll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${hostToken}` },
      body: JSON.stringify({
        type: 'heartbeat', mem_avail_mb: 8192, disk_free_gb: 200, load: [0, 0, 0], gate: true, running_jobs: 0, sessions_summary: {}
      })
    }));

    const availRes = await doFetch(new Request('http://example.com/api/machines/available', {
      headers: { 'Authorization': `Bearer ${hostToken}` }
    }));
    const availBody = await availRes.json() as any;
    expect(availBody.machines.length).toBeGreaterThan(0);

    const homeMachine = availBody.machines.find((m: any) => m.id === machineId);
    expect(homeMachine).toBeDefined();

    // machines.py requires: id, name, hostname, ssh, roles, capabilities
    expect(homeMachine.name).toBeDefined();

    // For home hosts, inbound SSH must NOT be fabricated as `ssh: hostname`
    // Because home machines connect outbound only via WebSocket and cannot accept inbound SSH
    expect(homeMachine.ssh).not.toBe('win35-desktop');
  });

  // Test 7: WebSocket Connect without heartbeat must NOT immediately mark checkin
  it('REPRO / CONTRACT: WebSocket connect without heartbeat must not make host available', async () => {
    const cleanMachineId = 'm_clean_ws_test';
    const cleanToken = 'hubt_clean_ws_test_token_12345';

    await runInDurableObject(stub, async (instance: Hub) => {
      await seedHostCredential(instance.sql, cleanToken, cleanMachineId, 'host-clean-ws');
    });

    // Verify machine is registered but NOT available
    let availRes = await doFetch(new Request('http://example.com/api/machines/available', {
      headers: { 'Authorization': `Bearer ${hostToken}` }
    }));
    let availBody = await availRes.json() as any;
    expect(availBody.machines.find((m: any) => m.id === cleanMachineId)).toBeUndefined();

    // Connect via WebSocket without sending any heartbeat
    const connectRes = await doFetch(new Request('http://example.com/api/host/connect', {
      headers: {
        'Authorization': `Bearer ${cleanToken}`,
        'Upgrade': 'websocket'
      }
    }));
    expect(connectRes.status).toBe(101);

    // SPEC §7.1 and §13: "Registration is not checkin; heartbeat makes it available; connect without heartbeat/checkin behavior must follow SPEC semantics consistently."
    // Connecting alone without any heartbeat must NOT mark the machine as available!
    // In registry.ts line 133: sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', new Date().toISOString(), cred.machine_id);
    // This erroneously marks the machine as checked-in and available immediately!
    availRes = await doFetch(new Request('http://example.com/api/machines/available', {
      headers: { 'Authorization': `Bearer ${hostToken}` }
    }));
    availBody = await availRes.json() as any;
    const foundClean = availBody.machines.find((m: any) => m.id === cleanMachineId);
    expect(foundClean).toBeUndefined();
  });

  // Test 8: Re-registration after drain must preserve drain=1
  it('CONTRACT: re-registration after drain must preserve drain=1 and not make machine available', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('UPDATE machines SET drained = 1 WHERE id = ?', machineId);
    });

    // Re-register the drained machine
    const regRes = await doFetch(new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${hostToken}` },
      body: JSON.stringify({
        hostname: 'host-adv-1-re', os: 'Linux', network: 'default', roles: ['host'], capabilities: { launcher: 'v0.2' }
      })
    }));
    expect(regRes.status).toBe(200);

    await runInDurableObject(stub, async (instance: Hub) => {
      const row = instance.sql.exec('SELECT drained FROM machines WHERE id = ?', machineId).toArray()[0] as any;
      expect(row.drained).toBe(1);
    });

    const availRes = await doFetch(new Request('http://example.com/api/machines/available', {
      headers: { 'Authorization': `Bearer ${hostToken}` }
    }));
    const availBody = await availRes.json() as any;
    const found = availBody.machines.find((m: any) => m.id === machineId);
    expect(found).toBeUndefined(); // drained machines must never be available
  });

  // Test 9: WebSocket message with revoked credential is rejected with 1008
  it('CONTRACT: WebSocket message with revoked credential must close with 1008', async () => {
    const wsMachineId = 'm_ws_revoke_test';
    const wsToken = 'hubt_ws_revoke_test_token_12345';

    await runInDurableObject(stub, async (instance: Hub) => {
      await seedHostCredential(instance.sql, wsToken, wsMachineId, 'host-ws-revoke');
    });

    const connectRes = await doFetch(new Request('http://example.com/api/host/connect', {
      headers: { 'Authorization': `Bearer ${wsToken}`, 'Upgrade': 'websocket' }
    }));
    expect(connectRes.status).toBe(101);
    const ws = (connectRes as any).webSocket;
    expect(ws).toBeDefined();

    // Revoke credential in SQL
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('UPDATE credentials SET revoked_at = ? WHERE machine_id = ?', new Date().toISOString(), wsMachineId);
    });

    // When a message arrives on the WebSocket with a revoked credential, registry.ts checks SQL and closes with 1008
    await runInDurableObject(stub, async (instance: Hub) => {
      // Simulate incoming message
      let closedCode = 0;
      let closedReason = '';
      const fakeWs: any = {
        deserializeAttachment: () => ({ machineId: wsMachineId, credentialId: 'cred_' + wsMachineId }),
        close: (code: number, reason: string) => {
          closedCode = code;
          closedReason = reason;
        }
      };
      instance.webSocketMessage(fakeWs, JSON.stringify({ type: 'heartbeat' }));
      expect(closedCode).toBe(1008);
      expect(closedReason).toBe('Credential revoked or expired');
    });
  });

  // Test 10: WebSocket message oversized (> 4KB) is rejected with 1009
  it('CONTRACT: WebSocket message exceeding 4KB must close with 1009', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      let closedCode = 0;
      let closedReason = '';
      const fakeWs: any = {
        deserializeAttachment: () => ({ machineId: 'm_adv', credentialId: 'cred_m_adv' }),
        close: (code: number, reason: string) => {
          closedCode = code;
          closedReason = reason;
        }
      };
      // Provide valid credential
      instance.sql.exec("INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, scopes, created_by_sub, created_at, expires_at) VALUES ('cred_m_adv', 'host', 'hash_test', 'fam_test', 'subj', '[\"host\"]', 'admin-1', datetime('now'), datetime('now', '+1 hour'))");

      const oversized = 'x'.repeat(4097);
      instance.webSocketMessage(fakeWs, oversized);
      expect(closedCode).toBe(1009);
      expect(closedReason).toBe('Message too large');
    });
  });
});

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
    const apprRes = await approveDeviceStable(user_code, ['host']);
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

    // 5. Register with >4 KiB capabilities (matching multi-workspace/route hosts up to 16 KiB)
    const largeCaps = {
      launcher: true,
      workspaces: Array.from({ length: 80 }, (_, i) => `workspace-item-${i}-${'x'.repeat(30)}`),
    };
    expect(JSON.stringify(largeCaps).length).toBeGreaterThan(4096);
    expect(JSON.stringify(largeCaps).length).toBeLessThanOrEqual(16384);
    const largeRegRes = await doFetch(new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${hostToken}` },
      body: JSON.stringify({
        hostname: 'testhost-1', os: 'Linux', network: 'default',
        roles: ['host'], capabilities: largeCaps
      })
    }));
    expect(largeRegRes.status).toBe(200);
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
