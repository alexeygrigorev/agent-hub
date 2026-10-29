// Intake c206f556: a busy host reports a full 500-row census whose rows are
// mostly exited history. Exit settlement must stay bounded (one managed-session
// lookup per census, not per exited row) so the host PUT finishes well within
// its 10 s request timeout.
import { it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const now = Date.UTC(2026, 9, 29, 4, 0, 0);
const iso = (offset = 0) => new Date(now + offset).toISOString();
const host = { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner' }, scopes: ['host'] } as any;

it('settles a 500-row mostly-exited census against hundreds of managed sessions quickly', async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('census-perf-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const s = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    s.init(); const sql = hub.sql;
    sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES ('hetzner','hetzner','hetzner',?,?,0)", iso(), iso());
    // 400 retained managed sessions bound to natives on this host.
    for (let i = 0; i < 400; i++) {
      const launch = `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
      sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, launch, 'owner', 'agent', 's', 'k' + i, '{}', 'hetzner', 'codex', 'a', 'interactive', '/w', 'p', i < 50 ? 'running' : 'exited', 'native-' + i, iso(), iso());
      sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, `10000000-0000-4000-8000-${String(i).padStart(12, '0')}`, 'owner', 'r' + i, JSON.stringify({ prompt: 'x'.repeat(4000) }),
        launch, 'hetzner', 'codex', 'interactive', i < 50 ? 'running' : 'stopped',
        JSON.stringify({ native_id: 'native-' + i, tag: 'hub-' + launch, workspace: '/w', native_created_at_ms: now - 1000 - i }), iso(), iso());
    }
    const sessions = Array.from({ length: 500 }, (_, i) => ({
      native_id: 'native-' + i, tag: i < 400 ? `hub-00000000-0000-4000-8000-${String(i).padStart(12, '0')}` : 'other-' + i,
      engine: 'codex', phase: i < 50 ? 'running' : 'exited', confirmed_exit: i >= 50, agent_state: null,
      workspace: '/w', cwd: '/w', profile: null, account: null, last_activity_at: iso(-60000), created_at_ms: now - 1000 - i,
    }));
    const start = performance.now();
    const response = await s.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions }) }), host);
    const elapsed = performance.now() - start;
    expect(response.status).toBe(200);
    console.log(`census PUT 500 rows: ${elapsed.toFixed(0)} ms`);
    expect(elapsed).toBeLessThan(2000);
  });
}, 120000);
