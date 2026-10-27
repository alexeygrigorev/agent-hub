/** Operator-mediated earned-credit evidence. Never writes quota, routing or rewards. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { loadProviderPacing } from './pacing';

const response = (data: unknown, status = 200) => Response.json(data, {
  status, headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
});
const id = (v: unknown): v is string => typeof v === 'string' && /^[A-Za-z0-9_:.-]{1,256}$/.test(v);
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
const fresh = (at: unknown, now: number, age = 180000): at is number => integer(at) && at <= now + 5000 && now - at <= age;
const keys = (v: any, allowed: string[]) => v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).every(k => allowed.includes(k));
const canonical = (v: any): any => Array.isArray(v) ? v.map(canonical) : v && typeof v === 'object'
  ? Object.fromEntries(Object.keys(v).sort().map(k => [k, canonical(v[k])])) : v;

function schema(sql: SqlStorage) {
  sql.exec(`CREATE TABLE IF NOT EXISTS tbm_reset_credit_evidence (
    event_id TEXT PRIMARY KEY, account TEXT NOT NULL, credit_id TEXT NOT NULL,
    provider_account_id TEXT NOT NULL, decision_json TEXT NOT NULL, receipt_json TEXT,
    work_json TEXT, created_at_ms INTEGER NOT NULL, UNIQUE(provider_account_id,credit_id))`);
}
function view(row: any) {
  return { event_id: row.event_id, decision_event_id: row.event_id + ':decision',
    receipt_event_id: row.receipt_json ? row.event_id + ':receipt' : null,
    work_event_id: row.work_json ? row.event_id + ':work' : null,
    decision: JSON.parse(row.decision_json), receipt: row.receipt_json ? JSON.parse(row.receipt_json) : null,
    work: row.work_json ? JSON.parse(row.work_json) : null,
    authority: 'operator_attestation_with_hub_state', ac2_accepted: false };
}

/** Mounted behind the existing TBM admin authentication and cookie CSRF checks. */
export async function handleResetCreditEvidence(request: Request, sql: SqlStorage, now: number, actor = 'unknown'): Promise<Response> {
  schema(sql);
  const url = new URL(request.url);
  if (request.method === 'GET') {
    const eventId = url.searchParams.get('event_id');
    const rows = eventId ? sql.exec('SELECT * FROM tbm_reset_credit_evidence WHERE event_id=?', eventId).toArray()
      : sql.exec('SELECT * FROM tbm_reset_credit_evidence ORDER BY created_at_ms DESC LIMIT 50').toArray();
    const sentinel: any = sql.exec("SELECT kill_switch_active FROM tbm_quota_pools WHERE pool_id='*'").toArray()[0];
    const globalHealthHold = sql.exec("SELECT engine FROM fleet_engine_health WHERE engine='codex' AND status='unavailable' AND (machine_id='' OR machine_id IS NULL) LIMIT 1").toArray().length > 0;
    return response({ events: rows.map((r: any) => {
      const d = JSON.parse(r.decision_json);
      const pools: any[] = sql.exec("SELECT pool_id,last_updated_ms,kill_switch_active FROM tbm_quota_pools WHERE provider='codex' AND account=?", r.account).toArray();
      const account: any = sql.exec('SELECT provider,enabled FROM fleet_accounts WHERE label=?', r.account).toArray()[0];
      const hasTick = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='tbm_tick_state'").toArray().length > 0;
      const tick: any = hasTick ? sql.exec('SELECT last_tick_ms FROM tbm_tick_state WHERE id=1').toArray()[0] : null;
      const poolIds = new Set(pools.map(p => p.pool_id));
      const tranches: any[] = sql.exec("SELECT tranche_id,pool_id,expires_at_ms FROM tbm_banked_tranches WHERE provider='codex' AND status='active' AND expires_at_ms>? ORDER BY expires_at_ms,tranche_id", now).toArray()
        .filter((t: any) => poolIds.has(t.pool_id));
      return { ...view(r), consumption_ready: !r.receipt_json && !sentinel?.kill_switch_active && !globalHealthHold &&
        account?.enabled === 1 && account.provider === 'codex' && fresh(r.created_at_ms, now, 300000) &&
        fresh(tick?.last_tick_ms, now, 300000) && !!loadProviderPacing(sql, now).get('codex:' + r.account) &&
        tranches[0]?.tranche_id === d.tranche_id && tranches[0]?.expires_at_ms === d.expires_at_ms &&
        tranches.filter(t => t.expires_at_ms === d.expires_at_ms).length === 1 &&
        d.expires_at_ms > now && pools.length > 0 && pools.every(p => fresh(p.last_updated_ms, now) && !p.kill_switch_active) };
    }) });
  }
  if (request.method !== 'POST') return response({ error: 'method_not_allowed' }, 405);
  const reader = request.body?.getReader();
  if (!reader) return response({ error: 'invalid_body' }, 400);
  const chunks: Uint8Array[] = []; let size = 0;
  for (;;) {
    const part = await reader.read(); if (part.done) break;
    size += part.value.length;
    if (size > 16384) { await reader.cancel(); return response({ error: 'body_too_large' }, 413); }
    chunks.push(part.value);
  }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const c of chunks) { bytes.set(c, offset); offset += c.length; }
  let body: any;
  try { body = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes)); }
  catch { return response({ error: 'invalid_body' }, 400); }
  if (body?.action === 'prepare') {
    if (!keys(body, ['action', 'account', 'credit_id', 'provider_account_id', 'expires_at_ms', 'observed_at_ms', 'available_count', 'provider_used_percent']) ||
      !id(body.account) || !id(body.credit_id) || !id(body.provider_account_id) || !integer(body.expires_at_ms) ||
      !fresh(body.observed_at_ms, now, 60000) || !integer(body.available_count) || body.available_count < 1 ||
      typeof body.provider_used_percent !== 'number' || !Number.isFinite(body.provider_used_percent) || body.provider_used_percent < 85 || body.provider_used_percent > 100) {
      return response({ error: 'invalid_credit_observation' }, 400);
    }
    const prior: any = sql.exec('SELECT * FROM tbm_reset_credit_evidence WHERE provider_account_id=? AND credit_id=?', body.provider_account_id, body.credit_id).toArray()[0];
    if (prior) {
      const old = JSON.parse(prior.decision_json);
      return prior.account === body.account && old.expires_at_ms === body.expires_at_ms
        ? response(view(prior)) : response({ error: 'credit_identity_conflict' }, 409);
    }
    const pools: any[] = sql.exec("SELECT * FROM tbm_quota_pools WHERE provider='codex' AND account=? ORDER BY pool_id", body.account).toArray();
    const poolIds = new Set(pools.map(p => p.pool_id));
    const tranches: any[] = sql.exec("SELECT * FROM tbm_banked_tranches WHERE provider='codex' AND status='active' AND expires_at_ms>? ORDER BY expires_at_ms,tranche_id", now).toArray()
      .filter((t: any) => poolIds.has(t.pool_id));
    const tranche = tranches[0];
    const sentinel: any = sql.exec("SELECT kill_switch_active FROM tbm_quota_pools WHERE pool_id='*'").toArray()[0];
    const account: any = sql.exec('SELECT provider,enabled FROM fleet_accounts WHERE label=?', body.account).toArray()[0];
    const globalHealthHold = sql.exec("SELECT engine FROM fleet_engine_health WHERE engine='codex' AND status='unavailable' AND (machine_id='' OR machine_id IS NULL) LIMIT 1").toArray().length > 0;
    const hasTick = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='tbm_tick_state'").toArray().length > 0;
    const tick: any = hasTick ? sql.exec('SELECT last_tick_ms FROM tbm_tick_state WHERE id=1').toArray()[0] : null;
    const factors = loadProviderPacing(sql, now).get('codex:' + body.account);
    if (sentinel?.kill_switch_active || pools.some(p => p.kill_switch_active) || !account?.enabled || account.provider !== 'codex' || globalHealthHold ||
      !tranche || tranche.expires_at_ms !== body.expires_at_ms ||
      tranches.filter(t => t.expires_at_ms === body.expires_at_ms).length !== 1 ||
      body.expires_at_ms - now > 86400000 || !fresh(tick?.last_tick_ms, now, 300000) ||
      !pools.length || pools.some(p => !fresh(p.last_updated_ms, now)) || !factors) {
      return response({ error: 'fresh_urgent_hub_decision_required' }, 409);
    }
    const eventId = 'earned-reset:' + crypto.randomUUID();
    const decision = { captured_at_ms: now, controller_tick_ms: tick.last_tick_ms, operator_actor: actor,
      idempotency_key: crypto.randomUUID(),
      account: body.account, provider_account_id: body.provider_account_id, credit_id: body.credit_id,
      expires_at_ms: body.expires_at_ms, provider_observed_at_ms: body.observed_at_ms,
      provider_available_count: body.available_count, tranche_id: tranche.tranche_id,
      provider_used_percent: body.provider_used_percent,
      pool_id: tranche.pool_id, routing_factors: factors,
      reason: 'expiring_earned_reset_edf', reset_needed_basis: 'native_quota_at_or_below_reserve',
      routing_eligibility_unchanged: true, provider_identity_authority: 'operator_native_read',
      pools: pools.map(p => ({ pool_id: p.pool_id, remaining_percent: p.remaining_percent,
        last_updated_ms: p.last_updated_ms, reset_at_ms: p.reset_at_ms })),
      // Retain other grants for the independent no-starvation audit, never claim it from a factor.
      other_active_tranches: sql.exec("SELECT tranche_id,pool_id,provider,expires_at_ms FROM tbm_banked_tranches WHERE status='active' AND expires_at_ms>? ORDER BY expires_at_ms LIMIT 100", now).toArray() };
    sql.exec('INSERT INTO tbm_reset_credit_evidence VALUES(?,?,?,?,?,NULL,NULL,?)', eventId, body.account,
      body.credit_id, body.provider_account_id, JSON.stringify(decision), now);
    return response(view(sql.exec('SELECT * FROM tbm_reset_credit_evidence WHERE event_id=?', eventId).one()), 201);
  }
  if (!id(body?.event_id)) return response({ error: 'invalid_event_id' }, 400);
  const row: any = sql.exec('SELECT * FROM tbm_reset_credit_evidence WHERE event_id=?', body.event_id).toArray()[0];
  if (!row) return response({ error: 'not_found' }, 404);
  const decision = JSON.parse(row.decision_json);
  if (body.action === 'receipt') {
    if (!keys(body, ['action', 'event_id', 'receipt']) || !keys(body.receipt,
      ['account_id', 'credit_id', 'idempotency_key', 'outcome', 'before_at_ms', 'after_at_ms', 'before_count', 'after_count', 'selected_credit_state', 'sha256', 'after_windows', 'after_usage_allowed'])) {
      return response({ error: 'invalid_receipt' }, 400);
    }
    const r = body.receipt;
    const successful = ['reset', 'alreadyRedeemed'].includes(r.outcome);
    const validWindows = Array.isArray(r.after_windows) && r.after_windows.length <= 64 && r.after_windows.every((w: any) =>
      keys(w, ['bucket', 'window', 'used_percent', 'reset_at_ms', 'window_minutes']) && id(w.bucket) &&
      ['primary', 'secondary'].includes(w.window) && typeof w.used_percent === 'number' && Number.isFinite(w.used_percent) && w.used_percent >= 0 && w.used_percent <= 100 &&
      (w.reset_at_ms === null || integer(w.reset_at_ms)) && (w.window_minutes === null || (integer(w.window_minutes) && w.window_minutes > 0)));
    if (!validWindows || ![true, false, null].includes(r.after_usage_allowed) ||
      (!successful && (r.after_windows.length !== 0 || r.after_usage_allowed !== null)) ||
      r.account_id !== row.provider_account_id || r.credit_id !== row.credit_id || r.idempotency_key !== decision.idempotency_key ||
      !['reset', 'alreadyRedeemed', 'nothingToReset', 'noCredit', 'unknown'].includes(r.outcome) ||
      !integer(r.before_at_ms) || r.before_at_ms < row.created_at_ms - 1000 || r.before_at_ms > now + 5000 ||
      !integer(r.before_count) || (successful ? (!integer(r.after_at_ms) || !integer(r.after_count) ||
        r.after_at_ms < r.before_at_ms || r.after_at_ms > now + 5000 || r.after_at_ms >= decision.expires_at_ms)
        : (r.after_at_ms !== null || r.after_count !== null || r.selected_credit_state !== 'unknown')) ||
      !['available', 'redeemed', 'absent', 'unknown'].includes(r.selected_credit_state) ||
      typeof r.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(r.sha256)) return response({ error: 'invalid_receipt' }, 400);
    if (row.receipt_json) {
      const previous = JSON.parse(row.receipt_json);
      return Object.keys(r).every(k => JSON.stringify(canonical(previous[k])) === JSON.stringify(canonical(r[k])))
        ? response(view(row)) : response({ error: 'receipt_conflict' }, 409);
    }
    const encoded = JSON.stringify({ ...r, hash_format: 'canonical_receipt_json_sha256', hub_recorded_at_ms: now, operator_actor: actor });
    sql.exec('UPDATE tbm_reset_credit_evidence SET receipt_json=? WHERE event_id=?', encoded, row.event_id);
  } else if (body.action === 'link-work') {
    if (!keys(body, ['action', 'event_id', 'routing_job_id']) || !id(body.routing_job_id) || !row.receipt_json) return response({ error: 'receipt_required' }, 409);
    const receipt = JSON.parse(row.receipt_json);
    const postReceipt = Math.max(receipt.after_at_ms ?? 0, receipt.hub_recorded_at_ms ?? 0);
    if (!['reset', 'alreadyRedeemed'].includes(receipt.outcome) || receipt.after_count >= receipt.before_count ||
      !['absent', 'redeemed'].includes(receipt.selected_credit_state)) return response({ error: 'coupon_consumption_unproven' }, 409);
    const job: any = sql.exec(`SELECT j.id,j.state,j.created_at,j.updated_at,j.selection_json,j.request_json,l.id AS launch_id,l.account,l.machine_id
      FROM routing_jobs j JOIN fleet_launches l ON l.id=j.launch_id WHERE j.id=?`, body.routing_job_id).toArray()[0];
    if (!job || job.account !== row.account || job.state !== 'done' || !job.selection_json ||
      !Number.isFinite(Date.parse(job.created_at)) || !Number.isFinite(Date.parse(job.updated_at)) ||
      Date.parse(job.created_at) < postReceipt || Date.parse(job.updated_at) >= decision.expires_at_ms) {
      return response({ error: 'completed_post_reset_routed_work_required' }, 409);
    }
    const intent = JSON.parse(job.request_json);
    if (intent.model || intent.preferred_runner) return response({ error: 'unforced_hub_routing_required' }, 409);
    const linked: any = sql.exec('SELECT id FROM session_control_sessions WHERE fleet_launch_id=?', job.launch_id).toArray()[0];
    const rewardId = 'session:' + (linked?.id ?? job.id);
    const reward: any = sql.exec('SELECT event_id,work_units,tokens_known,tokens_burned,timestamp_ms FROM tbm_reward_events WHERE event_id=?', rewardId).toArray()[0];
    const pool: any = sql.exec('SELECT pool_id,last_updated_ms,remaining_percent,reset_at_ms,cycle_ms FROM tbm_quota_pools WHERE pool_id=?', decision.pool_id).toArray()[0];
    if (!(reward?.work_units > 0) || reward.timestamp_ms < postReceipt || !pool || pool.last_updated_ms < postReceipt || !fresh(pool.last_updated_ms, now)) {
      return response({ error: 'fresh_ingested_quota_and_reward_required' }, 409);
    }
    const baselines = receipt.after_windows.filter((w: any) => w.bucket === 'codex' &&
      w.window_minutes * 60000 === pool.cycle_ms && w.reset_at_ms === pool.reset_at_ms);
    if (receipt.after_usage_allowed !== true || baselines.length !== 1 || typeof pool.remaining_percent !== 'number' ||
      pool.remaining_percent < 0 || pool.remaining_percent >= 100 - baselines[0].used_percent ||
      pool.last_updated_ms < Date.parse(job.updated_at) || pool.last_updated_ms >= decision.expires_at_ms) {
      return response({ error: 'same_window_post_work_drawdown_required' }, 409);
    }
    const work = { linked_at_ms: now, operator_actor: actor, routing_job_id: job.id, launch_id: job.launch_id,
      account: job.account, machine_id: job.machine_id, selection: JSON.parse(job.selection_json),
      reward, pool,
      quota_baseline: { source: 'operator_native_post_reset_read', observed_at_ms: receipt.after_at_ms, ...baselines[0] },
      quota_drawdown: { source: 'normal_host_ingestion', observed_at_ms: pool.last_updated_ms,
        remaining_percent: pool.remaining_percent, used_delta_percentage_points: 100 - baselines[0].used_percent - pool.remaining_percent },
      no_starvation_verified: false,
      other_routed_work: sql.exec(`SELECT j.id,j.state,l.account FROM routing_jobs j JOIN fleet_launches l ON l.id=j.launch_id
        WHERE l.account!=? AND j.created_at>=? ORDER BY j.created_at LIMIT 100`, row.account, new Date(row.created_at_ms).toISOString()).toArray() };
    if (row.work_json) return JSON.parse(row.work_json).routing_job_id === job.id ? response(view(row)) : response({ error: 'work_link_conflict' }, 409);
    sql.exec('UPDATE tbm_reset_credit_evidence SET work_json=? WHERE event_id=?', JSON.stringify(work), row.event_id);
  } else return response({ error: 'invalid_action' }, 400);
  return response(view(sql.exec('SELECT * FROM tbm_reset_credit_evidence WHERE event_id=?', row.event_id).one()));
}
