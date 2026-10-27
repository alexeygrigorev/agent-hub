import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import {
  evaluateTbmAdmission, tbmHardFloor, tbmSoftReserve, engineProvider, withPolicyTbmFloors, type TbmPoolRow,
} from '../src/quota_admission';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const source = { type: 'unknown', reference: null, timestamp: null };
const NOW = Date.parse('2026-10-22T12:00:00Z');
const DAY = 86400000;
let cookie: string;
let seq = 0;

const pool = (over: Partial<TbmPoolRow> = {}): TbmPoolRow => ({
  pool_id: 'claude:main', provider: 'claude', total_capacity_tokens: 1_000_000, remaining_tokens: 900_000,
  reset_at_ms: NOW + 7 * DAY, convexity_exponent: 1.5, kill_switch_active: 0, ...over,
});

describe('TBM floors (pure)', () => {
  it('R_hard = max(5% of total, 50k)', () => {
    expect(tbmHardFloor(1_000_000)).toBe(50_000);
    expect(tbmHardFloor(10_000_000)).toBe(500_000);
    expect(tbmHardFloor(100_000)).toBe(50_000);
  });
  it('R_soft decays convexly from R_soft_init to R_hard', () => {
    const total = 1_000_000;
    expect(tbmSoftReserve(total, NOW, NOW + 7 * DAY, 7 * DAY)).toBeCloseTo(200_000);
    expect(tbmSoftReserve(total, NOW, NOW, 7 * DAY)).toBeCloseTo(50_000);
    const mid = tbmSoftReserve(total, NOW, NOW + 3.5 * DAY, 7 * DAY);
    expect(mid).toBeCloseTo(50_000 + 150_000 * Math.pow(0.5, 1.5));
    expect(mid).toBeGreaterThan(50_000);
    expect(mid).toBeLessThan(200_000);
  });
  it('R_soft stays at its initial level when the cycle is unknown', () => {
    expect(tbmSoftReserve(1_000_000, NOW, NOW + DAY, null)).toBe(200_000);
  });
  it('maps engines to quota providers', () => {
    expect(engineProvider('claude')).toBe('claude');
    expect(engineProvider('zcodex')).toBe('zai');
    expect(engineProvider('opencode')).toBe('go');
    expect(engineProvider('nope')).toBeNull();
  });
});

describe('evaluateTbmAdmission (pure)', () => {
  const ev = (pools: TbmPoolRow[], provider: string | null = 'claude') =>
    evaluateTbmAdmission({ pools, provider, nowMs: NOW, cycleMs: 7 * DAY });

  it('admits with no pools, or above both floors', () => {
    expect(ev([]).ok).toBe(true);
    expect(ev([pool()]).ok).toBe(true);
  });
  it('rejects at or below the hard floor', () => {
    const r = ev([pool({ remaining_tokens: 50_000 })]);
    expect(r).toMatchObject({ ok: false, reason: 'hard_floor', pool_id: 'claude:main' });
  });
  it('rejects below the dynamic soft reserve, admits once it has decayed', () => {
    const early = pool({ remaining_tokens: 150_000, reset_at_ms: NOW + 7 * DAY });
    expect(ev([early]).reason).toBe('soft_reserve');
    const late = pool({ remaining_tokens: 150_000, reset_at_ms: NOW + 0.5 * DAY });
    expect(ev([late]).ok).toBe(true);
  });
  it('provider kill-switch blocks only that provider; fleet kill-switch blocks all', () => {
    const killed = pool({ kill_switch_active: 1, kill_switch_reason: 'ops' });
    expect(ev([killed])).toMatchObject({ ok: false, reason: 'kill_switch_active', detail: 'ops' });
    expect(ev([killed], 'codex').ok).toBe(true);
    const fleet = pool({ pool_id: '*', provider: '*', kill_switch_active: 1 });
    expect(ev([fleet], 'codex').reason).toBe('kill_switch_active');
    expect(ev([fleet], null).reason).toBe('kill_switch_active');
  });
  it('without a known provider only kill-switches apply', () => {
    expect(ev([pool({ remaining_tokens: 0 })], null).ok).toBe(true);
  });
  it('fails closed on malformed pool rows', () => {
    expect(ev([pool({ total_capacity_tokens: 0 })]).reason).toBe('malformed_pool');
    expect(ev([pool({ remaining_tokens: Number.NaN })]).reason).toBe('malformed_pool');
    expect(ev([pool({ kill_switch_active: 2 })]).reason).toBe('malformed_pool');
  });
});

