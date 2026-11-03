/**
 * Per-host command-channel health (intake 3aa850b4): a host can check in as
 * online with free capacity while Hub commands (capture/send/stop) are never
 * claimed or acked by it, so placement keeps sending work to a host that
 * cannot be controlled. Health is derived from command evidence only, never
 * from an observation timeout alone.
 *
 * Evidence, evaluated only while the host check-in is fresh (same 180 s gate
 * as routing online):
 *  - LIVE STUCK: commands still in `recorded`/`transported` (offered/queued)
 *    for live sessions on this host, older than STUCK_MS, not not_before-
 *    deferred, and not superseded by a newer channel proof. The window
 *    comfortably exceeds the 60 s claim-TTL self-heal, the <=60 s message
 *    not-ready backoff and multi-minute single-inflight host work (a worktree
 *    command can wait minutes behind git fetch), so slow-but-working hosts
 *    never trip it.
 *  - TERMINAL STOP EVIDENCE: stops that expired as failed `stop_not_claimed`
 *    (host was online, never claimed) within LOOKBACK_MS — the online-host
 *    signature of an unreachable channel, because expireUnclaimedStops drains
 *    unclaimed stops out of recorded/transported within a minute.
 *
 * Corroboration (review cc84ca6f): placement exclusion requires at least TWO
 * distinct stuck commands. One isolated delayed, lost or missed command is
 * never channel evidence; the recurring command traffic a live host receives
 * (pings, captures, sends) is what makes the evidence corroborated.
 *
 * Delivery receipt (review cc84ca6f, AC3): live-stuck evidence counts only if
 * the Hub demonstrably DELIVERED to this host after the oldest stuck command
 * was created — a stuck command currently carrying an offers poll receipt
 * (`poll_at`, set only when the Hub handed the offer to the host) or a later
 * claim-or-later channel proof. A command-poll request that failed at the Hub
 * (5xx/timeout) leaves no receipt and no proof while check-ins stay fresh, so
 * such a host reads `unknown`, never `unreachable`: Hub transport failures do
 * not become host evidence. Terminal stop evidence needs no receipt: a stop
 * only expires as `stop_not_claimed` when the host was reachable at expiry.
 *
 * A CHANNEL PROOF is any later command on the host that reached
 * claimed/acked/refused/uncertain, or a terminal failed row whose persisted
 * host ack contains the matching claim token: the claim direction worked.
 * A proof newer than a stuck command supersedes it (AC2 automatic recovery),
 * and exhausted claim retries (`claim_expired_retry_exhausted`) are not
 * evidence at all because claims happened.
 *
 * Residual AC3 corner (documented, safe direction): a host whose command
 * polls fail permanently at the Hub while heartbeats succeed leaves no
 * receipt and reads `unknown` — undetectable without host-side poll-failure
 * telemetry, and never penalized.
 *
 * Bounded by construction: existing commands migrate through a durable PK
 * cursor in 128-row pages. New writes maintain a small derived evidence queue
 * and one latest proof per host. Evidence queries seek at most 129 rows before
 * session PK lookups or eligibility filters; incomplete observations remain
 * unknown and preserve existing incidents. All new indexes are installed on
 * empty derived tables, never rebuilt over retained command history.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { effectiveCheckin } from './registry';

/** Same freshness gate as routing/dispatch online checks (180 s). */
export const COMMAND_CHANNEL_FRESH_MS = 180_000;
/** Offered/queued commands unclaimed beyond this window are stuck evidence. */
export const COMMAND_CHANNEL_STUCK_MS = 600_000;
/** Recent-command window: live-stuck AND terminal stop evidence older than
 * this ages out, keeping both scans inside a finite age bound. */
export const COMMAND_CHANNEL_LOOKBACK_MS = 3_600_000;

export type CommandChannelThresholds = { stuckMs: number; lookbackMs: number };
export const COMMAND_CHANNEL_THRESHOLDS: CommandChannelThresholds = {
  stuckMs: COMMAND_CHANNEL_STUCK_MS,
  lookbackMs: COMMAND_CHANNEL_LOOKBACK_MS,
};

export type CommandChannelHealth = {
  state: 'ok' | 'unreachable' | 'unknown';
  reason: 'command_channel_unreachable' | null;
  detail: string;
  stuck_unclaimed: number;
  stuck_failed_stops: number;
  oldest_stuck_age_s: number | null;
  last_channel_proof_at: string | null;
};

