import { MeteredDb, initializeSchema } from './db';
import {SqlReceiptMeter,SQL_PROBE_HEADER} from './sql-receipt';
import { actorFrom, bodyObject, boundedString, error, hasScope, HttpError, json } from './http';

export const BUDGET_SCHEMA = [
  'CREATE TABLE budget_days(day TEXT PRIMARY KEY,settled_pusd TEXT NOT NULL,reserved_pusd TEXT NOT NULL,units_json TEXT NOT NULL,costs_json TEXT NOT NULL,coverage_json TEXT NOT NULL,events INTEGER NOT NULL,hard_stopped INTEGER NOT NULL,updated_at INTEGER NOT NULL) WITHOUT ROWID',
  'CREATE TABLE budget_reservations(id TEXT PRIMARY KEY,day TEXT NOT NULL,kind TEXT NOT NULL,estimate_json TEXT NOT NULL,coverage_json TEXT NOT NULL,estimate_pusd TEXT NOT NULL,state TEXT NOT NULL,retry_at INTEGER,actual_json TEXT,actual_pusd TEXT,created_at INTEGER NOT NULL,settled_at INTEGER) WITHOUT ROWID',
  'CREATE INDEX budget_reservation_day ON budget_reservations(day,state,id)',
  'CREATE TABLE budget_overrides(day TEXT PRIMARY KEY,ceiling_pusd TEXT NOT NULL,actor_id TEXT NOT NULL,reason TEXT NOT NULL,created_at INTEGER NOT NULL) WITHOUT ROWID',
  'CREATE TABLE budget_override_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,day TEXT NOT NULL,ceiling_pusd TEXT NOT NULL,actor_id TEXT NOT NULL,reason TEXT NOT NULL,created_at INTEGER NOT NULL)',
  'CREATE TABLE budget_provider(day TEXT PRIMARY KEY,source TEXT NOT NULL,observed_at INTEGER NOT NULL,reported_through INTEGER NOT NULL,provider_total_pusd TEXT NOT NULL,hub_attributed_pusd TEXT NOT NULL,unattributed_pusd TEXT NOT NULL,coverage_json TEXT NOT NULL) WITHOUT ROWID',
  'CREATE TABLE budget_health(day TEXT NOT NULL,metric TEXT NOT NULL,reason TEXT NOT NULL,state TEXT NOT NULL,failures INTEGER NOT NULL,first_at INTEGER NOT NULL,last_at INTEGER NOT NULL,PRIMARY KEY(day,metric)) WITHOUT ROWID',
  'CREATE TABLE budget_alerts(id TEXT PRIMARY KEY,day TEXT NOT NULL,kind TEXT NOT NULL,payload_json TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL,created_at INTEGER NOT NULL,retry_at INTEGER NOT NULL,claim_token TEXT,claim_until INTEGER,delivered_at INTEGER) WITHOUT ROWID',
  'CREATE INDEX budget_alert_due ON budget_alerts(state,retry_at,created_at,id)',
];

export const METRICS = [
  'worker_requests', 'worker_cpu_ms', 'do_requests', 'do_ws_messages', 'do_gb_ms',
  'background_invocations', 'sqlite_rows_read', 'sqlite_rows_written', 'sqlite_byte_days',
  'kv_reads', 'kv_writes', 'kv_deletes', 'kv_lists', 'kv_byte_days',
] as const;
export type Metric = typeof METRICS[number];
export type Units = Record<Metric, number>;
export type CoverageValue = 'exact' | 'upper_bound' | 'unknown';
export type Coverage = Record<Metric, CoverageValue>;

const PICO = 1_000_000_000_000n;
const ALERT_PUSD = 30_000_000_000n;
const DEFAULT_CEILING_PUSD = 250_000_000_000n;
const RECOVERY_RESERVE_PUSD = 10_000_000_000n;
const MAX_OVERRIDE_PUSD = 1_000_000_000_000n;
const SQLITE_STORAGE_DENOMINATOR = 1_073_741_824n * 30n;
const SAFE_KINDS = new Set([
  'native_exit', 'capacity_reconcile', 'auth_revoke', 'alert_delivery', 'operator_stop', 'budget_recovery',
]);

