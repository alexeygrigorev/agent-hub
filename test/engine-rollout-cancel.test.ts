import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ProvisioningService } from '../src/provisioning';
import { cancelEngineRollout } from '../src/engine-rollout-cancel';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';
const admin = { caller: { isAdmin: true, kind: 'human' }, scopes: ['admin'] };
const host = { caller: { kind: 'host', machineId: 'CANARY' }, scopes: ['host'] };
const request = (id: string, reason = 'repair unintended global bump') => new Request('https://hub.test/api/engines/rollouts/' + id + '/cancel', { method: 'POST', body: JSON.stringify({ reason }) });
const zai = (version: string, label = 'zai-central') => ({ label, provider: 'zai', version,
  files: { '.zcode/v2/credentials.json': 'synthetic-credential', '.zcodex/config.toml': 'check_for_update_on_startup = false\n' },
  env: { ZAI_API_KEY: 'synthetic-env' }, args: ['--model', 'glm-5.3-flash'] });
async function fixture(fn: (service: ProvisioningService, sql: any, transaction: <T>(fn: () => T) => T) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('cancel-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state) => {
    const tx = <T>(fn: () => T) => state.storage.transactionSync(fn);
    const service = new ProvisioningService(hub.sql, tx, { SESSION_SECRET: 'synthetic-session-secret-32-characters' } as any, hub.fleetService, () => true);
    service.init();
    await fn(service, hub.sql, tx);
  });
}
async function imported(s: ProvisioningService, label = 'zai-central') {
  expect((await s.handle(new Request('https://hub.test/api/engines/' + label, { method: 'PUT', body: JSON.stringify(zai('0.166.0', label)) }), admin)).status).toBe(200);
}
function history(sql: any, detail: any = { bumped: ['zai-central'] }, state = 'canary_pending') {
  const id = crypto.randomUUID();
  sql.exec('INSERT INTO engine_rollouts VALUES (?,?,?,?,?,?,?,?,?)', id, 'zcodex', '0.166.0', '{"zai-central":"0.155.1"}', 'CANARY', state, 1, 1, JSON.stringify(detail));
  return id;
}

