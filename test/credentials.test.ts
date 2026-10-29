// Hub-owned engine credentials (intake 5814e109): central storage, central
// refresh with rotation, CAS host write-back, direct provider quota, central
// Claude login, new-host enrollment and rejected-account placement.
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { ProvisioningService } from '../src/provisioning';
import { codexQuotaFrom, claudeQuotaFrom, credentialFreshness, needsRefresh, jwtExpiryMs, CODEX_OAUTH, CLAUDE_OAUTH } from '../src/credentials';
import { freshCpu } from './cpu-fixture';

const DAY = 86400000;
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const hostAuth = (machineId: string) => ({ caller: { kind: 'host', machineId, actor: 'machine:' + machineId }, scopes: ['host'] });
const b64 = (v: unknown) => btoa(JSON.stringify(v)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const jwt = (expMs: number) => `${b64({ alg: 'none' })}.${b64({ exp: Math.floor(expMs / 1000) })}.sig`;
const codexAuth = (tag: string, lastRefresh: string, expMs: number) => JSON.stringify({ OPENAI_API_KEY: null, auth_mode: 'chatgpt', last_refresh: lastRefresh,
  tokens: { access_token: jwt(expMs), id_token: 'id-' + tag, refresh_token: 'refresh-' + tag, account_id: 'acct-1' } });
const SECRET_MARKERS = ['refresh-', 'id-', 'sk-'];

type Call = { url: string; body: any };
function fakeProvider(script: Record<string, (body: any, n: number) => { status: number; json: any }>) {
  const calls: Call[] = []; const counts: Record<string, number> = {};
  const fn = async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, body });
    counts[url] = (counts[url] ?? 0) + 1;
    const handler = script[url];
    if (!handler) return new Response('{}', { status: 404 });
    const r = handler(body, counts[url]);
    return new Response(JSON.stringify(r.json), { status: r.status });
  };
  return { fn, calls };
}

async function fixture(fn: (ctx: { sql: any; fleet: FleetService; provision: ProvisioningService; alerts: any[] }) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('credentials-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    fleet.init();
    const provision = new ProvisioningService(hub.sql, cb => state.storage.transactionSync(cb), bindings, fleet, () => true);
    provision.init();
    const alerts: any[] = [];
    provision.setAlertSink((label, problem) => alerts.push({ label, problem }));
    for (const id of ['rmthz', 'tabo']) {
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        id, id, id, iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0,
        JSON.stringify({ engines: ['codex', 'claude'], fleet_modes: ['interactive', 'headless'], workspaces: ['demo'], cpu_count: 8, fleet_route_rejections: [] }));
    }
    await fn({ sql: hub.sql, fleet, provision, alerts });
  });
}

const put = (provision: ProvisioningService, label: string, files: Record<string, string>, provider = 'codex') =>
  provision.handle(new Request('https://hub.test/api/engines/' + label, { method: 'PUT', body: JSON.stringify({ label, provider, version: '0.162.1', files, env: {}, args: [] }) }), admin);
const sync = async (provision: ProvisioningService, machine: string) =>
  (await (await provision.handle(new Request('https://hub.test/api/engines/sync'), hostAuth(machine))).json() as any);
const writeBack = async (provision: ProvisioningService, machine: string, label: string, base: number, files: Record<string, string>) =>
  provision.handle(new Request(`https://hub.test/api/engines/${label}/credential`, { method: 'POST', body: JSON.stringify({ base_generation: base, files }) }), hostAuth(machine));
const installed = async (provision: ProvisioningService, machine: string, label: string, revision: string) =>
  provision.handle(new Request('https://hub.test/api/engines/' + label, { method: 'POST', body: JSON.stringify({ revision }) }), hostAuth(machine));

