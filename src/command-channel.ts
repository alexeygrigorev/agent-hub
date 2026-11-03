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
 * Bounded by construction: every evidence scan is a partial created_at index
 * scan inside a finite age window (proof and failed stops: lookback; live
 * stuck: [now-lookback, now-stuckMs] with index-ordered output, no sort) and
 * nothing here runs on host-polled endpoints; plan() memoizes per machine.
 * COMMAND_CHANNEL_PLAN_SQL is EXPLAIN QUERY PLAN-tested (index used, no temp
 * sort, no bare table scan).
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

/** Partial indexes keep every evidence scan inside a finite age window; the
 * pending index also satisfies the stuck scan's created_at order (no sort). */
const COMMAND_CHANNEL_SCHEMA = `
CREATE INDEX IF NOT EXISTS session_control_commands_proof_created_v2 ON session_control_commands(created_at) WHERE state IN ('claimed','acked','refused','uncertain','failed');
CREATE INDEX IF NOT EXISTS session_control_commands_failed_stops ON session_control_commands(created_at) WHERE kind = 'stop' AND state = 'failed';
CREATE INDEX IF NOT EXISTS session_control_commands_pending_created ON session_control_commands(created_at) WHERE state IN ('recorded','transported');
`;

export function ensureCommandChannelSchema(sql: SqlStorage): void {
  // The partial indexes hang off the session-control command table; fleet-only
  // boots and fixtures may not have created it yet. Session-control init (or a
  // later watch cycle) completes the schema.
  if (!sql.exec("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_control_commands'").toArray().length) return;
  for (const stmt of COMMAND_CHANNEL_SCHEMA.split(';')) if (stmt.trim()) sql.exec(stmt);
}

const ISO = '';
/** States that are only reachable through a successful claim: channel proof.
 * Inlined as SQL literals (never bound parameters): the partial-index scan
 * requires SQLite to prove the index WHERE clause at planning time. */
const PROOF_STATES_SQL = "('claimed','acked','refused','uncertain','failed')";

/** The exact evidence queries, exported for EXPLAIN QUERY PLAN tests (review
 * cc84ca6f finding 3): each must stay index-bounded as history grows. */
export const COMMAND_CHANNEL_PLAN_SQL = {
  proof: `SELECT MAX(c.created_at) AS p FROM session_control_commands c INDEXED BY session_control_commands_proof_created_v2
     CROSS JOIN session_control_sessions s ON s.id = c.session_id
     WHERE c.state IN ${PROOF_STATES_SQL} AND (c.state != 'failed' OR json_extract(c.ack_json, '$.claim_token') = c.claim_token)
       AND c.created_at >= ? AND s.machine_id = ?`,
  stuck: `SELECT c.id, c.kind, c.created_at, c.poll_at FROM session_control_commands c INDEXED BY session_control_commands_pending_created
     CROSS JOIN session_control_sessions s ON s.id = c.session_id
     WHERE c.state IN ('recorded','transported') AND s.machine_id = ?
       AND s.state IN ('queued','held','offered','starting','running','uncertain')
       AND c.created_at <= ? AND c.created_at >= ? AND c.created_at > ?
       AND (c.not_before IS NULL OR c.not_before <= ?)
     ORDER BY c.created_at LIMIT 10`,
  failedStops: `SELECT c.id, c.created_at FROM session_control_commands c INDEXED BY session_control_commands_failed_stops
     CROSS JOIN session_control_sessions s ON s.id = c.session_id
     WHERE c.kind = 'stop' AND c.state = 'failed'
       AND json_extract(c.ack_json, '$.reason') = 'stop_not_claimed'
       AND c.created_at >= ? AND c.created_at > ?
       AND +s.machine_id = ?
     ORDER BY c.created_at DESC LIMIT 10`,
};

/** Latest claim-or-later command creation time on this host within the
 * lookback window, or null when the host has no recent channel proof. */
function newestProofAt(sql: SqlStorage, machineId: string, sinceIso: string): string | null {
  const row = sql.exec(COMMAND_CHANNEL_PLAN_SQL.proof, sinceIso, machineId).toArray()[0];
  const p = row?.p;
  return typeof p === 'string' && Number.isFinite(Date.parse(p)) ? p : null;
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
    proof = newestProofAt(sql, machine.id, lookbackCutoff);
    // A proof older than a stuck command does not supersede it; '' (empty
    // string) sorts before every ISO timestamp when no proof exists.
    const proofFloor = proof ?? ISO;
    // Finite age bound on both sides: evidence must be recent (lookback) and
    // old enough to be stuck; the pending partial index serves the range and
    // the created_at order, so the scan stays window-bounded and sort-free.
    stuck = sql.exec(COMMAND_CHANNEL_PLAN_SQL.stuck, machine.id, stuckCutoff, lookbackCutoff, proofFloor, at).toArray() as any[];
    failedStops = sql.exec(COMMAND_CHANNEL_PLAN_SQL.failedStops, lookbackCutoff, proofFloor, machine.id).toArray() as any[];
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
  const rows = sql.exec('SELECT id, name, hostname, last_checkin_at, drained FROM machines WHERE revoked_at IS NULL').toArray() as any[];
  for (const m of rows) {
    const health = commandChannelHealth(sql, m, now, thresholds);
    if (health.state === 'unreachable') {
      // Opens on first sight; an existing incident only refreshes its detail
      // (setAlert returns null), so a still-broken host never re-notifies.
      const opened = liveness.setAlert('command_channel', m.id, { state: 'command_channel_unreachable', detail: `${m.name || m.id}: ${health.detail}` });
      if (opened) notifications.push(opened);
    } else {
      const existing: any = sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind = 'command_channel' AND subject = ? AND resolved_at IS NULL", m.id).toArray()[0];
      if (!existing) continue;
      // Recovered (fresh proof) or unjudgeable (check-in stale or missing
      // delivery receipt) — host liveness or the Hub transport owns those.
      liveness.setAlert('command_channel', m.id, null);
      if (health.state === 'ok') {
        const recovered = liveness.setAlert('host_recovery', `command_channel:${m.id}`, { state: 'RECOVERED', detail: `${m.name || m.id}: command channel recovered; ${health.detail}; incident ${existing.id}` });
        if (recovered) notifications.push(recovered);
      }
    }
  }
  return notifications;
}
