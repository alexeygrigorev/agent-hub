// Run real DO constructor initialization against existing files before listening.
// Constructor recovery (for example pending intake-route registration) is real
// and idempotent. This probe never starts timers or delivers alarms/messages.
import { createHash } from 'node:crypto';
import { lstatSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { NodeDurableStorage, NodeSqlStorage } from './sqlite-storage.ts';

type Handler = { fetch?(request: Request): Response | Promise<Response> };
type Constructor = new (ctx: any, env: any) => Handler;
export type SchemaReadinessOptions = {
  classes: Record<string, Constructor>;
  env: Record<string, unknown>;
  dataDir: string;
};
type ExistingObject = { binding: string; file: string; name: string; sql?: NodeSqlStorage; handler?: Handler };
const MAX_DATABASES = 5000;
const READINESS_TIMEOUT_MS = 30_000;
const refusal = () => new Error('schema_readiness_failed');

function fileName(binding: string, name: string): string {
  const safe = name.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 60);
  const hash = createHash('sha1').update(`${binding}\u0000${name}`).digest('hex').slice(0, 10);
  return `${binding}-${safe}-${hash}.sqlite`;
}

/** Returns only the number of existing databases whose initialization completed.
 * Missing initialization dependencies fail closed rather than create databases.
 * SQL migrations/constructor recovery intentionally persist; rollback snapshots
 * must never be restored over user writes made while the service was running.
 */
export async function probeSchemaReadiness(options: SchemaReadinessOptions): Promise<number> {
  const objects = new Map<string, ExistingObject>();
  const settled: Promise<void>[] = [];
  const tracked = new Set<Promise<unknown>>();
  let failed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(refusal()), READINESS_TIMEOUT_MS);
  });
  // Also consume the timeout if synchronous validation fails before draining.
  void expired.catch(() => undefined);
  const track = <T>(promise: Promise<T>): Promise<T> => {
    if (!tracked.has(promise)) {
      tracked.add(promise);
      settled.push(promise.then(() => undefined, () => { failed = true; }));
    }
    return promise;
  };
  const drain = async () => {
    let offset = 0;
    while (offset < settled.length) {
      const batch = settled.slice(offset);
      offset = settled.length;
      await Promise.race([Promise.all(batch), expired]);
    }
  };
  const env: Record<string, unknown> = { ...options.env };
  const instantiate = (object: ExistingObject): Handler => {
    if (object.handler) return object.handler;
    if (object.sql) throw refusal();
    const path = join(options.dataDir, object.file);
    const stat = lstatSync(path);
    if (!stat.isFile() || stat.isSymbolicLink()) throw refusal();
    const sql = object.sql = new NodeSqlStorage(path);
    // Identity was introduced after some production databases. Its absence is
    // supported: binding comes from the filename, not a newly invented table.
    if (sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='hub2_identity'").toArray().length) {
      const identity = sql.exec('SELECT binding,name FROM hub2_identity WHERE singleton=1').toArray()[0];
      if (!identity || identity.binding !== object.binding || typeof identity.name !== 'string') throw refusal();
      object.name = identity.name;
    }
    const context = {
      id: { name: object.name, toString: () => `${object.binding}/${object.name}` },
      storage: new NodeDurableStorage(sql),
      blockConcurrencyWhile: <T>(callback: () => T | Promise<T>) => track(Promise.resolve().then(callback)),
      getWebSockets: () => [],
      acceptWebSocket: () => { throw refusal(); },
    };
    object.handler = new options.classes[object.binding](context, env);
    // Most classes retain the same promise as `ready`. Capture any additional
    // constructor-owned readiness promises too, with rejection handlers now.
    for (const value of Object.values(object.handler)) {
      if (value instanceof Promise) track(value);
    }
    return object.handler;
  };
  try {
    const directory = lstatSync(options.dataDir);
    if (!directory.isDirectory() || directory.isSymbolicLink()) throw refusal();
    for (const file of readdirSync(options.dataDir).sort()) {
      if (!file.endsWith('.sqlite')) continue;
      if (objects.size >= MAX_DATABASES) throw refusal();
      const binding = file.split('-', 1)[0];
      const stat = lstatSync(join(options.dataDir, file));
      if (!Object.hasOwn(options.classes, binding) || !stat.isFile() || stat.isSymbolicLink()) throw refusal();
      const legacyName = file.slice(binding.length + 1, -7).replace(/-[0-9a-f]{10}$/, '');
      objects.set(file, { binding, file, name: legacyName });
    }
    for (const binding of Object.keys(options.classes)) {
      env[binding] = {
        idFromName: (name: string) => ({ name, toString: () => `${binding}/${name}` }),
        get: (id: { name: string }) => ({
          fetch: (input: Request | string, init?: RequestInit) => track(Promise.resolve().then(() => {
            // Track lookup failures too: real constructor recovery may catch
            // the rejection and schedule a retry, but startup must still fail
            // closed when the dependency's database does not exist.
            const object = objects.get(fileName(binding, id.name));
            if (!object) throw refusal();
            const handler = instantiate(object);
            if (!handler.fetch) throw refusal();
            return handler.fetch(input instanceof Request ? input : new Request(input, init));
          })),
        }),
      };
    }
    for (const object of objects.values()) instantiate(object);
    await drain();
    if (failed) throw refusal();
    return objects.size;
  } catch {
    failed = true;
    // All registered rejections remain handled even when a later constructor
    // throws. Finish tracked work before releasing its SQLite handles.
    await drain().catch(() => undefined);
    throw refusal();
  } finally {
    if (timer) clearTimeout(timer);
    for (const object of objects.values()) {
      try { object.sql?.close(); } catch { /* Preserve the sanitized refusal. */ }
    }
  }
}
