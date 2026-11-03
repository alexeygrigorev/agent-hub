import type { SqlStorage } from '@cloudflare/workers-types';

/** A prepaid, durable circuit breaker. This ledger deliberately uses KV outside
 * application SQL transactions: rolling back SQL must never refund billed work.
 * Reservations survive eviction; unused credit is lost on a crash, not reissued.
 * Cloudflare exposes row counters after executing SQL, so the first statement
 * that exceeds credit can overshoot. Queries must ALSO be bounded and indexed.
 * The breaker prevents any subsequent SQL, even if an application catches it.
 */
export const DAILY_COMBINED_ROW_LIMIT = 20_000;
export const DAILY_ROW_LIMITS = Object.freeze({ read: 20_000, written: 20_000 });
const DAY_MS = 86_400_000;
const LEDGER_KEY = 'cf_daily_row_budget_v1';
const DEFAULT_CHUNK = Object.freeze({ read: 192, written: 64 });
/** Index DDL scans its entire source before exposing cursor counters. Never
 * let a new migration perform that scan against retained large tables. */
export const INDEX_BUILD_SOURCE_LIMIT = 1000;
export const INDEX_PARTIAL_READ_SOURCE_LIMIT = 50_000;
const INDEX_METADATA_LIMIT = 64;

type IndexBuild = { name: string; table: string; predicate?: string };

/** Tokenize identifiers, punctuation and literals separately so comments or
 * a quoted column containing CREATE INDEX cannot hide or invent a build. */
