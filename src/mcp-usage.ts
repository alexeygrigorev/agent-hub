import { MCP_ROUTES } from './mcp-tools';

const toolNames = new Set(MCP_ROUTES.map(route => route.name));
const kinds = new Set(['human', 'agent', 'host']);
/** Lifetime aggregates only: at most (route count + 2) * 4 rows.
 * No actor IDs, bearer material, arguments, payloads or client-provided names.
 * Counters saturate at the largest exactly representable JSON integer.
 */
export class McpUsage {
  constructor(private sql: SqlStorage) {
    sql.exec(`CREATE TABLE IF NOT EXISTS mcp_tool_usage (
      tool_name TEXT NOT NULL, caller_kind TEXT NOT NULL,
      calls INTEGER NOT NULL, last_called_at INTEGER NOT NULL,
      PRIMARY KEY (tool_name, caller_kind)
    )`);
  }
  record(name: string, callerKind: string) {
    const tool = name === 'tools/list' || toolNames.has(name) ? name : 'unknown';
    const kind = kinds.has(callerKind) ? callerKind : 'other';
    this.sql.exec(`INSERT INTO mcp_tool_usage VALUES (?, ?, 1, ?)
      ON CONFLICT(tool_name, caller_kind) DO UPDATE SET
      calls = MIN(calls + 1, 9007199254740991), last_called_at = excluded.last_called_at`,
      tool, kind, Date.now());
  }
  summary() {
    return this.sql.exec(`SELECT tool_name, caller_kind, calls, last_called_at
      FROM mcp_tool_usage ORDER BY tool_name, caller_kind`).toArray();
  }
}
