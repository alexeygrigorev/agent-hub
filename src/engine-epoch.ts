/**
 * Credential push epoch (intake 5814e109 A7).
 *
 * Hub pushes fresh engine credentials to every host: any change to the central
 * store (new generation, rotation, bundle add/remove) bumps this epoch, and the
 * epoch rides on the 2 s host offers poll. A host that sees an epoch it has not
 * applied syncs immediately and acks the generations it installed, so delivery
 * takes seconds instead of waiting for the next heartbeat. Offline hosts see the
 * epoch on reconnect; the periodic pull stays as the fallback.
 *
 * Hosts poll every few seconds, so the value is cached in memory per SQL handle:
 * a poll costs no row reads, a bump costs one row write.
 */
type Sql = { exec: (query: string, ...bindings: any[]) => { toArray(): any[] } };

const cache = new WeakMap<object, { value: string }>();

function load(sql: Sql): { value: string } {
  let entry = cache.get(sql);
  if (entry) return entry;
  sql.exec('CREATE TABLE IF NOT EXISTS engine_epoch (k INTEGER PRIMARY KEY CHECK (k = 1), v TEXT NOT NULL)');
  const row: any = sql.exec('SELECT v FROM engine_epoch WHERE k = 1').toArray()[0];
  entry = { value: typeof row?.v === 'string' ? row.v : '0' };
  cache.set(sql, entry);
  return entry;
}

export function engineEpoch(sql: Sql): string {
  return load(sql).value;
}

/** Strictly increasing, so two bumps in one millisecond still differ. */
export function bumpEngineEpoch(sql: Sql, nowMs: number): string {
  const entry = load(sql);
  const next = String(Math.max(nowMs, Number(entry.value) + 1));
  sql.exec('INSERT OR REPLACE INTO engine_epoch VALUES (1, ?)', next);
  entry.value = next;
  return next;
}
