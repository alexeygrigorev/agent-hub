import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
async function fixture(fn: (service: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-claim-idem-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    service.init(); const sql = hub.sql;
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: { total: 10 }, gate: { launch_allowed: true, at: iso() } }), 0, JSON.stringify({ engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', machine, '1.0', iso(), now, iso(), now, 'ok', null, JSON.stringify(Object.fromEntries(['codex', 'claude'].map(provider => [provider, { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } }]))));
    }
    await fn(service, sql);
  });
}
function registry() {
  return { accounts: [
    { label: 'shared-codex', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true },
  ], routes: [
    { machine_id: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null },
  ] };
}
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'private test goal', idempotency_key: crypto.randomUUID(), ...extra });
async function configure(s: FleetService) {
  expect((await s.handle(request('/accounts', registry(), 'PUT'), admin)).status).toBe(200);
}
const baseClaim = { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true };

describe('intake 488b860e: idempotent same-host re-claim', () => {
  it('replays a same-key retry on an effect-free starting launch exactly once', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const path = '/launches/' + launch.id + '/claim';
    const key = crypto.randomUUID().replace(/-/g, '');
    const first = await s.handle(request(path, { ...baseClaim, claim_key: key }), host('hetzner'));
    expect(first.status).toBe(200);
    expect(await first.json()).toMatchObject({ id: launch.id, state: 'starting' });
    expect(sql.exec('SELECT state, claim_key, native_id, launcher_task_id FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'starting', claim_key: key, native_id: null, launcher_task_id: null });
    // Late-landing original claim plus retry: the retry replays, no second claim.
    const replay = await s.handle(request(path, { ...baseClaim, claim_key: key }), host('hetzner'));
    expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({ id: launch.id, state: 'starting', replay: true });
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches WHERE id = ?', launch.id).one().n).toBe(1);
    expect(sql.exec('SELECT state, native_id, launcher_task_id FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'starting', native_id: null, launcher_task_id: null });
  }));
  it('conflicts on a different key or a missing key once claimed', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const path = '/launches/' + launch.id + '/claim';
    const key = crypto.randomUUID().replace(/-/g, '');
    expect((await s.handle(request(path, { ...baseClaim, claim_key: key }), host('hetzner'))).status).toBe(200);
    // Sibling attempt with another key: conflict, never a second claim.
    expect((await s.handle(request(path, { ...baseClaim, claim_key: crypto.randomUUID().replace(/-/g, '') }), host('hetzner'))).status).toBe(409);
    // Keyless retry on a claimed launch: conflict as before.
    expect((await s.handle(request(path, baseClaim), host('hetzner'))).status).toBe(409);
    expect(sql.exec('SELECT state, claim_key FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'starting', claim_key: key });
  }));
  it('rejects malformed keys and stays backward compatible with keyless claims', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const path = '/launches/' + launch.id + '/claim';
    for (const bad of ['x'.repeat(129), 'has space', '', 42, 'semi;colon']) {
      expect((await s.handle(request(path, { ...baseClaim, claim_key: bad }), host('hetzner'))).status).toBe(400);
    }
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', launch.id).one().state).toBe('offered');
    // Old hosts send no key: claim works exactly as before.
    expect((await s.handle(request(path, baseClaim), host('hetzner'))).status).toBe(200);
    expect((await s.handle(request(path, baseClaim), host('hetzner'))).status).toBe(409);
  }));
  it('never replays once a native effect exists', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner', mode: 'headless' })), admin)).json();
    const path = '/launches/' + launch.id + '/claim';
    const key = crypto.randomUUID().replace(/-/g, '');
    expect((await s.handle(request(path, { ...baseClaim, claim_key: key }), host('hetzner'))).status).toBe(200);
    const resultPath = '/launches/' + launch.id + '/result';
    expect((await s.handle(request(resultPath, { state: 'running', native_id: 'native-1', launcher_task_id: 'hub-' + launch.id }), host('hetzner'))).status).toBe(200);
    // Same key after the launch left the effect-free window: conflict, never a second start.
    expect((await s.handle(request(path, { ...baseClaim, claim_key: key }), host('hetzner'))).status).toBe(409);
  }));
  it('keeps the claim key out of the public fleet snapshot', async () => fixture(async (s) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const key = crypto.randomUUID().replace(/-/g, '');
    expect((await s.handle(request('/launches/' + launch.id + '/claim', { ...baseClaim, claim_key: key }), host('hetzner'))).status).toBe(200);
    const snapshot: any = await (await s.handle(request('', undefined, 'GET'), admin)).json();
    const row = snapshot.launches.find((l: any) => l.id === launch.id);
    expect(row).toBeTruthy();
    expect('claim_key' in row).toBe(false);
  }));
  it('re-offers effect-free starting launches with their key; hides launches with native effects', async () => fixture(async (s, sql) => {
    expect((await s.handle(request('/accounts', { accounts: [
      { label: 'shared-codex', provider: 'codex', max_concurrency: 5, reserve_percent: 15, enabled: true },
    ], routes: [
      { machine_id: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null },
    ] }, 'PUT'), admin)).status).toBe(200);
    const mk = async () => ((await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json()) as any).id;
    const idOld = await mk(), idNative = await mk(), idTask = await mk(), idNew = await mk();
    const keyOld = crypto.randomUUID().replace(/-/g, '');
    const keyNative = crypto.randomUUID().replace(/-/g, '');
    const keyTask = crypto.randomUUID().replace(/-/g, '');
    const keyNew = crypto.randomUUID().replace(/-/g, '');
    for (const [id, key] of [[idOld, keyOld], [idNative, keyNative], [idTask, keyTask], [idNew, keyNew]] as const) {
      expect((await s.handle(request('/launches/' + id + '/claim', { ...baseClaim, claim_key: key }), host('hetzner'))).status).toBe(200);
    }
    // Native effects land: a native binding on one, a launcher task on the other.
    expect((await s.handle(request('/launches/' + idNative + '/result', { state: 'running', native_id: 'native-1', launcher_task_id: 'hub-' + idNative }), host('hetzner'))).status).toBe(200);
    expect((await s.handle(request('/launches/' + idTask + '/result', { state: 'uncertain', launcher_task_id: 'hub-' + idTask }), host('hetzner'))).status).toBe(200);
    // The fixture clock is frozen, so pin distinct claim times explicitly.
    sql.exec("UPDATE fleet_launches SET updated_at = ? WHERE id = ?", new Date(now - 60000).toISOString(), idOld);
    sql.exec("UPDATE fleet_launches SET updated_at = ? WHERE id = ?", new Date(now).toISOString(), idNew);
    const offers: any = await (await s.handle(request('/offers', undefined, 'GET'), host('hetzner'))).json();
    const byId = Object.fromEntries(offers.offers.map((o: any) => [o.id, o]));
    // The effect-free starting launches are re-offered with their stored keys
    // so the owning host can replay the claim after an all-attempts timeout.
    for (const [id, key] of [[idOld, keyOld], [idNew, keyNew]] as const) {
      expect(byId[id]).toBeTruthy();
      expect(byId[id].state).toBe('starting');
      expect(byId[id].claim_key).toBe(key);
    }
    // Oldest waiting first in the base order; rotation (tested below)
    // guarantees no row starves regardless of age.
    expect(offers.offers.findIndex((o: any) => o.id === idOld)).toBeLessThan(offers.offers.findIndex((o: any) => o.id === idNew));
    // Launches with a native effect are never re-offered for a second start.
    expect(byId[idNative]).toBeUndefined();
    expect(byId[idTask]).toBeUndefined();
  }));
  it('rotates re-offered starting launches so no row starves', async () => fixture(async (s, sql) => {
    // Seed 105 effect-free starting launches directly: past the 100-row
    // fetch bound, so the test proves rows beyond it are still reached
    // (max_concurrency caps at 100, so this bypasses the launch gate).
    const stamp = new Date(now).toISOString();
    for (let i = 0; i < 105; i++) {
      sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, claim_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        crypto.randomUUID(), 'test-admin', 'human', null, '', 'seed-' + i,
        JSON.stringify({ workspace: 'demo', prompt: 'x', task_kind: 'goal' }),
        'hetzner', 'codex', 'shared-codex', null, 'headless', 'demo', 'x',
        'starting', crypto.randomUUID().replace(/-/g, ''), stamp, stamp);
    }
    // 105 rows exceed the 100-row fetch bound and the 30-row window: four
    // polls must cover every row with no poll over the window.
    const seen = new Set<string>();
    let firstOfFirst = '', firstOfSecond = '';
    for (let poll = 0; poll < 4; poll++) {
      const page: any = await (await s.handle(request('/offers', undefined, 'GET'), host('hetzner'))).json();
      expect(page.offers.length).toBeLessThanOrEqual(30);
      if (poll === 0) firstOfFirst = page.offers[0].id;
      if (poll === 1) firstOfSecond = page.offers[0].id;
      for (const o of page.offers) {
        expect(o.state).toBe('starting');
        expect(typeof o.claim_key).toBe('string');
        seen.add(o.id);
      }
    }
    // The window rotates instead of repeating the same head rows.
    expect(firstOfSecond).not.toBe(firstOfFirst);
    expect(seen.size).toBe(105);
  }));

  it('keeps retry rotation independent across alternating hosts and offered occupancy', async () => fixture(async (s, sql) => {
    const target = new Set<string>();
    for (const machine of ['hetzner', 'win35']) {
      const count = machine === 'hetzner' ? 65 : 1;
      for (let i = 0; i < count + 20; i++) {
        const id = crypto.randomUUID(), starting = i < count;
        const stamp = new Date(now - (machine === 'win35' ? 1000 : 0)).toISOString();
        sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, claim_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          id, 'test-admin', 'human', '', id, JSON.stringify({ workspace: 'demo', task_kind: 'goal' }), machine, 'codex', 'shared-codex', 'headless', 'demo', 'x', starting ? 'starting' : 'offered', starting ? crypto.randomUUID() : null, stamp, stamp);
        if (machine === 'hetzner' && starting) target.add(id);
      }
    }
    const seen = new Set<string>();
    for (let poll = 0; poll < 7; poll++) {
      for (const machine of ['hetzner', 'win35']) {
        const page: any = await (await s.handle(request('/offers', undefined, 'GET'), host(machine))).json();
        expect(page.offers.length).toBeLessThanOrEqual(30);
        if (machine === 'hetzner') for (const row of page.offers) if (row.state === 'starting') seen.add(row.id);
      }
    }
    expect(seen).toEqual(target);
  }));

  it('requires fresh admission and enabled accounts on same-key replay without releasing capacity', async () => fixture(async (s, sql) => {
    await configure(s);
    const launch: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const path = '/launches/' + launch.id + '/claim', claim_key = crypto.randomUUID();
    expect((await s.handle(request(path, { ...baseClaim, claim_key }), host('hetzner'))).status).toBe(200);
    expect((await s.handle(request(path, { ...baseClaim, observed_at: iso(-60000), claim_key }), host('hetzner'))).status).toBe(409);
    sql.exec("UPDATE fleet_accounts SET enabled = 0 WHERE label = 'shared-codex'");
    expect((await s.handle(request(path, { ...baseClaim, claim_key }), host('hetzner'))).status).toBe(409);
    expect(sql.exec('SELECT state, native_id, launcher_task_id FROM fleet_launches WHERE id = ?', launch.id).one()).toMatchObject({ state: 'starting', native_id: null, launcher_task_id: null });
  }));

});
