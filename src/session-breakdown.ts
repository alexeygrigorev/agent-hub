/**
 * Live session counts with a host x engine x account breakdown (intake 93e7817b).
 *
 * Read-only projection over the canonical session store: callers pass the
 * already-projected session views (the same `state`/`stalled`/`account` the
 * session list shows), so the counts always agree with `hub sessions list`.
 * No second registry, no writes.
 *
 * Buckets:
 * - running: running, waiting_until (a scheduled wake is healthy waiting)
 * - queued:  recorded/held/queued/offered/starting (not yet a live native)
 * - stalled: stalled, idle_with_open_ac, uncertain (needs attention)
 * Terminal states (stopped/failed/cancelled/refused/exited) are not live.
 *
 * Host is the canonical machine: records sharing one physical hostname (for
 * example `hetzner` and `urgent-hetzner` on RMTHZ) resolve to the live
 * canonical record and list the other ids as aliases, so one physical host
 * is counted once.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { resolveMachineAliases } from './machine-alias';
import { effectiveCheckin } from './registry';

export const BREAKDOWN_DIMS = ['host', 'engine', 'account'] as const;
export type BreakdownDim = typeof BREAKDOWN_DIMS[number];
export type Bucket = 'running' | 'queued' | 'stalled';

/** Non-terminal stored lifecycle states (pre-projection filter). */
export const LIVE_STORED_STATES = ['recorded', 'held', 'queued', 'offered', 'starting', 'running', 'uncertain'] as const;

const RUNNING = new Set(['running', 'waiting_until', 'active']);
const QUEUED = new Set(['recorded', 'held', 'queued', 'offered', 'starting']);
const STALLED = new Set(['stalled', 'idle_with_open_ac', 'uncertain']);

export const UNPLACED_HOST = 'unplaced';
export const UNKNOWN = 'unknown';

/** Parse `by=host,engine,account`; null when any part is not a known dimension. */
export function parseBreakdownDims(raw: string | null | undefined): BreakdownDim[] | null {
  if (raw == null || raw.trim() === '') return [...BREAKDOWN_DIMS];
  const out: BreakdownDim[] = [];
  for (const part of raw.split(',').map(p => p.trim().toLowerCase()).filter(Boolean)) {
    if (!(BREAKDOWN_DIMS as readonly string[]).includes(part)) return null;
    if (!out.includes(part as BreakdownDim)) out.push(part as BreakdownDim);
  }
  return out.length ? out : null;
}

/** Bucket for one projected session view, or null when it is not live. */
export function sessionBucket(view: { state?: unknown; stalled?: unknown }): Bucket | null {
  const state = typeof view.state === 'string' ? view.state : '';
  if (view.stalled === true || STALLED.has(state)) return 'stalled';
  if (RUNNING.has(state)) return 'running';
  if (QUEUED.has(state)) return 'queued';
  return null;
}

export type HostInfo = { id: string; label: string; hostname: string | null; aliases: { id: string; label: string }[] };

/** Canonical host per machine id plus display labels (name when unique, else id). */
export function machineHosts(sql: SqlStorage): { canonical: (id: string) => string; info: Map<string, HostInfo> } {
  let rows: any[] = [];
  try { rows = sql.exec('SELECT id, name, hostname, last_checkin_at, drained FROM machines ORDER BY id').toArray() as any[]; } catch { rows = []; }
  const counts = new Map<string, number>();
  for (const m of rows) if (m.name) counts.set(String(m.name), (counts.get(String(m.name)) || 0) + 1);
  const labelOf = (m: any) => m.name && counts.get(String(m.name)) === 1 ? String(m.name) : String(m.id);
  let aliasOf = new Map<string, string>();
  try {
    aliasOf = resolveMachineAliases(rows.map(m => {
      const best = effectiveCheckin(String(m.id), m.last_checkin_at);
      const seen = typeof best === 'string' ? Date.parse(best) : NaN;
      return { id: String(m.id), hostname: m.hostname, drained: m.drained, last_seen_ms: Number.isFinite(seen) ? seen : null };
    })).aliasOf;
  } catch { aliasOf = new Map(); }
  const info = new Map<string, HostInfo>();
  for (const m of rows) {
    const id = String(m.id);
    if (aliasOf.has(id)) continue;
    info.set(id, { id, label: labelOf(m), hostname: typeof m.hostname === 'string' && m.hostname.trim() ? m.hostname.trim() : null, aliases: [] });
  }
  for (const m of rows) {
    const canonicalId = aliasOf.get(String(m.id));
    if (canonicalId) info.get(canonicalId)?.aliases.push({ id: String(m.id), label: labelOf(m) });
  }
  return { canonical: (id: string) => aliasOf.get(id) ?? id, info };
}

