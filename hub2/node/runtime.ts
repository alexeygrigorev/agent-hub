// Hosts hub2 Durable Object classes on Node: one lazily-constructed instance
// per (class, name), namespaces that route stub.fetch to instances, a periodic
// alarm sweep, and the WebSocket registry backing ctx.getWebSockets.

import { mkdirSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { NodeDurableStorage, NodeSqlStorage } from './sqlite-storage.ts';
import type { ServerSideSocket } from './wire.ts';

type DoHandler = {
  fetch?(request: Request): Promise<Response> | Response;
  alarm?(due: number): Promise<void> | void;
  webSocketMessage?(socket: unknown, message: string | ArrayBuffer): Promise<void> | void;
  webSocketClose?(socket: unknown, code: number, reason: string, wasClean: boolean): Promise<void> | void;
  webSocketError?(socket: unknown, error: unknown): Promise<void> | void;
};

type DoClass = new (ctx: any, env: unknown) => DoHandler;

type DoObjectId = { name: string; toString(): string };

type Namespace = {
  idFromName(name: string): DoObjectId;
  get(id: DoObjectId): { fetch(input: Request | string, init?: RequestInit): Promise<Response> };
};

function safeFileName(cls: string, name: string): string {
  const sanitized = name.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 60);
  const hash = createHash('sha1').update(`${cls}\u0000${name}`).digest('hex').slice(0, 10);
  return `${cls}-${sanitized}-${hash}.sqlite`;
}

class Container {
  private instance: DoHandler | undefined;
  private sql: NodeSqlStorage | undefined;
  private storage: NodeDurableStorage | undefined;
  private alarmRunning = false;
  readonly sockets = new Set<ServerSideSocket>();

  constructor(
    readonly cls: string,
    readonly name: string,
    private readonly doClass: DoClass,
    private readonly env: unknown,
    private readonly dataDir: string,
  ) {}

  private ensure(): { handler: DoHandler; storage: NodeDurableStorage } {
    if (!this.instance) {
      const sql = new NodeSqlStorage(join(this.dataDir, safeFileName(this.cls, this.name)));
      sql.exec('CREATE TABLE IF NOT EXISTS hub2_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL, name TEXT NOT NULL)');
      sql.exec('INSERT OR REPLACE INTO hub2_identity VALUES(1,?,?)',this.cls,this.name);
      const storage = new NodeDurableStorage(sql);
      const state = {
        // Node constructs each instance inside ensure() before any event can be
        // dispatched to it, so pass-through preserves the init-before-events order.
        blockConcurrencyWhile: async (fn: () => unknown) => fn(),
        storage,
        getWebSockets: (tag?: string) =>
          [...this.sockets].filter(socket => tag === undefined || socket.tags.includes(tag)),
        acceptWebSocket: (socket: ServerSideSocket, tags: string[] = []) => {
          socket.tags.push(...tags);
          this.sockets.add(socket);
          socket.__deliver = {
            message: data => this.socketMessage(socket, data),
            closed: () => {
              this.sockets.delete(socket);
              void Promise.resolve(this.ensure().handler.webSocketClose?.(socket, 1005, '', true)).catch(() => {});
            },
            error: error => {
              void Promise.resolve(this.ensure().handler.webSocketError?.(socket, error)).catch(() => {});
            },
          };
        },
      } as never;
      this.instance = new this.doClass(state, this.env);
      this.sql = sql;
      this.storage = storage;
    }
    return { handler: this.instance, storage: this.storage! };
  }

  fetch(request: Request): Promise<Response> {
    const { handler } = this.ensure();
    if (!handler.fetch) return Promise.resolve(new Response('no_fetch_handler', { status: 404 }));
    return Promise.resolve(handler.fetch(request));
  }

  private socketMessage(socket: ServerSideSocket, data: string | ArrayBuffer): void {
    const { handler } = this.ensure();
    void Promise.resolve(handler.webSocketMessage?.(socket, data)).catch(error => {
      void Promise.resolve(handler.webSocketError?.(socket, error)).catch(() => {});
    });
  }

  /** Delivers due alarms. The stored alarm is cleared before the handler runs,
   * so handlers that re-arm inside alarm() (setAlarm on a later deadline) are
   * preserved; a crashed alarm surfaces in logs, never silently. */
  async pollAlarm(now: number): Promise<void> {
    const { handler, storage } = this.ensure();
    const due = await storage.getAlarm();
    if (due === null || due > now || this.alarmRunning) return;
    this.alarmRunning = true;
    try {
      await storage.deleteAlarm();
      await handler.alarm?.(due);
    } catch (error) {
      console.error(`[hub2-node] ${this.cls}/${this.name} alarm failed:`, error);
    } finally {
      this.alarmRunning = false;
    }
  }