type Bucket = 'worker' | 'durable_objects' | 'sqlite' | 'workers_kv';
const PRICES: Record<Metric, { bucket: Bucket | 'tracking'; numerator: bigint; denominator: bigint }> = {
  worker_requests: { bucket: 'worker', numerator: 300_000n, denominator: 1n },
  worker_cpu_ms: { bucket: 'worker', numerator: 20_000n, denominator: 1n },
  do_requests: { bucket: 'durable_objects', numerator: 150_000n, denominator: 1n },
  do_ws_messages: { bucket: 'durable_objects', numerator: 7_500n, denominator: 1n },
  do_gb_ms: { bucket: 'durable_objects', numerator: 12_500n, denominator: 1n },
  background_invocations: { bucket: 'tracking', numerator: 0n, denominator: 1n },
  sqlite_rows_read: { bucket: 'sqlite', numerator: 1_000n, denominator: 1n },
  sqlite_rows_written: { bucket: 'sqlite', numerator: 1_000_000n, denominator: 1n },
  sqlite_byte_days: { bucket: 'sqlite', numerator: 200_000_000_000n, denominator: SQLITE_STORAGE_DENOMINATOR },
  kv_reads: { bucket: 'workers_kv', numerator: 500_000n, denominator: 1n },
  kv_writes: { bucket: 'workers_kv', numerator: 5_000_000n, denominator: 1n },
  kv_deletes: { bucket: 'workers_kv', numerator: 5_000_000n, denominator: 1n },
  kv_lists: { bucket: 'workers_kv', numerator: 5_000_000n, denominator: 1n },
  kv_byte_days: { bucket: 'workers_kv', numerator: 500_000_000_000n, denominator: SQLITE_STORAGE_DENOMINATOR },
};

type BudgetEnv = Record<string, never>;
type Row = Record<string, unknown>;
type Reservation = { id: string; day: string; kind: string; estimate_json: string; coverage_json: string; estimate_pusd: string; state: string; retry_at: number | null; actual_json: string | null; actual_pusd: string | null; created_at: number };
type DayRow = { day: string; settled_pusd: string; reserved_pusd: string; units_json: string; costs_json: string; coverage_json: string; events: number; hard_stopped: number; updated_at: number };

const emptyUnits = (): Record<Metric, string> => Object.fromEntries(METRICS.map(k => [k, '0'])) as Record<Metric, string>;
const emptyCosts = (): Record<Bucket, string> => ({ worker: '0', durable_objects: '0', sqlite: '0', workers_kv: '0' });
const emptyCoverage = (): Coverage => Object.fromEntries(METRICS.map(k => [k, 'unknown'])) as Coverage;
const dayAt = (ms: number): string => new Date(ms).toISOString().slice(0, 10);
const nextDayAt = (day: string): number => Date.parse(`${day}T00:00:00.000Z`) + 86_400_000;
const validDay = (value: unknown): value is string => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(`${value}T00:00:00.000Z`)) && dayAt(Date.parse(`${value}T00:00:00.000Z`)) === value;
const asPusd = (value: unknown): bigint | undefined => typeof value === 'string' && /^(0|[1-9][0-9]*)$/.test(value) ? BigInt(value) : undefined;
const ceilDiv = (n: bigint, d: bigint): bigint => n === 0n ? 0n : (n + d - 1n) / d;
const usd = (amount: bigint): string => {
  const whole = amount / PICO;
  const fraction = String(amount % PICO).padStart(12, '0');
  return `${whole}.${fraction}`;
};
const stable = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${stable((value as Row)[k])}`).join(',')}}`;
  return JSON.stringify(value);
};

export function costForUnits(units: Units): { total_pusd: string; costs_pusd: Record<Bucket, string> } {
  const totals: Record<Bucket, bigint> = { worker: 0n, durable_objects: 0n, sqlite: 0n, workers_kv: 0n };
  for (const key of METRICS) {
    const { bucket, numerator, denominator } = PRICES[key];
    if (bucket !== 'tracking') totals[bucket] += ceilDiv(BigInt(units[key]) * numerator, denominator);
  }
  const total = Object.values(totals).reduce((sum, v) => sum + v, 0n);
  return { total_pusd: String(total), costs_pusd: Object.fromEntries(Object.entries(totals).map(([k, v]) => [k, String(v)])) as Record<Bucket, string> };
}

function parseUnits(value: unknown): Units | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const obj = value as Record<string, unknown>;
  if (Object.keys(obj).length !== METRICS.length || METRICS.some(k => !Object.hasOwn(obj, k))) return undefined;
  const out = {} as Units;
  for (const key of METRICS) {
    const n = obj[key];
    if (typeof n !== 'number' || !Number.isSafeInteger(n) || n < 0) return undefined;
    out[key] = n;
  }
  return out;
}

function parseCoverage(value: unknown): Coverage | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const obj = value as Record<string, unknown>;
  if (Object.keys(obj).length !== METRICS.length || METRICS.some(k => obj[k] !== 'exact' && obj[k] !== 'upper_bound')) return undefined;
  return Object.fromEntries(METRICS.map(k => [k, obj[k]])) as Coverage;
}

function mergeCoverage(prior: Coverage, next: Coverage, preserveUnknown: boolean): Coverage {
  return Object.fromEntries(METRICS.map(k => [k,
    prior[k] === 'unknown' && preserveUnknown ? 'unknown'
      : prior[k] === 'upper_bound' || next[k] === 'upper_bound' ? 'upper_bound' : next[k],
  ])) as Coverage;
}