/** These new tables start empty: installing indexes never rebuilds the
 * retained command table. Source rows migrate only through bounded PK pages. */
export const COMMAND_CHANNEL_BACKFILL_LIMIT = 128;
const EVIDENCE_LIMIT = 128;
const COMMAND_CHANNEL_SCHEMA = `
CREATE TABLE IF NOT EXISTS command_channel_projection_state(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL,after_id TEXT NOT NULL,complete INTEGER NOT NULL,since_iso TEXT NOT NULL);
INSERT OR IGNORE INTO command_channel_projection_state VALUES(1,0,'',0,'');
CREATE TABLE IF NOT EXISTS command_channel_evidence(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,machine_id TEXT NOT NULL,generation INTEGER NOT NULL,bucket TEXT NOT NULL,kind TEXT NOT NULL,created_at TEXT NOT NULL,poll_at TEXT,not_before TEXT);
CREATE INDEX IF NOT EXISTS command_channel_evidence_host_time ON command_channel_evidence(generation,machine_id,bucket,created_at,id);
CREATE INDEX IF NOT EXISTS command_channel_evidence_age ON command_channel_evidence(created_at);
CREATE TABLE IF NOT EXISTS command_channel_proofs(machine_id TEXT NOT NULL,generation INTEGER NOT NULL,command_id TEXT NOT NULL,session_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(machine_id,generation));
CREATE INDEX IF NOT EXISTS command_channel_proofs_command ON command_channel_proofs(generation,command_id);
CREATE INDEX IF NOT EXISTS command_channel_proofs_generation ON command_channel_proofs(generation);
CREATE INDEX IF NOT EXISTS command_channel_proofs_session ON command_channel_proofs(generation,session_id);
`;
const proofPredicate = (c: string) => `(${c}.state IN ('claimed','acked','refused','uncertain') OR
  (${c}.state='failed' AND ${c}.claim_token IS NOT NULL AND json_valid(${c}.ack_json)
    AND json_extract(${c}.ack_json,'$.claim_token')=${c}.claim_token))`;
const stopPredicate = (c: string) => `(${c}.kind='stop' AND ${c}.state='failed' AND json_valid(${c}.ack_json)
  AND json_extract(${c}.ack_json,'$.reason')='stop_not_claimed')`;
const proofUpsert = `ON CONFLICT(machine_id,generation) DO UPDATE SET command_id=excluded.command_id,
  session_id=excluded.session_id,created_at=excluded.created_at
  WHERE excluded.created_at>command_channel_proofs.created_at OR
    (excluded.created_at=command_channel_proofs.created_at AND excluded.command_id>command_channel_proofs.command_id)`;
const evidenceUpsert = `ON CONFLICT(id) DO UPDATE SET session_id=excluded.session_id,machine_id=excluded.machine_id,
  generation=excluded.generation,bucket=excluded.bucket,kind=excluded.kind,created_at=excluded.created_at,
  poll_at=excluded.poll_at,not_before=excluded.not_before
  WHERE command_channel_evidence.session_id IS NOT excluded.session_id OR command_channel_evidence.machine_id IS NOT excluded.machine_id
    OR command_channel_evidence.generation IS NOT excluded.generation OR command_channel_evidence.bucket IS NOT excluded.bucket
    OR command_channel_evidence.kind IS NOT excluded.kind OR command_channel_evidence.created_at IS NOT excluded.created_at
    OR command_channel_evidence.poll_at IS NOT excluded.poll_at OR command_channel_evidence.not_before IS NOT excluded.not_before`;
const invalidate = `UPDATE command_channel_projection_state SET generation=generation+1,after_id='',complete=0 WHERE id=1`;

