/** Intake 77f492ed: expiry notifications only. No provider mutations or credentials. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { LivenessService } from './liveness';

const HOUR = 3_600_000;
const LABEL = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$/;
type Grant = { expires_at: number; label: string };
type Snapshot = { at: number; grants: Grant[] };
type Caller = { isAdmin?: boolean; kind?: string; actor?: string };

export function expiryThreshold(expiresAt: number, now: number): 'early' | 'urgent' | null {
  const remaining = expiresAt - now;
  if (!Number.isFinite(remaining) || remaining <= 0) return null;
  // A 48h window leaves 12h margin before the required >=36h warning.
  if (remaining < 24 * HOUR) return 'urgent';
  return remaining <= 48 * HOUR ? 'early' : null;
}

export function berlinDeadline(ms: number): string {
  return new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Berlin', year: 'numeric',
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hourCycle: 'h23', timeZoneName: 'short' }).format(ms) + ' (Europe/Berlin)';
}

function parseGrants(quota: any): Grant[] | null {
  // Missing/error is unknown, never proof that banked grants disappeared.
  if (!quota || quota.status !== 'ok' || quota.details?.banked_resets_error ||
      !Array.isArray(quota.details?.banked_resets)) return null;
  const rows = quota.details.banked_resets;
  if (rows.length > 128 || rows.some((g: any) => !g || typeof g.available !== 'boolean' ||
      typeof g.expires_at !== 'string' || !Number.isFinite(Date.parse(g.expires_at)) ||
      (g.label != null && typeof g.label !== 'string') || !LABEL.test(g.label || 'Full reset'))) return null;
  return rows.filter((g: any) => g.available).map((g: any) => ({
    expires_at: Date.parse(g.expires_at), label: g.label || 'Full reset',
  }));
}

export class BankedResets {
  constructor(private sql: SqlStorage, private liveness: LivenessService,
              private now: () => number = Date.now) {
    sql.exec(`CREATE TABLE IF NOT EXISTS banked_reset_grants (
      id TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL, label TEXT NOT NULL,
      expires_at INTEGER NOT NULL, source TEXT NOT NULL, active INTEGER NOT NULL,
      observed_at INTEGER NOT NULL, recorded_by TEXT)`);
    sql.exec(`CREATE TABLE IF NOT EXISTS banked_reset_notices (
      grant_id TEXT NOT NULL, threshold TEXT NOT NULL, alert_subject TEXT NOT NULL,
      created_at INTEGER NOT NULL, PRIMARY KEY(grant_id,threshold))`);
    sql.exec(`CREATE TABLE IF NOT EXISTS banked_reset_accounts (
      account TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, source TEXT NOT NULL)`);
  }

  private accounts(): any[] {
    return this.sql.exec("SELECT label,provider FROM fleet_accounts WHERE provider IN ('codex','zai') ORDER BY label").toArray();
  }

  /** Use newest successful per-account evidence, across server and registered host producers. */
  sync() {
    const now = this.now();
    const accounts = this.accounts();
    const latest = new Map<string, Snapshot>();
    const accept = (account: string, at: number, quota: any) => {
      const grants = parseGrants(quota);
      if (grants !== null && at <= now && at >= now - 20 * 60_000 && (!latest.has(account) || latest.get(account)!.at < at)) latest.set(account, { at, grants });
    };
    for (const s of this.sql.exec('SELECT account,captured_at,quota_json FROM server_quota_snapshots').toArray() as any[]) {
      try { accept(s.account, Date.parse(s.captured_at), JSON.parse(s.quota_json)); } catch { /* retain previous deadlines */ }
    }
    // The maintained host publishes account-specific probes here, including alternate homes.
    for (const s of this.sql.exec('SELECT account,observed_at,quota_json FROM fleet_subscriptions').toArray() as any[]) {
      try {
        const quota = JSON.parse(s.quota_json);
        accept(s.account, Date.parse(quota.observed_at || s.observed_at), quota);
      } catch { /* retain previous deadlines */ }
    }
    for (const s of this.sql.exec('SELECT host_id,captured_at_ms,routes_json FROM host_quota_snapshots').toArray() as any[]) {
      let routes: any;
      try { routes = JSON.parse(s.routes_json); } catch { continue; }
      const registered = this.sql.exec('SELECT DISTINCT account FROM fleet_routes WHERE machine_id=?', s.host_id).toArray() as any[];
      for (const a of accounts.filter(a => registered.some(r => r.account === a.label))) {
        // Legacy wire key explicitly identifies the second Codex account.
        const key = a.label === 'codex-orin' ? 'codex-orin' : a.provider;
        // An unlabelled provider route cannot identify several accounts on one host.
        const sameProvider = accounts.filter(b => b.provider === a.provider && b.label !== 'codex-orin' && registered.some(r => r.account === b.label));
        if (a.label === 'codex-orin' || sameProvider.length === 1) accept(a.label, s.captured_at_ms, routes[key]);
      }
    }
    for (const a of accounts) {
      const snapshot = latest.get(a.label);
      if (!snapshot) continue;
      const previous: any = this.sql.exec('SELECT observed_at FROM banked_reset_accounts WHERE account=?', a.label).toArray()[0];
      if (previous && previous.observed_at >= snapshot.at) continue;
      this.sql.exec('UPDATE banked_reset_grants SET active=0 WHERE account=?', a.label);
      const duplicates = new Map<string, number>();
      for (const g of snapshot.grants) {
        const group = `${g.expires_at}:${g.label}`;
        const ordinal = duplicates.get(group) || 0;
        duplicates.set(group, ordinal + 1);
        const id = `quota:${a.label}:${group}:${ordinal}`;
        this.sql.exec(`INSERT INTO banked_reset_grants VALUES (?,?,?,?,?, 'quota',1,?,NULL)
          ON CONFLICT(id) DO UPDATE SET active=1, observed_at=excluded.observed_at`,
          id, a.label, a.provider, g.label, g.expires_at, snapshot.at);
      }
      this.sql.exec("INSERT OR REPLACE INTO banked_reset_accounts VALUES (?,?,'quota')", a.label, snapshot.at);
      this.liveness.setAlert('banked_reset_unknown', a.label, null);
    }
  }

  /** Called by existing 5-minute Hub checks cron. Notices survive alert retention/restarts. */
  run(): any[] {
    this.sync();
    const now = this.now();
    const alerts: any[] = [];
    for (const a of this.accounts()) {
      if (!this.sql.exec('SELECT account FROM banked_reset_accounts WHERE account=?', a.label).toArray().length) {
        const alert = this.liveness.setAlert('banked_reset_unknown', a.label, { state: 'attention',
          detail: `${a.label} (${a.provider}): banked-reset expiry is unknown. Needed: open ${a.provider === 'zai' ? 'https://z.ai/manage-apikey/coding-plan/personal/usage (email/password; complete captcha yourself)' : 'Codex /usage > Redeem reset on this account, without confirming a reset'}, then record each available grant and exact expiry, or confirm none, at /banked-resets.html. Hub does not scrape captcha pages.` });
        if (alert) alerts.push(alert);
      }
    }
    const active = this.sql.exec('SELECT * FROM banked_reset_grants WHERE active=1 AND expires_at>? ORDER BY expires_at,id', now).toArray() as any[];
    for (const g of active) {
      const threshold = expiryThreshold(g.expires_at, now);
      if (!threshold || this.sql.exec('SELECT grant_id FROM banked_reset_notices WHERE grant_id=? AND threshold=?', g.id, threshold).toArray().length) continue;
      const subject = `${g.id}:${threshold}`;
      const action = g.provider === 'codex'
        ? 'Alexey, please approve whether to use this reset before the deadline. Only you may apply it manually on the correct Codex account using /usage > Redeem reset. Hub and agents must not redeem it. If you choose to use it, verify /status weekly 100% and credits unchanged; never choose paid/credit options or apply two resets back to back.'
        : 'Alexey, please approve whether to use this reset before the deadline. Only you may apply it manually at https://z.ai/manage-apikey/coding-plan/personal/usage (email/password; complete the Aliyun slider/captcha yourself). Hub and agents must not redeem it. Do not apply two back to back; update the recorded grant if you use it.';
      const detail = `${g.account} (${g.provider}), ${g.label}: expires ${berlinDeadline(g.expires_at)}. ` +
        `${threshold === 'urgent' ? 'URGENT: less than 24 hours remain.' : (g.expires_at - now >= 36 * HOUR ? 'Early warning: at least 36 hours remain.' : 'Late discovery: less than 36 hours remain.')} Needed: ${action}`;
      const alert = this.liveness.setAlert('banked_reset_expiry', subject, { state: 'attention', detail });
      this.sql.exec('INSERT INTO banked_reset_notices VALUES (?,?,?,?)', g.id, threshold, subject, now);
      if (alert) alerts.push(alert);
    }
    // Resolved/expired/used grants stop retrying undelivered stale notifications.
    for (const a of this.sql.exec("SELECT subject FROM liveness_alerts WHERE subject_kind='banked_reset_expiry' AND resolved_at IS NULL").toArray() as any[]) {
      if (!active.some(g => a.subject.startsWith(g.id + ':'))) this.liveness.setAlert('banked_reset_expiry', a.subject, null);
    }
    return alerts;
  }

  async handle(request: Request, caller: Caller): Promise<Response> {
    const reply = (data: unknown, status = 200) => Response.json(data, { status, headers: { 'Cache-Control': 'no-store' } });
    if (!caller.isAdmin) return reply({ error: 'forbidden' }, 403);
    if (request.method === 'GET') {
      this.sync();
      return reply({ accounts: this.accounts(), grants: this.sql.exec('SELECT * FROM banked_reset_grants WHERE active=1 ORDER BY expires_at').toArray(),
        observations: this.sql.exec('SELECT * FROM banked_reset_accounts').toArray(),
        notices: this.sql.exec('SELECT * FROM banked_reset_notices ORDER BY created_at DESC LIMIT 100').toArray() });
    }
    if (request.method !== 'POST') return reply({ error: 'method_not_allowed' }, 405);
    if (caller.kind !== 'human') return reply({ error: 'operator_required' }, 403);
    const raw = await request.text();
    if (raw.length > 16384) return reply({ error: 'too_large' }, 413);
    let d: any;
    try { d = JSON.parse(raw); } catch { return reply({ error: 'invalid_json' }, 400); }
    if (!d || typeof d !== 'object' || Array.isArray(d) || Object.keys(d).some(k => !['account', 'grants'].includes(k))) return reply({ error: 'invalid_payload' }, 400);
    const account = this.accounts().find(a => a.label === d.account);
    if (!account || !Array.isArray(d.grants) || d.grants.length > 32 ||
        d.grants.some((g: any) => !g || typeof g.label !== 'string' || !LABEL.test(g.label) || typeof g.expires_at !== 'string' ||
          !/(Z|[+-]\d{2}:\d{2})$/.test(g.expires_at) || !Number.isFinite(Date.parse(g.expires_at)) || Date.parse(g.expires_at) <= this.now())) return reply({ error: 'invalid_grants' }, 400);
    // Replace the complete manual list for an account. An empty list explicitly confirms none.
    this.sql.exec('UPDATE banked_reset_grants SET active=0 WHERE account=?', account.label);
    const counts = new Map<string, number>();
    for (const g of d.grants) {
      const expiry = Date.parse(g.expires_at), group = `${expiry}:${g.label}`;
      const ordinal = counts.get(group) || 0;
      counts.set(group, ordinal + 1);
      // Same IDs as automatic observations: re-recording/recovered API cannot duplicate alerts.
      const id = `quota:${account.label}:${group}:${ordinal}`;
      this.sql.exec(`INSERT INTO banked_reset_grants VALUES (?,?,?,?,?,'manual',1,?,?)
        ON CONFLICT(id) DO UPDATE SET active=1, source='manual',observed_at=excluded.observed_at,recorded_by=excluded.recorded_by`,
        id, account.label, account.provider, g.label, expiry, this.now(), caller.actor || 'operator');
    }
    this.sql.exec("INSERT OR REPLACE INTO banked_reset_accounts VALUES (?,?,'manual')", account.label, this.now());
    this.liveness.setAlert('banked_reset_unknown', account.label, null);
    return reply({ status: 'recorded', account: account.label, grants: d.grants.length });
  }
}
