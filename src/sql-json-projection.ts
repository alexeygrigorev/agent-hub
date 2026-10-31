/** SQL-only field projection; column/fields are trusted source constants.
 * The -> operator plus json() preserves JSON booleans, objects and arrays,
 * unlike json_extract() passed straight to json_object(). Invalid legacy
 * JSON behaves like the existing parse-or-empty-object helpers.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { backfillWhenCountsDiffer } from './sql-probes';

export function jsonFields(column: string, fields: readonly string[]): string {
  const valid = `(CASE WHEN json_valid(${column}) THEN ${column} ELSE '{}' END)`;
  const object = `json_object(${fields.map(field => `'${field}',json(COALESCE(${valid} -> '$.${field}','null'))`).join(',')})`;
  // SQLite paths select the first duplicate key; JSON.parse selects the last.
  // Leave legacy duplicate-key objects intact for the existing parser.
  const duplicates = `EXISTS(SELECT 1 FROM json_each(${valid}) GROUP BY key HAVING count(*)>1)`;
  return `(CASE WHEN json_type(${valid})='object' AND NOT ${duplicates} THEN ${object} ELSE ${valid} END)`;
}

const RESOURCE_FIELDS = ['project','workspace','task_kind','kind','tier','intake_task_id','keep','provenance'];
type RequestTable = 'fleet_launches' | 'session_control_sessions';

/** Compact immutable request facts are projected once per write, in the same
 * SQLite transaction. Live bindings, protection rows and telemetry stay live. */
export function initResourceRequestProjection(sql: SqlStorage, table: RequestTable): void {
  sql.exec(`CREATE TABLE IF NOT EXISTS resource_request_projection (
    source TEXT NOT NULL,id TEXT NOT NULL,request_json TEXT NOT NULL,PRIMARY KEY(source,id))`);
  const insert = `INSERT OR REPLACE INTO resource_request_projection(source,id,request_json)
    VALUES('${table}',NEW.id,${jsonFields('NEW.request_json',RESOURCE_FIELDS)});`;
  sql.exec(`CREATE TRIGGER IF NOT EXISTS resource_request_${table}_insert AFTER INSERT ON ${table} BEGIN ${insert} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS resource_request_${table}_update AFTER UPDATE OF id,request_json ON ${table} BEGIN
    DELETE FROM resource_request_projection WHERE source='${table}' AND id=OLD.id; ${insert} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS resource_request_${table}_delete AFTER DELETE ON ${table} BEGIN
    DELETE FROM resource_request_projection WHERE source='${table}' AND id=OLD.id; END`);
  backfillWhenCountsDiffer(sql, 'resource_request_projection', ['fleet_launches', 'session_control_sessions'], () => sql.exec(`INSERT INTO resource_request_projection(source,id,request_json)
    SELECT '${table}',s.id,${jsonFields('s.request_json',RESOURCE_FIELDS)} FROM ${table} s
    WHERE NOT EXISTS(SELECT 1 FROM resource_request_projection p WHERE p.source='${table}' AND p.id=s.id)`));
}

export function resourceRequest(table: RequestTable, id: string, raw: string): string {
  // Missing rows use the existing parse-or-empty-object reader directly.
  return `COALESCE((SELECT p.request_json FROM resource_request_projection p WHERE p.source='${table}' AND p.id=${id}),${raw})`;
}
