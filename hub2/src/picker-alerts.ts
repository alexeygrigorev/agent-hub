import {HttpError} from './http';
import { randomToken, sha256Hex, safeEqual } from './crypto';
import { MeteredDb } from './db';
import { normalizeStartPickerEvidence, type StartPickerEvidence } from './start-picker-evidence';

export const PICKER_ALERT_RECIPIENT = 'dev@dtcdev.click';
export const PICKER_ALERT_SCHEMA = [
  `CREATE TABLE picker_draws (project_id TEXT NOT NULL,operation_id TEXT NOT NULL,attempt INTEGER NOT NULL,
    input_digest TEXT NOT NULL,response_json TEXT NOT NULL,PRIMARY KEY(project_id,operation_id,attempt)) WITHOUT ROWID`,
  'CREATE TABLE picker_coverage(singleton INTEGER PRIMARY KEY CHECK(singleton=1),owner_epoch TEXT NOT NULL,epoch TEXT NOT NULL,since INTEGER NOT NULL)',
  `CREATE TABLE picker_receipts (
    receipt_seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    operation_id TEXT NOT NULL, attempt INTEGER NOT NULL, facts_json TEXT NOT NULL,
    UNIQUE(project_id,operation_id,attempt))`,
  `CREATE TABLE picker_alert_counters (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), total_picks INTEGER NOT NULL,
    degraded_picks INTEGER NOT NULL, consecutive_degraded INTEGER NOT NULL,
    occurrence_watermark INTEGER NOT NULL, receipt_watermark INTEGER NOT NULL)`,
  `CREATE TABLE picker_alert_deliveries (
    occurrence INTEGER PRIMARY KEY, receipt_seq INTEGER NOT NULL UNIQUE,
    payload_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','leased','delivered')),
    attempts INTEGER NOT NULL, available_at INTEGER NOT NULL, lease_hash TEXT,
    lease_until INTEGER, delivered_at INTEGER)`,
  'CREATE INDEX picker_alert_due ON picker_alert_deliveries(status,available_at,lease_until,occurrence)',
];

export type PickerReceipt = {
  project_id: string; operation_id: string; attempt: 0 | 1;
  committed_at: number; picker: StartPickerEvidence;
};
export type PickerAlertCounters = {
  total_picks: number; degraded_picks: number; consecutive_degraded: number;
  occurrence_watermark: number; receipt_watermark: number;
};
type Delivery = {
  occurrence: number; receipt_seq: number; payload_json: string; status: string;
  attempts: number; available_at: number; lease_hash: string | null;
  lease_until: number | null; delivered_at: number | null;
};

function identifier(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,179}$/.test(value);
}

function projectIdentifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 180
    && value.trim() === value && !/[\x00-\x1f\x7f-\x9f]/.test(value);
}

function timestamp(value: number): void {
  if (!Number.isSafeInteger(value) || value < 0 || value > Number.MAX_SAFE_INTEGER - 600_000)
    throw new HttpError('invalid_picker_timestamp',400);
}

function receipt(value: unknown): PickerReceipt {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError('invalid_picker_receipt',400);
  const input = value as Record<string, unknown>;
  const picker = normalizeStartPickerEvidence(input.picker);
  if (!projectIdentifier(input.project_id) || !identifier(input.operation_id)
      || (input.attempt !== 0 && input.attempt !== 1) || !picker || picker.attempt !== input.attempt
      || typeof input.committed_at !== 'number') throw new HttpError('invalid_picker_receipt',400);
  timestamp(input.committed_at);
  return { project_id: input.project_id, operation_id: input.operation_id, attempt: input.attempt,
    committed_at: input.committed_at, picker };
}

// Diagnostic projection only: these public identifiers never influence routing.
// Unknown/custom historical IDs and reasons remain explicit unknowns, not raw text.
const DIAGNOSTIC_POLICIES = new Set(['codex-luna-max','codex-sol-medium','claude-sonnet-medium',
  'claude-fable-5-2','go-muse','claude-opus-medium','claude-opus-high','antigravity-hydrogen-medium',
  'antigravity-argon-medium','zai-glm-5-3-flash','antigravity-carbon-medium']);