export function ensureCommandChannelSchema(sql: SqlStorage): void {
  if (!sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_control_commands'").toArray().length) return;
  for (const stmt of COMMAND_CHANNEL_SCHEMA.split(';')) if (stmt.trim()) sql.exec(stmt);
  for (const event of ['INSERT','UPDATE']) {
    const retract = event === 'UPDATE' ? `${invalidate} AND EXISTS(SELECT 1 FROM command_channel_proofs
      WHERE generation=command_channel_projection_state.generation AND command_id=OLD.id)
      AND (NOT ${proofPredicate('NEW')} OR OLD.session_id IS NOT NEW.session_id OR OLD.created_at IS NOT NEW.created_at);` : '';
    sql.exec(`CREATE TRIGGER IF NOT EXISTS command_channel_project_${event.toLowerCase()} AFTER ${event} ON session_control_commands BEGIN
      ${retract}
      DELETE FROM command_channel_evidence WHERE id=NEW.id AND NOT(NEW.state IN ('recorded','transported') OR ${stopPredicate('NEW')});
      INSERT INTO command_channel_evidence
        SELECT NEW.id,NEW.session_id,s.machine_id,p.generation,
          CASE WHEN NEW.state IN ('recorded','transported') THEN 'pending' ELSE 'failed_stop' END,
          NEW.kind,NEW.created_at,NEW.poll_at,NEW.not_before
        FROM session_control_sessions s CROSS JOIN command_channel_projection_state p
        WHERE s.id=NEW.session_id AND s.machine_id IS NOT NULL AND p.id=1
          AND NEW.created_at>=p.since_iso AND (NEW.state IN ('recorded','transported') OR ${stopPredicate('NEW')})
        ${evidenceUpsert};
      INSERT INTO command_channel_proofs SELECT s.machine_id,p.generation,NEW.id,NEW.session_id,NEW.created_at
        FROM session_control_sessions s CROSS JOIN command_channel_projection_state p
        WHERE s.id=NEW.session_id AND s.machine_id IS NOT NULL AND p.id=1 AND NEW.created_at>=p.since_iso
          AND ${proofPredicate('NEW')} ${proofUpsert};
    END`);
  }
  sql.exec(`CREATE TRIGGER IF NOT EXISTS command_channel_project_delete AFTER DELETE ON session_control_commands BEGIN
    ${invalidate} AND EXISTS(SELECT 1 FROM command_channel_proofs WHERE generation=command_channel_projection_state.generation AND command_id=OLD.id);
    DELETE FROM command_channel_evidence WHERE id=OLD.id;
  END`);
  // The deployed UNIQUE(session_id,actor,request_key) autoindex supplies a
  // singleton existence seek. New sessions without commands cannot restart
  // the entire migration on their first placement.
  sql.exec(`CREATE TRIGGER IF NOT EXISTS command_channel_session_move AFTER UPDATE OF machine_id ON session_control_sessions
    WHEN OLD.machine_id IS NOT NEW.machine_id AND EXISTS(
      SELECT 1 FROM session_control_commands INDEXED BY sqlite_autoindex_session_control_commands_2 WHERE session_id=NEW.id LIMIT 1)
    BEGIN ${invalidate}; END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS command_channel_session_delete AFTER DELETE ON session_control_sessions
    WHEN EXISTS(SELECT 1 FROM command_channel_proofs WHERE generation=(SELECT generation FROM command_channel_projection_state WHERE id=1) AND session_id=OLD.id)
    BEGIN ${invalidate}; END`);
}

/** Advance a durable, fair source cursor. Triggers cover writes on either
 * side of this cursor. Partial migration is unknown, never healthy/unhealthy. */
function advanceProjection(sql: SqlStorage, since: string): boolean {
  // The materialization watermark advances once per minute, while health
  // applies its exact cutoff. Host placement polls cannot write it every call.
  since = new Date(Math.floor(Date.parse(since) / 60000) * 60000).toISOString();
  let state: any = sql.exec('SELECT * FROM command_channel_projection_state WHERE id=1').toArray()[0];
  if (!state) return false;
  if (since < state.since_iso) {
    sql.exec(invalidate);
    state = sql.exec('SELECT * FROM command_channel_projection_state WHERE id=1').toArray()[0];
  }
  if (since !== state.since_iso) sql.exec('UPDATE command_channel_projection_state SET since_iso=? WHERE id=1', since);
  // Prune derived rows only, in bounded ranges. The source is untouched.
  sql.exec(`DELETE FROM command_channel_evidence WHERE id IN
    (SELECT id FROM command_channel_evidence WHERE created_at<? LIMIT 64)`, since);
  sql.exec(`DELETE FROM command_channel_evidence WHERE id IN
    (SELECT id FROM command_channel_evidence WHERE generation<? LIMIT 64)`, state.generation);
  sql.exec(`DELETE FROM command_channel_proofs WHERE (machine_id,generation) IN
    (SELECT machine_id,generation FROM command_channel_proofs WHERE generation<? LIMIT 64)`, state.generation);
  if (state.complete) return true;
  const source = sql.exec(`SELECT c.*,s.machine_id AS channel_machine FROM session_control_commands c
    LEFT JOIN session_control_sessions s ON s.id=c.session_id
    WHERE c.id>? ORDER BY c.id LIMIT ?`, state.after_id, COMMAND_CHANNEL_BACKFILL_LIMIT + 1).toArray() as any[];
  const page = source.slice(0, COMMAND_CHANNEL_BACKFILL_LIMIT);
  for (const row of page) {
    if (typeof row.channel_machine !== 'string' || row.created_at < since) continue;
    let ack: any = null;
    try { ack = JSON.parse(row.ack_json); } catch {}
    if (['recorded','transported'].includes(row.state) || (row.kind==='stop' && row.state==='failed' && ack?.reason==='stop_not_claimed')) {
      sql.exec(`INSERT INTO command_channel_evidence VALUES(?,?,?,?,?,?,?,?,?) ${evidenceUpsert}`,
        row.id,row.session_id,row.channel_machine,state.generation,['recorded','transported'].includes(row.state)?'pending':'failed_stop',
        row.kind,row.created_at,row.poll_at,row.not_before);
    }
    if (['claimed','acked','refused','uncertain'].includes(row.state) || (row.state==='failed' && row.claim_token!==null && ack?.claim_token===row.claim_token))
      sql.exec(`INSERT INTO command_channel_proofs VALUES(?,?,?,?,?) ${proofUpsert}`,
        row.channel_machine,state.generation,row.id,row.session_id,row.created_at);
  }
  const complete = source.length <= COMMAND_CHANNEL_BACKFILL_LIMIT;
  sql.exec('UPDATE command_channel_projection_state SET after_id=?,complete=? WHERE id=1', page.at(-1)?.id ?? state.after_id, complete?1:0);
  return complete;
}

const ISO = '';
/** All machine and eligibility filters follow a bounded evidence prefix.
 * Session joins are primary-key lookups and cannot scan missing/orphan rows
 * indefinitely while looking for LIMIT eligible results. */
export const COMMAND_CHANNEL_PLAN_SQL = {
  proof: `SELECT p.created_at AS p,s.machine_id AS current_machine FROM command_channel_proofs p
    LEFT JOIN session_control_sessions s ON s.id=p.session_id
    WHERE p.generation=(SELECT generation FROM command_channel_projection_state WHERE id=1) AND p.created_at>=? AND p.machine_id=?`,
  stuck: `SELECT c.*,s.state AS session_state,s.machine_id AS current_machine FROM (
    SELECT * FROM command_channel_evidence INDEXED BY command_channel_evidence_host_time
    WHERE generation=(SELECT generation FROM command_channel_projection_state WHERE id=1)
      AND machine_id=? AND bucket='pending' AND created_at<=? AND created_at>=? AND created_at>? AND (? IS NOT NULL)
    ORDER BY created_at,id LIMIT ${EVIDENCE_LIMIT + 1}) c LEFT JOIN session_control_sessions s ON s.id=c.session_id`,
  failedStops: `SELECT c.*,s.machine_id AS current_machine FROM (
    SELECT * FROM command_channel_evidence INDEXED BY command_channel_evidence_host_time
    WHERE generation=(SELECT generation FROM command_channel_projection_state WHERE id=1)
      AND created_at>=? AND created_at>? AND machine_id=? AND bucket='failed_stop'
    ORDER BY created_at DESC,id DESC LIMIT ${EVIDENCE_LIMIT + 1}) c LEFT JOIN session_control_sessions s ON s.id=c.session_id`,
};
function newestProofAt(sql: SqlStorage, machineId: string, sinceIso: string): string | null {
  const row: any = sql.exec(COMMAND_CHANNEL_PLAN_SQL.proof, sinceIso, machineId).toArray()[0];
  if (row && row.current_machine !== machineId) throw new Error('command projection binding changed');
  return typeof row?.p==='string' && Number.isFinite(Date.parse(row.p)) ? row.p : null;
}

/** Bounded per-host derivation. `machine` carries at least id,
 * last_checkin_at and drained; unknown (never unhealthy) unless the check-in
 * is fresh and the Hub demonstrably delivered to this host. */
export function commandChannelHealth(
  sql: SqlStorage,
  machine: { id: string; last_checkin_at?: unknown; drained?: unknown } | null | undefined,
  now: number,
  thresholds: CommandChannelThresholds = COMMAND_CHANNEL_THRESHOLDS,
): CommandChannelHealth {
  const unknown = (detail: string): CommandChannelHealth =>
    ({ state: 'unknown', reason: null, detail, stuck_unclaimed: 0, stuck_failed_stops: 0, oldest_stuck_age_s: null, last_channel_proof_at: null });
  if (!machine || typeof machine.id !== 'string' || !machine.id || machine.drained) return unknown('machine absent or drained');
  const checkin = effectiveCheckin(machine.id, machine.last_checkin_at);
  const seen = typeof checkin === 'string' ? Date.parse(checkin) : NaN;
  if (!Number.isFinite(seen) || now - seen > COMMAND_CHANNEL_FRESH_MS) {
    return unknown(`check-in stale (${Number.isFinite(seen) ? Math.round((now - seen) / 1000) + 's' : 'never'}); host liveness owns this host`);
  }
  const at = new Date(now).toISOString();
  const stuckCutoff = new Date(now - thresholds.stuckMs).toISOString();
  const lookbackCutoff = new Date(now - thresholds.lookbackMs).toISOString();
  // Fail OPEN on any evidence-read failure (missing schema in fleet-only
  // boots, unexpected storage errors): an unevaluable channel never blocks
  // placement, mirroring the projection fail-open rule (intake 4398ce2a).
  let proof: string | null, stuck: any[], failedStops: any[];
  try {
    if (!advanceProjection(sql, lookbackCutoff)) return unknown('command channel projection backfill pending');
    proof = newestProofAt(sql, machine.id, lookbackCutoff);
    // A proof older than a stuck command does not supersede it; '' (empty
    // string) sorts before every ISO timestamp when no proof exists.
    const proofFloor = proof ?? ISO;
    // Finite age bound on both sides: evidence must be recent (lookback) and
    // old enough to be stuck; the pending partial index serves the range and
    // the created_at order, so the scan stays window-bounded and sort-free.
    stuck = sql.exec(COMMAND_CHANNEL_PLAN_SQL.stuck, machine.id, stuckCutoff, lookbackCutoff, proofFloor, at).toArray() as any[];
    failedStops = sql.exec(COMMAND_CHANNEL_PLAN_SQL.failedStops, lookbackCutoff, proofFloor, machine.id).toArray() as any[];
    if (stuck.length>EVIDENCE_LIMIT || failedStops.length>EVIDENCE_LIMIT) return unknown('command channel evidence prefix exceeds bounded observation');
    stuck=stuck.filter(row=>row.current_machine===machine.id && ['queued','held','offered','starting','running','uncertain'].includes(row.session_state)
      && (row.not_before===null || row.not_before<=at)).slice(0,10);
    failedStops=failedStops.filter(row=>row.current_machine===machine.id).slice(0,10);
  } catch (e: any) {
    return unknown('command channel evidence unavailable: ' + String(e?.message || e).slice(0, 120));
  }
  const last_channel_proof_at = proof ?? null;
  // Corroboration: one isolated stranded command is never channel evidence
  // (a single lost poll response or a missed ack must not hold a host).
  const distinct = new Set([...stuck, ...failedStops].map(c => String(c.id))).size;
  if (!distinct) {
    return { state: 'ok', reason: null, detail: proof ? `channel proof ${proof}` : 'no pending channel evidence', stuck_unclaimed: 0, stuck_failed_stops: 0, oldest_stuck_age_s: null, last_channel_proof_at };
  }
  if (distinct < 2) {
    return { state: 'ok', reason: null, detail: `single pending command (${String(stuck[0]?.id ?? failedStops[0]?.id ?? '').slice(0, 8)}) is not corroborated; ${proof ? `channel proof ${proof}` : 'no channel proof yet'}`, stuck_unclaimed: stuck.length, stuck_failed_stops: failedStops.length, oldest_stuck_age_s: null, last_channel_proof_at };
  }
  const oldestIso = [...stuck, ...failedStops].map(c => String(c.created_at)).filter(s => Number.isFinite(Date.parse(s))).sort()[0];
  const oldest = Date.parse(oldestIso);
  const oldest_stuck_age_s = Number.isFinite(oldest) ? Math.max(0, Math.round((now - oldest) / 1000)) : null;
  // Delivery receipt (AC3): live-stuck evidence only counts when the Hub
  // delivered to this host after the oldest stuck command was created — a
  // stuck command currently carrying an offers receipt, or a later claim proof.
  // Without one, a failing Hub command endpoint (5xx/timeout)
  // with fresh check-ins must read unknown, not unreachable. Terminal stop
  // evidence is exempt: stop_not_claimed required reachability at expiry.
  if (stuck.length) {
    const delivered = stuck.some(c => typeof c.poll_at === 'string' && Date.parse(c.poll_at) > oldest) ||
      (proof !== null && proof > oldestIso);
    if (!delivered) {
      return { state: 'unknown', reason: null, detail: `${distinct} pending command(s) without a Hub delivery receipt; not counted against the host (possible Hub transport failure)`, stuck_unclaimed: stuck.length, stuck_failed_stops: failedStops.length, oldest_stuck_age_s, last_channel_proof_at };
    }
  }
  const parts = [
    ...(stuck.length ? [`${stuck.length} offered/queued command(s) unclaimed ${thresholds.stuckMs / 1000}s+`] : []),
    ...(failedStops.length ? [`${failedStops.length} stop(s) failed stop_not_claimed within ${thresholds.lookbackMs / 60000}min`] : []),
  ];
  return {
    state: 'unreachable',
    reason: 'command_channel_unreachable',
    detail: `${parts.join('; ')}; oldest ${oldest_stuck_age_s ?? 'unknown'}s; delivery receipt after oldest stuck command; last channel proof ${proof ?? 'none'}; sample ${String(stuck[0]?.id ?? failedStops[0]?.id ?? '').slice(0, 8)}`,
    stuck_unclaimed: stuck.length,
    stuck_failed_stops: failedStops.length,
    oldest_stuck_age_s,
    last_channel_proof_at,
  };
}

/** Host watchers never resolve each other's incidents: command-channel health
 * owns the `command_channel` subject kind, host_watch owns check-in liveness. */
export function watchCommandChannels(sql: SqlStorage, liveness: { setAlert: (kind: string, subject: string, problem: { state: string; detail: string } | null) => unknown }, now: number, thresholds: CommandChannelThresholds = COMMAND_CHANNEL_THRESHOLDS): any[] {
  ensureCommandChannelSchema(sql);
  const notifications: any[] = [];
  const rows = sql.exec('SELECT id,name,hostname,last_checkin_at,drained,revoked_at FROM machines ORDER BY id LIMIT 129').toArray() as any[];
  if (rows.length>128) return notifications;
  for (const m of rows) {
    if (m.revoked_at) continue;
    const health = commandChannelHealth(sql, m, now, thresholds);
    if (health.state === 'unreachable') {
      // Opens on first sight; an existing incident only refreshes its detail
      // (setAlert returns null), so a still-broken host never re-notifies.
      const opened = liveness.setAlert('command_channel', m.id, { state: 'command_channel_unreachable', detail: `${m.name || m.id}: ${health.detail}` });
      if (opened) notifications.push(opened);
    } else if (health.state === 'ok') {
      const existing: any = sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind = 'command_channel' AND subject = ? AND resolved_at IS NULL", m.id).toArray()[0];
      if (!existing) continue;
      // Only complete healthy evidence resolves an incident. A partial
      // backfill, overflow, stale host or missing receipt remains unknown.
      liveness.setAlert('command_channel', m.id, null);
      if (health.state === 'ok') {
        const recovered = liveness.setAlert('host_recovery', `command_channel:${m.id}`, { state: 'RECOVERED', detail: `${m.name || m.id}: command channel recovered; ${health.detail}; incident ${existing.id}` });
        if (recovered) notifications.push(recovered);
      }
    }
  }
  return notifications;
}
