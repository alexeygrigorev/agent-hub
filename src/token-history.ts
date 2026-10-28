import { canonicalAccountLabel, canonicalHarness } from './quota';
/** Native token usage history (usage-history intake, AC3).
 *
 * Engines that keep native per-session token logs (codex rollouts incl. the
 * orin and zcodex homes, Claude project JSONL, the OpenCode session DB) get
 * real hourly input/output token counts. Engines without a native token log
 * on the host stay explicitly unsupported per engine (see TOKEN_SOURCES),
 * never a blanket "Unknown" and never inferred from quota percentages.
 *
 * Storage reuses resource_history (kind 'tokens',
 * entity '<engine> / <account> / <project>', metrics input_tokens /
 * output_tokens, 32-day retention, 24h/7d/30d ranges). Absence of points is
 * a gap; collectors post only measured complete hours, explicit zeroes
 * included. No prompts, transcripts, costs or account identifiers are stored.
 */

export const TOKEN_SCHEMA_VERSION = '1.0';
export const TOKEN_MAX_HOURS_PER_POST = 48;
export const TOKEN_MAX_COUNT = 1e12;
const TOKEN_LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$/;
export const TOKEN_ENGINES = new Set(['codex', 'zcodex', 'claude', 'opencode']);

export interface TokenSourceStatus {
  engine: string;
  supported: boolean;
  /** Native log the counts come from, or the reason they cannot. */
  detail: string;
}

/** Per-engine provenance, shown verbatim in the Usage UI. */
export const TOKEN_SOURCES: TokenSourceStatus[] = [
  { engine: 'codex', supported: true, detail: 'codex rollout token_usage_record (shared-codex and orin homes)' },
  { engine: 'zcodex', supported: true, detail: 'zcodex rollout token_usage_record (shared-zai)' },
  { engine: 'claude', supported: true, detail: 'Claude project JSONL message usage (shared-claude)' },
  { engine: 'opencode', supported: true, detail: 'OpenCode completed assistant-message tokens (shared-go)' },
  { engine: 'gemini', supported: false, detail: 'no native per-session token log on the host; quota percentages only' },
  { engine: 'grok', supported: false, detail: 'no native per-session token log on the host; quota percentages only' },
  { engine: 'copilot', supported: false, detail: 'no native per-session token log on the host; quota percentages only' },
];

export interface TokenHour {
  hour_start: string;
  hour_ms: number;
  engine: string;
  account: string;
  project: string;
  input_tokens: number;
  output_tokens: number;
}

export function validateTokenPayload(
  raw: unknown,
  nowMs: number,
  retentionMs: number
): { ok: boolean; status: number; error?: string; reason?: string; hours?: TokenHour[] } {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { ok: false, status: 400, error: 'invalid_json_object' };
  }
  const body = raw as Record<string, unknown>;
  if (body.schema_version !== TOKEN_SCHEMA_VERSION) {
    return { ok: false, status: 400, error: 'invalid_schema_version' };
  }
  if (!Array.isArray(body.hours) || body.hours.length === 0 || body.hours.length > TOKEN_MAX_HOURS_PER_POST) {
    return { ok: false, status: 400, error: 'invalid_hours', reason: 'hours must list 1..48 hourly buckets' };
  }
  const hours: TokenHour[] = [];
  const seen = new Set<string>();
  for (const entry of body.hours) {
    if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) {
      return { ok: false, status: 400, error: 'invalid_hour' };
    }
    const h = entry as Record<string, unknown>;
    if (typeof h.hour_start !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:00:00(\.000)?Z$/.test(h.hour_start)) {
      return { ok: false, status: 400, error: 'invalid_hour_start', reason: 'hour_start must be a UTC hour boundary' };
    }
    const hourMs = Date.parse(h.hour_start);
    if (!Number.isFinite(hourMs) || hourMs > nowMs || nowMs - hourMs > retentionMs) {
      return { ok: false, status: 400, error: 'invalid_hour_start', reason: 'hour_start outside retained history' };
    }
    for (const key of ['engine', 'account', 'project']) {
      const value = h[key];
      if (typeof value !== 'string' || !TOKEN_LABEL_RE.test(value)) {
        return { ok: false, status: 400, error: 'invalid_hour', reason: `invalid_${key}` };
      }
    }
    if (!TOKEN_ENGINES.has(h.engine as string)) {
      return { ok: false, status: 400, error: 'unsupported_engine', reason: 'engine has no native token source' };
    }
    for (const key of ['input_tokens', 'output_tokens']) {
      const value = h[key];
      if (typeof value !== 'number' || !Number.isInteger(value) || value < 0 || value > TOKEN_MAX_COUNT) {
        return { ok: false, status: 400, error: 'invalid_hour', reason: `invalid_${key}` };
      }
    }
    const dedupe = `${h.engine}\0${h.account}\0${h.project}\0${h.hour_start}`;
    if (seen.has(dedupe)) return { ok: false, status: 400, error: 'duplicate_hour' };
    seen.add(dedupe);
    hours.push({
      hour_start: h.hour_start as string,
      hour_ms: hourMs,
      engine: h.engine as string,
      account: h.account as string,
      project: h.project as string,
      input_tokens: h.input_tokens as number,
      output_tokens: h.output_tokens as number,
    });
  }
  return { ok: true, status: 200, hours };
}

