import { MCP_ROUTES } from './mcp-tools';
import { CORE_TOOL_NAMES, DOMAIN_TOOL_NAMES } from './mcp-curation';

const DAY_MS = 24 * 60 * 60 * 1000;
const OBSERVATION_DAYS = 30;
const routeNames = new Set(MCP_ROUTES.map(route => route.name));
const canonicalNames = new Set<string>([...CORE_TOOL_NAMES, ...DOMAIN_TOOL_NAMES, 'tools/list']);
const kinds = new Set(['human', 'agent', 'host']);

/** Bounded daily aggregates retain alias-vs-canonical usage for a rolling 30-day window.
 * No actor IDs, bearer material, arguments or payloads are stored. At most 30 daily buckets
 * per known route/canonical tool and caller kind are retained.
 */
export class McpUsage {
  constructor(private sql: SqlStorage) {
    // Retain the old aggregate table during rollout so its already-expired rows
    // can be cleared without losing recent pre-window diagnostics abruptly.
    sql.exec(`CREATE TABLE IF NOT EXISTS mcp_tool_usage (
      tool_name TEXT NOT NULL, caller_kind TEXT NOT NULL,
      calls INTEGER NOT NULL, last_called_at INTEGER NOT NULL,
      PRIMARY KEY (tool_name, caller_kind)
    )`);
    sql.exec(`CREATE TABLE IF NOT EXISTS mcp_tool_usage_30d (
      bucket_start INTEGER NOT NULL,
      tool_name TEXT NOT NULL,
      canonical_tool_name TEXT NOT NULL,
      usage_kind TEXT NOT NULL,
      caller_kind TEXT NOT NULL,
      calls INTEGER NOT NULL,
      last_called_at INTEGER NOT NULL,
      PRIMARY KEY (bucket_start, tool_name, canonical_tool_name, usage_kind, caller_kind)
    )`);
  }

  record(name: string, callerKind: string, canonicalName = name, at = Date.now(), usageKindOverride?: 'alias' | 'canonical') {
    const toolName = name === 'tools/list' || routeNames.has(name) || canonicalNames.has(name) ? name : 'unknown';
    const canonical = canonicalNames.has(canonicalName) ? canonicalName : toolName === 'unknown' ? 'unknown' : toolName;
    const usageKind = toolName === 'unknown' ? 'unknown' : usageKindOverride || (toolName === canonical ? 'canonical' : 'alias');
    const kind = kinds.has(callerKind) ? callerKind : 'other';
    const bucket = Math.floor(at / DAY_MS) * DAY_MS;
    this.sql.exec(`INSERT INTO mcp_tool_usage_30d
      (bucket_start, tool_name, canonical_tool_name, usage_kind, caller_kind, calls, last_called_at)
      VALUES (?, ?, ?, ?, ?, 1, ?)
      ON CONFLICT(bucket_start, tool_name, canonical_tool_name, usage_kind, caller_kind) DO UPDATE SET
      calls = MIN(calls + 1, 9007199254740991), last_called_at = excluded.last_called_at`,
      bucket, toolName, canonical, usageKind, kind, at);
    this.prune(at);
  }

  summary(now = Date.now()) {
    this.prune(now);
    return this.sql.exec(`SELECT tool_name, canonical_tool_name, usage_kind, caller_kind,
        calls, last_called_at, bucket_start
      FROM mcp_tool_usage_30d
      WHERE bucket_start >= ?
      ORDER BY tool_name, caller_kind, bucket_start`,
      this.cutoff(now)).toArray();
  }

  private prune(now: number) {
    this.sql.exec('DELETE FROM mcp_tool_usage_30d WHERE bucket_start < ?', this.cutoff(now));
    this.sql.exec('DELETE FROM mcp_tool_usage WHERE last_called_at < ?', now - OBSERVATION_DAYS * DAY_MS);
  }

  private cutoff(now: number) {
    return Math.floor(now / DAY_MS) * DAY_MS - (OBSERVATION_DAYS - 1) * DAY_MS;
  }
}