async function api(path: string, payload?: any) {
  const res = await SELF.fetch('https://hub.test' + path, { method: payload ? 'POST' : 'GET',
    headers: { Cookie: cookie, 'x-csrf-token': 'csrf' }, ...(payload ? { body: JSON.stringify(payload) } : {}) });
  return { status: res.status, data: await res.json() as any };
}
async function intakeTask(extra: any = {}): Promise<string> {
  const r = await api('/api/intake/tasks', { key: 'tbm-task-' + ++seq, project: 'demo', title: 'Synthetic task',
    text: 'Synthetic request', source, acceptance_criteria: 'Done means the synthetic gate is exercised.', ...extra });
  expect(r.status, JSON.stringify(r.data)).toBe(200);
  return r.data.task.id;
}
const start = (extra: any = {}) => api('/api/sessions', { key: 'tbm-start-' + ++seq, workspace: 'demo', mode: 'headless', brief: 'Synthetic goal', ...extra });
const setPool = (row: Partial<TbmPoolRow> & { pool_id: string; provider: string }) => inDO(stub(), (h: any) => {
  const p = { ...pool(), ...row };
  h.sql.exec(`INSERT OR REPLACE INTO tbm_quota_pools (pool_id, provider, total_capacity_tokens, remaining_tokens, headroom_percentage, reset_at_ms, kill_switch_active, kill_switch_reason, last_updated_ms)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`, p.pool_id, p.provider, p.total_capacity_tokens, p.remaining_tokens,
    (p.remaining_tokens / p.total_capacity_tokens) * 100, p.reset_at_ms, p.kill_switch_active, p.kill_switch_reason ?? null, Date.now());
});

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    for (const t of ['tasks', 'grants', 'rules', 'receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sql.exec('DELETE FROM tbm_quota_pools');
    h.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('fixture', 'admin@fake.local', 1, '2026-01-01', '2026-01-01')");
  });
});

describe('policy TBM floors (intake 4f6cf562: burn Codex/zai to 0)', () => {
  const ev = (pools: TbmPoolRow[], provider: string) =>
    evaluateTbmAdmission({ pools, provider, nowMs: NOW, cycleMs: 7 * DAY });
  const pct = (provider: string, remaining: number, account = 'shared-' + provider): TbmPoolRow => pool({
    pool_id: `${provider}:${account}:weekly`, provider, account, total_capacity_tokens: 0, remaining_tokens: 0,
    remaining_percent: remaining, capacity_known: 0, quota_unit: 'percent', hard_floor_percent: 5, soft_reserve_percent: 20 });
  it.each([['codex', 'codex'], ['codex-orin', 'codex'], ['zai', 'zai']])('%s pools admit while remaining > 0 and block at 0', (provider, target) => {
    for (const remaining of [11, 3, 0.5]) expect(ev([pct(provider, remaining)], target)).toMatchObject({ ok: true });
    expect(ev([pct(provider, 0)], target)).toMatchObject({ ok: false, reason: 'hard_floor', r_hard: 0 });
    const tokens = pool({ pool_id: provider + ':t', provider, total_capacity_tokens: 1_000_000, remaining_tokens: 1_000 });
    expect(ev([tokens], target).ok).toBe(true);
    expect(ev([{ ...tokens, remaining_tokens: 0 }], target).reason).toBe('hard_floor');
  });
  it('keeps stored floors for providers without an override and ignores invalid overrides', () => {
    expect(ev([pct('claude', 3)], 'claude')).toMatchObject({ ok: false, reason: 'hard_floor', r_hard: 5 });
    expect(ev([pct('claude', 15)], 'claude').reason).toBe('soft_reserve');
    expect(withPolicyTbmFloors(pct('codex', 3), { codex: { hard: -1, soft: 'x' } })).toMatchObject({ hard_floor_percent: 5, soft_reserve_percent: 20 });
    expect(withPolicyTbmFloors(pct('codex', 3), { codex: { hard: 0, soft: 0 } })).toMatchObject({ hard_floor_percent: 0, soft_reserve_percent: 0 });
  });
  it('explicit zero token floors skip the absolute token minimums', () => {
    expect(tbmHardFloor(1_000_000, 0)).toBe(0);
    expect(tbmSoftReserve(1_000_000, NOW, NOW + 7 * DAY, 7 * DAY, 1.5, 0, 0)).toBe(0);
  });
});