describe('Hub-owned credentials: pure provider logic', () => {
  it('orders codex copies by last_refresh and decides refresh before the CLI would', () => {
    const fresh = { '.codex/auth.json': codexAuth('a', iso(-DAY), now + 9 * DAY) };
    const old = { '.codex/auth.json': codexAuth('b', iso(-4 * DAY), now + 6 * DAY) };
    expect(credentialFreshness('codex', fresh)! > credentialFreshness('codex', old)!).toBe(true);
    expect(needsRefresh('codex', fresh, now)).toBe(false);
    expect(needsRefresh('codex', old, now)).toBe(true); // 4 days old: Hub refreshes long before Codex's own 8-day threshold
    expect(jwtExpiryMs(jwt(now + DAY))).toBe(Math.floor((now + DAY) / 1000) * 1000);
    const claude = { '.claude/.credentials.json': JSON.stringify({ claudeAiOauth: { accessToken: 'a', refreshToken: 'r', expiresAt: now + 3600000 } }) };
    expect(needsRefresh('claude', claude, now)).toBe(true);
  });
  it('maps provider usage bodies to Hub quota routes', () => {
    const codex = codexQuotaFrom({ rate_limit: { limit_reached: false, primary_window: { used_percent: 24, limit_window_seconds: 604800, reset_at: Math.floor((now + DAY) / 1000) }, secondary_window: null } }, now);
    expect(codex.windows['7d']).toMatchObject({ present: true, percent_remaining: 76 });
    expect(codex.windows['5h'].present).toBe(false);
    const claude = claudeQuotaFrom({ five_hour: { utilization: 7, resets_at: iso(3600000) }, seven_day: { utilization: 100, resets_at: iso(DAY) } }, now);
    expect(claude.windows['5h'].percent_remaining).toBe(93);
    expect(claude.details.limit_reached).toBe(true);
  });
});

