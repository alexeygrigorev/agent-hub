import { initJobBackgroundProducers } from './background-job-producers';
import type { SqlStorage } from '@cloudflare/workers-types';

export const BACKGROUND_QUEUE_CAPACITY = 128;
export const BACKGROUND_MAX_ATTEMPTS = 3;
const MAX_PAYLOAD_BYTES = 2048;
const LEASE_MS = 30_000;
export type BackgroundKind = 'job-deadline' | 'session-wake' | 'webhook-delivery' | 'root-slot';
export type BackgroundIntent = { id: string; kind: BackgroundKind; dueAt: number; payload: Record<string, unknown> };
export type BackgroundWork = BackgroundIntent & { token: string; attempts: number; exhausted: boolean };
const KINDS = new Set<BackgroundKind>(['job-deadline','session-wake','webhook-delivery','root-slot']);

export class BackgroundQueueFull extends Error {
  constructor() { super('background_queue_capacity'); this.name = 'BackgroundQueueFull'; }
}

/** A queue of explicit entity mutations. Every read is an indexed LIMIT 1 or
 * a primary-key lookup. A lease remains a real pending row, so crashes retry
 * durably; three attempts end the intent instead of forming an alarm loop. */
export class BackgroundWorkQueue {
  constructor(private sql: SqlStorage, private tx: <T>(fn: () => T) => T, private now = Date.now) {}
  init(): void {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS hub_background_work(id TEXT PRIMARY KEY,kind TEXT NOT NULL,
      due_ms INTEGER NOT NULL,payload TEXT NOT NULL,token TEXT,attempts INTEGER NOT NULL DEFAULT 0,source_due_ms INTEGER NOT NULL DEFAULT 0)`);
    this.sql.exec('CREATE TABLE IF NOT EXISTS hub_background_failed(id TEXT PRIMARY KEY,kind TEXT NOT NULL,due_ms INTEGER NOT NULL,payload TEXT NOT NULL)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS hub_background_due ON hub_background_work(due_ms,id)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS hub_background_count(id INTEGER PRIMARY KEY CHECK(id=1),pending INTEGER NOT NULL)');
    this.sql.exec('INSERT OR IGNORE INTO hub_background_count VALUES(1,0)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS hub_background_entity_seed(id INTEGER PRIMARY KEY CHECK(id=1),done INTEGER NOT NULL)');
    // Source-domain triggers may enqueue known IDs directly; count updates
    // remain atomic and constant work for those producers too.
    this.sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_capacity BEFORE INSERT ON hub_background_work
      WHEN NOT EXISTS(SELECT 1 FROM hub_background_work WHERE id=NEW.id)
        AND NOT EXISTS(SELECT 1 FROM hub_background_failed WHERE id=NEW.id AND kind=NEW.kind AND due_ms=NEW.source_due_ms AND payload=NEW.payload)
        AND (SELECT pending FROM hub_background_count WHERE id=1)>=${BACKGROUND_QUEUE_CAPACITY}
      BEGIN SELECT RAISE(ABORT,'background_queue_capacity'); END`);
    this.sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_failed_fence BEFORE INSERT ON hub_background_work
      WHEN EXISTS(SELECT 1 FROM hub_background_failed WHERE id=NEW.id AND kind=NEW.kind AND due_ms=NEW.source_due_ms AND payload=NEW.payload)
      BEGIN SELECT RAISE(IGNORE); END`);
    this.sql.exec('CREATE TRIGGER IF NOT EXISTS hub_background_new_generation AFTER INSERT ON hub_background_work BEGIN DELETE FROM hub_background_failed WHERE id=NEW.id; END');
    this.sql.exec('CREATE TRIGGER IF NOT EXISTS hub_background_insert AFTER INSERT ON hub_background_work BEGIN UPDATE hub_background_count SET pending=pending+1 WHERE id=1; END');
    this.sql.exec('CREATE TRIGGER IF NOT EXISTS hub_background_delete AFTER DELETE ON hub_background_work BEGIN UPDATE hub_background_count SET pending=pending-1 WHERE id=1; END');
  }
  /** Install after source schemas exist. Producers address NEW.id only;
   * they cannot enumerate a fleet or turn a cron into maintenance work. */
  initEntityProducers(): void {
    this.tx(() => initJobBackgroundProducers(this.sql,intent => this.enqueue(intent)));
    const epoch = (value: string) => `ROUND((julianday(${value})-2440587.5)*86400000)`;
    const install = (table: string, key: string, kind: BackgroundKind, active: string, due: string, entity = 'NEW.id', generation = 'NULL') => {
      for (const event of ['INSERT','UPDATE']) this.sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_${key}_${event.toLowerCase()}
        AFTER ${event} ON ${table} BEGIN
          DELETE FROM hub_background_work WHERE id='${kind}:'||${entity} AND NOT(${active});
          INSERT INTO hub_background_work(id,kind,due_ms,payload,source_due_ms)
            SELECT '${kind}:'||${entity},'${kind}',${due},json_object('entity_id',${entity},'source_generation',${generation}),${due} WHERE ${active} AND (${due}) IS NOT NULL
            ON CONFLICT(id) DO UPDATE SET due_ms=excluded.due_ms,source_due_ms=excluded.source_due_ms,payload=excluded.payload,token=NULL,attempts=0
              WHERE hub_background_work.token IS NULL AND (hub_background_work.source_due_ms IS NOT excluded.source_due_ms OR hub_background_work.payload IS NOT excluded.payload);
        END`);
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_${key}_delete AFTER DELETE ON ${table}
        BEGIN DELETE FROM hub_background_work WHERE id='${kind}:'||${entity.replaceAll('NEW.','OLD.')}; END`);
    };
    const wakeDue = `CASE NEW.state WHEN 'pending' THEN NEW.wake_at_ms WHEN 'queued' THEN NEW.wake_at_ms+600000
      WHEN 'delivered' THEN COALESCE(${epoch('NEW.delivered_at')},NEW.wake_at_ms)+600000 END`;
    install('session_control_wakes','wake','session-wake',"NEW.state IN ('pending','queued','delivered')",wakeDue,'NEW.id','json_array(NEW.state,NEW.command_id,NEW.delivered_at)');
    install('session_control_pings','ping','session-wake',"NEW.state='active'",'NEW.next_at_ms',"'ping:'||NEW.id",'json_array(NEW.state,NEW.last_command_id)');
    const deliveryDue = `COALESCE(${epoch('NEW.next_attempt_at')},${epoch('NEW.created_at')},0)`;
    install('session_deliveries','delivery','webhook-delivery',"NEW.status IN ('pending','queued')",deliveryDue,'NEW.id','json_array(NEW.status,NEW.command_id,NEW.attempts)');
    this.sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_delivery_ack AFTER UPDATE OF ack_json ON session_control_commands
      WHEN NEW.ack_json IS NOT OLD.ack_json AND NEW.ack_json IS NOT NULL BEGIN
      UPDATE session_deliveries SET next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
      WHERE id IN(SELECT id FROM session_deliveries INDEXED BY session_deliveries_command_queued
        WHERE command_id=NEW.id AND status='queued' LIMIT 129); END`);

    this.tx(() => {
    if(this.sql.exec('SELECT done FROM hub_background_entity_seed WHERE id=1').toArray()[0]?.done)return;
    // Existing active work is real queued work, not retained history. Refuse
    // an incomplete restoration instead of silently dropping the 129th item.
    const intents = new Map<string, BackgroundIntent>();
    const add = (kind: BackgroundKind, id: string, dueAt: number, generation: unknown[]) => {
      const key=kind+':'+id;
      intents.set(key,{id:key,kind,dueAt,payload:{entity_id:id,source_generation:generation}});
      if(intents.size>BACKGROUND_QUEUE_CAPACITY)throw new BackgroundQueueFull();
    };
    for(const state of ['pending','queued','delivered']) {
      const remaining=BACKGROUND_QUEUE_CAPACITY-intents.size+1;
      const order=state==='delivered'?'delivered_at,id':'wake_at_ms,created_at,id';
      for(const row of this.sql.exec(`SELECT id,wake_at_ms,delivered_at,command_id FROM session_control_wakes WHERE state=? ORDER BY ${order} LIMIT ?`,state,remaining).toArray() as any[]) {
        const delivered=Date.parse(row.delivered_at);
        add('session-wake',row.id,state==='pending'?row.wake_at_ms:(state==='delivered'&&Number.isFinite(delivered)?delivered:row.wake_at_ms)+600000,[state,row.command_id??null,row.delivered_at??null]);
      }
    }
    for(const row of this.sql.exec("SELECT id,next_at_ms,last_command_id FROM session_control_pings WHERE state='active' ORDER BY next_at_ms LIMIT ?",BACKGROUND_QUEUE_CAPACITY-intents.size+1).toArray() as any[])
      add('session-wake','ping:'+row.id,row.next_at_ms,['active',row.last_command_id??null]);
    for(const status of ['pending','queued']) for(const row of this.sql.exec('SELECT id,next_attempt_at,created_at,command_id,attempts FROM session_deliveries WHERE status=? ORDER BY next_attempt_at LIMIT ?',status,BACKGROUND_QUEUE_CAPACITY-intents.size+1).toArray() as any[]) {
      const due=Date.parse(row.next_attempt_at);
      add('webhook-delivery',row.id,Number.isFinite(due)?due:(Date.parse(row.created_at)||0),[status,row.command_id??null,row.attempts]);
    }
    let additional=0;
    for(const id of intents.keys())if(!this.sql.exec('SELECT id FROM hub_background_work WHERE id=?',id).toArray().length)additional++;
    const count=Number(this.sql.exec('SELECT pending FROM hub_background_count WHERE id=1').toArray()[0]?.pending);
    if(!Number.isSafeInteger(count)||count<0||count+additional>BACKGROUND_QUEUE_CAPACITY)throw new BackgroundQueueFull();
    for(const intent of intents.values())this.enqueue(intent);
    this.sql.exec('INSERT OR REPLACE INTO hub_background_entity_seed VALUES(1,1)');
    });
  }

  private validTime(value: number): void {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error('invalid_background_due');
  }
  enqueue(intent: BackgroundIntent): boolean {
    this.validTime(intent.dueAt);
    if (!/^[A-Za-z0-9:._-]{1,160}$/.test(intent.id) || !KINDS.has(intent.kind)) throw new Error('invalid_background_intent');
    const payload = JSON.stringify(intent.payload);
    if (!payload || new TextEncoder().encode(payload).length > MAX_PAYLOAD_BYTES || !intent.payload || Array.isArray(intent.payload)) throw new Error('invalid_background_payload');
    return this.tx(() => {
      const failed:any=this.sql.exec('SELECT kind,due_ms,payload FROM hub_background_failed WHERE id=?',intent.id).toArray()[0];
      if(failed?.kind===intent.kind && failed.due_ms===intent.dueAt && failed.payload===payload)return false;
      const existing: any = this.sql.exec('SELECT kind,due_ms,source_due_ms,payload,token FROM hub_background_work WHERE id=?', intent.id).toArray()[0];
      if (existing) {
        if (existing.token!==null && existing.kind===intent.kind && existing.payload===payload) return false;
        if (existing.kind===intent.kind && existing.source_due_ms===intent.dueAt && existing.payload===payload) return false;
        this.sql.exec('UPDATE hub_background_work SET kind=?,due_ms=?,source_due_ms=?,payload=?,token=NULL,attempts=0 WHERE id=?', intent.kind,intent.dueAt,intent.dueAt,payload,intent.id);
        return true;
      }
      const count = this.sql.exec('SELECT pending FROM hub_background_count WHERE id=1').toArray()[0]?.pending;
      if (!Number.isSafeInteger(count) || Number(count)<0 || Number(count)>=BACKGROUND_QUEUE_CAPACITY) throw new BackgroundQueueFull();
      this.sql.exec('INSERT INTO hub_background_work(id,kind,due_ms,payload,source_due_ms) VALUES(?,?,?,?,?)', intent.id,intent.kind,intent.dueAt,payload,intent.dueAt);
      return true;
    });
  }
  takeDue(): BackgroundWork | null {
    return this.tx(() => {
      const now = this.now();
      const row: any = this.sql.exec('SELECT id,kind,due_ms,source_due_ms,payload,token,attempts FROM hub_background_work INDEXED BY hub_background_due WHERE due_ms<=? ORDER BY due_ms,id LIMIT 1', now).toArray()[0];
      if (!row) return null;
      const token = crypto.randomUUID();
      let payload: any, corrupt = false;
      try { payload=JSON.parse(row.payload); } catch { corrupt=true; }
      if(!payload || typeof payload!=='object' || Array.isArray(payload) || !KINDS.has(row.kind)
        || !Number.isSafeInteger(row.attempts) || row.attempts<0
        || typeof row.payload!=='string' || new TextEncoder().encode(row.payload).length>MAX_PAYLOAD_BYTES)corrupt=true;
      const work: BackgroundWork = { id:row.id,kind:row.kind,dueAt:row.due_ms,payload:payload??{},token,attempts:Number(row.attempts)+1,exhausted:corrupt||Number(row.attempts)>=BACKGROUND_MAX_ATTEMPTS };
      if (work.exhausted) {
        this.sql.exec('INSERT OR REPLACE INTO hub_background_failed(id,kind,due_ms,payload) VALUES(?,?,?,?)',row.id,row.kind,row.source_due_ms,row.payload);
        this.sql.exec('DELETE FROM hub_background_work WHERE id=?', row.id);
      } else this.sql.exec('UPDATE hub_background_work SET due_ms=?,token=?,attempts=? WHERE id=?', now+LEASE_MS,token,work.attempts,row.id);
      return work;
    });
  }
  complete(work: BackgroundWork): boolean {
    return this.tx(() => {
      const changed = this.sql.exec('DELETE FROM hub_background_work WHERE id=? AND token=? RETURNING id', work.id,work.token).toArray().length>0;
      return changed;
    });
  }
  cancel(id: string): boolean {
    return this.sql.exec('DELETE FROM hub_background_work WHERE id=? RETURNING id', id).toArray().length>0;
  }
  reschedule(work: BackgroundWork, dueAt: number, failed = false): boolean {
    this.validTime(dueAt);
    return this.sql.exec('UPDATE hub_background_work SET due_ms=?,token=NULL,attempts=? WHERE id=? AND token=? RETURNING id', Math.max(this.now()+15000,dueAt),failed?work.attempts:0,work.id,work.token).toArray().length>0;
  }
  nextAt(): number | null {
    const row: any = this.sql.exec('SELECT due_ms FROM hub_background_work INDEXED BY hub_background_due ORDER BY due_ms,id LIMIT 1').toArray()[0];
    return typeof row?.due_ms==='number' ? row.due_ms : null;
  }
}
