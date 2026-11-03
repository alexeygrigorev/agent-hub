import { initUsageStore } from './usage-store';
/** Numeric, bounded operational history. Never stores prompts or raw heartbeat DTOs.
 * Source tables are read only. Compaction/expiry applies only to this derived cache.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { FleetService } from './fleet';
import { dedupeAccountsByCanonical } from './quota';
import { resolveMachineAliases } from './machine-alias';
import { isValidMachineId } from './machine-id';
import { effectiveCheckin } from './registry';
import type { WriteWatch } from './write-watch';

const MINUTE = 60000, DAY = 86400000;
export const HISTORY_SCAN_LIMIT = 128;
export const HISTORY_COMPACT_WINDOWS = 8;
export const HISTORY_RETENTION_MS = 32 * DAY;
/** Quota freshness gate mirrors QUOTA_STALE_MS (20 minutes). Quota collection
 * runs every 600 s host-side; a snapshot older than this is stale and must
 * stay a gap, never a held-forward value. Sampling runs at most once a
 * minute, so live history holds points at least every 15 minutes. */
export const QUOTA_WINDOW_STALE_MS = 20 * MINUTE;
export const HISTORY_RANGES: Record<string, number> = { '1h': 60 * MINUTE, '24h': DAY, '7d': 7 * DAY, '30d': 30 * DAY };
export const HOST_HISTORY_SCHEMA = `
CREATE TABLE IF NOT EXISTS resource_history (
 bucket_ms INTEGER PRIMARY KEY,payload_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS resource_history_entities (
 kind TEXT NOT NULL,entity TEXT NOT NULL,machine_id TEXT NOT NULL,
 last_seen_ms INTEGER NOT NULL,PRIMARY KEY(kind,entity,machine_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS resource_history_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS resource_history_entities_seen ON resource_history_entities(last_seen_ms);
CREATE TABLE IF NOT EXISTS token_hours_posted (
  entity TEXT NOT NULL, hour_ms INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(entity, hour_ms)
) WITHOUT ROWID;
`;
type Values = Record<string, number | null>;
const numeric = (v: unknown): number | null => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;
const json = (v: any) => { try { return JSON.parse(v || '{}'); } catch { return {}; } };