  close(): void {
    this.sql?.close();
  }
}

export type Hub2RuntimeOptions = {
  /** Binding name -> DO class, e.g. {CONTROL: ControlDO}. */
  classes: Record<string, DoClass>;
  /** Worker entry (production-worker default export). */
  entry: { fetch(request: Request, env: unknown): Promise<Response> };
  env: Record<string, unknown>;
  dataDir: string;
};

export class Hub2Runtime {
  readonly env: Record<string, unknown>;
  private readonly containers = new Map<string, Container>();
  private readonly timer: NodeJS.Timeout;

  constructor(private readonly options: Hub2RuntimeOptions) {
    mkdirSync(options.dataDir, { recursive: true });
    const namespaces: Record<string, Namespace> = {};
    for (const [binding, doClass] of Object.entries(options.classes)) {
      namespaces[binding] = this.makeNamespace(binding, doClass);
    }
    this.env = { ...options.env, ...namespaces };
    this.restoreAlarms();
    this.timer = setInterval(() => void this.sweepAlarms(), 1000);
    this.timer.unref();
  }

  private restoreAlarms(): void {
    for(const file of readdirSync(this.options.dataDir).filter(name=>name.endsWith('.sqlite'))){
      const sql=new DatabaseSync(join(this.options.dataDir,file),{readOnly:true});
      try{
        const tables=new Set((sql.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as Array<{name:string}>).map(row=>row.name));
        if(!tables.has('hub2_alarm')||!sql.prepare('SELECT due FROM hub2_alarm WHERE singleton=1 AND due IS NOT NULL').get())continue;
        let identity=tables.has('hub2_identity')?sql.prepare('SELECT binding,name FROM hub2_identity WHERE singleton=1').get() as {binding:string;name:string}|undefined:undefined;
        if(!identity){
          // Migrate existing files only when their exact hashed filename proves
          // the full name. Project names can also be read from retained intakes.
          for(const binding of Object.keys(this.options.classes)){
            const simple=file.slice(binding.length+1,-18);
            const names=[simple];
            if(binding==='PROJECTS'&&tables.has('intakes'))names.push(...(sql.prepare('SELECT DISTINCT project FROM intakes').all() as Array<{project:string}>).map(row=>row.project));
            if(binding==='MACHINES'&&tables.has('machines'))names.push(...(sql.prepare('SELECT machine_id FROM machines').all() as Array<{machine_id:string}>).map(row=>row.machine_id));
            const name=names.find(name=>safeFileName(binding,name)===file);
            if(name!==undefined){identity={binding,name};break;}
          }
        }
        if(!identity||!this.options.classes[identity.binding]||safeFileName(identity.binding,identity.name)!==file){
          console.error('[hub2-node] cannot recover alarm identity',file);
          continue;
        }
        // Construction is deferred until the sweep, after the read handle closes.
        this.container(identity.binding,this.options.classes[identity.binding],identity.name);
      }finally{sql.close();}
    }
  }

  private makeNamespace(binding: string, doClass: DoClass): Namespace {
    return {
      idFromName(name: string): DoObjectId {
        return { name, toString: () => `${binding}/${name}` };
      },
      get: (id: DoObjectId) => ({
        fetch: async (input: Request | string, init?: RequestInit) => {
          const request = input instanceof Request ? input : new Request(input, init);
          return this.container(binding, doClass, id.name).fetch(request);
        },
      }),
    };
  }

  private container(binding: string, doClass: DoClass, name: string): Container {
    const key = `${binding}\u0000${name}`;
    let container = this.containers.get(key);
    if (!container) {
      container = new Container(binding, name, doClass, this.env, this.options.dataDir);
      this.containers.set(key, container);
    }
    return container;
  }

  /** Direct container access for tests and administrative tools. */
  containerOf(binding: string, name: string): Container | undefined {
    return this.containers.get(`${binding}\u0000${name}`);
  }

  async fetch(request: Request): Promise<Response> {
    return this.options.entry.fetch(request, this.env);
  }

  private async sweepAlarms(): Promise<void> {
    for (const container of this.containers.values()) {
      await container.pollAlarm(Date.now()).catch(error => {
        console.error('[hub2-node] alarm sweep failed:', error);
      });
    }
  }

  async dispose(): Promise<void> {
    clearInterval(this.timer);
    for (const container of this.containers.values()) container.close();
    this.containers.clear();
  }
}
