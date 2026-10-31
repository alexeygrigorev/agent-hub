/**
 * Table-existence probes without a sqlite_master scan (intake 8081a05a).
 *
 * sqlite_master has no index, so `SELECT name FROM sqlite_master WHERE
 * type='table' AND name=?` reads every schema row (~600 tables, indexes and
 * triggers) and Durable Objects bill each one as a row read. Services run
 * such probes on hot paths hundreds of times per alarm. `pragma_table_list(name)`
 * answers the same question by name lookup and reads at most one row.
 *
 * `rewriteSchemaProbe` maps exactly the probe shapes the code base uses onto
 * that pragma and keeps the selected column; anything else is left alone.
 * The Hub is the only schema owner and has no views or virtual tables, so
 * `type='table'` in the main schema is the same set sqlite_master reports.
 */

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;
const PROBE = /^\s*SELECT\s+(name|1)\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*(=\s*(\?|'[A-Za-z0-9_]+')|IN\s*\(([^)]*)\))\s*(LIMIT\s+1\s*)?;?\s*$/i;

const one = (column: string, arg: string) =>
  `SELECT ${column} FROM pragma_table_list(${arg}) WHERE schema='main' AND type='table'`;

/** The equivalent pragma query, or null when `query` is not a plain probe. */
export function rewriteSchemaProbe(query: string): string | null {
  const m = PROBE.exec(query);
  if (!m) return null;
  const column = m[1].toLowerCase() === 'name' ? 'name' : '1';
  if (m[3]) return one(column, m[3]);
  const names = m[4].split(',').map(s => s.trim());
  if (!names.length || names.some(n => !/^'[A-Za-z0-9_]+'$/.test(n) || !IDENT.test(n.slice(1, -1)))) return null;
  const union = names.map(n => one(column, n)).join(' UNION ALL ');
  return m[5] ? `SELECT * FROM (${union}) LIMIT 1` : union;
}

/**
 * Run a trigger-maintained backfill once per database instead of on every
 * cold start. Backfills like `INSERT ... SELECT ... WHERE NOT EXISTS` are
 * idempotent but scan their whole source table each time the DO wakes;
 * after the first complete run the table's triggers keep the target current.
 * A one-row stamp (point lookup) records that the backfill has completed.
 */
export function backfillOnce(sql: { exec: (q: string, ...a: any[]) => any }, key: string, backfill: () => void): void {
  sql.exec('CREATE TABLE IF NOT EXISTS hub_init_stamps (key TEXT PRIMARY KEY, at INTEGER NOT NULL)');
  if (sql.exec('SELECT 1 FROM hub_init_stamps WHERE key=?', key).toArray().length) return;
  backfill();
  sql.exec('INSERT OR IGNORE INTO hub_init_stamps VALUES(?,?)', key, Date.now());
}

/**
 * Backfill a 1:1 trigger-maintained projection only when its row count no
 * longer matches its sources. COUNT(*) over a whole table is answered from
 * the b-tree without reading rows, so the per-cold-start check is free,
 * yet a projection that lost rows still repairs itself on the next wake.
 */
export function backfillWhenCountsDiffer(sql: { exec: (q: string, ...a: any[]) => any }, target: string, sources: string[], backfill: () => void): void {
  const count = (table: string) => Number(sql.exec(`SELECT COUNT(*) AS n FROM ${table}`).one().n);
  const present = sources.filter(t => sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name=?", t).toArray().length);
  if (count(target) !== present.reduce((n, t) => n + count(t), 0)) backfill();
}