function indexBuilds(query: string): { builds: IndexBuild[]; unsupported: boolean; executable: string } {
  const matches = [...query.matchAll(/--[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/|'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_$]*|[^\s]/g)]
    .filter(match => !match[0].startsWith('--') && !match[0].startsWith('/*'));
  const tokens=matches.map(match=>match[0]);
  const word = (i: number, text: string) => tokens[i]?.toUpperCase() === text;
  const identifier = (i: number): string | null => {
    const value = tokens[i];
    if (!value) return null;
    if (/^[A-Za-z_][A-Za-z0-9_$]*$/.test(value)) return value;
    if (value[0] === '"') return value.slice(1, -1).replaceAll('""', '"');
    if (value[0] === '`') return value.slice(1, -1).replaceAll('``', '`');
    if (value[0] === '[') return value.slice(1, -1);
    return null;
  };
  const builds: IndexBuild[] = [];
  let unsupported = false;
  for (let i = 0; i < tokens.length; i++) {
    if (!word(i, 'CREATE')) continue;
    let j = i + 1;
    if (word(j, 'UNIQUE')) j++;
    if (!word(j, 'INDEX')) continue;
    j++;
    if (word(j, 'IF') && word(j + 1, 'NOT') && word(j + 2, 'EXISTS')) j += 3;
    let name = identifier(j++);
    if (tokens[j] === '.') {
      if (name?.toLowerCase() !== 'main') unsupported = true;
      name = identifier(j + 1); j += 2;
    }
    if (!word(j++, 'ON')) { unsupported = true; continue; }
    let table = identifier(j++);
    if (tokens[j] === '.') {
      if (table?.toLowerCase() !== 'main') unsupported = true;
      table = identifier(j + 1); j += 2;
    }
    if (!name || !table || tokens[j] !== '(') { unsupported = true; continue; }
    let depth=0,k=j;
    for(;k<tokens.length;k++) {
      if(tokens[k]==='(') depth++;
      if(tokens[k]===')' && --depth===0) { k++;break; }
    }
    if(depth!==0) { unsupported=true;continue; }
    let predicate:string|undefined;
    if(word(k,'WHERE')) {
      const last=tokens.at(-1)===';'?matches.length-2:matches.length-1;
      const end=last>=k?matches[last].index!+matches[last][0].length:matches[k].index!+matches[k][0].length;
      predicate=query.slice(matches[k].index!+matches[k][0].length,end).trim();
      if(!predicate) unsupported=true;
    } else if(k<tokens.length && !(tokens[k]===';' && k===tokens.length-1)) unsupported=true;
    builds.push({ name, table, predicate });
  }
  // A batch could mutate/recreate the source after preflight and before its
  // CREATE INDEX. Require separate calls so every build sees its real source.
  if (builds.length && tokens.slice(0, -1).includes(';')) unsupported = true;
  const last = tokens.at(-1) === ';' ? matches.length - 2 : matches.length - 1;
  const executable = builds.length && last >= 0
    ? query.slice(0, matches[last].index! + matches[last][0].length) : query;
  return { builds, unsupported, executable };
}

type Counts = { read: number; written: number };
type Ledger = Counts & { day: number; stopped: boolean };
type Store = { get<T>(key: string): Promise<T | undefined>; put<T>(key: string, value: T): Promise<void> };

export class RowBudgetExceeded extends Error {
  readonly code = 'do_daily_row_budget_exceeded';
  constructor(readonly detail: string) { super('do_daily_row_budget_exceeded: ' + detail); this.name = 'RowBudgetExceeded'; }
}

export class RowBudget {
  private ledger: Ledger | undefined;
  private credit: Counts = { read: 0, written: 0 };
  private debt: Counts = { read: 0, written: 0 };
  private stopped = true;
  private dirty = false;
  private queue: Promise<void> = Promise.resolve();
  private loggedDay = -1;
  private readonly limits: Counts;
  private readonly chunk: Counts;
  private readonly combinedLimit: number;
  private readonly storage: Store;
  private emergencyAlarmDeleted = false;
  private ledgerFailure = false;

  constructor(storage: Store, private now: () => number = Date.now,
    limits: Counts = DAILY_ROW_LIMITS, chunk: Counts = DEFAULT_CHUNK, combinedLimit = DAILY_COMBINED_ROW_LIMIT) {
    // Capture native ledger calls before installing KV guards on the object.
    this.storage = { get: storage.get.bind(storage), put: storage.put.bind(storage) };
    // Even configuration mistakes cannot raise the production hard ceiling.
    const safe = (value: number, ceiling: number) => Number.isSafeInteger(value) && value > 0 ? Math.min(value, ceiling) : 0;
    this.limits = { read: safe(limits.read, DAILY_ROW_LIMITS.read), written: safe(limits.written, DAILY_ROW_LIMITS.written) };
    this.chunk = { read: safe(chunk.read, this.limits.read), written: safe(chunk.written, this.limits.written) };
    this.combinedLimit = safe(combinedLimit, Math.min(DAILY_COMBINED_ROW_LIMIT,this.limits.read+this.limits.written));
  }

  private day() { return Math.floor(this.now() / DAY_MS); }
  private valid(value: unknown): value is Ledger {
    const v = value as Ledger;
    return !!v && Number.isSafeInteger(v.day) && Number.isSafeInteger(v.read) && v.read >= 0 &&
      Number.isSafeInteger(v.written) && v.written >= 0 && typeof v.stopped === 'boolean';
  }
  private async exclusive(fn: () => Promise<void>): Promise<void> {
    // Catch the operation INSIDE the queued callback. Passing an async fn
    // directly to .then() creates an intermediate rejected promise before
    // promise adoption installs its handler in workerd. Expected circuit
    // breaker denials must not emit unhandled-rejection events.
    const task = this.queue.then(async () => {
      try { await fn(); return { ok: true as const }; }
      catch (error) { return { ok: false as const, error }; }
    });
    // The serialization chain is always fulfilled; only this caller's awaited
    // promise rejects. A rejected event never poisons later queue entries.
    this.queue = task.then(() => {});
    const outcome = await task;
    if (!outcome.ok) throw outcome.error;
  }
  private trip(detail: string): never {
    this.stopped = true;
    if (this.loggedDay !== this.day()) {
      this.loggedDay = this.day();
      console.error(JSON.stringify({ event: 'do_daily_row_budget_exceeded', day: this.day(), detail,
        reserved: this.ledger, available: this.credit, debt: this.debt,
        spent: this.ledger ? {read:this.ledger.read-this.credit.read+this.debt.read,written:this.ledger.written-this.credit.written+this.debt.written}:null,
        limit: this.limits, limit_total: this.combinedLimit }));
    }
    throw new RowBudgetExceeded(detail);
  }

  /** MUST run before constructor initialization and each request/RPC/alarm.
   * Cached credit makes normal preflight zero-I/O. Reservations are serialized,
   * while overlapping events spend the same synchronous credit atomically. */
  begin(bootstrap = false): Promise<void> {
    return this.exclusive(async () => {
      const day = this.day();
      if (this.ledgerFailure) this.trip('durable budget storage unavailable');
      if (!this.ledger) {
        let stored: Ledger | undefined;
        try { stored = await this.storage.get<Ledger>(LEDGER_KEY); }
        catch (error) { this.ledgerFailure = true; throw error; }
        if (stored !== undefined && !this.valid(stored)) { this.ledgerFailure = true; this.trip('invalid durable ledger'); }
        this.ledger = stored ?? { day, read: 0, written: 0, stopped: false };
        // KV lookup itself consumes a row; persist it with the next reservation.
        this.debt.read++;
      }
      if (day < this.ledger.day) this.trip('clock moved behind durable ledger');
      if (day > this.ledger.day) {
        this.ledger = { day, read: 0, written: 0, stopped: false };
        this.credit = { read: 0, written: 0 };
        this.debt = { read: 0, written: 0 };
        this.stopped = false;
        this.emergencyAlarmDeleted = false;
      }
      if (this.ledger.stopped) this.trip('durable daily allowance exhausted');
      if (!this.limits.read || !this.limits.written || !this.chunk.read || !this.chunk.written || !this.combinedLimit) this.trip('invalid budget configuration');
      if (!this.stopped && this.credit.read > this.chunk.read / 4 && this.credit.written > this.chunk.written / 4 && !this.dirty) return;
      await this.checkpoint(true, bootstrap);
      if (this.ledger.stopped || this.credit.read <= 0 || this.credit.written <= 0) this.trip('daily allowance exhausted');
      this.stopped = false;
    });
  }

  /** Persist overdraw outside the caller's SQL transaction. Normal completion
   * performs no I/O. Always await this in the event's finally block. */
  finish(): Promise<void> {
    return this.exclusive(async () => {
      if (this.ledgerFailure) return;
      if (this.dirty && this.ledger) await this.checkpoint(false);
    });
  }

  private async checkpoint(refill: boolean, bootstrap = false): Promise<void> {
    const old = this.ledger!;
    // A refill returns its old UNUSED directional reservations in this same
    // durable write, then issues a fresh small block. Hot write-only traffic
    // must not be denied because an untouched read reservation held the pool.
    const ledger: Ledger = { ...old,
      read: old.read-(refill ? this.credit.read : 0)+this.debt.read,
      written: old.written-(refill ? this.credit.written : 0)+this.debt.written+1 };
    const add = { read: 0,written: 0 };
    const chunk = bootstrap ? { read: Math.max(this.chunk.read,Math.min(16_000,this.limits.read)),
      written: Math.max(this.chunk.written,Math.min(3_000,this.limits.written)) } : this.chunk;
    if (refill) {
      add.read = Math.max(0,Math.min(chunk.read,this.limits.read-ledger.read));
      add.written = Math.max(0,Math.min(chunk.written,this.limits.written-ledger.written));
      const available = Math.max(0,this.combinedLimit-1-ledger.read-ledger.written);
      const requested = add.read+add.written;
      if (requested > available) {
        add.read = Math.floor(available*add.read/requested);
        add.written = Math.max(0,Math.min(add.written,available-add.read));
      }
    }
    ledger.read += add.read;
    ledger.written += add.written;
    ledger.stopped ||= ledger.read >= this.limits.read || ledger.written >= this.limits.written ||
      ledger.read+ledger.written >= this.combinedLimit || (refill && (add.read <= 0 || add.written <= 0));
    // Fail closed across a failed write. Never hand out credit until durable.
    this.stopped = true;
    try { await this.storage.put(LEDGER_KEY, ledger); }
    catch (error) { this.ledgerFailure = true; throw error; }
    this.ledger = ledger;
    if (refill) this.credit = { ...add };
    this.debt = { read: 0, written: 0 };
    this.dirty = false;
  }

  /** Checkpoint a quiet object's actual spending and return ONLY unused
   * prepaid credit. The predicate is rechecked after acquiring the ledger
   * queue, so activity arriving while a refund waits cannot lose its credit.
   * A crash before this checkpoint still loses reservations conservatively.
   * No application SQL transaction can refund actual billed work. */
  async releaseUnused(canRelease: () => boolean = () => true): Promise<boolean> {
    let released = false;
    await this.exclusive(async () => {
      if (!canRelease() || !this.ledger || this.ledgerFailure || this.day() !== this.ledger.day) return;
      if (this.credit.read <= 0 && this.credit.written <= 0 && !this.dirty) return;
      const previous = this.ledger;
      const ledger: Ledger = {
        day: previous.day,
        read: Math.max(0,previous.read-this.credit.read)+this.debt.read,
        written: Math.max(0,previous.written-this.credit.written)+this.debt.written+1,
        stopped: previous.stopped,
      };
      ledger.stopped ||= ledger.read >= this.limits.read || ledger.written >= this.limits.written || ledger.read+ledger.written >= this.combinedLimit;
      // Freeze the old credits until the checkpoint is durable. New events
      // serialize begin() behind this operation, rather than spending them.
      this.stopped = true;
      try { await this.storage.put(LEDGER_KEY,ledger); }
      catch (error) {
        this.ledgerFailure = true;
        // Keep the old, larger reservation and local credit on failure.
        // It is never made spendable again in this incarnation.
        throw error;
      }
      this.ledger = ledger;
      this.credit = { read: 0,written: 0 };
      this.debt = { read: 0,written: 0 };
      this.dirty = false;
      released = true;
    });
    return released;
  }

  /** No storage access: safe to inspect after the breaker trips. */
  status() {
    return { stopped: this.stopped, day: this.ledger?.day ?? this.day(),
      reserved: this.ledger ? { read: this.ledger.read, written: this.ledger.written } : null,
      available: { ...this.credit }, limit: { ...this.limits }, limit_total: this.combinedLimit,
      reserved_total: this.ledger ? this.ledger.read+this.ledger.written : null, available_total: this.credit.read+this.credit.written };
  }

  private assertAllowed() {
    if (!this.ledger || this.day() !== this.ledger.day || this.stopped) this.trip('preflight required or credit exhausted');
  }
  private consume(read: number, written: number) {
    const fungible = this.limits.read >= this.combinedLimit && this.limits.written >= this.combinedLimit;
    const charge = (key: keyof Counts, amount: number) => {
      if (!Number.isFinite(amount) || amount < 0) {
        if (this.ledger) this.ledger.stopped = true;
        this.dirty = true;
        this.trip('invalid SQL row counters');
      }
      if (fungible && amount > this.credit[key]) {
        const other = key === 'read' ? 'written' : 'read';
        const transfer = Math.min(amount-this.credit[key],this.credit[other]);
        // A reservation pays for combined rows, regardless of direction. Move
        // only UNUSED credit and its reservation label; total prepaid cost is
        // invariant, including on a crash before the next durable checkpoint.
        this.credit[other] -= transfer; this.credit[key] += transfer;
        this.ledger![other] -= transfer; this.ledger![key] += transfer;
      }
      const paid = Math.min(this.credit[key], amount);
      this.credit[key] -= paid;
      const overdraw = amount - paid;
      if (overdraw > 0) { this.debt[key] += overdraw; this.dirty = true; }
    };
    charge('read', read);
    charge('written', written);
    if ((fungible ? this.credit.read+this.credit.written <= 0 : this.credit.read <= 0 || this.credit.written <= 0) || this.dirty)
      this.trip('statement exhausted prepaid credit');
  }

  /** Install alongside the SQL wrapper. SQLite-backed KV and alarm updates
   * are billable too; callers must not be able to bypass the SQL breaker by
   * repeatedly setting alarms or writing KV. Ledger calls retain native binds. */
  guardStorage(storage: any): void {
    const budget = this;
    const units = (value: any, key = ''): number => {
      let bytes: number;
      if (value instanceof ArrayBuffer) bytes = value.byteLength;
      else if (ArrayBuffer.isView(value)) bytes = value.byteLength;
      else {
        try {
          const encoded = JSON.stringify(value);
          if (encoded === undefined) return budget.trip('unmeasurable KV value');
          bytes = new TextEncoder().encode(encoded).length * 2;
        } catch { return budget.trip('unmeasurable KV value'); }
      }
      // Include serialized key/record framing and conservatively round each key.
      return Math.max(1, Math.ceil((bytes + key.length * 2 + 128) / 4096));
    };
    const keys = (input: any): string[] => typeof input === 'string' ? [input] : Array.isArray(input) && input.every(v => typeof v === 'string') ? input : budget.trip('unbounded KV keys');
    const charge = (read: number, written: number) => { budget.assertAllowed(); budget.consume(read, written); };
    const install = (name: string, fn: (native: (...args: any[]) => any, args: any[]) => any) => {
      if (typeof storage[name] !== 'function') return;
      const native = storage[name].bind(storage);
      Object.defineProperty(storage, name, { configurable: true, writable: true, value: (...args: any[]) => fn(native, args) });
    };
    install('get', async (native, args) => {
      const requested = keys(args[0]);
      charge(requested.length, 0);
      const value = await native(...args);
      let extra = 0;
      if (value instanceof Map) { for (const [key, item] of value) extra += units(item, String(key)) - 1; }
      else if (value !== undefined) extra += units(value, requested[0]) - 1;
      if (extra) charge(extra, 0);
      return value;
    });
    install('put', (native, args) => {
      let count = 0;
      if (typeof args[0] === 'string') count = units(args[1], args[0]);
      else if (args[0] && typeof args[0] === 'object' && !Array.isArray(args[0])) {
        for (const [key, value] of Object.entries(args[0])) count += units(value, key);
      } else budget.trip('unmeasurable KV put');
      charge(0, Math.max(1, count));
      return native(...args);
    });
    install('delete', (native, args) => { charge(0, Math.max(1, keys(args[0]).length)); return native(...args); });
    install('list', async (native, args) => {
      const limit = args[0]?.limit;
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1000) budget.trip('KV list requires bounded limit');
      // Charge maximum result size before issuing the query, not just returned
      // rows afterwards. Large values are charged additionally below.
      charge(limit, 0);
      const result = await native(...args);
      let extra = 0;
      for (const [key, value] of result) extra += units(value, String(key)) - 1;
      if (extra) charge(extra, 0);
      return result;
    });
    install('getAlarm', (native, args) => { charge(1, 0); return native(...args); });
    install('setAlarm', (native, args) => { charge(0, 1); return native(...args); });
    install('deleteAlarm', (native, args) => {
      if (budget.stopped) {
        if (budget.emergencyAlarmDeleted) return Promise.resolve();
        budget.emergencyAlarmDeleted = true;
        budget.debt.written++;
        budget.dirty = !!budget.ledger;
        return native(...args);
      }
      charge(0, 1);
      return native(...args);
    });
    // Bulk deletion cannot be bounded from its API result and is unnecessary
    // for Hub operation. In particular it cannot erase the budget ledger.
    install('deleteAll', () => budget.trip('bulk storage deletion forbidden'));
  }

  /** Preflight queries use this same native-cursor wrapper, so their real
   * rows are charged too. Metadata stays table-local and capped. A large
   * existing index is a no-op and never probes/scans its source table. */
  private preflightIndexes(sql: SqlStorage, query: string): string {
    const parsed = indexBuilds(query);
    const deny = (detail: string): never => {
      this.ledger!.stopped = true;
      this.dirty = true;
      return this.trip(detail);
    };
    if (parsed.unsupported) deny('index migration requires a supported single-statement build');
    for (const build of parsed.builds) {
      const indexes = sql.exec("SELECT name FROM pragma_index_list(?, 'main') LIMIT ?", build.table, INDEX_METADATA_LIMIT + 1).toArray() as any[];
      // SQLite identifier folding is ASCII-only. Unicode lowercasing could
      // mistake a different non-ASCII index for the requested existing one.
      const fold = (value: string) => value.replace(/[A-Z]/g, char => char.toLowerCase());
      if (indexes.some(row => fold(String(row.name)) === fold(build.name))) continue;
      if (indexes.length > INDEX_METADATA_LIMIT) deny('index metadata exceeds bounded migration allowance');
      // Keep source probe + build within current prepaid credit, allowing
      // conservative schema overhead. This does not preempt arbitrary SQL:
      // it bounds the particular full-source operation before native exec.
      const fungible = this.limits.read >= this.combinedLimit && this.limits.written >= this.combinedLimit;
      const pooledCap = Math.floor((this.credit.read+this.credit.written-128)/4);
      // Probe + predicate probe + native read + index write consume the SAME
      // pool. Divide it once, never allocate the whole pool to both directions.
      const cap = fungible
        ? Math.min(build.predicate ? INDEX_PARTIAL_READ_SOURCE_LIMIT : INDEX_BUILD_SOURCE_LIMIT,pooledCap)
        : build.predicate
          ? Math.min(INDEX_PARTIAL_READ_SOURCE_LIMIT,Math.floor((this.credit.read-128)/4))
          : Math.min(INDEX_BUILD_SOURCE_LIMIT, Math.floor((this.credit.read - 64) / 3), Math.floor(this.credit.written - 16));
      if (cap < 0) deny('insufficient prepaid credit for index migration');
      const partialWriteCap=Math.min(INDEX_BUILD_SOURCE_LIMIT,fungible ? pooledCap : Math.floor(this.credit.written-16));
      if(build.predicate && partialWriteCap<0) deny('insufficient prepaid write credit for partial index migration');
      const table = '"main"."' + build.table.replaceAll('"', '""') + '"';
      const source = sql.exec(`SELECT 1 FROM ${table} NOT INDEXED LIMIT ?`, cap + 1).toArray();
      if (source.length > cap) deny('index source exceeds bounded migration allowance');
      if(build.predicate) {
        // A partial index reads every source row but writes only matching
        // entries. Prove BOTH dimensions before DDL: sparse job credentials
        // may have a large retained non-job history and only a few entries.
        // The source bound above makes this unindexed predicate probe finite.
        const matching=sql.exec(`SELECT 1 FROM ${table} NOT INDEXED WHERE ${build.predicate} LIMIT ?`,partialWriteCap+1).toArray();
        if(matching.length>partialWriteCap) deny('partial index entries exceed bounded migration allowance');
      }
    }
    return parsed.executable;
  }

  wrap(sql: SqlStorage): SqlStorage {
    const budget = this;
    return new Proxy(sql as any, {
      get(target, prop) {
        if (prop !== 'exec') {
          const value = target[prop];
          return typeof value === 'function' ? value.bind(target) : value;
        }
        return (query: string, ...bindings: any[]) => {
          budget.assertAllowed();
          // Run through a wrapped native handle: preflight must never be
          // unmetered SQL hidden behind the application-level cursor meter.
          const executable = budget.preflightIndexes(budget.wrap(target as SqlStorage), String(query));
          let cursor: any;
          try { cursor = target.exec(executable, ...bindings); }
          catch (error) {
            // These schema compilation failures execute no row work. Older
            // migrations intentionally catch them while adding existing columns.
            const message = String((error as any)?.message ?? error);
            if (/^(?:SQLITE_ERROR:\s*)?(?:duplicate column name:|table \S+ already exists|index \S+ already exists)/i.test(message)) throw error;
            // The runtime returned no cursor and therefore no trustworthy row
            // counters. Persist a daily stop; retries cannot hide this work.
            budget.ledger!.stopped = true;
            budget.dirty = true;
            budget.trip('SQL execution failed without measurable row counters');
          }
          let lastRead = 0, lastWritten = 0;
          const charge = () => {
            let read = NaN, written = NaN;
            try { read = Number(cursor.rowsRead); written = Number(cursor.rowsWritten); }
            catch {
              budget.ledger!.stopped = true; budget.dirty = true;
              budget.trip('SQL row counters unavailable');
            }
            const deltaRead = read - lastRead, deltaWritten = written - lastWritten;
            lastRead = read; lastWritten = written;
            budget.consume(deltaRead, deltaWritten);
          };
          charge();
          // Charge every next(), including raw() and Symbol.iterator. toArray()
          // must drain incrementally rather than bypassing the per-next guard.
          const advance = (source: any, args: any[]) => {
            budget.assertAllowed();
            let value: any;
            try { value = source.next(...args); }
            catch {
              // Failed cursor stepping may already have scanned rows. Charge
              // observable counters, then stop the day even if a caller catches.
              try { charge(); } catch { /* daily stop below is stronger */ }
              budget.ledger!.stopped = true; budget.dirty = true;
              budget.trip('SQL cursor stepping failed');
            }
            charge();
            return value;
          };
          const iterator = (source: any): any => ({
            next(...args: any[]) { return advance(source, args); },
            [Symbol.iterator]() { return this; },
          });
          return new Proxy(cursor, {
            get(c, key) {
              if (key === Symbol.iterator) return () => iterator(c[Symbol.iterator]());
              if (key === 'raw') return (...args: any[]) => iterator(c.raw(...args));
              if (key === 'next') return (...args: any[]) => advance(c, args);
              if (key === 'toArray') return () => Array.from(iterator(c[Symbol.iterator]()));
              if (key === 'one') return () => {
                const rows = iterator(c[Symbol.iterator]());
                const first = rows.next();
                if (first.done || !rows.next().done) throw new Error('Expected exactly one result from SQL query');
                return first.value;
              };
              const value = c[key];
              return typeof value === 'function' ? value.bind(c) : value;
            },
          });
        };
      },
    }) as SqlStorage;
  }
}