describe('admin rollout cancellation', () => {
  it('restores a legacy bumped version with current configuration and unchanged credential generation', async () => fixture(async (s, sql) => {
    await imported(s);
    const id = history(sql);
    const before: any = sql.exec('SELECT * FROM engine_bundles WHERE label=?', 'zai-central').one();
    const credentials = sql.exec('SELECT * FROM engine_credentials').toArray();
    const accounts = sql.exec('SELECT * FROM fleet_accounts').toArray();
    const response = await s.handle(request(id), admin);
    expect(response.status).toBe(200);
    const result: any = await response.json();
    expect(result).toMatchObject({ rollout_id: id, state: 'rolled_back', restored: [{ label: 'zai-central', version: '0.155.1' }] });
    const after: any = sql.exec('SELECT * FROM engine_bundles WHERE label=?', 'zai-central').one();
    expect(after.version).toBe('0.155.1');
    expect(after.revision).not.toBe(before.revision);
    expect(sql.exec('SELECT * FROM engine_credentials').toArray()).toEqual(credentials);
    expect(sql.exec('SELECT * FROM fleet_accounts').toArray()).toEqual(accounts);
    const sync: any = await (await s.handle(new Request('https://hub.test/api/engines/sync'), host)).json();
    expect(sync.engines[0]).toMatchObject({ version: '0.155.1', files: zai('0.155.1').files, env: zai('0.155.1').env, args: zai('0.155.1').args });
    const replay = await s.handle(request(id, 'a later retry reason'), admin);
    expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({ state: 'rolled_back', replayed: true });
    expect(sql.exec('SELECT * FROM engine_bundles WHERE label=?', 'zai-central').one()).toEqual(after);
  }));
  it('denies hosts and workers before touching history or bundles', async () => fixture(async (s, sql) => {
    await imported(s);
    const id = history(sql), before = sql.exec('SELECT * FROM engine_bundles').toArray();
    for (const actor of [host, { caller: { kind: 'worker' }, scopes: ['worker'] }])
      expect((await s.handle(request(id), actor)).status).toBe(403);
    expect(sql.exec('SELECT * FROM engine_bundles').toArray()).toEqual(before);
    expect(sql.exec('SELECT state FROM engine_rollouts WHERE id=?', id).one().state).toBe('canary_pending');
  }));
  it('withdraws a staged candidate without rewriting the baseline', async () => fixture(async (s, sql) => {
    await imported(s);
    const id = history(sql, { protocol: 2, staged: ['zai-central'] });
    const before = sql.exec('SELECT * FROM engine_bundles').toArray();
    expect((await s.handle(request(id), admin)).status).toBe(200);
    expect(sql.exec('SELECT * FROM engine_bundles').toArray()).toEqual(before);
    expect(JSON.parse(sql.exec('SELECT detail FROM engine_rollouts WHERE id=?', id).one().detail)).toMatchObject({ cancel_mode: 'candidate_withdrawn', reason: 'operator_cancelled' });
  }));
  it('accepts a held legacy record but refuses already promoted/done rollouts', async () => fixture(async (s, sql) => {
    await imported(s);
    const held = history(sql, { bumped: ['zai-central'] }, 'held_legacy');
    expect((await s.handle(request(held), admin)).status).toBe(200);
    for (const state of ['rolling', 'done']) {
      const id = history(sql, {}, state);
      expect((await s.handle(request(id), admin)).status).toBe(409);
    }
  }));
  it('fails closed on drift, damaged ciphertext and malformed history', async () => fixture(async (s, sql) => {
    await imported(s);
    const id = history(sql);
    sql.exec("UPDATE engine_bundles SET version='0.200.0' WHERE label='zai-central'");
    const drifted = sql.exec('SELECT * FROM engine_bundles').toArray();
    expect((await s.handle(request(id), admin)).status).toBe(409);
    expect(sql.exec('SELECT * FROM engine_bundles').toArray()).toEqual(drifted);
    sql.exec("UPDATE engine_bundles SET version='0.166.0',ciphertext='damaged' WHERE label='zai-central'");
    expect((await s.handle(request(id), admin)).status).toBe(503);
    expect(sql.exec('SELECT state FROM engine_rollouts WHERE id=?', id).one().state).toBe('canary_pending');
    sql.exec("UPDATE engine_rollouts SET previous_json='[]' WHERE id=?", id);
    expect((await s.handle(request(id), admin)).status).toBe(409);
  }));
  it('rejects invalid input and does not require staged tables for a legacy rollback', async () => fixture(async (s, sql) => {
    await imported(s);
    const id = history(sql);
    // Model the deployed prototype schema: no candidate table is consulted.
    sql.exec('DROP TABLE IF EXISTS engine_rollout_candidates');
    expect((await s.handle(request(id), admin)).status).toBe(200);
    expect((await s.handle(request('not-a-uuid'), admin)).status).toBe(400);
    expect((await s.handle(request(crypto.randomUUID()), admin)).status).toBe(404);
    expect((await s.handle(request(id, 'bad\nreason'), admin)).status).toBe(400);
  }));
  it('prepares all ciphertexts before atomically comparing revisions', async () => fixture(async (_s, sql, tx) => {
    const id = history(sql);
    const bundle = zai('0.166.0');
    sql.exec('INSERT INTO engine_bundles VALUES (?,?,?,?,?)', bundle.label, bundle.provider, bundle.version, 'before', JSON.stringify(bundle));
    const result = await cancelEngineRollout({ sql, tx, now: () => 10,
      open: async row => JSON.parse(row.ciphertext), seal: async value => {
        sql.exec("UPDATE engine_bundles SET revision='concurrent-import' WHERE label='zai-central'");
        return JSON.stringify(value);
      } }, id, { reason: 'cancel incident' });
    expect(result).toMatchObject({ error: 'bundle_changed' });
    expect(sql.exec('SELECT revision,version FROM engine_bundles').one()).toEqual({ revision: 'concurrent-import', version: '0.166.0' });
    expect(sql.exec('SELECT state FROM engine_rollouts WHERE id=?', id).one().state).toBe('canary_pending');
  }));
});