function addUnits(prior: Record<Metric, string>, next: Units): Record<Metric, string> {
  return Object.fromEntries(METRICS.map(k => [k, String(BigInt(prior[k]) + BigInt(next[k]))])) as Record<Metric, string>;
}

function addCosts(prior: Record<Bucket, string>, next: Record<Bucket, string>): Record<Bucket, string> {
  return Object.fromEntries(Object.keys(prior).map(k => [k, String(BigInt(prior[k as Bucket]) + BigInt(next[k as Bucket]))])) as Record<Bucket, string>;
}

function parseObject<T>(raw: string | null | undefined, fallback: T): T {
  try { return raw ? JSON.parse(raw) as T : fallback; } catch { return fallback; }
}

function fundedCoverage(row:DayRow|undefined,openHealth:boolean):Coverage{
 const raw=parseObject<Record<string,unknown>>(row?.coverage_json,{}),known=Object.fromEntries(METRICS.map(k=>[k,raw[k]==='exact'||raw[k]==='upper_bound'?raw[k]:'unknown'])) as Coverage;
 // Every accepted reservation has complete validated exact/upper-bound units.
 // A hold certifies funding, never fabricated observed usage or repaired health.
 return row&&BigInt(row.reserved_pusd)>0n?mergeCoverage(known,Object.fromEntries(METRICS.map(k=>[k,'upper_bound'])) as Coverage,row.events>0||openHealth):known;
}

function httpError(e: unknown): Response {
  return e instanceof HttpError ? error(e.code, e.status) : error('internal_error', 500);
}

/** Durable paid-rate ceiling. It records estimates before effects and actuals
 * afterward; ambiguous reservations are never released by a timer. */
export class BudgetDO {
  private readonly db: MeteredDb;
  private readonly ready: Promise<void>;
  private readonly sqlMeter:SqlReceiptMeter;

  constructor(private readonly ctx: DurableObjectState, _env: BudgetEnv = {}, private readonly clock: () => number = Date.now) {
    this.db = new MeteredDb(ctx.storage.sql);
    this.sqlMeter=new SqlReceiptMeter(this.db,{hibernates_after_handler:true});
    this.ready = initializeSchema(ctx, this.db, 1, BUDGET_SCHEMA);
  }

  fetch(request:Request):Promise<Response>{return this.sqlMeter.measure(()=>this.handle(request),request.headers.get(SQL_PROBE_HEADER));}