const DIAGNOSTIC_REASONS = new Set(['codex_reserve','quota_below_minimum','goal_not_advertised',
  'engine_not_advertised','capability_not_advertised','central_host_not_approved',
  'central_credentials_unavailable','local_credentials_missing','credential_missing','credential_unknown',
  'credential_expired','credential_refresh_failed','credential_unavailable','credential_ready','deep_prefers_strong_model','deep_prefers_high_effort']);
const MAX_DRAW_JSON = 65536;
function storedObject(raw: unknown): Record<string, any> | null {
  if(typeof raw!=='string')return null;
  try {
    const value=JSON.parse(raw);
    // Stored writers use JSON.stringify. Refuse duplicate keys/nonfinite or
    // noncanonical corrupt records instead of normalizing them into evidence.
    return value&&typeof value==='object'&&!Array.isArray(value)&&JSON.stringify(value)===raw ? value : null;
  } catch { return null; }
}

/** Control calls this collector while serializing actual draws. */
export class PickerAlerts {
  constructor(private readonly db: MeteredDb, private readonly state: DurableObjectState) {}

  beginCoverage(ownerEpoch: string, now = Date.now(), interrupted = false, intervalEpoch = ownerEpoch): void {
    timestamp(now);
    if (!identifier(ownerEpoch) || !identifier(intervalEpoch)) throw new HttpError('invalid_picker_coverage',400);
    this.db.transaction(this.state, () => {
      if (!interrupted && this.db.one<{owner_epoch:string}>('SELECT owner_epoch FROM picker_coverage WHERE singleton=1')?.owner_epoch === ownerEpoch) return;
      this.db.run('INSERT INTO picker_coverage VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET owner_epoch=excluded.owner_epoch,epoch=excluded.epoch,since=excluded.since',ownerEpoch,intervalEpoch,now);
      this.db.run('UPDATE picker_alert_counters SET consecutive_degraded=0 WHERE singleton=1');
    });
  }

  coverage() {
    const row=this.db.one<{epoch:string;since:number}>('SELECT epoch,since FROM picker_coverage WHERE singleton=1');
    return row ? {available:true,...row} : {available:false};
  }

  replayDraw(project: string, operation: string, attempt: number, digest: string) {
    const prior=this.db.one<{input_digest:string;response_json:string}>(
      'SELECT input_digest,response_json FROM picker_draws WHERE project_id=? AND operation_id=? AND attempt=?',project,operation,attempt);
    if (!prior) return undefined;
    if (prior.input_digest!==digest) throw new HttpError('picker_draw_conflict',409);
    return JSON.parse(prior.response_json);
  }

  commitDraw(value: unknown, digest: string, response: unknown, now=Date.now()) {
    const facts=receipt(value);
    return this.db.transaction(this.state,()=>{
      const prior=this.replayDraw(facts.project_id,facts.operation_id,facts.attempt,digest);
      if(prior!==undefined)return prior;
      this.recordWithin(facts,now);
      this.db.run('INSERT INTO picker_draws VALUES(?,?,?,?,?)',facts.project_id,facts.operation_id,facts.attempt,digest,JSON.stringify(response));
      return response;
    });
  }