describe('TBM admission on POST /api/sessions', () => {
  it('rejects headless starts without intake_task_id (fail closed)', async () => {
    const r = await start();
    expect(r.status).toBe(400);
    expect(r.data.error).toBe('intake_task_id_required');
  });
  it('rejects unknown and closed intake tasks', async () => {
    expect((await start({ intake_task_id: 'missing-task' })).data.error).toBe('unknown_intake_task');
    const id = await intakeTask();
    const closed = await api('/api/intake/tasks/' + id, { key: 'tbm-close-' + ++seq, expected_version: 1, status: 'cancelled', reason: 'dropped by Alexey: synthetic admission fixture' });
    expect(closed.status, JSON.stringify(closed.data)).toBe(200);
    const r = await start({ intake_task_id: id });
    expect(r.status).toBe(409);
    expect(r.data.error).toBe('intake_task_closed');
  });
  it('does not require intake for interactive sessions, even under a fleet kill-switch', async () => {
    await setPool({ pool_id: '*', provider: '*', kill_switch_active: 1 });
    const r = await start({ mode: 'interactive', brief: undefined });
    expect(r.data.error).not.toBe('intake_task_id_required');
    expect(r.data.error).not.toBe('tbm_admission_denied');
  });
  it('admits a headless start with an open intake task and no pools', async () => {
    const r = await start({ intake_task_id: await intakeTask() });
    expect(r.data.error).toBeUndefined();
  });
  it('fleet kill-switch denies autonomous starts with 423', async () => {
    await setPool({ pool_id: '*', provider: '*', kill_switch_active: 1, kill_switch_reason: 'drill' });
    const r = await start({ intake_task_id: await intakeTask() });
    expect(r.status).toBe(423);
    expect(r.data).toMatchObject({ error: 'tbm_admission_denied', reason: 'kill_switch_active', pool_id: '*', detail: 'drill' });
  });
  it('provider kill-switch denies only that engine', async () => {
    await setPool({ pool_id: 'claude:main', provider: 'claude', kill_switch_active: 1 });
    const id = await intakeTask();
    expect((await start({ intake_task_id: id, engine: 'claude', engine_override_reason: 'Exercise the Claude pool admission gate.' })).status).toBe(423);
    expect((await start({ intake_task_id: id, engine: 'codex', engine_override_reason: 'Exercise independent Codex pool admission.' })).data.error).not.toBe('tbm_admission_denied');
  });
  it('hard floor denies an explicit-engine start', async () => {
    await setPool({ pool_id: 'claude:main', provider: 'claude', remaining_tokens: 40_000, reset_at_ms: Date.now() + DAY });
    const r = await start({ intake_task_id: await intakeTask(), engine: 'claude', engine_override_reason: 'Exercise the Claude pool admission gate.' });
    expect(r.status).toBe(409);
    expect(r.data).toMatchObject({ error: 'tbm_admission_denied', reason: 'hard_floor' });
  });
  it('soft reserve denies near full-cycle remaining below R_soft, admits when above', async () => {
    await setPool({ pool_id: 'claude:main', provider: 'claude', remaining_tokens: 150_000, reset_at_ms: Date.now() + 7 * DAY });
    const id = await intakeTask();
    const denied = await start({ intake_task_id: id, engine: 'claude', engine_override_reason: 'Exercise the Claude pool admission gate.' });
    expect(denied.data).toMatchObject({ error: 'tbm_admission_denied', reason: 'soft_reserve' });
    await setPool({ pool_id: 'claude:main', provider: 'claude', remaining_tokens: 600_000, reset_at_ms: Date.now() + 7 * DAY });
    const admitted = await start({ intake_task_id: id, engine: 'claude', engine_override_reason: 'Exercise the Claude pool admission gate.' });
    expect(admitted.data.error).not.toBe('tbm_admission_denied');
  });
  it('codex headless start at 3% remaining is not denied by hard floor or soft reserve (4f6cf562)', async () => {
    await setPool({ pool_id: 'codex:shared-codex:weekly', provider: 'codex', total_capacity_tokens: 1_000_000, remaining_tokens: 30_000, reset_at_ms: Date.now() + 7 * DAY });
    const r = await start({ intake_task_id: await intakeTask(), engine: 'codex', engine_override_reason: 'Exercise zero Codex floor admission.' });
    expect(r.data.error, JSON.stringify(r.data)).not.toBe('tbm_admission_denied');
    await setPool({ pool_id: 'codex:shared-codex:weekly', provider: 'codex', total_capacity_tokens: 1_000_000, remaining_tokens: 0, reset_at_ms: Date.now() + 7 * DAY });
    const empty = await start({ intake_task_id: await intakeTask(), engine: 'codex', engine_override_reason: 'Exercise zero Codex floor admission.' });
    expect(empty.data).toMatchObject({ error: 'tbm_admission_denied', reason: 'hard_floor' });
  });
});