type Counts = { running: number; queued: number; stalled: number; total: number };
const zero = (): Counts => ({ running: 0, queued: 0, stalled: 0, total: 0 });
const add = (c: Counts, b: Bucket) => { c[b]++; c.total++; };

export type BreakdownInput = { machine?: unknown; engine?: unknown; account?: unknown; state?: unknown; stalled?: unknown };

export function buildSessionBreakdown(sql: SqlStorage, views: BreakdownInput[], dims: BreakdownDim[],
  meta: { now_ms: number; scanned: number; truncated: boolean }) {
  const hosts = machineHosts(sql);
  const text = (v: unknown) => typeof v === 'string' && v.trim() ? v.trim() : null;
  const groups = new Map<string, { host: string | null; engine: string | null; account: string | null } & Counts>();
  const perHost = new Map<string, Counts>();
  const perEngine = new Map<string, Counts>();
  const perAccount = new Map<string, Counts>();
  const totals = zero();
  const bump = (map: Map<string, Counts>, key: string, b: Bucket) => { const c = map.get(key) ?? zero(); add(c, b); map.set(key, c); };
  for (const view of views) {
    const bucket = sessionBucket(view);
    if (!bucket) continue;
    const machine = text(view.machine);
    const host = machine ? hosts.canonical(machine) : UNPLACED_HOST;
    const engine = text(view.engine) ?? UNKNOWN;
    const account = text(view.account) ?? UNKNOWN;
    add(totals, bucket);
    bump(perHost, host, bucket); bump(perEngine, engine, bucket); bump(perAccount, account, bucket);
    const key = { host: dims.includes('host') ? host : null, engine: dims.includes('engine') ? engine : null, account: dims.includes('account') ? account : null };
    const id = JSON.stringify([key.host, key.engine, key.account]);
    const row = groups.get(id) ?? { ...key, ...zero() };
    add(row, bucket);
    groups.set(id, row);
  }
  const hostView = (id: string) => {
    const h = hosts.info.get(id);
    return { host_label: h?.label ?? id, hostname: h?.hostname ?? null, host_aliases: h?.aliases ?? [] };
  };
  const rows = [...groups.values()].map(r => ({ ...r, ...(r.host !== null ? hostView(r.host) : {}) }))
    .sort((a, b) => b.total - a.total || String(a.host).localeCompare(String(b.host)) ||
      String(a.engine).localeCompare(String(b.engine)) || String(a.account).localeCompare(String(b.account)));
  const list = (map: Map<string, Counts>, name: string) => [...map.entries()]
    .map(([k, c]) => ({ [name]: k, ...c })).sort((a: any, b: any) => b.total - a.total || String(a[name]).localeCompare(String(b[name])));
  return {
    view: 'breakdown', by: dims, generated_at: new Date(meta.now_ms).toISOString(),
    buckets: { running: ['running', 'waiting_until'], queued: [...QUEUED], stalled: [...STALLED] },
    totals, rows,
    hosts: list(perHost, 'host').map((h: any) => ({ ...h, ...hostView(h.host) })),
    engines: list(perEngine, 'engine'),
    accounts: list(perAccount, 'account'),
    scanned: meta.scanned, truncated: meta.truncated,
  };
}