  /** Read existing committed responses only. Never replay, draw, lease or repair. */
  async drawDiagnostics(limit=10, sinceReceiptSeq=0) {
    if(!Number.isSafeInteger(limit)||limit<1||limit>25
      ||!Number.isSafeInteger(sinceReceiptSeq)||sinceReceiptSeq<0)
      throw new HttpError('invalid_picker_diagnostic_query',400);
    const windowSize=limit+1;
    // picker_draws is WITHOUT ROWID. Bound both existing-index key windows
    // BEFORE lookup, JSON inspection or final sorting. Orphan key order is
    // deliberately not described as chronological or exhaustive history.
    const rows=this.db.all<{project_id:string;operation_id:string;attempt:number;receipt_seq:number|null;
      response_json:string|null;facts_json:string|null;has_draw:number}>(`
      WITH recent_receipts AS MATERIALIZED (
        SELECT project_id,operation_id,attempt,receipt_seq FROM picker_receipts
        ORDER BY receipt_seq DESC LIMIT ?
      ), orphan_key_window AS MATERIALIZED (
        SELECT project_id,operation_id,attempt FROM picker_draws
        ORDER BY project_id DESC,operation_id DESC,attempt DESC LIMIT ?
      ), candidates AS (
        SELECT r.project_id,r.operation_id,r.attempt,r.receipt_seq
        FROM recent_receipts r WHERE r.receipt_seq>? OR r.receipt_seq<=0
        UNION ALL
        SELECT k.project_id,k.operation_id,k.attempt,NULL AS receipt_seq
        FROM orphan_key_window k LEFT JOIN picker_receipts r
          ON r.project_id=k.project_id AND r.operation_id=k.operation_id AND r.attempt=k.attempt
        WHERE r.receipt_seq IS NULL
      )
      SELECT substr(k.project_id,1,181) AS project_id,substr(k.operation_id,1,181) AS operation_id,
        k.attempt,k.receipt_seq,CASE WHEN d.project_id IS NULL THEN 0 ELSE 1 END AS has_draw,
        CASE WHEN length(CAST(d.response_json AS BLOB))<=? THEN d.response_json ELSE NULL END AS response_json,
        CASE WHEN length(CAST(r.facts_json AS BLOB))<=? THEN r.facts_json ELSE NULL END AS facts_json
      FROM candidates k LEFT JOIN picker_draws d
        ON d.project_id=k.project_id AND d.operation_id=k.operation_id AND d.attempt=k.attempt
      LEFT JOIN picker_receipts r
        ON r.project_id=k.project_id AND r.operation_id=k.operation_id AND r.attempt=k.attempt
      ORDER BY k.receipt_seq DESC,k.project_id,k.operation_id,k.attempt LIMIT ?`,
      windowSize,windowSize,sinceReceiptSeq,MAX_DRAW_JSON,MAX_DRAW_JSON,limit+1);
    const items=await Promise.all(rows.slice(0,limit).map(async row=>{
      const draw_id=await sha256Hex(JSON.stringify([row.project_id,row.operation_id,row.attempt]));
      const sequence=typeof row.receipt_seq==='number'&&Number.isSafeInteger(row.receipt_seq)&&row.receipt_seq>0?row.receipt_seq:null;
      const bound=projectIdentifier(row.project_id)&&identifier(row.operation_id)&&(row.attempt===0||row.attempt===1);
      const facts=storedObject(row.facts_json),response=storedObject(row.response_json);
      const time=bound&&facts&&facts.project_id===row.project_id&&facts.operation_id===row.operation_id
        &&facts.attempt===row.attempt&&Number.isSafeInteger(facts.committed_at)&&facts.committed_at>=0
        &&facts.committed_at<=Number.MAX_SAFE_INTEGER-600000 ? facts.committed_at as number : null;
      let status='observed';
      let exclusions:Array<{policy_id:string|null;reason:string}>|null=null;
      if(!bound)status='unknown_identity';
      else if(!row.has_draw)status='missing_response';
      else if(row.response_json===null)status='oversized_response';
      else if(!response)status='malformed_response';
      else if(!response.diagnostics||typeof response.diagnostics!=='object'||Array.isArray(response.diagnostics)
        ||!Array.isArray(response.diagnostics.excluded))status='diagnostics_unavailable';
      else if(response.diagnostics.excluded.length>32)status='oversized_exclusions';
      else {
        exclusions=response.diagnostics.excluded.map((entry:unknown)=>{
          if(!entry||typeof entry!=='object'||Array.isArray(entry)){status='unknown_exclusion';return {policy_id:null,reason:'malformed_exclusion'};}
          const value=entry as Record<string,unknown>;
          const policy_id=typeof value.policy_id==='string'&&DIAGNOSTIC_POLICIES.has(value.policy_id)?value.policy_id:null;
          const reason=typeof value.reason==='string'&&DIAGNOSTIC_REASONS.has(value.reason)?value.reason:'unknown_reason';
          if(policy_id===null||reason==='unknown_reason')status='unknown_exclusion';
          return {policy_id,reason};
        });
      }
      if(status==='observed'&&(sequence===null||time===null))status='unknown_receipt_metadata';
      return {draw_id,receipt_seq:sequence,committed_at_ms:time,status,exclusions};
    }));
    return {kind:'hub_picker_draw_diagnostics_v1',scope:'committed_successful_draws_only',
      absence_authority:false,limit,since_receipt_seq:sinceReceiptSeq,order:'newest_receipt_first',
      window_size:windowSize,max_candidates:2*windowSize,
      windows:['newest_receipt_sequence','descending_composite_draw_key_orphans'],
      has_more:rows.length>limit,items,
      limitations:['direct_picker_calls_not_recorded','not_exhaustive_alert_history',
        'bounded_recent_page','receipt_sequence_filter_applied_after_window',
        'orphan_key_window_not_chronological','older_rows_outside_windows_not_searched']};
  }

