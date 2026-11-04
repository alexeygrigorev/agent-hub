import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { updateHeartbeat, effectiveHeartbeat } from '../src/registry';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

describe('host admission heartbeat persistence', () => {
  it('persists bounded swap telemetry and refreshes changed load/swap samples', async () => {
    const bindings = env as unknown as Env;
    const machine = 'admission-' + crypto.randomUUID();
    await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('admission-heartbeat-' + crypto.randomUUID())),
      (hub: Hub) => {
        const now = Date.now();
        hub.sql.exec('INSERT INTO machines(id,name,hostname,registered_at) VALUES(?,?,?,?)', machine, machine, machine, new Date(now).toISOString());
        const cpu = { cores: 4, busy_percent: 20, busy_cores: .8, busy_percent_window: 20,
          window_s: 10, psi_avg10: 0, psi_full_avg10: 0, memory_psi_full_avg10: 0,
          io_psi_full_avg10: 0, oom_kills_delta: 0, host_oom_kills_delta: 0,
          observed_at: new Date(now).toISOString(), interval_s: 10 };
        const heartbeat = (load: number, swap: number) => ({ type: 'heartbeat', load: [load, load, load],
          swap_used_percent: swap, mem_avail_mb: 4096, mem_total_mb: 8192, cpu_usage: cpu });

        updateHeartbeat(machine, heartbeat(1, 25), hub.sql, now);
        let stored = JSON.parse(String(hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id=?', machine).one().last_heartbeat));
        expect(stored).toMatchObject({ load: [1, 1, 1], swap_used_percent: 25 });

        updateHeartbeat(machine, heartbeat(30, 85), hub.sql, now + 1000);
        stored = JSON.parse(String(hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id=?', machine).one().last_heartbeat));
        expect(stored).toMatchObject({ load: [1, 1, 1], swap_used_percent: 25 });
        expect(JSON.parse(effectiveHeartbeat(hub.sql,machine,JSON.stringify(stored))!)).toMatchObject({load:[30,30,30],swap_used_percent:85});

        updateHeartbeat(machine, heartbeat(30, 82), hub.sql, now + 1500);
        stored = JSON.parse(String(hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id=?', machine).one().last_heartbeat));
        expect(stored.swap_used_percent).toBe(25);
        expect(JSON.parse(effectiveHeartbeat(hub.sql,machine,JSON.stringify(stored))!).swap_used_percent).toBe(82);

        updateHeartbeat(machine, heartbeat(40, 101), hub.sql, now + 2000);
        stored = JSON.parse(String(hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id=?', machine).one().last_heartbeat));
        expect(stored.swap_used_percent).toBe(25);
        expect(JSON.parse(effectiveHeartbeat(hub.sql,machine,JSON.stringify(stored))!).swap_used_percent).toBeUndefined();
        updateHeartbeat(machine,heartbeat(40,82),hub.sql,now+900000);
        stored=JSON.parse(String(hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id=?',machine).one().last_heartbeat));
        expect(stored).toMatchObject({load:[40,40,40],swap_used_percent:82});
      });
  });
});