describe('Hub-owned credentials: store, refresh, write-back, enrollment', () => {
  it('imports a credential as generation 1 and every host (including a newly enrolled one) syncs it with no per-host login', async () => {
    await fixture(async ({ provision }) => {
      const res = await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('a', iso(-DAY), now + 9 * DAY) });
      expect(res.status).toBe(200);
      expect((await res.json() as any).credential_generation).toBe(1);
      // A brand-new machine enrolls and syncs: it receives the account and its current credential.
      const first = await sync(provision, 'tabo');
      const engine = first.engines.find((e: any) => e.label === 'codex-orin');
      expect(engine.credential_generation).toBe(1);
      expect(engine.credential_paths).toEqual(['.codex/auth.json']);
      expect(JSON.parse(engine.files['.codex/auth.json']).tokens.refresh_token).toBe('refresh-a');
      // Listing is metadata only.
      const list = JSON.stringify(await (await provision.handle(new Request('https://hub.test/api/engines'), admin)).json());
      for (const marker of SECRET_MARKERS) expect(list).not.toContain(marker);
      expect(list).toContain('"generation":1');
    });
  });

  it('refreshes centrally with rotation, so another host can never be left holding a used refresh token', async () => {
    await fixture(async ({ provision, sql }) => {
      await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('a', iso(-4 * DAY), now + 6 * DAY) });
      const provider = fakeProvider({
        [CODEX_OAUTH.token]: body => ({ status: body.refresh_token === 'refresh-a' ? 200 : 400,
          json: body.refresh_token === 'refresh-a' ? { access_token: jwt(now + 10 * DAY), id_token: 'id-b', refresh_token: 'refresh-b' } : { error: 'refresh_token_reused' } }),
        [CODEX_OAUTH.usage]: () => ({ status: 200, json: { rate_limit: { limit_reached: false, primary_window: { used_percent: 10, limit_window_seconds: 604800, reset_at: Math.floor((now + DAY) / 1000) } } } }),
      });
      provision.fetchFn = provider.fn as any;
      const [result] = await provision.maintainCredentials(now);
      expect(result).toMatchObject({ label: 'codex-orin', refresh: 'refreshed', quota: 'stored', generation: 2 });
      expect(provider.calls.filter(c => c.url === CODEX_OAUTH.token)).toHaveLength(1);
      // Both hosts now receive the rotated token; neither ever refreshed itself.
      for (const host of ['rmthz', 'tabo']) {
        const e = (await sync(provision, host)).engines[0];
        expect(e.credential_generation).toBe(2);
        expect(JSON.parse(e.files['.codex/auth.json']).tokens.refresh_token).toBe('refresh-b');
      }
      // Quota came straight from the provider with the stored credential.
      const snap: any = sql.exec("SELECT * FROM server_quota_snapshots WHERE account='codex-orin'").one();
      expect(JSON.parse(snap.quota_json).windows['7d'].percent_remaining).toBe(90);
      // A second pass within the window neither refreshes nor re-reads quota.
      const again = await provision.maintainCredentials(now + 60000);
      expect(again[0].refresh).toBe('skipped');
      expect(provider.calls.filter(c => c.url === CODEX_OAUTH.token)).toHaveLength(1);
    });
  });

  it('accepts a host-side rotation by CAS on the generation and converges every other host on it', async () => {
    await fixture(async ({ provision }) => {
      await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('a', iso(-DAY), now + 9 * DAY) });
      const rotated = codexAuth('b', iso(), now + 10 * DAY);
      const accepted = await (await writeBack(provision, 'rmthz', 'codex-orin', 1, { '.codex/auth.json': rotated })).json() as any;
      expect(accepted).toMatchObject({ accepted: true, generation: 2 });
      // Tabo still holds generation 1 and a divergent older copy: Hub refuses it and hands back generation 2.
      const stale = await (await writeBack(provision, 'tabo', 'codex-orin', 1, { '.codex/auth.json': codexAuth('z', iso(-2 * DAY), now + 8 * DAY) })).json() as any;
      expect(stale.accepted).toBe(false);
      expect(stale.generation).toBe(2);
      expect(JSON.parse(stale.files['.codex/auth.json']).tokens.refresh_token).toBe('refresh-b');
      // Migration case: a host without state (base -1) holding a provably newer copy wins.
      const newer = await (await writeBack(provision, 'tabo', 'codex-orin', -1, { '.codex/auth.json': codexAuth('c', iso(1000), now + 10 * DAY) })).json() as any;
      expect(newer).toMatchObject({ accepted: true, generation: 3 });
      // A torn/logged-out file or a provably older copy never becomes a generation, even with a matching base.
      for (const bad of ['{"auth_mode":"chatgpt"', JSON.stringify({ auth_mode: 'chatgpt', tokens: {} }), codexAuth('older', iso(-3 * DAY), now + 7 * DAY)]) {
        const r = await (await writeBack(provision, 'tabo', 'codex-orin', 3, { '.codex/auth.json': bad })).json() as any;
        expect(r).toMatchObject({ accepted: false, generation: 3 });
      }
      // Paths outside the provider's credential set are refused.
      expect((await writeBack(provision, 'tabo', 'codex-orin', 3, { '.codex/config.toml': 'x' })).status).toBe(400);
    });
  });

  it('marks a provider-rejected credential invalid, alerts once and stops offering the account anywhere', async () => {
    await fixture(async ({ provision, fleet, alerts }) => {
      const res = await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('a', iso(-5 * DAY), now - DAY) });
      const { revision } = await res.json() as any;
      for (const host of ['rmthz', 'tabo']) expect((await installed(provision, host, 'codex-orin', revision)).status).toBe(200);
      provision.fetchFn = fakeProvider({ [CODEX_OAUTH.token]: () => ({ status: 400, json: { error: 'invalid_grant' } }) }).fn as any;
      // First rejection is a strike, not a withdrawal (a host may hold the newer rotation).
      const [r1] = await provision.maintainCredentials(now);
      expect(r1).toMatchObject({ status: 'suspect', reason: 'refresh_token_rejected' });
      expect(alerts.filter(a => a.problem)).toHaveLength(0);
      const [r2] = await provision.maintainCredentials(now + 600000);
      expect(r2).toMatchObject({ status: 'invalid', reason: 'refresh_token_rejected' });
      await provision.maintainCredentials(now + 1200000);
      expect(alerts.filter(a => a.problem)).toHaveLength(1); // one alert per incident
      const plan = fleet.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', account: 'codex-orin', prompt: 'x' });
      expect(plan.selected).toBeNull();
      expect(plan.rejected!.every((x: any) => x.reason === 'credential_invalid')).toBe(true);
      const admission = await (await provision.handle(new Request('https://hub.test/api/engines/admission', { method: 'POST', body: JSON.stringify({ label: 'codex-orin' }) }), hostAuth('tabo'))).json() as any;
      expect(admission).toMatchObject({ launch_allowed: false, reason: 'credential_invalid' });
      // Re-authentication (a new import) publishes a new generation and clears the state.
      await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('n', iso(), now + 10 * DAY) });
      const status = (await (await provision.handle(new Request('https://hub.test/api/engines'), admin)).json() as any).credentials[0];
      expect(status).toMatchObject({ generation: 2, status: 'ok' });
    });
  });

  it('never offers a host an account it reported rejected until Hub publishes a newer generation', async () => {
    await fixture(async ({ provision, fleet }) => {
      const res = await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('a', iso(-DAY), now + 9 * DAY) });
      const { revision } = await res.json() as any;
      for (const host of ['rmthz', 'tabo']) await installed(provision, host, 'codex-orin', revision);
      const rejected = await (await provision.handle(new Request('https://hub.test/api/engines/codex-orin/rejection', { method: 'POST', body: JSON.stringify({ generation: 1, reason: 'credential_rejected' }) }), hostAuth('tabo'))).json() as any;
      expect(rejected.status).toBe('recorded');
      for (let i = 0; i < 5; i++) {
        const plan = fleet.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', account: 'codex-orin', prompt: 'x' });
        expect(plan.selected?.machine).toBe('rmthz');
        expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'tabo', reason: 'credential_rejected' }));
      }
      const pinned = fleet.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', account: 'codex-orin', machine: 'tabo', prompt: 'x' });
      expect(pinned.selected).toBeNull();
      // A rotation published by another host is a new generation: Tabo becomes eligible again.
      await writeBack(provision, 'rmthz', 'codex-orin', 1, { '.codex/auth.json': codexAuth('b', iso(), now + 10 * DAY) });
      const after = fleet.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', account: 'codex-orin', machine: 'tabo', prompt: 'x' });
      expect(after.selected?.machine).toBe('tabo');
    });
  });

  it('honours a fresh host-advertised credential_rejected route projection', async () => {
    await fixture(async ({ provision, fleet, sql }) => {
      const { revision } = await (await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('a', iso(-DAY), now + 9 * DAY) })).json() as any;
      for (const host of ['rmthz', 'tabo']) await installed(provision, host, 'codex-orin', revision);
      const caps = JSON.parse(sql.exec("SELECT capabilities FROM machines WHERE id='tabo'").one().capabilities as string);
      caps.fleet_route_rejections = [{ engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin', mode: 'headless', reason: 'credential_rejected', observed_at: iso() }];
      sql.exec("UPDATE machines SET capabilities=? WHERE id='tabo'", JSON.stringify(caps));
      const plan = fleet.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', account: 'codex-orin', machine: 'tabo', prompt: 'x' });
      expect(plan.selected).toBeNull();
      expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'tabo', reason: 'credential_rejected' }));
    });
  });

  it('creates a Hub-owned Claude account from a central login; hosts receive it with no login of their own', async () => {
    await fixture(async ({ provision }) => {
      const start = await (await provision.handle(new Request('https://hub.test/api/engines/credential-logins', { method: 'POST', body: JSON.stringify({ provider: 'claude', label: 'hub-claude', version: '2.1.304' }) }), admin)).json() as any;
      const url = new URL(start.url);
      expect(url.origin + url.pathname).toBe(CLAUDE_OAUTH.authorize);
      expect(url.searchParams.get('code_challenge_method')).toBe('S256');
      const state = url.searchParams.get('state');
      const provider = fakeProvider({
        [CLAUDE_OAUTH.token]: body => ({ status: body.grant_type === 'authorization_code' && body.code === 'the-code' && body.code_verifier ? 200 : 400,
          json: { access_token: 'acc-1', refresh_token: 'refresh-c1', expires_in: 28800, scope: CLAUDE_OAUTH.scopes.join(' ') } }),
        [CLAUDE_OAUTH.profile]: () => ({ status: 200, json: { organization: { organization_type: 'claude_max', rate_limit_tier: 'default_claude_max_20x' } } }),
      });
      provision.fetchFn = provider.fn as any;
      const done = await (await provision.handle(new Request(`https://hub.test/api/engines/credential-logins/${start.id}`, { method: 'POST', body: JSON.stringify({ code: `the-code#${state}` }) }), admin)).json() as any;
      expect(done).toMatchObject({ status: 'configured', label: 'hub-claude', credential_generation: 1 });
      const e = (await sync(provision, 'tabo')).engines.find((x: any) => x.label === 'hub-claude');
      const oauth = JSON.parse(e.files['.claude/.credentials.json']).claudeAiOauth;
      expect(oauth).toMatchObject({ refreshToken: 'refresh-c1', subscriptionType: 'max' });
      expect(oauth.expiresAt).toBeGreaterThan(now);
      // The one-time login id cannot be replayed.
      const replay = await provision.handle(new Request(`https://hub.test/api/engines/credential-logins/${start.id}`, { method: 'POST', body: JSON.stringify({ code: `the-code#${state}` }) }), admin);
      expect(replay.status).toBe(404);
    });
  });

  it('migrates a pre-existing bundle without judging its stale snapshot, then adopts the host\'s live rotation', async () => {
    await fixture(async ({ provision, sql, alerts }) => {
      // Bundle imported before Hub owned credentials: its refresh token was
      // already rotated on one host (the 29 Oct codex-orin incident).
      await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('old', iso(-11 * DAY), now - DAY) });
      sql.exec('DELETE FROM engine_credentials');
      const provider = fakeProvider({
        [CODEX_OAUTH.token]: () => ({ status: 400, json: { error: 'refresh_token_reused' } }),
        [CODEX_OAUTH.usage]: (_b, n) => ({ status: n === 1 ? 401 : 200, json: { rate_limit: { primary_window: { used_percent: 5, limit_window_seconds: 604800, reset_at: Math.floor((now + DAY) / 1000) } } } }),
      });
      provision.fetchFn = provider.fn as any;
      const [first] = await provision.maintainCredentials(now);
      expect(first).toMatchObject({ refresh: 'migration_grace', quota: 'migration_grace', generation: 1 });
      expect(provider.calls.some(c => c.url === CODEX_OAUTH.token)).toBe(false);
      expect(alerts.filter(a => a.problem)).toHaveLength(0);
      // RMTHZ (no local state yet) offers its newer live copy; Hub adopts it.
      const res = await (await writeBack(provision, 'rmthz', 'codex-orin', -1, { '.codex/auth.json': codexAuth('live', iso(-DAY), now + 9 * DAY) })).json() as any;
      expect(res).toMatchObject({ accepted: true, generation: 2 });
      const [second] = await provision.maintainCredentials(now + 300000);
      expect(second).toMatchObject({ refresh: 'skipped', quota: 'stored', generation: 2 });
      // Tabo's stale copy is refused and replaced with the live one.
      const tabo = await (await writeBack(provision, 'tabo', 'codex-orin', -1, { '.codex/auth.json': codexAuth('old', iso(-11 * DAY), now - DAY) })).json() as any;
      expect(tabo.accepted).toBe(false);
      expect(JSON.parse(tabo.files['.codex/auth.json']).tokens.refresh_token).toBe('refresh-live');
    });
  });

  it('refuses host credential reads/writes to non-host callers', async () => {
    await fixture(async ({ provision }) => {
      await put(provision, 'codex-orin', { '.codex/auth.json': codexAuth('a', iso(-DAY), now + 9 * DAY) });
      const res = await provision.handle(new Request('https://hub.test/api/engines/codex-orin/credential'), admin);
      expect(res.status).toBe(403);
    });
  });
});