  counters(): PickerAlertCounters {
    const row = this.db.one<PickerAlertCounters>(
      'SELECT total_picks,degraded_picks,consecutive_degraded,occurrence_watermark,receipt_watermark FROM picker_alert_counters WHERE singleton=1',
    ) ?? { total_picks: 0, degraded_picks: 0, consecutive_degraded: 0, occurrence_watermark: 0, receipt_watermark: 0 };
    return { ...row };
  }

  /** @internal Production draws use commitDraw to commit their complete response. */
  record(value: unknown, now = Date.now()) {
    timestamp(now);
    const facts = receipt(value);
    return this.db.transaction(this.state, () => this.recordWithin(facts,now));
  }

  private recordWithin(facts:PickerReceipt,now:number) {
    timestamp(now);
    const factsJson=JSON.stringify(facts);
    const prior = this.db.one<{ receipt_seq: number; facts_json: string }>(
      'SELECT receipt_seq,facts_json FROM picker_receipts WHERE project_id=? AND operation_id=? AND attempt=?',
      facts.project_id, facts.operation_id, facts.attempt,
    );
    if (prior) {
      if (prior.facts_json !== factsJson) throw new HttpError('picker_receipt_conflict',409);
      return { replay: true, receipt_seq: prior.receipt_seq, ...this.counters() };
    }
    const before = this.counters();
    if (before.total_picks >= Number.MAX_SAFE_INTEGER) throw new HttpError('picker_counter_capacity',503);
    const degraded = new Set(facts.picker.pool.map(row => row.model)).size < 3;
    const streak = degraded ? before.consecutive_degraded + 1 : 0;
    const occurrence = before.occurrence_watermark + Number(streak === 3);
    this.db.run('INSERT INTO picker_receipts(project_id,operation_id,attempt,facts_json) VALUES(?,?,?,?)',
      facts.project_id, facts.operation_id, facts.attempt, factsJson);
    const accepted = this.db.one<{ receipt_seq: number }>(
      'SELECT receipt_seq FROM picker_receipts WHERE project_id=? AND operation_id=? AND attempt=?',
      facts.project_id, facts.operation_id, facts.attempt,
    )!;
    const counts: PickerAlertCounters = { total_picks: before.total_picks + 1,
      degraded_picks: before.degraded_picks + Number(degraded), consecutive_degraded: streak,
      occurrence_watermark: occurrence, receipt_watermark: accepted.receipt_seq };
    this.db.run(`INSERT INTO picker_alert_counters VALUES(1,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
      total_picks=excluded.total_picks,degraded_picks=excluded.degraded_picks,
      consecutive_degraded=excluded.consecutive_degraded,occurrence_watermark=excluded.occurrence_watermark,
      receipt_watermark=excluded.receipt_watermark`,
    counts.total_picks, counts.degraded_picks, streak, occurrence, accepted.receipt_seq);
    if (streak === 3) {
      const payload = { to: PICKER_ALERT_RECIPIENT, subject: 'Hub picker has three consecutive degraded picks',
        occurrence, receipt_seq: accepted.receipt_seq, ...facts };
      this.db.run(`INSERT INTO picker_alert_deliveries
        (occurrence,receipt_seq,payload_json,status,attempts,available_at) VALUES(?,?,?,'pending',0,?)`,
      occurrence, accepted.receipt_seq, JSON.stringify(payload), now);
    }
    return { replay: false, receipt_seq: accepted.receipt_seq, ...counts };
  }