type HistoryResponse = {series: any[];[key: string]: any};
type Observation = {kind: string;entity: string;machine_id: string;at: number;values: Values;source: string;recorded_at?: number};
export class HostHistoryService {
  private pending = new Map<number, Map<string, Observation>>();
  private cache = new Map<string, {at: number; data: HistoryResponse}>();
  private identity(o: {kind:string;entity:string;machine_id:string}) { return JSON.stringify([o.kind,o.entity,o.machine_id]); }
  private packet(raw: string): Observation[] { const p = json(raw); return Array.isArray(p) ? p : []; }
  private writes: WriteWatch | null = null;
  private lastSampleStamp: string | null = null;
  constructor(private sql: SqlStorage, private fleet: FleetService, private now = Date.now, writes?: WriteWatch) {
    this.writes = writes ?? null;
    if (writes) writes.trackAll(['host_health_observations','disk_observations','fleet_accounts','fleet_launches','fleet_sessions_meta','machines','session_resource_usage','resource_resident_samples','usage_quota_history','server_quota_snapshots','fleet_subscriptions']);
  }
  init() { initUsageStore(this.sql); for (const s of HOST_HISTORY_SCHEMA.split(';')) if (s.trim()) this.sql.exec(s); }
  /** One real observation per minute; retries and backfill cannot overwrite newer evidence. */
  record(kind: string, entity: string, machine: string, at: number, values: Values, source: string) {
    if (!Number.isFinite(at) || at > this.now() + MINUTE || at < this.now() - HISTORY_RETENTION_MS) return;
    entity = entity.slice(0, 256);
    const safe: Values = {};
    for (const [key, value] of Object.entries(values)) safe[key] = numeric(value);
    if (!Object.keys(safe).length) return;
    const known = this.rows('SELECT last_seen_ms FROM resource_history_entities WHERE kind=? AND entity=? AND machine_id=?', kind, entity, machine)[0];
    if (!known && Number(this.rows('SELECT COUNT(*) AS n FROM (SELECT 1 FROM resource_history_entities LIMIT 512)')[0].n) >= 512) {
      this.sql.exec("INSERT OR IGNORE INTO resource_history_meta VALUES('dimension_limit',?)", this.now());
      return;
    }
    // The dimension registry changes at most daily, not for every metric/minute.
    if (!known) this.sql.exec('INSERT INTO resource_history_entities VALUES(?,?,?,?)', kind, entity, machine, at);
    else if (at - Number(known.last_seen_ms) >= DAY) this.sql.exec('UPDATE resource_history_entities SET last_seen_ms=? WHERE kind=? AND entity=? AND machine_id=?', at, kind, entity, machine);
    const bucket = Math.floor(at / MINUTE) * MINUTE;
    let packet = this.pending.get(bucket);
    if (!packet) { packet = new Map(); this.pending.set(bucket, packet); }
    const observation = {kind,entity,machine_id:machine,at,values:safe,source,
      ...(kind==='tokens' ? {recorded_at:this.now()} : {})};
    const key = this.identity(observation), existing = packet.get(key);
    if (!existing || at > existing.at) packet.set(key, observation);
  }
  /** One write per affected minute packet, independent of host/account count. */
  flush() {
    if (!this.pending.size) return;
    for (const [bucket, pending] of this.pending) {
      const old = this.rows('SELECT payload_json FROM resource_history WHERE bucket_ms=?', bucket)[0];
      const packet = new Map(this.packet(old?.payload_json).map(o=>[this.identity(o),o]));
      let changed = false;
      for (const [key, o] of pending) {
        const previous = packet.get(key);
        if (!previous || o.at > previous.at) { packet.set(key,o); changed = true; }
      }
      if (changed) this.sql.exec('INSERT INTO resource_history VALUES(?,?) ON CONFLICT(bucket_ms) DO UPDATE SET payload_json=excluded.payload_json', bucket, JSON.stringify([...packet.values()]));
    }
    this.pending.clear(); this.cache.clear();
  }
  private rows(query: string, ...args: any[]): any[] { return this.sql.exec(query, ...args).toArray(); }
  private host(row: any) {
    const p = json(row.payload_json);
    const memoryKnown = p.memory?.status !== 'unknown' && !p.memory?.error && p.memory?.total_bytes > 0;
    const loadKnown = p.load?.status !== 'unknown' && !p.load?.error;
    this.record('host', row.machine_id, row.machine_id, Number(row.observed_at_ms), {
      memory_used_gib: !memoryKnown || numeric(p.memory?.used_bytes) === null ? null : p.memory.used_bytes / 1073741824,
      memory_available_gib: !memoryKnown || numeric(p.memory?.available_bytes) === null ? null : p.memory.available_bytes / 1073741824,
      load1: loadKnown ? numeric(p.load?.load1) : null, load15: loadKnown ? numeric(p.load?.load15) : null,
    }, 'host_health_observations');
  }
  private disk(row: any) {
    for (const fs of (json(row.payload_json).filesystems || []).slice(0, 32)) {
      if (typeof fs.mount_point !== 'string' || fs.error || fs.status === 'error') continue;
      this.record('disk', fs.mount_point, row.machine_id, Number(row.observed_at_ms), { percent: numeric(fs.percent) }, 'disk_observations');
    }
  }
  /** Per-account AND per-window quota history (AC1: usage-history intake).
   * One line per window: metric `<window>` carries percent_remaining (null
   * stays a gap, never an invented value); metric `<window>__reset_at`
   * carries the window reset time as epoch milliseconds (null when unknown).
   * The UI derives reset markers from reset_at changes. Entity stays the
   * stored account label so old rows join renamed accounts via the display
   * mapping (sibling intake 9c6d2c0f); stored keys are never rewritten here.
   */
  private quotaWindowFromBlob(account: string, quotaJson: string, at: number, source: string) {
    const p = json(quotaJson);
    if (p && typeof p === 'object' && ((Object.hasOwn(p,'status') && p.status !== 'ok') || p.details?.stale === true)) return;
    const windows = (p && typeof p === 'object' && (p as any).windows && typeof (p as any).windows === 'object') ? (p as any).windows : {};
    const names = Object.keys(windows).slice(0, 32);
    // No windows: record nothing. A gap is the absence of points, and an
    // explicit null marker would only consume the dimension registry.
    if (!names.length) return;
    const values: Values = {};
    for (const name of names) {
      const w = windows[name] || {};
      const remaining = w.present === true ? numeric(w.percent_remaining) : null;
      if (remaining !== null) values[name] = remaining;
      const resetMs = typeof w.reset_at === 'string' ? Date.parse(w.reset_at) : NaN;
      const reset = Number.isFinite(resetMs) && resetMs >= 0 ? resetMs : null;
      if (reset !== null) values[name + '__reset_at'] = reset;
      // A fully unknown window (null percent, unknown reset) leaves no
      // trace; its gap after the last known point is the outage signal.
    }
    // record() keeps one observation per account and timestamp. All windows
    // must share that observation or later windows at the same instant vanish.
    if (Object.keys(values).length) this.record('quota_window', account, '', at, values, source);
  }
  /** Fresh per-window observations from every quota source. Stale snapshots
   * (older than QUOTA_WINDOW_STALE_MS) are skipped: a stale hold-forward
   * would masquerade as a live measurement. Unknown windows stay gaps. */
  private quotaWindows(now: number) {
    // The same persisted collection journal feeds TBM; minute packets below
    // are only the bounded rendering/compaction projection of those samples.
    for (const row of this.rows('SELECT account,observed_at_ms,quota_json FROM usage_quota_history WHERE observed_at_ms>=? AND observed_at_ms<=? ORDER BY observed_at_ms LIMIT 4096',now-QUOTA_WINDOW_STALE_MS,now)) {
      this.quotaWindowFromBlob(row.account,row.quota_json,row.observed_at_ms,'usage_quota_history');
    }
    for (const row of this.rows('SELECT account,captured_at,quota_json FROM server_quota_snapshots LIMIT 128')) {
      const at = Date.parse(row.captured_at);
      if (!Number.isFinite(at) || now - at > QUOTA_WINDOW_STALE_MS || at > now + MINUTE) continue;
      this.quotaWindowFromBlob(String(row.account || ''), row.quota_json, at, 'server_quota_snapshots');
    }
    for (const row of this.rows('SELECT account,observed_at,quota_json FROM fleet_subscriptions LIMIT 4096')) {
      const at = Date.parse(row.observed_at);
      if (!Number.isFinite(at) || now - at > QUOTA_WINDOW_STALE_MS || at > now + MINUTE) continue;
      this.quotaWindowFromBlob(String(row.account || ''), row.quota_json, at, 'fleet_subscriptions');
    }
  }
  /** Import only surviving measurements, preserving their original timestamps.
   * Latest-only tables cannot reconstruct earlier memory/quota and stay gaps.
   * Fleet failures are historical events, not invented past running counts.
   */
  backfill() {
    if (this.rows("SELECT value FROM resource_history_meta WHERE key='backfill_v1'").length) return;
    const cutoff = this.now() - HISTORY_RETENTION_MS;
    const machines = this.rows('SELECT id FROM machines ORDER BY id LIMIT ?', HISTORY_SCAN_LIMIT + 1);
    if (machines.length > HISTORY_SCAN_LIMIT) this.flag('backfill_truncated');
    for (const machine of machines.slice(0, HISTORY_SCAN_LIMIT)) {
      const disks = this.rows(`SELECT machine_id,observed_at_ms,payload_json FROM disk_observations INDEXED BY idx_disk_obs_machine
        WHERE machine_id=? AND observed_at_ms>=? ORDER BY observed_at_ms LIMIT ?`, machine.id, cutoff, HISTORY_SCAN_LIMIT + 1);
      if (disks.length > HISTORY_SCAN_LIMIT) this.flag('backfill_truncated');
      for (const row of disks.slice(0, HISTORY_SCAN_LIMIT)) this.disk(row);
      const host = this.rows('SELECT machine_id,observed_at_ms,payload_json FROM host_health_observations WHERE machine_id=?', machine.id)[0];
      if (host) this.host(host);
    }
    // Existing lifecycle rows establish failure timestamps without guessing when
    // a queued/starting session actually began running. Bucket counts are events.
    // Read a bounded raw time-index range before filtering/grouping. LIMIT
    // outside GROUP BY would still visit every historical launch. An overflow
    // is explicitly unknown, never a prefix presented as an exact event count.
    const launches = this.rows(`SELECT machine_id,state,updated_at FROM fleet_launches INDEXED BY fleet_launch_updated
      WHERE updated_at>=? ORDER BY updated_at LIMIT ?`, new Date(cutoff).toISOString(), HISTORY_SCAN_LIMIT + 1);
    const overflow = launches.length > HISTORY_SCAN_LIMIT;
    if (overflow) this.flag('backfill_truncated');
    const events = new Map<string, { machine: string; at: number; count: number }>();
    for (const row of launches.slice(0, HISTORY_SCAN_LIMIT)) {
      const at = Math.floor(Date.parse(row.updated_at) / MINUTE) * MINUTE;
      if (row.state !== 'failed' || !Number.isFinite(at)) continue;
      const machine = String(row.machine_id || ''), key = JSON.stringify([machine, at]);
      const previous = events.get(key);
      events.set(key, { machine, at, count: (previous?.count ?? 0) + 1 });
    }
    for (const event of events.values()) this.record('failures', 'fleet', event.machine, event.at,
      { failed_events: overflow ? null : event.count }, 'fleet_launches_bounded_backfill');
    for (const row of this.rows('SELECT machine_id,account,observed_at,quota_json FROM fleet_subscriptions LIMIT 4096')) {
      const p = json(row.quota_json);
      const values = Object.values(p.windows || {}).map((w: any) => numeric(w.percent_remaining)).filter((v): v is number => v !== null);
      if (values.length) this.record('quota', row.account, '', Date.parse(row.observed_at), { remaining_percent: Math.min(...values) }, 'fleet_subscriptions');
      const at = Date.parse(row.observed_at);
      if (Number.isFinite(at)) this.quotaWindowFromBlob(String(row.account || ''), row.quota_json, at, 'fleet_subscriptions');
    }
    for (const row of this.rows('SELECT account,captured_at,quota_json FROM server_quota_snapshots LIMIT 128')) {
      const p = json(row.quota_json);
      const values = Object.values(p.windows || {}).map((w: any) => numeric(w.percent_remaining)).filter((v): v is number => v !== null);
      if (values.length) this.record('quota', row.account, '', Date.parse(row.captured_at), { remaining_percent: Math.min(...values) }, 'server_quota_snapshots');
      this.quotaWindowFromBlob(String(row.account || ''), row.quota_json, Date.parse(row.captured_at), 'server_quota_snapshots');
    }
    this.flush();
    this.sql.exec("INSERT OR REPLACE INTO resource_history_meta VALUES('backfill_v1',?)", this.now());
  }
  /** Called by the existing Durable Object alarm and after successful host writes.
   * Minute idempotency prevents page refreshes/poll frequency from inflating history.
   */
  sample() {
    const now = this.now();
    let sampleStamp: string | null = null;
    // Source-driven sampling: unchanged evidence cannot create a new sample.
    // Quota journals are dependencies too, so new quota samples invalidate
    // the stamp without a timer-driven full census of an idle fleet.
    if (this.writes) {
      sampleStamp = this.writes.stamp('host_health_observations','disk_observations','fleet_accounts','fleet_launches','fleet_sessions_meta','machines','session_resource_usage','resource_resident_samples','usage_quota_history','server_quota_snapshots','fleet_subscriptions');
      if (sampleStamp === this.lastSampleStamp) return;
    }
    this.backfill();
    const last = this.rows("SELECT value FROM resource_history_meta WHERE key='sample'")[0]?.value;
    if (typeof last === 'number' && now - last < MINUTE) return;
    for (const machine of this.rows('SELECT id FROM machines ORDER BY id LIMIT 128')) {
      const host = this.rows('SELECT machine_id,observed_at_ms,payload_json FROM host_health_observations WHERE machine_id=?', machine.id)[0];
      if (host) this.host(host);
      const row = this.rows(`SELECT machine_id,observed_at_ms,payload_json FROM disk_observations
        INDEXED BY idx_disk_obs_machine WHERE machine_id=? ORDER BY observed_at_ms DESC LIMIT 1`, machine.id)[0];
      if (row) this.disk(row);
    }
    for (const h of this.fleet.hostResources()) {
      // Reuse the memory owner's complete reservation, including pending
      // offers. A stale heartbeat must not masquerade as fresh headroom.
      if (h.stale_telemetry) continue;
      this.record('reservation', h.machine_id, h.machine_id, Date.parse(h.observed_at), {
        available_gib: numeric(h.mem_avail_mb) === null ? null : h.mem_avail_mb / 1024,
        reserved_gib: numeric(h.reserved_mb) === null ? null : h.reserved_mb / 1024,
      }, 'fleet_memory_headroom');
    }
    // Reuse FleetService account accounting/admission; no second slot calculation.
    // Canonical identity: a rename transition can briefly hold two registry
    // rows for one pool; record the canonical one exactly once.
    for (const row of dedupeAccountsByCanonical(this.rows('SELECT * FROM fleet_accounts ORDER BY label LIMIT 128'))) {
      const a = this.fleet.cachedAccountStatus(row);
      this.record('account', a.label, '', now, { used: a.active, max: a.max_concurrency, native_live: numeric((a as any).native_live) }, 'fleet_account_status_census_v2');
      this.record('quota', a.label, '', now, { remaining_percent: a.status === 'unknown' ? null : a.remaining_percent }, 'fleet_account_status');
    }
    // Per-account AND per-window quota history, sampled with every sample
    // tick (at most once a minute; quota collection refreshes every 600 s,
    // so live series always hold points at least every 15 minutes).
    this.quotaWindows(now);
    for (const machine of this.historyMachines()) {
      this.record('sessions', 'fleet', machine, now,
        { running: machine ? this.nativeRunning(machine, now) : null,
          queued: this.launchCount(machine, 'queued'), failed: this.launchCount(machine, 'failed') }, 'fleet_catalog_and_launches_census_v2');
    }
    this.flush();
    this.sql.exec("INSERT OR REPLACE INTO resource_history_meta VALUES('sample',?)", now);
    const compacted = this.rows("SELECT value FROM resource_history_meta WHERE key='compact'")[0]?.value;
    if (!compacted || now - Number(compacted) >= 3600000) { this.compact(); this.sql.exec("INSERT OR REPLACE INTO resource_history_meta VALUES('compact',?)", now); }
    // Only a completed sample serves this source generation. A write inside
    // the minute throttle (or a failed sample) must remain pending for retry.
    this.lastSampleStamp = sampleStamp;
  }
  private nativeRunning(machine: string, now: number): number | null {
    const host = this.rows('SELECT last_checkin_at FROM machines WHERE id=?', machine)[0];
    const at = Date.parse(effectiveCheckin(machine, host?.last_checkin_at) || '');
    if (!Number.isFinite(at) || now - at > 180000 || at > now + MINUTE) return null;
    const catalog = Date.parse(this.rows('SELECT observed_at FROM fleet_catalog_seen WHERE machine_id=?', machine)[0]?.observed_at || '');
    if (!Number.isFinite(catalog) || now - catalog > 180000 || catalog > now + MINUTE) return null;
    const rows = this.rows(`SELECT 1 FROM fleet_sessions INDEXED BY fleet_session_machine_phase
      WHERE machine_id=? AND phase='running' LIMIT ?`, machine, HISTORY_SCAN_LIMIT + 1);
    return this.boundedCount(rows);
  }
  private flag(key: string) {
    this.sql.exec('INSERT OR IGNORE INTO resource_history_meta VALUES(?,?)', key, this.now());
  }
  private boundedCount(rows: any[]): number | null {
    if (rows.length > HISTORY_SCAN_LIMIT) { this.flag('sample_truncated'); return null; }
    return rows.length;
  }
  private launchCount(machine: string, state: string): number | null {
    const rows = this.rows(`SELECT 1 FROM fleet_launches INDEXED BY fleet_launch_machine_state
      WHERE machine_id=? AND state=? LIMIT ?`, machine, state, HISTORY_SCAN_LIMIT + 1);
    return this.boundedCount(rows);
  }
  /** Seek distinct launch hosts instead of scanning every duplicate index key.
   * Orphan launch hosts remain represented, capped with an explicit flag. */
  private historyMachines(): string[] {
    const registry = this.rows('SELECT id FROM machines ORDER BY id LIMIT ?', HISTORY_SCAN_LIMIT + 1);
    const ids = new Set(registry.slice(0, HISTORY_SCAN_LIMIT).map(row => String(row.id)));
    if (registry.length > HISTORY_SCAN_LIMIT) this.flag('sample_truncated');
    let previous = '';
    const blank = this.rows("SELECT 1 FROM fleet_launches INDEXED BY fleet_launch_machine_state WHERE machine_id='' LIMIT 1");
    if (blank.length) ids.add('');
    for (let i = 0; i <= HISTORY_SCAN_LIMIT; i++) {
      const row = this.rows(`SELECT machine_id FROM fleet_launches INDEXED BY fleet_launch_machine_state
        WHERE machine_id>? ORDER BY machine_id LIMIT 1`, previous)[0];
      if (!row) break;
      if (i === HISTORY_SCAN_LIMIT || (!ids.has(row.machine_id) && ids.size >= HISTORY_SCAN_LIMIT)) {
        this.flag('sample_truncated'); break;
      }
      previous = String(row.machine_id); ids.add(previous);
    }
    return [...ids].slice(0, HISTORY_SCAN_LIMIT);
  }
  nextSampleAt(): number {
    const last = Number(this.rows("SELECT value FROM resource_history_meta WHERE key='sample'")[0]?.value);
    return Number.isFinite(last) ? last + MINUTE : this.now() + MINUTE;
  }
  compact() {
    this.flush();
    const now = this.now();
    // Expiry is batched even if this DO wakes after months of downtime.
    this.sql.exec(`DELETE FROM resource_history WHERE bucket_ms IN
      (SELECT bucket_ms FROM resource_history WHERE bucket_ms<? ORDER BY bucket_ms LIMIT ?)`, now - HISTORY_RETENTION_MS, HISTORY_SCAN_LIMIT);
    this.sql.exec(`DELETE FROM resource_history_entities WHERE (kind,entity,machine_id) IN
      (SELECT kind,entity,machine_id FROM resource_history_entities INDEXED BY resource_history_entities_seen
       WHERE last_seen_ms<? ORDER BY last_seen_ms LIMIT ?)`, now - HISTORY_RETENTION_MS, HISTORY_SCAN_LIMIT);
    // Each invocation processes at most eight hourly / sixteen 5m windows.
    // The 5m tier can compact 80 minutes per hourly call, above its arrival
    // rate of 60 minute packets, so a busy history does not grow forever.
    // PK seeks skip
    // empty time spans; a window contains at most 60 minute-keyed packets.
    // The durable cursor prevents rescanning all compacted history hourly.
    for (const [age, step] of [[7 * DAY, 3600000], [2 * DAY, 5 * MINUTE]]) {
      const key = 'compact_cursor_' + step;
      let after = Number(this.rows('SELECT value FROM resource_history_meta WHERE key=?', key)[0]?.value ?? -1);
      const windows = step === 5 * MINUTE ? HISTORY_COMPACT_WINDOWS * 2 : HISTORY_COMPACT_WINDOWS;
      for (let i = 0; i < windows; i++) {
        const first = this.rows('SELECT bucket_ms FROM resource_history WHERE bucket_ms>? AND bucket_ms<? ORDER BY bucket_ms LIMIT 1', after, now - age)[0];
        if (!first) {
          // Revisit previously processed windows on the next hourly call:
          // delayed source observations can arrive with an old timestamp.
          after = -1; break;
        }
        const start = Math.floor(Number(first.bucket_ms) / step) * step;
        const rows = this.rows('SELECT bucket_ms,payload_json FROM resource_history WHERE bucket_ms>=? AND bucket_ms<? ORDER BY bucket_ms LIMIT 61', start, start + step);
        const merged = new Map<string, Observation>();
        for (const row of rows) for (const o of this.packet(row.payload_json)) {
          const id = this.identity(o), previous = merged.get(id);
          if (!previous || o.at > previous.at) merged.set(id, o);
        }
        const keep = Number(rows.at(-1)!.bucket_ms);
        if (rows.length > 1) {
          this.sql.exec('UPDATE resource_history SET payload_json=? WHERE bucket_ms=?', JSON.stringify([...merged.values()]), keep);
          this.sql.exec('DELETE FROM resource_history WHERE bucket_ms>=? AND bucket_ms<? AND bucket_ms<>?', start, start + step, keep);
        }
        after = start + step - 1;
      }
      this.sql.exec('INSERT OR REPLACE INTO resource_history_meta VALUES(?,?)', key, after);
    }
    this.cache.clear();
  }
  query(range: string, machine?: string): HistoryResponse {
    this.flush();
    const now = this.now(), start = now - HISTORY_RANGES[range];
    const cacheKey=JSON.stringify([range,machine]);
    const cached=this.cache.get(cacheKey);
    if (cached && now-cached.at<MINUTE) return cached.data;
    const step = range === '1h' ? MINUTE : range === '24h' ? 5 * MINUTE : range === '7d' ? 30 * MINUTE : 2 * 3600000;
    const machines = this.rows('SELECT id,name,hostname,drained,last_checkin_at FROM machines WHERE revoked_at IS NULL ORDER BY id LIMIT 128');
    const aliases = resolveMachineAliases(machines.map(m => ({id:String(m.id),hostname:m.hostname,drained:m.drained,last_seen_ms:Number.isFinite(Date.parse(m.last_checkin_at)) ? Date.parse(m.last_checkin_at) : null}))).aliasOf;
    const canonical = (id: string) => aliases.get(id) || id;
    const groups = new Map<string, any>();
    let truncated = false, earliest: number | null = null;
    const first = Math.floor(start / step) * step, count = Math.floor((now - first) / step) + 1;
    // Stream bounded minute/coarse packets, rather than materializing a
    // metric-row-per-minute table. A 24h read examines at most 1441 packets.
    for (const packet of this.sql.exec('SELECT payload_json FROM resource_history WHERE bucket_ms>=? AND bucket_ms<=? ORDER BY bucket_ms',Math.floor(start/3600000)*3600000,now)) {
      for (const o of this.packet(String(packet.payload_json))) {
        if (o.at<start || o.at>now) continue;
        const host=canonical(o.machine_id);
        if (machine && host && host!==canonical(machine)) continue;
        earliest=earliest===null ? o.at : Math.min(earliest,o.at);
        const entity=o.kind==='host' || o.kind==='reservation' ? host : o.entity;
        for (const [metric,value] of Object.entries(o.values)) {
          const key=JSON.stringify([o.kind,entity,host,metric]);
          let series=groups.get(key);
          // Native token observations are hourly totals, not minute gauges.
          // Preserve every hour even on 30d, and fold revisions into that hour.
          const seriesStep=o.kind==='tokens' ? 3600000 : o.kind==='quota_window' ? Math.max(step,15*MINUTE) : step;
          const seriesFirst=Math.floor(start/seriesStep)*seriesStep;
          const seriesCount=Math.floor((now-seriesFirst)/seriesStep)+1;
          if (!series) {
            if (groups.size>=256) {truncated=true;continue;}
            series={kind:o.kind,entity,machine_id:host,metric,step_ms:seriesStep,points:Array.from({length:seriesCount},(_,i)=>({at:seriesFirst+i*seriesStep,value:null,observed_at:null,...(o.kind==='quota_window' ? {bucket_at:seriesFirst+i*seriesStep} : {})})),source:o.source};
            groups.set(key,series);
          }
          const index=Math.floor((o.at-seriesFirst)/seriesStep), previous=series.points[index];
          const legacyNative = o.kind==='account' && metric==='native_live' && o.source==='fleet_account_status';
          const legacyRunning = o.kind==='sessions' && metric==='running' && o.source==='fleet_catalog_and_launches';
          const proven = legacyNative || legacyRunning ? null : numeric(value);
          // Simultaneous alias gauges must not overwrite the canonical host's
          // real running count with the stale alias's unknown/zero projection.
          const previousAt=previous.observed_at ? Date.parse(previous.recorded_at || previous.observed_at) : -Infinity;
          const currentAt=o.kind==='tokens' ? (o.recorded_at ?? o.at) : o.at;
          if (!previous.observed_at || currentAt>previousAt || (currentAt===previousAt && o.machine_id===host)) series.points[index]={at:o.kind==='tokens' ? seriesFirst+index*seriesStep : o.at,value:proven,observed_at:new Date(o.at).toISOString(),
            // Quota aliases share a grid identity even when observations are off-grid.
            ...(o.kind==='quota_window' ? {bucket_at:seriesFirst+index*seriesStep} : {}),
            ...(o.kind==='tokens' && Number.isFinite(o.recorded_at) ? {recorded_at:new Date(o.recorded_at!).toISOString()} : {})};
        }
      }
    }
    const sampleTruncated = this.rows("SELECT key FROM resource_history_meta WHERE key IN ('sample_truncated','backfill_truncated')");
    const data={truncated:truncated || sampleTruncated.length>0,sample_truncated:sampleTruncated.some(r=>r.key==='sample_truncated'),backfill_truncated:sampleTruncated.some(r=>r.key==='backfill_truncated'),dimension_limit:this.rows("SELECT value FROM resource_history_meta WHERE key='dimension_limit'").length>0,
      machines:machines.map(m=>({id:m.id,name:m.name,alias_of:aliases.get(m.id)||null})),machine_aliases:Object.fromEntries(aliases),
      range,start:new Date(start).toISOString(),end:new Date(now).toISOString(),step_ms:step,retention_days:32,
      earliest_observation:earliest===null ? null : new Date(earliest).toISOString(),coverage:'observations_only',
      note:'Gaps are unknown. Earlier memory and quota cannot be recovered from latest-only snapshots. Failed sessions is the current failed-record count; historical failure events are separate.',series:[...groups.values()]};
    if (this.cache.size>=2) this.cache.delete(this.cache.keys().next().value!);
    this.cache.set(cacheKey,{at:now,data});
    return data;
  }
  handle(request: Request, auth: any) {
    const headers = { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' };
    if (auth.error) return Response.json({ error: 'unauthorized_or_forbidden' }, { status: auth.error.status || 401, headers });
    if (!auth.caller) return Response.json({ error: 'unauthorized' }, { status: 401, headers });
    if (request.method !== 'GET') return Response.json({ error: 'method_not_allowed' }, { status: 405, headers });
    const url = new URL(request.url), range = url.searchParams.get('range') || '24h', machine = url.searchParams.get('machine') || undefined;
    if (!Object.hasOwn(HISTORY_RANGES, range) || (machine && !isValidMachineId(machine))) return Response.json({ error: 'invalid_range_or_machine' }, { status: 400, headers });
    const kinds = url.searchParams.getAll('kind');
    const usageOnly = Boolean(auth.scopes?.includes('worker:own-intake'));
    const allowed = ['quota_window', 'quota', 'tokens'];
    if (kinds.some(kind => !allowed.includes(kind))) return Response.json({error:'invalid_kind'}, {status:400, headers});
    this.sample();
    const data = this.query(range, machine);
    const selected = kinds.length ? kinds : usageOnly ? allowed : null;
    return Response.json(selected ? {...data, series:data.series.filter(s => selected.includes(s.kind))} : data, { headers });
  }
}