  private async handle(request: Request): Promise<Response> {
    try {
      await this.ready;
      const path = new URL(request.url).pathname;
      if (path === '/api/budget' && request.method === 'GET') {
        const actor = actorFrom(request);
        if (!hasScope(actor, 'budget:read') && !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
        return this.status(dayAt(this.clock()));
      }
      if (path === '/api/budget/override' && request.method === 'POST') {
        const actor = actorFrom(request);
        if (!hasScope(actor, 'budget:override') && !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
        return await this.override(request, actor!);
      }
      if (path === '/_internal/reserve' && request.method === 'POST') return await this.reserve(request);
      if (path === '/_internal/settle' && request.method === 'POST') return await this.settle(request);
      if (path === '/_internal/cancel' && request.method === 'POST') return await this.cancel(request);
      if (path === '/_internal/reconcile' && request.method === 'POST') return await this.reconcile(request);
      if (path === '/_internal/alerts' && request.method === 'GET') return this.pendingAlerts(request);
      const claim = path.match(/^\/_internal\/alerts\/([^/]+)\/(claim|ack|fail)$/);
      if (claim && request.method === 'POST') return await this.alertDelivery(request, decodeURIComponent(claim[1]), claim[2]);
      if (path === '/_metrics' && request.method === 'GET') return json({ counts: this.db.snapshot() });
      return error('not_found', 404);
    } catch (e) { return httpError(e); }
  }

  private async reserve(request: Request): Promise<Response> {
    const b = await bodyObject(request, 8192), now = this.clock(), day = dayAt(now);
    if (!boundedString(b.id, 160) || !boundedString(b.kind, 80)) return error('invalid_budget_reservation', 400);
    const units = parseUnits(b.estimated_units), coverage = parseCoverage(b.coverage);
    if (!units || !coverage) { this.markUnknown(day, 'reserve', units, b.coverage); return error('metrics_unknown', 503); }
    const emergency = b.emergency === true;
    if (b.emergency !== undefined && typeof b.emergency !== 'boolean' || emergency && !SAFE_KINDS.has(String(b.kind))) return error('invalid_budget_reservation', 400);
    if (!emergency && this.db.one('SELECT metric FROM budget_health WHERE day=? AND state=\'open\' LIMIT 1', day))
      return error('budget_health_degraded', 503);
    const estimate = costForUnits(units), estimateValue = BigInt(estimate.total_pusd);
    if (estimateValue === 0n) return error('unpriced_budget_reservation', 400);
    const recoveryActor=b.override!==undefined?actorFrom(request):undefined;
    if(b.override!==undefined&&(!emergency||b.kind!=='budget_recovery'||!recoveryActor||!hasScope(recoveryActor,'budget:override')&&!hasScope(recoveryActor,'operator:admin')))return error('forbidden',403);
    const estimateJson = stable(b.override===undefined?units:{units,override:b.override,actor:recoveryActor!.id}), coverageJson = stable(coverage);
    const response = this.db.transaction(this.ctx, () => {
      const prior = this.db.one<Reservation>('SELECT * FROM budget_reservations WHERE id=?', b.id);
      if (prior && (prior.estimate_json !== estimateJson || prior.coverage_json !== coverageJson || prior.kind !== b.kind))
        throw new HttpError('budget_reservation_conflict', 409);
      if (prior?.state === 'settled') return { allowed: true, replay: true, state: 'settled', id: prior.id, day: prior.day };
      if (prior?.state === 'reserved') return { allowed: true, replay: true, state: 'reserved', id: prior.id, day: prior.day, estimate_pusd: prior.estimate_pusd };
      if (prior?.state === 'cancelled') throw new HttpError('budget_reservation_cancelled', 409);
      if (prior?.state === 'denied' && prior.day === day && (prior.retry_at ?? 0) > now)
        return { allowed: false, replay: true, reason: 'daily_ceiling', retry_at: prior.retry_at };
      if(b.override!==undefined)this.applyOverride(b.override as Record<string,unknown>,recoveryActor!,now);
      const d = this.getOrCreateDay(day);
      const limit = this.ceilingFor(day);
      const spendableLimit = emergency ? limit : (limit > RECOVERY_RESERVE_PUSD ? limit - RECOVERY_RESERVE_PUSD : 0n);
      const used = BigInt(d.settled_pusd) + BigInt(d.reserved_pusd);
      if ((Number(d.hard_stopped) && !emergency) || used + estimateValue > spendableLimit) {
        if(b.override!==undefined)throw new HttpError('budget_override_cannot_fund_recovery',409);
        const retryAt = nextDayAt(day);
        // A refused request has no effect/reservation. Never allocate one row per runaway retry.
        return { allowed: false, reason: 'daily_ceiling', retry_at: retryAt, settled_pusd: d.settled_pusd, reserved_pusd: d.reserved_pusd, ceiling_pusd: String(limit) };
      }
      const reserved = String(BigInt(d.reserved_pusd) + estimateValue);
      this.db.run('UPDATE budget_days SET reserved_pusd=?,updated_at=? WHERE day=?', reserved, now, day);
      this.db.run(`INSERT INTO budget_reservations(id,day,kind,estimate_json,coverage_json,estimate_pusd,state,created_at)
        VALUES(?,?,?,?,?,?,'reserved',?) ON CONFLICT(id) DO UPDATE SET day=excluded.day,state='reserved',retry_at=NULL`,
      b.id, day, b.kind, estimateJson, coverageJson, estimate.total_pusd, now);
      return { allowed: true, replay: false, state: 'reserved', id: b.id, day, estimate_pusd: estimate.total_pusd, ceiling_pusd: String(limit), reserved_pusd: reserved };
    });
    if ('allowed' in response && response.allowed) this.checkAlerts(day, now);
    return json(response, response.allowed ? 200 : 429);
  }

  private async settle(request: Request): Promise<Response> {
    const b = await bodyObject(request, 8192), now = this.clock();
    if (!boundedString(b.id, 160)) return error('invalid_budget_settlement', 400);
    const units = parseUnits(b.actual_units), coverage = parseCoverage(b.coverage);
    if (!units || !coverage) { this.markUnknown(dayAt(this.clock()), 'settle', units, b.coverage); return error('metrics_unknown', 503); }
    const actual = costForUnits(units), actualValue = BigInt(actual.total_pusd), actualJson = stable({ units, coverage });
    if (actualValue === 0n) return error('unpriced_budget_settlement', 400);
    const result = this.db.transaction(this.ctx, () => {
      const reservation = this.db.one<Reservation>('SELECT * FROM budget_reservations WHERE id=?', b.id);
      if (!reservation) throw new HttpError('budget_reservation_not_found', 404);
      if (reservation.state === 'settled') {
        if (reservation.actual_json !== actualJson) throw new HttpError('budget_settlement_conflict', 409);
        return { replay: true, day: reservation.day, actual_pusd: reservation.actual_pusd };
      }
      if (reservation.state !== 'reserved') throw new HttpError('budget_reservation_not_active', 409);
      const day = this.getOrCreateDay(reservation.day), estimate = BigInt(reservation.estimate_pusd);
      const settled = BigInt(day.settled_pusd) + actualValue;
      const held = BigInt(day.reserved_pusd) - estimate;
      if (held < 0n) throw new HttpError('budget_ledger_corrupt', 503);
      const unitsTotal = addUnits(parseObject(day.units_json, emptyUnits()), units);
      const costTotal = addCosts(parseObject(day.costs_json, emptyCosts()), actual.costs_pusd);
      const limit = this.ceilingFor(reservation.day);
      const overrun = actualValue > estimate || settled + held > limit;
      const hasOpenHealth = !!this.db.one('SELECT metric FROM budget_health WHERE day=? AND state=\'open\' LIMIT 1', reservation.day);
      const dailyCoverage = mergeCoverage(parseObject(day.coverage_json, emptyCoverage()), coverage, day.events > 0 || hasOpenHealth);
      this.db.run(`UPDATE budget_days SET settled_pusd=?,reserved_pusd=?,units_json=?,costs_json=?,coverage_json=?,events=events+1,hard_stopped=CASE WHEN ? THEN 1 ELSE hard_stopped END,updated_at=? WHERE day=?`,
        String(settled), String(held), JSON.stringify(unitsTotal), JSON.stringify(costTotal), JSON.stringify(dailyCoverage), Number(overrun), now, reservation.day);
      this.db.run(`UPDATE budget_reservations SET state='settled',actual_json=?,actual_pusd=?,settled_at=? WHERE id=? AND state='reserved'`, actualJson, String(actualValue), now, b.id);
      this.writeAlerts(reservation.day, now, settled, held, costTotal);
      return { replay: false, day: reservation.day, actual_pusd: String(actualValue), overrun, hard_stopped: overrun };
    });
    return json(result);
  }

  private async cancel(request: Request): Promise<Response> {
    const b = await bodyObject(request, 4096), now = this.clock();
    const proof = b.evidence as Row | undefined;
    if (!boundedString(b.id, 160) || b.effect_started !== false || !boundedString(b.reason, 240)
      || !proof || proof.source !== 'pre_effect_refusal' || proof.id !== b.id
      || !Number.isSafeInteger(proof.checked_at) || Number(proof.checked_at) > now + 5000) return error('unproved_budget_cancel', 409);
    return json(this.db.transaction(this.ctx, () => {
      const row = this.db.one<Reservation>('SELECT * FROM budget_reservations WHERE id=?', b.id);
      if (!row) throw new HttpError('budget_reservation_not_found', 404);
      if (Number(proof.checked_at) < row.created_at) throw new HttpError('unproved_budget_cancel', 409);
      if (row.state === 'cancelled') return { cancelled: true, replay: true, id: row.id };
      if (row.state !== 'reserved') throw new HttpError('budget_reservation_not_active', 409);
      const day = this.getOrCreateDay(row.day), held = BigInt(day.reserved_pusd) - BigInt(row.estimate_pusd);
      if (held < 0n) throw new HttpError('budget_ledger_corrupt', 503);
      this.db.run('UPDATE budget_days SET reserved_pusd=?,updated_at=? WHERE day=?', String(held), now, row.day);
      this.db.run(`UPDATE budget_reservations SET state='cancelled',settled_at=? WHERE id=? AND state='reserved'`, now, row.id);
      return { cancelled: true, replay: false, id: row.id };
    }));
  }

  private async override(request: Request, actor: NonNullable<ReturnType<typeof actorFrom>>): Promise<Response> {
    const b = await bodyObject(request,4096),now=this.clock();
    this.db.transaction(this.ctx,()=>this.applyOverride(b,actor,now));
    return this.status(dayAt(now));
  }

  private applyOverride(b:Record<string,unknown>,actor:NonNullable<ReturnType<typeof actorFrom>>,now:number):void {
    const day=dayAt(now),ceiling=asPusd(b.ceiling_pusd);
    if(b.day!==day||!ceiling||ceiling<RECOVERY_RESERVE_PUSD||ceiling>MAX_OVERRIDE_PUSD||!boundedString(b.reason,400)
      ||b.clear_hard_stop!==undefined&&typeof b.clear_hard_stop!=='boolean')throw new HttpError('invalid_budget_override',400);
    const current=this.db.one<DayRow>('SELECT * FROM budget_days WHERE day=?',day);
    if(b.clear_hard_stop===true){
      const open=!!this.db.one("SELECT metric FROM budget_health WHERE day=? AND state='open' LIMIT 1",day),known=fundedCoverage(current,open);
      if(METRICS.some(k=>known[k]!=='exact'&&known[k]!=='upper_bound')||open)throw new HttpError('budget_recovery_metrics_unknown',503);
    }
    if(current&&ceiling<BigInt(current.settled_pusd)+BigInt(current.reserved_pusd)+RECOVERY_RESERVE_PUSD)throw new HttpError('budget_override_below_committed',409);
    this.db.run("INSERT INTO budget_overrides(day,ceiling_pusd,actor_id,reason,created_at) VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET ceiling_pusd=excluded.ceiling_pusd,actor_id=excluded.actor_id,reason=excluded.reason,created_at=excluded.created_at",day,String(ceiling),actor.id,b.reason,now);
    this.db.run('INSERT INTO budget_override_audit(day,ceiling_pusd,actor_id,reason,created_at) VALUES(?,?,?,?,?)',day,String(ceiling),actor.id,String(b.reason)+(b.clear_hard_stop===true?' [explicit breaker recovery]':''),now);
    if(b.clear_hard_stop===true)this.db.run('UPDATE budget_days SET hard_stopped=0,updated_at=? WHERE day=?',now,day);
  }

  private async reconcile(request: Request): Promise<Response> {
    const b = await bodyObject(request, 4096), now = this.clock();
    const keys = ['worker', 'durable_objects', 'sqlite', 'workers_kv'];
    const coverage = b.coverage && typeof b.coverage === 'object' && !Array.isArray(b.coverage) ? b.coverage as Record<string, unknown> : undefined;
    const money = ['provider_total_pusd', 'hub_attributed_pusd', 'unattributed_pusd'].map(k => asPusd(b[k]));
    if (!validDay(b.day) || !boundedString(b.source, 100)
      || !Number.isSafeInteger(b.observed_at) || Number(b.observed_at) > now + 5000
      || !Number.isSafeInteger(b.reported_through) || Number(b.reported_through) > Number(b.observed_at)
      || money.some(v => v === undefined) || !coverage || Object.keys(coverage).length !== keys.length
      || keys.some(k => typeof coverage[k] !== 'boolean') || BigInt(String(money[1])) + BigInt(String(money[2])) !== money[0]) return error('invalid_provider_reconciliation', 400);
    const allCovered = keys.every(k => coverage[k] === true);
    const prior = this.db.one<{ reported_through: number }>('SELECT reported_through FROM budget_provider WHERE day=?', b.day);
    if (prior && Number(b.reported_through) < prior.reported_through) return error('stale_provider_reconciliation', 409);
    this.db.run(`INSERT INTO budget_provider(day,source,observed_at,reported_through,provider_total_pusd,hub_attributed_pusd,unattributed_pusd,coverage_json)
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET source=excluded.source,observed_at=excluded.observed_at,reported_through=excluded.reported_through,provider_total_pusd=excluded.provider_total_pusd,hub_attributed_pusd=excluded.hub_attributed_pusd,unattributed_pusd=excluded.unattributed_pusd,coverage_json=excluded.coverage_json`,
    b.day, b.source, b.observed_at, b.reported_through, ...money.map(String), stable(coverage));
    return json({ recorded: true, complete: allCovered, provider_lag_seconds: Math.max(0, Math.floor((now - Number(b.reported_through)) / 1000)) });
  }

  private pendingAlerts(request: Request): Response {
    const supplied = new URL(request.url).searchParams.get('limit'), parsed = supplied === null ? 10 : Number(supplied);
    if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 20) return error('invalid_page_limit', 400);
    const limit = parsed;
    return json({ items: this.db.all('SELECT id,day,kind,payload_json,state,attempts,created_at,retry_at FROM budget_alerts WHERE state IN (\'pending\',\'claimed\') ORDER BY created_at,id LIMIT ?', limit).map((r: Row) => ({ ...r, payload: parseObject(r.payload_json as string, {}) })) });
  }

  private async alertDelivery(request: Request, id: string, action: string): Promise<Response> {
    const b = await bodyObject(request, 4096), now = this.clock();
    if (action === 'claim') {
      if (!boundedString(b.worker_id, 120)) return error('invalid_alert_claim', 400);
      const token = crypto.randomUUID();
      const claimed = this.db.transaction(this.ctx, () => {
        const row = this.db.one<Row>(`SELECT * FROM budget_alerts WHERE id=? AND retry_at<=? AND (state='pending' OR (state='claimed' AND claim_until<=?))`, id, now, now);
        if (!row) return undefined;
        this.db.run(`UPDATE budget_alerts SET state='claimed',attempts=attempts+1,claim_token=?,claim_until=? WHERE id=? AND (state='pending' OR claim_until<=?)`, token, now + 60_000, id, now);
        return this.db.one<Row>('SELECT * FROM budget_alerts WHERE id=?', id);
      });
      return claimed ? json({ alert: { ...claimed, payload: parseObject(claimed.payload_json as string, {}) }, claim_token: token }) : error('alert_not_claimable', 409);
    }
    if (!boundedString(b.claim_token, 100)) return error('invalid_alert_claim', 400);
    if (action === 'ack') {
      const existing = this.db.one<{ state: string }>('SELECT state FROM budget_alerts WHERE id=?', id);
      if (existing?.state === 'delivered') return json({ delivered: true, replay: true });
      this.db.run(`UPDATE budget_alerts SET state='delivered',delivered_at=?,claim_token=NULL,claim_until=NULL WHERE id=? AND state='claimed' AND claim_token=?`, now, id, b.claim_token);
      const saved = this.db.one<{ state: string }>('SELECT state FROM budget_alerts WHERE id=?', id);
      return saved?.state === 'delivered' ? json({ delivered: true, replay: false }) : error('alert_claim_stale', 409);
    }
    if (action === 'fail') {
      const row = this.db.one<Row>('SELECT attempts FROM budget_alerts WHERE id=? AND state=\'claimed\' AND claim_token=?', id, b.claim_token);
      if (!row) return error('alert_claim_stale', 409);
      const attempts = Number(row.attempts), terminal = attempts >= 8;
      const retryAt = now + Math.min(3_600_000, 15_000 * (2 ** Math.max(0, attempts - 1)));
      this.db.run(`UPDATE budget_alerts SET state=?,retry_at=?,claim_token=NULL,claim_until=NULL WHERE id=? AND claim_token=?`, terminal ? 'delivery_failed' : 'pending', retryAt, id, b.claim_token);
      return json({ retry_at: terminal ? null : retryAt, delivery_failed: terminal, attempts });
    }
    return error('not_found', 404);
  }

  private status(day: string): Response {
    const row = this.db.one<DayRow>('SELECT * FROM budget_days WHERE day=?', day);
    const provider = this.db.one<Row>('SELECT * FROM budget_provider WHERE day=?', day);
    const override = this.db.one<Row>('SELECT ceiling_pusd,actor_id,reason,created_at FROM budget_overrides WHERE day=?', day);
    const units = parseObject<Record<Metric, string>>(row?.units_json, emptyUnits());
    const costs = parseObject<Record<Bucket, string>>(row?.costs_json, emptyCosts());
    const pending = this.db.all<Row>('SELECT id,kind,state,attempts,created_at FROM budget_alerts WHERE day=? ORDER BY created_at,id', day);
    const healthEvents = this.db.all<Row>('SELECT metric,reason,failures,first_at,last_at FROM budget_health WHERE day=? AND state=\'open\' ORDER BY metric', day);
    const coverage=fundedCoverage(row,healthEvents.length>0);
    const settled = BigInt(row?.settled_pusd || '0'), held = BigInt(row?.reserved_pusd || '0');
    const ceiling = this.ceilingFor(day), elapsed = Math.max(60_000, this.clock() - Date.parse(`${day}T00:00:00.000Z`));
    const projection = (settled + held) * 86_400_000n / BigInt(Math.min(elapsed, 86_400_000));
    const providerCoverage = parseObject<Record<string, boolean>>(provider?.coverage_json as string, {});
    const categoryCoverage = ['worker', 'durable_objects', 'sqlite', 'workers_kv'].every(k => providerCoverage[k] === true);
    const eventCoverage = METRICS.every(k => coverage[k] === 'exact' || coverage[k] === 'upper_bound');
    const deliveryFailed = pending.some(v => v.state === 'delivery_failed');
    const metering = this.db.snapshot();
    const degraded = !row || !eventCoverage || !provider || !categoryCoverage || deliveryFailed || healthEvents.length > 0 || !metering.measurement_valid;
    const health = degraded ? 'degraded' : Object.values(coverage).some(v => v === 'upper_bound') ? 'healthy_estimated' : 'healthy';
    return json({
      day, currency: 'USD', timezone: 'UTC', allowances: 'assumed_exhausted', rate_basis: 'marginal_paid_estimate_from_exact_or_upper_bound_units',
      alert_threshold_usd: usd(ALERT_PUSD), daily_ceiling_usd: usd(ceiling), recovery_reserve_usd: usd(RECOVERY_RESERVE_PUSD),
      settled_usd: usd(settled), reserved_usd: usd(held), projected_usd: usd(projection),
      units, units_basis:'Settled ledger units only; fully held estimates remain in reserved_usd, not observed usage.', category_costs_usd: Object.fromEntries(Object.entries(costs).map(([k, v]) => [k, usd(BigInt(v))])),
      coverage, health, health_events: healthEvents, controller_metering_valid: metering.measurement_valid,
      hard_stopped: !!row?.hard_stopped, override: override ? { ...override, ceiling_usd: usd(BigInt(String(override.ceiling_pusd))) } : null,
      alerts: pending.map(v => ({ id: v.id, kind: v.kind, state: v.state, attempts: v.attempts, created_at: v.created_at })),
      provider: provider ? {
        source: provider.source, observed_at: provider.observed_at, reported_through: provider.reported_through,
        lag_seconds: Math.max(0, Math.floor((this.clock() - Number(provider.reported_through)) / 1000)),
        total_usd: usd(BigInt(String(provider.provider_total_pusd))), hub_attributed_usd: usd(BigInt(String(provider.hub_attributed_pusd))),
        unattributed_usd: usd(BigInt(String(provider.unattributed_pusd))), coverage: providerCoverage,
      } : { state: 'unknown', lag_seconds: null },
    });
  }

  private getOrCreateDay(day: string): DayRow {
    const existing=this.db.one<DayRow>('SELECT * FROM budget_days WHERE day=?',day);
    if(existing)return existing;
    this.db.run(`INSERT OR IGNORE INTO budget_days(day,settled_pusd,reserved_pusd,units_json,costs_json,coverage_json,events,hard_stopped,updated_at)
      VALUES(?,'0','0',?,?,?,0,0,?)`, day, JSON.stringify(emptyUnits()), JSON.stringify(emptyCosts()), JSON.stringify(emptyCoverage()), this.clock());
    const row = this.db.one<DayRow>('SELECT * FROM budget_days WHERE day=?', day);
    if (!row) throw new HttpError('budget_ledger_unavailable', 503);
    return row;
  }

  private markUnknown(day: string, reason: string, units: Units | undefined, coverageValue: unknown): void {
    const c = coverageValue && typeof coverageValue === 'object' && !Array.isArray(coverageValue) ? coverageValue as Row : {};
    const missing = METRICS.filter(k => !units || !Object.hasOwn(c, k) || (c[k] !== 'exact' && c[k] !== 'upper_bound'));
    const metrics = missing.length ? missing : [...METRICS];
    const now = this.clock();
    this.db.transaction(this.ctx, () => {
      const row = this.getOrCreateDay(day), coverage = parseObject<Coverage>(row.coverage_json, emptyCoverage());
      for (const metric of metrics) {
        coverage[metric] = 'unknown';
        const prior = this.db.one<{ last_at: number }>('SELECT last_at FROM budget_health WHERE day=? AND metric=?', day, metric);
        if (prior && now - prior.last_at < 300_000) continue;
        this.db.run(`INSERT INTO budget_health(day,metric,reason,state,failures,first_at,last_at) VALUES(?,?,?,'open',1,?,?)
          ON CONFLICT(day,metric) DO UPDATE SET reason=excluded.reason,state='open',failures=failures+1,last_at=excluded.last_at`, day, metric, reason, now, now);
      }
      this.db.run('UPDATE budget_days SET coverage_json=?,updated_at=? WHERE day=?', JSON.stringify(coverage), now, day);
    });
  }

  private ceilingFor(day: string): bigint {
    const override = this.db.one<{ ceiling_pusd: string }>('SELECT ceiling_pusd FROM budget_overrides WHERE day=?', day);
    return asPusd(override?.ceiling_pusd) ?? DEFAULT_CEILING_PUSD;
  }

  private checkAlerts(day: string, now: number): void {
    const d = this.db.one<DayRow>('SELECT * FROM budget_days WHERE day=?', day);
    if (!d) return;
    const settled = BigInt(d.settled_pusd), held = BigInt(d.reserved_pusd);
    const costs = parseObject<Record<Bucket, string>>(d.costs_json, emptyCosts());
    this.writeAlerts(day, now, settled, held, costs);
  }

  private writeAlerts(day: string, now: number, settled: bigint, held: bigint, costs: Record<Bucket, string>): void {
    if (settled > ALERT_PUSD) this.insertAlert(day, 'threshold', now, { accumulated_usd: usd(settled), threshold_usd: usd(ALERT_PUSD), category_costs_usd: Object.fromEntries(Object.entries(costs).map(([k, v]) => [k, usd(BigInt(v))])), observed_at: now });
    const elapsed = Math.max(60_000, now - Date.parse(`${day}T00:00:00.000Z`));
    const projected = (settled + held) * 86_400_000n / BigInt(Math.min(elapsed, 86_400_000));
    if (settled <= ALERT_PUSD && projected > ALERT_PUSD && elapsed >= 300_000) this.insertAlert(day, 'forecast', now, { accumulated_usd: usd(settled), projected_usd: usd(projected), threshold_usd: usd(ALERT_PUSD), projection_window_seconds: Math.floor(elapsed / 1000), observed_at: now });
  }

  private insertAlert(day: string, kind: string, now: number, payload: Row): void {
    const id = `hub:${day}:${kind}`;
    if(this.db.one('SELECT id FROM budget_alerts WHERE id=?',id))return;
    const observed=this.db.one<DayRow>('SELECT * FROM budget_days WHERE day=?',day);
    const provider=this.db.one<Row>('SELECT observed_at,reported_through FROM budget_provider WHERE day=?',day);
    payload={...payload,day,kind,coverage:parseObject(observed?.coverage_json,emptyCoverage()),
      provider_observed_at:provider?.observed_at??null,provider_lag_seconds:provider?Math.max(0,Math.floor((now-Number(provider.reported_through))/1000)):null};
    this.db.run(`INSERT OR IGNORE INTO budget_alerts(id,day,kind,payload_json,state,attempts,created_at,retry_at)
      VALUES(?,?,?,?,'pending',0,?,?)`, id, day, kind, JSON.stringify(payload), now, now);
  }
}
