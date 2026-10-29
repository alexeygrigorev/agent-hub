import { it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { LivenessService } from '../src/liveness';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

it('watches hosts without configured checks, deduplicates incidents and recovers once', async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName(crypto.randomUUID()));
  await runInDurableObject(stub, (hub: Hub) => {
    let now = Date.parse('2026-10-29T10:00:00Z');
    const at = (age: number) => new Date(now - age * 1000).toISOString();
    const live = new LivenessService(hub.sql, undefined, () => now);
    live.init();
    hub.sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES(?,?,?,?,?)', 'tabo', 'Tabo', 'tabo', at(86400), at(900));
    expect(live.watchHosts()).toEqual([]);
    now += 1000;
    const opened = live.watchHosts();
    expect(opened).toHaveLength(1);
    expect(opened[0]).toMatchObject({ subject_kind: 'host_watch', subject: 'tabo', state: 'offline' });
    expect(live.watchHosts()).toEqual([]);
    // A DO/service restart and display-name change do not create an incident.
    hub.sql.exec("UPDATE machines SET name='renamed' WHERE id='tabo'");
    expect(new LivenessService(hub.sql, undefined, () => now).watchHosts()).toEqual([]);
    hub.sql.exec("UPDATE machines SET drained=1 WHERE id='tabo'");
    expect(live.watchHosts()).toEqual([]);
    expect(live.alerts().find(a => a.id === opened[0].id)?.resolved_at).toBeNull();
    hub.sql.exec("UPDATE machines SET drained=0,last_checkin_at=? WHERE id='tabo'", at(0));
    expect(live.watchHosts()[0]).toMatchObject({ subject_kind: 'host_recovery', state: 'RECOVERED' });
    expect(live.watchHosts()).toEqual([]);
    now += 901000;
    expect(live.watchHosts()).toHaveLength(1);
  });
});
