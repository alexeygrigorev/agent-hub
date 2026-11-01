import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ProvisioningService } from '../src/provisioning';
import { engineEpoch } from '../src/engine-epoch';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';
// Intake 5814e109 A7: Hub pushes credential generations; hosts ack the generation they installed.
const admin = { caller: { isAdmin: true, kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { kind: 'host', machineId: id }, scopes: ['host'] });
const token = '.gemini/antigravity-cli/antigravity-oauth-token';
const agy = (secret: string) => ({ label: 'hub-agy', provider: 'antigravity', version: '1.4.10', files: { [token]: secret }, env: {}, args: [] });
const req = (path = '', method = 'GET', data?: any) => new Request('https://hub.test/api/engines' + path, { method, body: data ? JSON.stringify(data) : undefined });
async function fixture(fn: (s: ProvisioningService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('a7-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state) => {
    const s = new ProvisioningService(hub.sql, cb => state.storage.transactionSync(cb), { SESSION_SECRET: 'synthetic-session-secret-32-characters' } as any, hub.fleetService, () => true);
    s.init(); await fn(s, hub.sql);
  });
}
describe('credential push (A7)', () => {
  it('bumps the epoch on every credential change and never repeats a value', async () => fixture(async (s, sql) => {
    const seen = [engineEpoch(sql)];
    for (const secret of ['gen1', 'gen2', 'gen3']) {
      expect((await s.handle(req('/hub-agy', 'PUT', agy(secret)), admin)).status).toBe(200);
      seen.push(engineEpoch(sql));
    }
    expect(new Set(seen).size).toBe(seen.length);
    expect(Number(seen[3])).toBeGreaterThan(Number(seen[1]));
    const before = engineEpoch(sql);
    expect((await s.handle(req('/hub-agy', 'DELETE'), admin)).status).toBe(200);
    expect(engineEpoch(sql)).not.toBe(before);
  }));
  it('serves the epoch from memory: polling reads no rows', async () => fixture(async (s, sql) => {
    await s.handle(req('/hub-agy', 'PUT', agy('gen1')), admin);
    const value = engineEpoch(sql);
    const spy = sql.exec.bind(sql); let calls = 0; sql.exec = (...a: any[]) => { calls++; return spy(...a); };
    for (let i = 0; i < 50; i++) expect(engineEpoch(sql)).toBe(value);
    sql.exec = spy;
    expect(calls).toBe(0);
  }));
  it('records the acked generation per host and shows it against the current one', async () => fixture(async (s, sql) => {
    sql.exec("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('m1','DESKTOP','d','2026-01-01'), ('m2','LAPTOP','l','2026-01-01')");
    const first: any = await (await s.handle(req('/hub-agy', 'PUT', agy('gen1')), admin)).json();
    const second: any = await (await s.handle(req('/hub-agy', 'PUT', agy('gen2')), admin)).json();
    expect(second.credential_generation).toBe(first.credential_generation + 1);
    const status = async () => ((await (await s.handle(req(), admin)).json()) as any).sync_status;
    const cell = (st: any[], m: string) => st.find(h => h.machine_id === m).accounts[0];
    expect(cell(await status(), 'm1')).toMatchObject({ credential_generation: 2, acked_generation: null });
    const report = (id: string, gen: any) => s.handle(req('/sync-report', 'POST', { state: 'synced', items: [{ label: 'hub-agy', revision: second.revision, state: 'installed', credential_generation: gen }] }), host(id));
    expect((await report('m1', 2)).status).toBe(200);
    let st = await status();
    expect(cell(st, 'm1')).toMatchObject({ credential_generation: 2, acked_generation: 2 });
    expect(cell(st, 'm2').acked_generation).toBeNull();
    // Unchanged acks write nothing; junk generations are ignored.
    sql.exec("UPDATE engine_credential_acks SET acked_at = 1 WHERE machine_id='m1'");
    await report('m1', 2);
    expect(sql.exec("SELECT acked_at FROM engine_credential_acks WHERE machine_id='m1'").one().acked_at).toBe(1);
    for (const junk of [-1, 1.5, '2', null, true]) await report('m2', junk);
    expect(sql.exec("SELECT * FROM engine_credential_acks WHERE machine_id='m2'").toArray()).toHaveLength(0);
    // A rotation shows the host as behind until it acks the new generation.
    await s.handle(req('/hub-agy', 'PUT', agy('gen3')), admin);
    st = await status();
    expect(cell(st, 'm1')).toMatchObject({ credential_generation: 3, acked_generation: 2 });
  }));
});