export function tokenEntity(hour: Pick<TokenHour, 'engine' | 'account' | 'project'>): string {
  return `${canonicalHarness(hour.engine)} / ${canonicalAccountLabel(hour.account)} / ${hour.project}`.slice(0, 256);
}

/** Posted-hour ledger (table token_hours_posted, created by
 * HOST_HISTORY_SCHEMA) so genuine late corrections overwrite while identical
 * reposts stay idempotent. */

export function tokenPostAt(
  sql: { exec: (query: string, ...args: any[]) => { toArray: () => any[] } },
  entity: string,
  hourMs: number,
  inputTokens: number,
  outputTokens: number,
  nowMs: number,
  retentionMs: number,
  machine = ''
): number {
  const ledgerEntity = machine ? JSON.stringify([machine, entity]) : entity;
  const row = sql.exec('SELECT input_tokens, output_tokens, revision FROM token_hours_posted WHERE entity=? AND hour_ms=?', ledgerEntity, hourMs).toArray()[0] as any;
  if (!row) {
    // Existing deployments shared one ledger across machines. Seed the new
    // machine ledger beyond that revision so its first correction wins over
    // retained pre-upgrade history, without borrowing another host's totals.
    const legacy = machine ? sql.exec('SELECT revision FROM token_hours_posted WHERE entity=? AND hour_ms=?', entity, hourMs).toArray()[0] : null;
    const revision = legacy ? Number(legacy.revision || 0) + 1 : 0;
    sql.exec('INSERT INTO token_hours_posted VALUES(?,?,?,?,?)', ledgerEntity, hourMs, inputTokens, outputTokens, revision);
    sql.exec('DELETE FROM token_hours_posted WHERE hour_ms < ?', nowMs - retentionMs);
    return hourMs + tokenRevisionOffset(revision);
  }
  if (Number(row.input_tokens) === inputTokens && Number(row.output_tokens) === outputTokens) return hourMs + tokenRevisionOffset(Number(row.revision || 0));
  const revision = Number(row.revision || 0) + 1;
  const offset = tokenRevisionOffset(revision);
  sql.exec('UPDATE token_hours_posted SET input_tokens=?, output_tokens=?, revision=? WHERE entity=? AND hour_ms=?',
    inputTokens, outputTokens, revision, ledgerEntity, hourMs);
  // Preserve legacy minute revision instants, then advance by milliseconds.
  // Every accepted correction must win while staying in its original hour.
  return hourMs + offset;
}

function tokenRevisionOffset(revision: number): number {
  const offset = revision <= 30 ? revision * 60000 : 1800000 + revision - 30;
  if (offset >= 3600000) throw new Error('token_hour_revision_exhausted');
  return offset;
}