  /** A transport receipt means the configured sender accepted this alert. */
  async offers(now: number, limit = 10) {
    timestamp(now);
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 10) throw new HttpError('invalid_picker_alert_limit',400);
    // Hash before entering the synchronous transaction. Selection and lease
    // acquisition then share one transaction, including competing pollers.
    const tokens = await Promise.all(Array.from({ length: limit }, async () => {
      const token = randomToken();
      return { token, hash: await sha256Hex(token) };
    }));
    return this.db.transaction(this.state, () => {
      const rows = this.db.all<Delivery>(`SELECT * FROM picker_alert_deliveries WHERE
        (status='pending' AND available_at<=?) OR (status='leased' AND lease_until<=?) ORDER BY occurrence LIMIT ?`,
      now, now, limit);
      return rows.map((row, index) => {
        const leaseUntil = now + 10 * 60_000;
        this.db.run(`UPDATE picker_alert_deliveries SET status='leased',attempts=attempts+1,
          lease_hash=?,lease_until=? WHERE occurrence=?`, tokens[index].hash, leaseUntil, row.occurrence);
        return { occurrence: row.occurrence, recipient: PICKER_ALERT_RECIPIENT,
          payload: JSON.parse(row.payload_json) as Record<string, unknown>, lease_token: tokens[index].token,
          lease_until: leaseUntil, attempt: row.attempts + 1 };
      });
    });
  }

  async settle(occurrence: number, token: string, action: 'ack' | 'nack', now: number) {
    timestamp(now);
    if (!Number.isSafeInteger(occurrence) || occurrence < 1 || typeof token !== 'string' || !/^[0-9a-f]{64}$/.test(token)
        || (action !== 'ack' && action !== 'nack')) throw new HttpError('invalid_picker_alert_receipt',400);
    const hash = await sha256Hex(token);
    return this.db.transaction(this.state, () => {
      const row = this.db.one<Delivery>('SELECT * FROM picker_alert_deliveries WHERE occurrence=?', occurrence);
      if (!row) throw new HttpError('picker_alert_not_found',404);
      if (!row.lease_hash || !safeEqual(row.lease_hash, hash)) throw new HttpError('picker_alert_lease_conflict',409);
      if (action === 'ack' && row.status === 'delivered') return { replay: true, delivered: true };
      if (action === 'nack' && row.status === 'pending') return { replay: true, delivered: false };
      if (row.status !== 'leased' || row.lease_until === null || row.lease_until <= now)
        throw new HttpError('picker_alert_lease_conflict',409);
      if (action === 'ack') {
        this.db.run("UPDATE picker_alert_deliveries SET status='delivered',lease_until=NULL,delivered_at=? WHERE occurrence=?",
          now, occurrence);
      } else {
        const delay = Math.min(300_000, 15_000 * 2 ** Math.min(row.attempts - 1, 4));
        this.db.run("UPDATE picker_alert_deliveries SET status='pending',lease_until=NULL,available_at=? WHERE occurrence=?",
          now + delay, occurrence);
      }
      return { replay: false, delivered: action === 'ack' };
    });
  }
}
