import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ProvisioningService } from '../src/provisioning';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';
// Intake 8cc9e31a: Hub owns engine version rollout (canary + smoke + rollback).
const admin = { caller: { isAdmin: true, kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { kind: 'host', machineId: id }, scopes: ['host'] });
const req = (path = '', method = 'GET', data?: any) => new Request('https://hub.test/api/engines' + path, { method, body: data ? JSON.stringify(data) : undefined });
async function fixture(fn: (s: ProvisioningService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('rollout-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state) => {
    const s = new ProvisioningService(hub.sql, cb => state.storage.transactionSync(cb), { SESSION_SECRET: 'synthetic-session-secret-32-characters' } as any, hub.fleetService, () => true);
    s.init(); await fn(s, hub.sql);
  });
}
const codex = (version: string) => ({ label: 'codex-main', provider: 'codex', version, files: { '.codex/auth.json': '{"t":"x"}' }, env: {}, args: [] });
describe('engine version rollout', () => {
  it('seeds desired pins and reports per-host versions', async () => fixture(async (s) => {
    const versions: any = await (await s.handle(req('/versions', 'GET'), admin)).json();
    expect(versions.desired.codex.version).toBe('0.166.0');
    expect(versions.desired.agy.version).toBe('1.4.10');
    expect(versions.reports).toEqual([]);
    const rep: any = await (await s.handle(req('/version-report', 'POST', { engines: { codex: { installed: '0.166.0', latest: '0.166.0', result: 'green' } } }), host('RMTHZ'))).json();
    expect(rep).toMatchObject({ status: 'recorded', engines: ['codex'] });
    const after: any = await (await s.handle(req('/versions', 'GET'), admin)).json();
    expect(after.reports).toHaveLength(1);
    expect(after.reports[0]).toMatchObject({ machine_id: 'RMTHZ', engine: 'codex', installed: '0.166.0', result: 'green' });
    // Junk is sanitized, never stored raw.
    await s.handle(req('/version-report', 'POST', { engines: { codex: { installed: '../../etc/passwd', latest: 'x', result: 'green; rm -rf' } } }), host('RMTHZ'));
    const clean: any = await (await s.handle(req('/versions', 'GET'), admin)).json();
    expect(clean.reports[0]).toMatchObject({ installed: null, result: null });
  }));
  it('bumps lagging bundles canary-first and rolls back on canary failure', async () => fixture(async (s) => {
    expect((await s.handle(req('/codex-main', 'PUT', codex('0.163.0')), admin)).status).toBe(200);
    const started: any = await (await s.handle(req('/update', 'POST', { engine: 'codex', canary: 'RMTHZ' }), admin)).json();
    expect(started.rollouts[0]).toMatchObject({ engine: 'codex', state: 'canary_pending', target_version: '0.166.0' });
    // A failed canary restores the previous versions and never rolls forward.
    await s.handle(req('/version-report', 'POST', { engines: { codex: { installed: '0.163.0', latest: '0.166.0', result: 'smoke_failed' } } }), host('RMTHZ'));
    const rolled: any = await (await s.handle(req('/versions', 'GET'), admin)).json();
    expect(rolled.rollouts.find((r: any) => r.state === 'rolled_back')).toBeTruthy();
    // Restart the rollout: canary green promotes to rolling, the rest sync next.
    const restarted: any = await (await s.handle(req('/update', 'POST', { engine: 'codex', canary: 'RMTHZ' }), admin)).json();
    expect(restarted.rollouts[0].state).toBe('canary_pending');
    await s.handle(req('/version-report', 'POST', { engines: { codex: { installed: '0.166.0', latest: '0.166.0', result: 'green' } } }), host('RMTHZ'));
    const versions: any = await (await s.handle(req('/versions', 'GET'), admin)).json();
    expect(versions.rollouts.find((r: any) => r.state === 'rolling')).toBeTruthy();
  }));
  it('rejects unknown engines and non-admin rollout starts', async () => fixture(async (s) => {
    expect((await s.handle(req('/update', 'POST', { engine: 'nope' }), admin)).status).toBe(400);
    expect((await s.handle(req('/update', 'POST', { engine: 'codex' }), host('RMTHZ'))).status).toBe(403);
    expect((await s.handle(req('/version-report', 'POST', { engines: {} }), admin)).status).toBe(403);
  }));
  it('daily check rate-limits and surfaces at most one actionable alert', async () => fixture(async (s) => {
    const first = await s.tickEngineRollout(Date.now(), async () => '9.9.9');
    expect(first.length).toBeLessThanOrEqual(1);
    if (first.length) expect(first[0].problem!.detail).toContain('hub engines update --engine');
    const second = await s.tickEngineRollout(Date.now(), async () => '9.9.9');
    expect(second).toEqual([]);
  }));
});
