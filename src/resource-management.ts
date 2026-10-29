import { worktreeIntakeProtected, worktreeIntakeSnapshot } from './worktree-intakes';
import { openWorkPreventsIdleReap, type IdleReapCache } from './intake-session';
/** Live host admission and durable per-session usage; token usage joins by native identity. */
import type { SqlStorage } from '@cloudflare/workers-types';
import defaults from '../config/resource-management.json';
import { IntakeService } from './intake';
import { policy } from './model-routing';
import { cpuVerdict } from './cpu-headroom';
import { resolveMachineAliases } from './machine-alias';
import { jsonFields, resourceRequest } from './sql-json-projection';
export const MEMORY_FLOOR_MB = defaults.memory_floor_mb;
export const NEXT_SLOT_MB = defaults.ramp_reserve_default_mb;
// Intake 63a9ee3a / 736ba282: an offered, starting or uncertain launch holds
// its full estimate only this long after its claim (or creation when never
// claimed). Parked offers (host_overload) and stuck uncertain rows otherwise
// reserved memory forever: 15 unclaimed Hetzner offers held 38 GiB and
// refused every new placement. A worker that is really alive past this
// window is already counted in the host's MemAvailable, and the host re-runs
// its own memory gate before every claim.
export const IN_FLIGHT_RESERVE_SECONDS = 600;
export const CPU_LOAD_FACTOR = defaults.cpu_load_factor;
export type ResourceConfig = typeof defaults;
const parse = (s: any): any => { try { return JSON.parse(s || '{}'); } catch { return {}; } };
const finite = (n: any) => typeof n === 'number' && Number.isFinite(n) && n >= 0;
const TERMINAL = new Set(['exited', 'finished', 'failed', 'stopped', 'dead', 'refused']);
export const RESOURCE_SCHEMA = `
CREATE TABLE IF NOT EXISTS resource_resident_samples (launch_id TEXT PRIMARY KEY, resident_mb REAL NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_choking_events (machine_id TEXT NOT NULL, reason TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(machine_id,reason));
CREATE TABLE IF NOT EXISTS resource_cpu_samples (launch_id TEXT PRIMARY KEY, current_cpu_cores REAL NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_cpu_starts (launch_id TEXT PRIMARY KEY, expected_cpu_cores REAL NOT NULL, expected_cpu_source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_cpu_usage (session_id TEXT PRIMARY KEY, p95_cpu_cores REAL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_cpu_finalized (session_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_resource_metadata (session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_ramps (launch_id TEXT PRIMARY KEY, started_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_resource_usage (
 session_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL,
 fleet_launch_id TEXT, intake_id TEXT, project TEXT, engine TEXT, model TEXT, tier TEXT, kind TEXT,
 started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
 peak_rss_mb REAL, peak_pss_mb REAL, cpu_seconds REAL, runtime_s REAL, avg_cpu_cores REAL,
 observed_at TEXT NOT NULL, UNIQUE(machine_id,native_id));
CREATE INDEX IF NOT EXISTS session_resource_estimate ON session_resource_usage(engine,project,kind,finished_at);
CREATE INDEX IF NOT EXISTS resource_memory_recent ON session_resource_usage(engine,observed_at DESC) WHERE finished_at IS NOT NULL AND peak_rss_mb > 0;
CREATE INDEX IF NOT EXISTS resource_cpu_recent ON session_resource_usage(observed_at DESC) WHERE finished_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS resource_history_revision(id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL);
INSERT OR IGNORE INTO resource_history_revision VALUES(1,'initial');
CREATE TABLE IF NOT EXISTS resource_evidence (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, native_id TEXT NOT NULL, reason TEXT NOT NULL, saved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_reaps (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, intake_id TEXT, actor TEXT NOT NULL, reason TEXT NOT NULL, freed_mb REAL, at TEXT NOT NULL, evidence_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS resource_reaps_native ON resource_reaps(machine_id,native_id,session_id);
CREATE TABLE IF NOT EXISTS resource_throttles (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, intake_id TEXT, action TEXT NOT NULL, cap_cores REAL, sustain_s REAL, cpu_max TEXT, outcome TEXT, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS resource_throttles_native ON resource_throttles(machine_id,native_id,session_id);
`;
export function percentile(values: number[], p: number): number | null {
  if (!values.length) return null;
  const sorted = values.slice().sort((a,b) => a-b);
  return sorted[Math.max(0, Math.ceil(sorted.length * p / 100) - 1)];
}
export function resourceConfig(value: any = {}): ResourceConfig {
  const result = { ...defaults };
  const bounds: Record<string, number[]> = {
    memory_floor_mb: [128, 1048576], disk_floor_gb: [1, 1048576], ramp_reserve_default_mb: [64, 65536], estimate_percentile: [50,100],
    estimate_multiplier: [1,4], ramp_seconds: [30,900], cpu_load_factor: [.5,4],
    watchdog_interval_s: [300,600], idle_minutes: [10,10080], rss_flag_gb: [.125,1024],
    cpu_target_utilization:[.1,1],cpu_default_cores:[.05,128],cpu_estimate_multiplier:[1,4],cpu_psi_hard_cap_pct:[1,100],cpu_psi_full_hard_cap_pct:[.1,100],memory_psi_full_hard_cap_pct:[.1,100],choking_cooldown_seconds:[30,3600],
    cpu_throttle_cores:[.25,128],cpu_throttle_sustain_s:[30,3600],
  };
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_resource_config');
  for (const [k,v] of Object.entries(value)) {
    if (!bounds[k] || !finite(v) || (v as number) < bounds[k][0] || (v as number) > bounds[k][1]) throw new Error('invalid_resource_config');
    if (!['disk_floor_gb','cpu_load_factor','estimate_multiplier','rss_flag_gb','cpu_target_utilization','cpu_default_cores','cpu_estimate_multiplier','cpu_psi_hard_cap_pct','cpu_psi_full_hard_cap_pct','memory_psi_full_hard_cap_pct','cpu_throttle_cores'].includes(k) && !Number.isSafeInteger(v)) throw new Error('invalid_resource_config');
    (result as any)[k] = v;
  }
  return result;
}
export function memoryVerdict(available: number, ramp: number, estimate: number, config: ResourceConfig) {
  return finite(available) && finite(ramp) && finite(estimate) && available - ramp - estimate >= config.memory_floor_mb;
}
// Idle-reap authority reads only request.intake_task_id; skip copying and
// re-parsing whole retained prompts per session. Invalid JSON passes through
// unchanged and still parses as an empty request.
const SLIM_REQUEST = `CASE WHEN json_valid(request_json) THEN json_object('intake_task_id', json_extract(request_json, '$.intake_task_id')) ELSE request_json END`;
// Matches the session_control_native index expression (src/hub.ts).
const NATIVE_KEY = `CASE WHEN json_valid(binding_json) THEN CASE WHEN json_type(binding_json, '$.native_id') = 'text'
  THEN json_extract(binding_json, '$.native_id') END END`;
export class ResourceManagement {
  private catalogReady = false;
  private catalogStructureRevision:string|null=null;
  private catalogRawRows=new Map<string,any[]>();
  private catalogRawBytes=0;
  private catalogIntakes: ReturnType<typeof worktreeIntakeSnapshot> | null = null;
  private catalogCache = new Map<string, {revision:string;rows:ReturnType<ResourceManagement['buildCanonicalSessions']>;bytes:number}>();
  /** Install after all canonical source schemas exist. SQLite triggers keep
   * protection/identity changes and their cache generations in one transaction. */
  initCatalogCache() {
    this.catalogReady=false; this.catalogCache.clear(); this.catalogRawRows.clear(); this.catalogRawBytes=0; this.catalogStructureRevision=null; this.catalogIntakes=null;
    // Every table read by worktreeIntakeSnapshot must bump structure_revision.
    // Its cached closure/DONE verdicts must never survive an authority write.
    const tables=['session_control_sessions','fleet_launches','fleet_sessions','session_control_protections','resource_reaps','intake_tasks','intake_task_projection','resource_request_projection','machines'];
    if (tables.some(table=>!this.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",table).toArray().length)) return;
    this.sql.exec('CREATE TABLE IF NOT EXISTS resource_catalog_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision TEXT NOT NULL,structure_revision TEXT NOT NULL)');
    this.sql.exec("INSERT OR IGNORE INTO resource_catalog_revision VALUES(1,lower(hex(randomblob(16))),lower(hex(randomblob(16))))");
    for(const table of tables) for(const op of ['INSERT','UPDATE','DELETE']) {
      this.sql.exec(`CREATE TRIGGER IF NOT EXISTS resource_catalog_${table}_${op.toLowerCase()} AFTER ${op} ON ${table} ${table === 'machines' && op === 'UPDATE' ? 'WHEN OLD.hostname IS NOT NEW.hostname OR OLD.id IS NOT NEW.id' : ''}
        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16)))${table==='fleet_sessions' ? '' : ',structure_revision=lower(hex(randomblob(16)))'} WHERE id=1; END`);
    }
    this.catalogReady=true;
  }
  private historyRevision: string | null = null;
  private memoryHistory = new Map<string | null, any[]>();
  private cpuHistory: any[] | null = null;
  private memoryEstimates = new Map<string, number>();
  private cpuEstimates = new Map<string, {expected_cpu_cores: number; expected_cpu_source: string}>();
  private historyScope = 0;
  private readSnapshot: Map<string, any[]> | null = null;
  private snapshotNow: number | null = null;
  private cpuPendingSnapshot: Map<string, {id:string;remaining:number}[]> | null = null;
  constructor(private sql: SqlStorage, private now: () => number = Date.now) {}
  init() {
    for (const statement of RESOURCE_SCHEMA.split(';')) if (statement.trim()) this.sql.exec(statement);
    for (const table of ['session_resource_usage', 'session_cpu_usage', 'resource_settings']) {
      for (const op of ['INSERT', 'UPDATE', 'DELETE']) {
        const sides = op === 'INSERT' ? ['NEW'] : op === 'DELETE' ? ['OLD'] : ['OLD', 'NEW'];
        const relevant = table === 'session_resource_usage' ? sides.map(s => `${s}.finished_at IS NOT NULL`).join(' OR ')
          : table === 'session_cpu_usage' ? sides.map(s => `EXISTS(SELECT 1 FROM session_resource_usage WHERE session_id=${s}.session_id AND finished_at IS NOT NULL)`).join(' OR ') : '1';
        this.sql.exec(`CREATE TRIGGER IF NOT EXISTS resource_history_${table}_${op.toLowerCase()}
          AFTER ${op} ON ${table} WHEN ${relevant} BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END`);
      }
    }
  }
  private refreshHistory() {
    if (this.historyScope) return;
    // Random generations avoid reusing a cached revision after a transaction
    // rolls back and another writer subsequently advances the same counter.
    const revision = String(this.readRows('SELECT revision FROM resource_history_revision WHERE id=1')[0].revision);
    if (revision === this.historyRevision) return;
    this.historyRevision = revision;
    this.memoryHistory.clear(); this.cpuHistory = null;
    this.memoryEstimates.clear(); this.cpuEstimates.clear();
  }
  /** Only synchronous read-only estimation; never cache pending commitments. */
  private withHistory<T>(fn: () => T): T {
    this.refreshHistory(); this.historyScope++;
    try { return fn(); } finally { this.historyScope--; }
  }
  /** Synchronous, read-only response projection after all admission writes. */
  withReadSnapshot<T>(fn: () => T): T {
    if (this.readSnapshot) return fn();
    this.readSnapshot = new Map();
    this.cpuPendingSnapshot = new Map();
    this.snapshotNow = this.now();
    try { return fn(); } finally { this.readSnapshot = null; this.cpuPendingSnapshot = null; this.snapshotNow = null; }
  }
  private readRows(query: string, ...args: any[]): any[] {
    const key = JSON.stringify([query,args]);
    const cached = this.readSnapshot?.get(key);
    if (cached) return cached;
    const rows = this.sql.exec(query,...args).toArray();
    this.readSnapshot?.set(key,rows);
    return rows;
  }
  config(): ResourceConfig { return resourceConfig(parse(this.readRows('SELECT data FROM resource_settings WHERE id=1')[0]?.data)); }
  configure(value: any) {
    const config = resourceConfig(value);
    this.sql.exec('INSERT INTO resource_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', JSON.stringify(config));
    return config;
  }
  estimate(project: string | null, engine: string | null, kind: string | null) {
    this.refreshHistory();
    const key = JSON.stringify([project, engine, kind]);
    const cached = this.memoryEstimates.get(key);
    if (cached !== undefined) return cached;
    const config = this.config();
    // Only actual measurements, never a cgroup cap or fabricated zero. Bounded recent history.
    let rows = this.memoryHistory.get(engine);
    if (!rows) {
      rows = this.sql.exec('SELECT project,kind,peak_rss_mb FROM session_resource_usage WHERE engine IS ? AND finished_at IS NOT NULL AND peak_rss_mb > 0 ORDER BY observed_at DESC LIMIT 500', engine).toArray() as any[];
      if (this.memoryHistory.size >= 64) this.memoryHistory.clear();
      this.memoryHistory.set(engine, rows);
    }
    const specific = rows.filter(r => r.project === project && r.kind === kind);
    const measured = percentile((specific.length ? specific : rows).map(r => Number(r.peak_rss_mb)), config.estimate_percentile);
    const result = Math.ceil(Math.max(64, measured === null ? config.ramp_reserve_default_mb : measured * config.estimate_multiplier));
    if (this.memoryEstimates.size >= 2048) this.memoryEstimates.clear();
    this.memoryEstimates.set(key, result);
    return result;
  }
  cpuEstimate(project: string | null, kind: string | null) {
    this.refreshHistory();
    const key = JSON.stringify([project, kind]);
    const cached = this.cpuEstimates.get(key);
    if (cached) return {...cached};
    const rows = this.cpuHistory ??= this.sql.exec(`SELECT u.project,u.kind,c.p95_cpu_cores FROM session_resource_usage u
      JOIN session_cpu_usage c ON c.session_id=u.session_id WHERE u.finished_at IS NOT NULL
      AND c.p95_cpu_cores IS NOT NULL ORDER BY u.observed_at DESC LIMIT 500`).toArray() as any[];
    const specific = rows.filter(r=>r.project===project && r.kind===kind);
    const repo = rows.filter(r=>r.project===project);
    const cohort = specific.length ? specific : repo.length ? repo : rows;
    const config=this.config();
    const measured=percentile(cohort.map(r=>Number(r.p95_cpu_cores)),95);
    const result = {expected_cpu_cores:measured === null ? config.cpu_default_cores : measured*config.cpu_estimate_multiplier,
      expected_cpu_source:specific.length?'repo_kind':repo.length?'repo':rows.length?'global':'default'};
    if (this.cpuEstimates.size >= 2048) this.cpuEstimates.clear();
    this.cpuEstimates.set(key, result);
    return {...result};
  }
  freezeCpuStart(launch: string, estimate: {expected_cpu_cores:number,expected_cpu_source:string}) {
    this.sql.exec('INSERT OR IGNORE INTO resource_cpu_starts VALUES(?,?,?)',launch,estimate.expected_cpu_cores,estimate.expected_cpu_source);
  }
  cpuStart(launch: string) {
    return this.sql.exec('SELECT expected_cpu_cores,expected_cpu_source FROM resource_cpu_starts WHERE launch_id=?',launch).toArray()[0] ?? null;
  }
  cpuHeadroom(machine: string, input: any = {}, excludeLaunch?: string) {
    return this.withHistory(() => this.cpuHeadroomFromHistory(machine, input, excludeLaunch));
  }
  private cpuHeadroomFromHistory(machine: string, input: any, excludeLaunch?: string) {
    const now = this.snapshotNow ?? this.now();
    const m:any=this.readRows('SELECT last_heartbeat,capabilities FROM machines WHERE id=?',machine)[0];
    const hb=parse(m?.last_heartbeat),config=this.config(),cutoff=new Date(now-config.ramp_seconds*1000).toISOString();
    const inFlightCutoff=new Date(now-IN_FLIGHT_RESERVE_SECONDS*1000).toISOString();
    const estimate:any=(excludeLaunch ? this.cpuStart(excludeLaunch) : null) ?? this.cpuEstimate(input.project || input.workspace || null,input.task_kind || input.kind || 'goal');
    // Expose the common state range to the machine/state index; the CASE below
    // applies the running-ramp cutoff or the bounded in-flight window and is
    // NULL (no reservation) for any other state, so the index filter stays
    // an optimisation only.
    const pending=this.readRows(`SELECT l.id,${resourceRequest('fleet_launches','l.id','l.request_json')} AS request_json,l.workspace,s.expected_cpu_cores,
      c.current_cpu_cores,c.observed_at AS cpu_observed_at FROM fleet_launches l
      LEFT JOIN resource_cpu_starts s ON s.launch_id=l.id LEFT JOIN resource_cpu_samples c ON c.launch_id=l.id WHERE machine_id=?
      AND l.state IN ('offered','starting','running','uncertain')
      AND COALESCE((SELECT started_at FROM resource_ramps WHERE launch_id=l.id),created_at)>=CASE WHEN state='running' THEN ? WHEN state IN ('offered','starting','uncertain') THEN ? END`,machine,cutoff,inFlightCutoff) as any[];
    const pendingKey=JSON.stringify([machine,cutoff,inFlightCutoff,now]);
    let commitments=this.cpuPendingSnapshot?.get(pendingKey);
    if (!commitments) {
      commitments=pending.map(row=>{
        const req=parse(row.request_json);
        const committed = row.expected_cpu_cores ?? this.cpuEstimate(req.project || row.workspace,req.task_kind || 'goal').expected_cpu_cores;
      // Dispatcher reports per-session counters after the host heartbeat, on
      // separate 30–60s polls. Fresh exact-binding measurements are advisory
      // ramp credit; they need not land inside the 200ms host /proc/stat sample.
        const at=Date.parse(row.cpu_observed_at);
        const credit=Number.isFinite(at)&&at>=now-180000&&at<=now+30000&&finite(row.current_cpu_cores) ? row.current_cpu_cores : 0;
        return {id:row.id,remaining:Math.max(0,committed-credit)};
      });
      this.cpuPendingSnapshot?.set(pendingKey,commitments);
    }
    // Keep the original summation order, excluding only this exact launch.
    let remaining=0;
    for(const row of commitments) if(row.id!==excludeLaunch) remaining+=row.remaining;
    const verdict=cpuVerdict(hb.cpu_usage,parse(m?.capabilities).cpu_count,estimate.expected_cpu_cores,remaining,config,now);
    const choking=this.chokingState(machine,now,config,verdict.reason);
    return {...verdict,...choking,admitted:choking.reason===null,expected_cpu_source:estimate.expected_cpu_source};
  }
  resourceChoking(machine: string) {
    // Watchdog needs current failure evidence, not a hypothetical launch's
    // reservation estimate across every pending request.
    const now=this.snapshotNow ?? this.now(),config=this.config();
    const m:any=this.readRows('SELECT last_heartbeat,capabilities FROM machines WHERE id=?',machine)[0];
    const verdict=cpuVerdict(parse(m?.last_heartbeat).cpu_usage,parse(m?.capabilities).cpu_count,0,0,config,now);
    return this.chokingState(machine,now,config,verdict.reason).resource_choking;
  }
  private chokingState(machine: string, now: number, config: ResourceConfig, verdictReason: string | null) {
    const cooldown=new Date(now-config.choking_cooldown_seconds*1000).toISOString();
    const event:any=this.readRows(`SELECT reason,observed_at FROM resource_choking_events WHERE machine_id=? AND observed_at>=? AND reason!='host_runtime_choking' ORDER BY observed_at DESC LIMIT 1`,machine,cooldown)[0];
    const runtime:any=this.readRows(`SELECT exit_reason,updated_at FROM fleet_launches WHERE machine_id=? AND updated_at>=?
      AND state IN ('failed','exited') AND exit_reason IN ('engine_runtime_timeout','engine_signal_exit')
      ORDER BY updated_at DESC LIMIT 1`,machine,cooldown)[0];
    let serviceChoking=false; let serviceAt:string|null=null;
    if(this.readRows("SELECT name FROM sqlite_master WHERE type='table' AND name='host_health_observations'").length) {
      const health:any=this.readRows('SELECT payload_json,observed_at FROM host_health_observations WHERE machine_id=? AND observed_at>=?',machine,cooldown)[0];
      serviceAt=health?.observed_at ?? null;
      // A missing native worker can be an ordinary stop, not a crash. Only
      // explicit runtime probes establish a service timeout/crash. Legacy
      // generic worker-broken cooldowns are likewise not admission evidence.
      serviceChoking=(parse(health?.payload_json).services || []).some((s:any)=>
        s.worker==='broken' && ['service_crash','service_timeout'].includes(s.reason));
    }
    const reason=event?.reason || (runtime || serviceChoking ? 'host_runtime_choking' : verdictReason);
    const chokingAt=event?.observed_at || runtime?.updated_at || (serviceChoking ? serviceAt : null);
    return {reason,choking_observed_at:chokingAt,
      resource_choking:['host_oom','host_runtime_choking'].includes(reason)&&chokingAt ? {reason,observed_at:chokingAt} : null};
  }
  headroom(machine: string, input: any = {}, engine: string | null = null) {
    return this.withHistory(() => this.headroomFromHistory(machine, input, engine));
  }
  private headroomFromHistory(machine: string, input: any, engine: string | null) {
    const now = this.snapshotNow ?? this.now();
    const machineRow: any = this.readRows('SELECT last_heartbeat FROM machines WHERE id=?', machine)[0];
    const hb = parse(machineRow?.last_heartbeat); const config = this.config();
    const cutoff = new Date(now - config.ramp_seconds * 1000).toISOString();
    const inFlightCutoff = new Date(now - IN_FLIGHT_RESERVE_SECONDS * 1000).toISOString();
    // Only launch ramps are additional commitments. Resident usage is already in MemAvailable.
    // The explicit state range lets SQLite skip retained terminal history.
    const pending = this.readRows(`SELECT l.id,l.engine,l.workspace,${resourceRequest('fleet_launches','l.id','l.request_json')} AS request_json,r.resident_mb FROM fleet_launches l
      LEFT JOIN resource_resident_samples r ON r.launch_id=l.id AND r.observed_at>=?
      WHERE l.machine_id=? AND l.state IN ('offered','starting','running','uncertain')
      AND COALESCE((SELECT started_at FROM resource_ramps WHERE launch_id=l.id),l.created_at)
        >=CASE WHEN l.state='running' THEN ? WHEN l.state IN ('offered','starting','uncertain') THEN ? END`, cutoff, machine, cutoff, inFlightCutoff) as any[];
    let reserved = 0;
    for (const row of pending) {
      const req = parse(row.request_json);
      const estimate = this.estimate(req.project || row.workspace, row.engine, req.task_kind || req.kind || 'goal');
      reserved += Math.max(0, estimate - (finite(row.resident_mb) ? row.resident_mb : 0));
    }
    return { mem_available_mb: finite(hb.mem_avail_mb) ? hb.mem_avail_mb : null,
      reserved_mb: reserved, floor_mb: config.memory_floor_mb,
      requested_mb: this.estimate(input.project || input.workspace || null, engine, input.task_kind || input.kind || 'goal'),
      observed_at: hb.launcher_resources?.observed_at ?? hb.at ?? null };
  }
  private boundSessions(machine?: string) {
    // Share repeated immutable JSON only within this catalog read. Protection
    // and binding facts are always re-read on the next request.
    const parsed = new Map<any, any>();
    const catalogJson = (raw: any) => {
      const key = raw || '{}';
      if (!parsed.has(key)) parsed.set(key, parse(key));
      return parsed.get(key);
    };
    // Native activity changes do not require rereading immutable control and
    // launch projections. These rows contain SQL scalars only; JSON is parsed
    // afresh below, and every structural/authority write invalidates them.
    const read=(query:string,...bindings:any[]):any[]=>{
      const key=JSON.stringify([query,bindings]);
      const prior=this.catalogRawRows.get(key);
      if(this.catalogStructureRevision!==null && prior) return prior;
      const value=this.sql.exec(query,...bindings).toArray();
      const bytes=value.reduce((n,row)=>n+256+Object.values(row).reduce<number>((sum,v)=>sum+(typeof v==='string'?v.length*2:8),0),0);
      if(this.catalogStructureRevision!==null && value.length<=10000 && bytes<=2*1024*1024 && value.every(row=>Object.values(row).every(v=>v===null || typeof v!=='object'))) {
        if(this.catalogRawRows.size>=4 || this.catalogRawBytes+bytes>4*1024*1024) {this.catalogRawRows.clear();this.catalogRawBytes=0;}
        this.catalogRawRows.set(key,value);this.catalogRawBytes+=bytes;
      }
      return value;
    };
    // Canonical binding, not tag matching; external interactive sessions can be sampled but never reaped.
    const rows = read(`SELECT s.id,s.machine_id,s.fleet_launch_id,s.binding_json,${resourceRequest('session_control_sessions','s.id','s.request_json')} AS request_json,s.request_key,
      s.engine,s.mode,s.state,s.created_at,s.updated_at,${resourceRequest('fleet_launches','l.id','l.request_json')} AS launch_request,l.state AS launch_state,l.created_at AS launch_created,
      l.finished_at AS launch_finished,${jsonFields('l.model_selection_json',['model'])} AS model_selection_json,l.workspace AS launch_workspace
      FROM session_control_sessions s LEFT JOIN fleet_launches l ON l.id=s.fleet_launch_id
      ${machine ? 'WHERE s.machine_id=?' : ''} ORDER BY CASE WHEN s.state IN ('stopped','failed','exited','finished','dead','refused','cancelled') THEN 1 ELSE 0 END,s.updated_at DESC`, ...(machine ? [machine] : []));
    const launches = read(`SELECT l.id,l.machine_id,l.native_id,l.workspace,l.engine,l.mode,${resourceRequest('fleet_launches','l.id','l.request_json')} AS request_json,
      ${jsonFields('l.model_selection_json',['model'])} AS model_selection_json,l.state,l.finished_at,l.updated_at
      FROM fleet_launches l WHERE l.native_id IS NOT NULL AND l.native_id<>'' ${machine ? 'AND l.machine_id=?' : ''}`,
      ...(machine ? [machine] : []));
    // Hydrate the complete alias set before selecting canonical sessions. Batch
    // catalog facts instead of re-reading full intake histories for every alias.
    const key = (...parts: any[]) => JSON.stringify(parts);
    const observedByNative = new Map(this.sql.exec(`SELECT machine_id,native_id,tag,last_activity_at,created_at_ms,phase
      FROM fleet_sessions ${machine ? 'WHERE machine_id=?' : ''}`, ...(machine ? [machine] : [])).toArray()
      .map((r:any)=>[key(r.machine_id,r.native_id),r]));
    const protectedIds = new Set(this.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_protections'").toArray().length
      ? this.sql.exec(`SELECT p.session_id FROM session_control_protections p JOIN session_control_sessions s ON s.id=p.session_id
          ${machine ? 'WHERE s.machine_id=?' : ''}`, ...(machine ? [machine] : [])).toArray().map((r:any)=>r.session_id) : []);
    const reapRows = this.sql.exec(`SELECT session_id,machine_id,native_id FROM resource_reaps ${machine ? 'WHERE machine_id=?' : ''}`,
      ...(machine ? [machine] : [])).toArray();
    const reapedAliases = new Set(reapRows.map((r:any)=>key(r.session_id,r.machine_id,r.native_id)));
    const reapedNatives = new Set(reapRows.map((r:any)=>key(r.machine_id,r.native_id)));
    const controlData = new Map(rows.flatMap(s=>{
      const binding=catalogJson(s.binding_json);
      if(!binding.native_id || !binding.workspace || !binding.tag) return [];
      return [[s.id,{binding,req:catalogJson(s.request_json),launch:catalogJson(s.launch_request),model:catalogJson(s.model_selection_json)}] as const];
    }));
    const launchData = new Map(launches.filter(l=>l.machine_id && l.workspace && l.engine && ['headless','interactive'].includes(l.mode))
      .map(l=>[l.id,{req:catalogJson(l.request_json),model:catalogJson(l.model_selection_json)}]));
    const intakeIds = [...new Set([...controlData.values()].map(s=>s.req.intake_task_id || s.launch.intake_task_id)
      .concat([...launchData.values()].map(l=>l.req.intake_task_id)).filter(Boolean))];
    const projects = new Map<any,any>();
    const intakeTable = this.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_task_projection'").toArray().length
      ? 'intake_task_projection' : 'intake_tasks';
    for(let i=0;i<intakeIds.length;i+=100) {
      const ids=intakeIds.slice(i,i+100);
      for(const row of this.sql.exec(`SELECT id,json_extract(CASE WHEN json_valid(data) THEN data ELSE '{}' END,'$.project') AS project
        FROM ${intakeTable} WHERE id IN (${ids.map(()=>'?').join(',')})`,...ids).toArray()) projects.set(row.id,row.project);
    }
    // Native census activity invalidates the canonical rows, but cannot change
    // intake closure or binding aliases. Reuse those facts until a structural
    // write changes the rollback-safe generation. Keep failures uncached.
    const worktreeLinks = this.catalogIntakes ?? worktreeIntakeSnapshot(this.sql);
    if (!this.catalogIntakes && this.catalogStructureRevision !== null && worktreeLinks.valid) {
      const entries = [...worktreeLinks.machines, ...worktreeLinks.projections, ...worktreeLinks.sessions];
      if (entries.length <= 10000 && entries.reduce((n, row) => n + 512 + JSON.stringify(row).length * 2, 0) <= 4 * 1024 * 1024) {
        this.catalogIntakes = worktreeLinks;
      }
    }
    const bound = rows.flatMap(s => {
      const data=controlData.get(s.id); if(!data) return [];
      const {binding,req,launch,model}=data;
      const intakeId=req.intake_task_id || launch.intake_task_id || null;
      const project=projects.get(intakeId) || req.project || launch.project || s.launch_workspace || binding.workspace;
      const observed: any = observedByNative.get(key(s.machine_id,binding.native_id));
      const protection = protectedIds.has(s.id);
      return [{ session_id: s.id, machine_id:s.machine_id, fleet_launch_id:s.fleet_launch_id,
        observed_shadow:!s.fleet_launch_id && s.id===binding.native_id && s.request_key==='native-'+binding.native_id
          && req.provenance==='observed_native' && binding.provenance==='observed_native',
        intake_id:intakeId,
        linked_intake_open:worktreeIntakeProtected(this.sql, s, worktreeLinks),
        native_id:binding.native_id, tag:binding.tag, workspace:binding.workspace,
        native_created_at_ms:binding.native_created_at_ms ?? observed?.created_at_ms ?? null,
        engine:s.engine, mode:s.mode, model:model.model ?? null, project,
        tier:req.tier || launch.tier || (s.fleet_launch_id ? policy.default_tier : null), kind:req.task_kind || launch.task_kind || 'goal',
        state:TERMINAL.has(s.state) ? s.state : s.launch_state || s.state, keep:protection || req.keep === true || launch.keep === true,
        managed:!!s.fleet_launch_id && s.mode === 'headless',
        reaped:reapedAliases.has(key(s.id,s.machine_id,binding.native_id)),
        last_activity_at:observed?.last_activity_at ?? null, started_at:Number.isFinite(binding.native_created_at_ms) ? new Date(binding.native_created_at_ms).toISOString() : s.launch_created || s.created_at,
        finished_at:s.launch_finished || (TERMINAL.has(s.state) ? s.updated_at : null) }];
    });
    // Routing can launch through Fleet without a SessionControl intent. Its
    // host-confirmed native identity still needs resource accounting. These
    // entries are accounting only: they create no control binding and grant
    // no new watchdog stop authority, including when an intake is attached.
    const shadowIds=new Map(bound.filter(s=>s.observed_shadow).map(s=>[JSON.stringify([s.machine_id,s.native_id]),s.session_id]));
    const fleetOnly = launches.flatMap(l => {
      if (!l.machine_id || !l.workspace || !l.engine || !['headless','interactive'].includes(l.mode)) return [];
      const {req,model}=launchData.get(l.id)!;
      const observed:any=observedByNative.get(key(l.machine_id,l.native_id));
      const intakeId=req.intake_task_id || null;
      return [{session_id:shadowIds.get(JSON.stringify([l.machine_id,l.native_id])) ?? 'fleet:'+l.id,machine_id:l.machine_id,fleet_launch_id:l.id,observed_shadow:false,
        intake_id:intakeId,linked_intake_open:worktreeIntakeProtected(this.sql, { id: l.id, machine_id: l.machine_id, request_json: l.request_json, binding_json: JSON.stringify({native_id:l.native_id}) }, worktreeLinks),native_id:l.native_id,tag:observed?.tag || (l.mode==='headless' ? 'task-hub-' : 'hub-')+l.id,
        workspace:l.workspace,native_created_at_ms:observed?.created_at_ms ?? null,
        engine:l.engine,mode:l.mode,model:model.model ?? null,project:projects.get(intakeId) || req.project || l.workspace,
        tier:req.tier || policy.default_tier,kind:req.task_kind || 'goal',state:l.state,keep:req.keep===true,managed:false,
        reaped:reapedNatives.has(key(l.machine_id,l.native_id)),
        last_activity_at:observed?.last_activity_at ?? null,
        // The request may have queued for hours; it is not a native start time.
        started_at:Number.isFinite(observed?.created_at_ms) ? new Date(observed.created_at_ms).toISOString() : null,
        finished_at:l.finished_at || (TERMINAL.has(l.state) ? l.updated_at : null)}];
    });
    return [...bound,...fleetOnly];
  }
  private canonicalSessions(machine?: string) {
    // No time-based authority cache: every source write changes the durable
    // random generation, including writes followed by rollback and retry.
    const raw=this.catalogReady ? this.sql.exec('SELECT revision,structure_revision FROM resource_catalog_revision WHERE id=1').toArray()[0] : null;
    const revision=typeof raw?.revision==='string' && /^[a-f0-9]{32}$/.test(raw.revision) ? raw.revision : null;
    const structure=revision!==null && typeof raw?.structure_revision==='string' && /^[a-f0-9]{32}$/.test(raw.structure_revision) ? raw.structure_revision : null;
    if(structure===null || structure!==this.catalogStructureRevision) {this.catalogRawRows.clear();this.catalogRawBytes=0;this.catalogIntakes=null;}
    this.catalogStructureRevision=structure;
    if(revision===null) this.catalogCache.clear();
    const key=machine ?? ''; const cached=this.catalogCache.get(key);
    let rows:ReturnType<ResourceManagement['buildCanonicalSessions']>;
    if(revision!==null && cached?.revision===revision) rows=cached.rows;
    else {
      rows=this.buildCanonicalSessions(machine);
      // Bound retained memory, including aliases; malformed legacy nested
      // values bypass caching so callers never share mutable nested data.
      const bytes=rows.reduce((n,row)=>n+256+Object.values(row).reduce<number>((sum,v)=>sum+(typeof v==='string'?v.length*2:Array.isArray(v)?v.reduce((a,b)=>a+String(b).length*2,0):8),0),0);
      const cacheable=rows.length<=10000 && rows.reduce((n,row)=>n+row.aliases.length,0)<=20000 && bytes<=4*1024*1024
        && rows.every(row=>Object.entries(row).every(([field,value])=>field==='aliases' || value===null || typeof value!=='object'));
      if(revision!==null && cacheable) {
        // Four MiB estimates each for raw, canonical and intake snapshot caches,
        // independent of host count. Snapshot closures retain only booleans
        // and row indexes, never raw intake history payloads.
        if(this.catalogCache.size>=8 || [...this.catalogCache.values()].reduce((n,v)=>n+v.bytes,0)+bytes>4*1024*1024) this.catalogCache.clear();
        this.catalogCache.set(key,{revision,rows,bytes});
      }
    }
    // Callers cannot mutate a cached protection or alias list.
    return rows.map(row=>({...row,aliases:[...row.aliases]}));
  }
  private buildCanonicalSessions(machine?: string) {
    type Bound = ReturnType<ResourceManagement['boundSessions']>[number];
    const groups = new Map<string, {canonical: Bound; keep: boolean; reaped: boolean; intakeOpen: boolean; aliases: Set<string>}>();
    for (const session of this.boundSessions(machine)) {
      const key = JSON.stringify([session.machine_id,session.native_id]);
      let group = groups.get(key);
      if (!group) {
        group = {canonical:session,keep:false,reaped:false,intakeOpen:false,aliases:new Set()};
        groups.set(key,group);
      }
      // Keep the first managed alias, otherwise the first non-shadow alias.
      const old = group.canonical;
      if (!old.managed && (session.managed || (old.observed_shadow && !session.observed_shadow))) group.canonical=session;
      // Every explicit interactive alias and protection protects the native,
      // even when a different alias supplies its canonical metadata.
      group.keep ||= session.keep || (session.mode==='interactive' && !session.observed_shadow);
      group.reaped ||= session.reaped;
      group.intakeOpen ||= session.linked_intake_open;
      group.aliases.add(session.session_id);
      if (session.fleet_launch_id) group.aliases.add('fleet:'+session.fleet_launch_id);
    }
    return [...groups.values()].map(group=>{
      const {observed_shadow,...session}=group.canonical;
      return {session:{...session,keep:group.keep,managed:session.managed && !group.keep,
        reaped:group.reaped,linked_intake_open:group.intakeOpen,aliases:[...group.aliases]},
        terminal:Number(TERMINAL.has(session.state)),at:Date.parse(session.finished_at || session.started_at)};
    }).sort((a,b)=>a.terminal-b.terminal || b.at-a.at).map(row=>row.session);
  }
  /** Authority rows for idle reaping: `id=session_id OR (machine_id, binding
   * native_id) = (machine, native)`. One projection loads them in bulk instead
   * of a json_extract table scan per session. A corrupt binding on a host makes
   * that host's native authority unreadable, which is never reap permission. */
  private idleAuthority(sessions: any[]) {
    const byId = new Map<string, any>(), byNative = new Map<string, any[]>(), corrupt = new Set<string>();
    const add = (row: any) => {
      if (byId.has(row.id)) return;
      byId.set(row.id, row);
      if (row.native_key !== null) {
        const key = JSON.stringify([row.machine_id, row.native_key]);
        byNative.set(key, [...(byNative.get(key) ?? []), row]);
      }
    };
    const columns = `id,machine_id,binding_json,${SLIM_REQUEST} AS request_json,${NATIVE_KEY} AS native_key`;
    const natives = new Map<string, Set<string>>();
    for (const s of sessions) if (s.machine_id !== null && s.machine_id !== undefined) {
      const set = natives.get(s.machine_id) ?? new Set<string>();
      if (typeof s.native_id === 'string') set.add(s.native_id);
      natives.set(s.machine_id, set);
    }
    for (const [machine, set] of natives) {
      if (this.sql.exec(`SELECT 1 FROM session_control_sessions WHERE machine_id=? AND binding_json IS NOT NULL
        AND NOT json_valid(binding_json) LIMIT 1`, machine).toArray().length) { corrupt.add(machine); continue; }
      const list = [...set];
      for (let i = 0; i < list.length; i += 100) {
        const chunk = list.slice(i, i + 100);
        for (const row of this.sql.exec(`SELECT ${columns} FROM session_control_sessions WHERE machine_id=? AND ${NATIVE_KEY} IN (${chunk.map(() => '?').join(',')})`, machine, ...chunk).toArray()) add(row);
      }
    }
    const ids = [...new Set(sessions.map(s => s.session_id).filter(id => id !== null && id !== undefined && !byId.has(id)))];
    for (let i = 0; i < ids.length; i += 100) {
      const chunk = ids.slice(i, i + 100);
      for (const row of this.sql.exec(`SELECT ${columns} FROM session_control_sessions WHERE id IN (${chunk.map(() => '?').join(',')})`, ...chunk).toArray()) add(row);
    }
    return (session: any): any[] | null => {
      if (session.machine_id !== null && session.machine_id !== undefined && corrupt.has(session.machine_id)) return null;
      const rows = new Map<string, any>();
      const own = byId.get(session.session_id);
      if (own) rows.set(own.id, own);
      if (session.machine_id !== null && session.machine_id !== undefined && typeof session.native_id === 'string') {
        for (const row of byNative.get(JSON.stringify([session.machine_id, session.native_id])) ?? []) rows.set(row.id, row);
      }
      return [...rows.values()];
    };
  }
  private idleReapAllowed(session: any, authority?: (session: any) => any[] | null, cache: IdleReapCache = { intakes: new Map() }): boolean {
    try {
      const rows = (authority ?? this.idleAuthority([session]))(session);
      if (rows === null) return false;
      if (rows.some(row => openWorkPreventsIdleReap(this.sql,row,this.now(),cache))) return false;
      return rows.length > 0 || !session.intake_id;
    } catch { return false; } // corrupt/missing authority is never reap permission
  }
  sessions(machine?: string) {
    const canonical = this.canonicalSessions(machine).slice(0,1000);
    let authority: (session: any) => any[] | null;
    try { authority = this.idleAuthority(canonical); } catch { authority = () => { throw new Error('idle_authority_unavailable'); }; }
    const cache: IdleReapCache = { intakes: new Map() };
    return canonical.map(({aliases,...session}) => {
      return { ...session, idle_reap_allowed: this.idleReapAllowed(session, authority, cache) };
    });
  }
  private note(session: any, key: string, text: string) {
    if (!session.intake_id) throw new Error('intake_required');
    const result = new IntakeService(this.sql).serviceRecordEvent(session.intake_id, {
      event:{kind:'note',text,actor:'machine:'+session.machine_id,sourceRef:'resource-watchdog:'+key,authority:'untrusted_task_data'},now:this.now(),
    });
    if (!result.ok) throw new Error(result.error==='not_found'?'intake_not_found':'intake_capacity');
  }

  evidence(machine: string, value: any) {
    const session = this.sessions(machine).find(s => s.session_id===value.session_id && s.native_id===value.native_id);
    if (!session?.managed || session.keep || (value.reason === 'idle' && !session.idle_reap_allowed) || !session.intake_id) throw new Error('session_not_reapable');
    // Worktree-guard (AC2): never reap for idleness while the linked intake
    // is open. Nudge/replace instead; reaping resumes after DONE/archive.
    if ((session as any).linked_intake_open) throw new Error('session_not_reapable');
    if (value.reason === 'terminal' && !TERMINAL.has(session.state)) throw new Error('session_not_reapable');
    if (!['idle','terminal','dead_pid'].includes(value.reason) || typeof value.id!=='string' || !/^[\w:-]{1,128}$/.test(value.id)
      || typeof value.output!=='string' || new TextEncoder().encode(value.output).byteLength > 6000) throw new Error('invalid_evidence');
    const prior: any = this.sql.exec('SELECT * FROM resource_evidence WHERE id=?',value.id).toArray()[0];
    if (prior && (prior.session_id!==session.session_id || prior.native_id!==session.native_id || prior.reason!==value.reason)) throw new Error('evidence_conflict');
    if (!prior && session.reaped) throw new Error('session_already_reaped');
    if (!prior) {
      this.note(session,value.id,`Resource watchdog saved output before ${value.reason} stop. Session ${session.session_id}. Restart unfinished work after reap.\n${value.output}`);
      this.sql.exec('INSERT INTO resource_evidence VALUES(?,?,?,?,?)',value.id,session.session_id,session.native_id,value.reason,new Date(this.now()).toISOString());
    }
    return {saved:true,evidence_id:value.id};
  }
  report(machine: string, value: any) {
    // Accept reports already in flight under an older canonical alias, but put
    // every physical native in exactly one durable ledger row.
    const sessions = new Map(this.canonicalSessions(machine).flatMap(s => s.aliases.map(id=>[id,s] as const)));
    if (!value || !Array.isArray(value.samples) || !Array.isArray(value.reaps) || value.samples.length>200 || value.reaps.length>100
      || (value.throttles !== undefined && (!Array.isArray(value.throttles) || value.throttles.length>100))) throw new Error('invalid_resource_report');
    for (const sample of value.samples) {
      const session = sessions.get(sample.session_id);
      if (!session || sample.native_id!==session.native_id) throw new Error('resource_binding_mismatch');
      for (const k of ['current_rss_mb','peak_rss_mb','peak_pss_mb','cpu_seconds','runtime_s','avg_cpu_cores','current_cpu_cores','p95_cpu_cores']) if (sample[k]!=null && (!finite(sample[k]) || sample[k]>1e12)) throw new Error('invalid_resource_sample');
      const at=Date.parse(sample.observed_at);
      if (!Number.isFinite(at) || at>this.now()+30000 || at<this.now()-86400000) throw new Error('invalid_resource_timestamp');
      const old:any=this.sql.exec('SELECT * FROM session_resource_usage WHERE machine_id=? AND native_id=?',machine,session.native_id).toArray()[0];
      const ledgerId=old?.session_id ?? session.session_id;
      this.sql.exec('INSERT INTO session_resource_metadata VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET workspace=excluded.workspace',ledgerId,session.workspace);
      if (session.fleet_launch_id && finite(sample.current_cpu_cores)) this.sql.exec('INSERT INTO resource_cpu_samples VALUES(?,?,?) ON CONFLICT(launch_id) DO UPDATE SET current_cpu_cores=excluded.current_cpu_cores,observed_at=excluded.observed_at WHERE excluded.observed_at>resource_cpu_samples.observed_at',session.fleet_launch_id,sample.current_cpu_cores,sample.observed_at);
      if (finite(sample.p95_cpu_cores)) {
        const finalized=this.sql.exec('SELECT 1 FROM session_cpu_finalized WHERE session_id=?',ledgerId).toArray().length>0;
        const prior:any=this.sql.exec('SELECT observed_at FROM session_cpu_usage WHERE session_id=?',ledgerId).toArray()[0];
        // Live lifetime p95 can fall as real idle intervals accumulate. Freeze
        // only an actual terminal CPU report, never a memory-only final row.
        if (!finalized && (!prior || at>=Date.parse(prior.observed_at))) {
          this.sql.exec('INSERT INTO session_cpu_usage VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET p95_cpu_cores=excluded.p95_cpu_cores,observed_at=excluded.observed_at',ledgerId,sample.p95_cpu_cores,sample.observed_at);
          if (TERMINAL.has(session.state)) this.sql.exec('INSERT OR IGNORE INTO session_cpu_finalized VALUES(?,?)',ledgerId,sample.observed_at);
        }
      }
      if (session.fleet_launch_id && finite(sample.current_rss_mb)) this.sql.exec('INSERT INTO resource_resident_samples VALUES(?,?,?) ON CONFLICT(launch_id) DO UPDATE SET resident_mb=excluded.resident_mb,observed_at=excluded.observed_at WHERE excluded.observed_at>=resource_resident_samples.observed_at',session.fleet_launch_id,sample.current_rss_mb,sample.observed_at);
      const max = (k:string) => finite(sample[k]) ? Math.max(old?.[k] ?? 0,sample[k]) : old?.[k] ?? null;
      const cpu=max('cpu_seconds'),runtime=max('runtime_s');
      this.sql.exec(`INSERT INTO session_resource_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET fleet_launch_id=excluded.fleet_launch_id,intake_id=excluded.intake_id,
        project=excluded.project,engine=excluded.engine,model=excluded.model,tier=excluded.tier,kind=excluded.kind,
        started_at=CASE WHEN session_resource_usage.started_at='' THEN excluded.started_at ELSE session_resource_usage.started_at END,
        finished_at=excluded.finished_at,state=excluded.state,
        peak_rss_mb=excluded.peak_rss_mb,peak_pss_mb=excluded.peak_pss_mb,cpu_seconds=excluded.cpu_seconds,
        runtime_s=excluded.runtime_s,avg_cpu_cores=excluded.avg_cpu_cores,observed_at=excluded.observed_at`,
        ledgerId,machine,session.native_id,session.fleet_launch_id,session.intake_id,session.project,session.engine,session.model,session.tier,session.kind,
        // Legacy storage requires text; an empty unknown is exposed as null,
        // never replaced with the time a launch entered the queue.
        session.started_at ?? '',old?.finished_at ?? session.finished_at,old?.finished_at ? old.state : session.state,max('peak_rss_mb'),max('peak_pss_mb'),cpu,runtime,runtime>0 && cpu!==null ? cpu/runtime : null,sample.observed_at);
    }
    for (const reap of value.reaps) {
      const session=sessions.get(reap.session_id);
      const evidence: any = this.sql.exec('SELECT * FROM resource_evidence WHERE id=?',reap.evidence_id ?? '').toArray()[0];
      if (!session?.managed || session.keep || (reap.reason === 'idle' && !this.idleReapAllowed(session)) || !evidence || evidence.session_id!==session.session_id || evidence.native_id!==reap.native_id || evidence.reason!==reap.reason || reap.stopped!==true || (reap.freed_mb!==null && !finite(reap.freed_mb))
        || reap.freed_mb>1e9 || Date.parse(reap.at)>this.now()+30000 || Date.parse(reap.at)<Date.parse(evidence.saved_at)-1000
        || typeof reap.id!=='string' || !/^[\w:-]{1,128}$/.test(reap.id) || !Number.isFinite(Date.parse(reap.at))) throw new Error('invalid_reap');
      const prior:any=this.sql.exec('SELECT * FROM resource_reaps WHERE id=?',reap.id).toArray()[0];
      if (prior) { if(prior.session_id!==session.session_id || prior.evidence_id!==reap.evidence_id) throw new Error('reap_conflict'); continue; }
      // Binding-scoped idempotency also covers a host retry that minted a new
      // attempt/reason after the first receipt was acknowledged.
      if (session.reaped || this.sql.exec('SELECT 1 FROM resource_reaps WHERE session_id=? AND machine_id=? AND native_id=? LIMIT 1',session.session_id,machine,reap.native_id).toArray().length) continue;
      this.note(session,reap.id,`Resource watchdog reaped session ${session.session_id}: ${reap.reason}; freed ${reap.freed_mb ?? 'unknown'} MiB; confirmed stopped at ${reap.at}. Output saved as ${reap.evidence_id}. Unfinished intake remains open for restart.`);
      this.sql.exec('INSERT INTO resource_reaps VALUES(?,?,?,?,?,?,?,?,?,?)',reap.id,session.session_id,machine,reap.native_id,session.intake_id,'machine:'+machine,reap.reason,reap.freed_mb,reap.at,reap.evidence_id);
      this.sql.exec("UPDATE session_resource_usage SET finished_at=COALESCE(finished_at,?),state=CASE WHEN finished_at IS NULL THEN 'stopped' ELSE state END WHERE machine_id=? AND native_id=?",reap.at,machine,session.native_id);
      if (!TERMINAL.has(session.state)) this.sql.exec("UPDATE session_control_sessions SET state='stopped',updated_at=? WHERE id=?",reap.at,session.session_id);
      this.sql.exec("UPDATE fleet_launches SET state=CASE WHEN state IN ('exited','failed','stopped') THEN state ELSE 'failed' END,finished_at=COALESCE(finished_at,?),exit_reason=COALESCE(exit_reason,?),updated_at=? WHERE id=?",reap.at,'watchdog_'+reap.reason,reap.at,session.fleet_launch_id);
      this.sql.exec("UPDATE fleet_sessions SET phase='exited',observed_at=? WHERE machine_id=? AND native_id=?",reap.at,machine,session.native_id);
    }
    for (const throttle of value.throttles ?? []) {
      // Reversible CPU-throttle receipts (intake f31b678f): the host only
      // reports verified cpu.max writes on the session's own workload scope;
      // a throttle never stops anything, so intake-note failures must not
      // fail the whole report and unlike reaps there is no replay gate.
      if (!throttle || typeof throttle.id!=='string' || !/^[\w:-]{1,128}$/.test(throttle.id)
        || !['throttled','released'].includes(throttle.action)
        || (throttle.cap_cores!=null && (!finite(throttle.cap_cores) || throttle.cap_cores>10000))
        || (throttle.sustain_s!=null && (!finite(throttle.sustain_s) || throttle.sustain_s>100000))
        || (throttle.cpu_max!=null && (typeof throttle.cpu_max!=='string' || throttle.cpu_max.length>64))
        || (throttle.prior_cpu_max!=null && (typeof throttle.prior_cpu_max!=='string' || throttle.prior_cpu_max.length>64))
        || (throttle.outcome!=null && (typeof throttle.outcome!=='string' || throttle.outcome.length>32))) throw new Error('invalid_throttle');
      const tsession=sessions.get(throttle.session_id);
      if (!tsession || throttle.native_id!==tsession.native_id) throw new Error('resource_binding_mismatch');
      const at=Date.parse(throttle.at);
      if (!Number.isFinite(at) || at>this.now()+30000 || at<this.now()-86400000) throw new Error('invalid_resource_timestamp');
      const prior:any=this.sql.exec('SELECT * FROM resource_throttles WHERE id=?',throttle.id).toArray()[0];
      if (prior) { if(prior.session_id!==tsession.session_id || prior.native_id!==tsession.native_id || prior.machine_id!==machine || prior.action!==throttle.action) throw new Error('throttle_conflict'); continue; }
      this.sql.exec('INSERT INTO resource_throttles VALUES(?,?,?,?,?,?,?,?,?,?,?)',throttle.id,tsession.session_id,machine,tsession.native_id,tsession.intake_id,throttle.action,throttle.cap_cores ?? null,throttle.sustain_s ?? null,throttle.cpu_max ?? null,throttle.outcome ?? null,throttle.at);
      if (tsession.intake_id) {
        const text = throttle.action==='throttled'
          ? `Resource watchdog throttled session ${tsession.session_id}: CPU stayed above ${throttle.cap_cores ?? 'the configured'} cores for at least ${throttle.sustain_s ?? 'the configured'}s; applied reversible cgroup cpu.max "${throttle.cpu_max ?? ''}" (previous "${throttle.prior_cpu_max ?? 'unknown'}"). No process was stopped; the throttle lifts automatically when usage drops.`
          : `Resource watchdog lifted the CPU throttle on session ${tsession.session_id}: usage fell below the cap; cgroup cpu.max restore outcome: ${throttle.outcome ?? 'unknown'}${throttle.cpu_max ? ` (restored "${throttle.cpu_max}")` : ''}.`;
        try { this.note(tsession,throttle.id,text); }
        catch (error) { if (!(error instanceof Error && ['intake_required','intake_not_found','intake_capacity','intake_history_capacity'].includes(error.message))) throw error; }
      }
    }
    return {reported:true,samples:value.samples.length,reaps:value.reaps.length};
  }
  usage(filters: any = {}) {
    const clauses:string[]=[], args:string[]=[];
    for (const k of ['project','engine','kind']) if(filters[k]) { clauses.push(k+'=?');args.push(filters[k]); }
    const latestThrottle=new Map<string,string>();
    for (const r of this.sql.exec('SELECT machine_id,native_id,action FROM resource_throttles ORDER BY at DESC, rowid DESC LIMIT 1000').toArray() as any[]) {
      const key=r.machine_id+':'+r.native_id;
      if (!latestThrottle.has(key)) latestThrottle.set(key,r.action);
    }
    return this.sql.exec('SELECT * FROM session_resource_usage'+(clauses.length?' WHERE '+clauses.join(' AND '):'')+' ORDER BY observed_at DESC LIMIT 500',...args).toArray().map((r:any)=>({...r,started_at:r.started_at || null,workspace:this.sql.exec('SELECT workspace FROM session_resource_metadata WHERE session_id=?',r.session_id).toArray()[0]?.workspace ?? null,p95_cpu_cores:this.sql.exec('SELECT p95_cpu_cores FROM session_cpu_usage WHERE session_id=?',r.session_id).toArray()[0]?.p95_cpu_cores ?? null,rss_flagged:r.peak_rss_mb>=this.config().rss_flag_gb*1024,token_join_key:r.machine_id+':'+r.native_id,cpu_throttled:latestThrottle.get(r.machine_id+':'+r.native_id)==='throttled'}));
  }
  reaps() { return this.sql.exec('SELECT * FROM resource_reaps ORDER BY at DESC LIMIT 100').toArray(); }
  throttles() {
    const rows=this.sql.exec('SELECT * FROM resource_throttles ORDER BY at DESC, rowid DESC LIMIT 100').toArray();
    // Newest receipt per physical native decides the active flag, and only
    // that single row may carry it: a released row is never ACTIVE even
    // when a newer throttled row exists for the same native.
    const latest=new Map<string,any>();
    for (const r of this.sql.exec('SELECT id,machine_id,native_id,action FROM resource_throttles ORDER BY at DESC, rowid DESC LIMIT 1000').toArray() as any[]) {
      const key=r.machine_id+':'+r.native_id;
      if (!latest.has(key)) latest.set(key,r);
    }
    return rows.map((r:any)=>({...r,active:latest.get(r.machine_id+':'+r.native_id)?.id===r.id && r.action==='throttled'}));
  }
  overview() {
    const rows=this.sql.exec('SELECT id,hostname,drained,capabilities,last_heartbeat,last_checkin_at FROM machines WHERE drained=0').toArray() as any[];
    // Intake 4398ce2a: a stale re-registered record of the same physical host
    // (SnV2 for live DEFLf4, hostname RMTHZ) has no telemetry and showed up as
    // a second host with CPU/memory 'unknown'. Mark-never-delete aliasing:
    // report the live canonical host only and list its alias ids.
    let aliasOf=new Map<string,string>();
    try {
      aliasOf=resolveMachineAliases(rows.map((m:any)=>{const at=typeof m.last_checkin_at==='string'?Date.parse(m.last_checkin_at):NaN;
        return {id:String(m.id),hostname:m.hostname,drained:m.drained,last_seen_ms:Number.isFinite(at)?at:null};})).aliasOf;
    } catch { aliasOf=new Map(); }
    const hosts=rows.filter((m:any)=>!aliasOf.has(String(m.id))).map((m:any)=>{
      const hb=parse(m.last_heartbeat),caps=parse(m.capabilities),headroom=this.headroom(m.id);
      return {machine_id:m.id,aliases:[...aliasOf].filter(([,c])=>c===String(m.id)).map(([a])=>a).sort(),mem_total_mb:hb.mem_total_mb??null,
        mem_used_mb:finite(hb.mem_total_mb)&&finite(hb.mem_avail_mb)?hb.mem_total_mb-hb.mem_avail_mb:null,
        load5:hb.load?.[1]??null,cores:caps.cpu_count??null,...headroom,cpu:this.cpuHeadroom(m.id),host_observed_at:m.last_checkin_at,
        running:Number(this.sql.exec("SELECT COUNT(*) AS n FROM fleet_sessions WHERE machine_id=? AND phase='running'",m.id).one().n),
        queued:Number(this.sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE state='queued' AND (machine_id=? OR machine_id='')",m.id).one().n)};
    });
    const sessions=this.usage(),groups=new Map<string,any[]>();
    for (const row of sessions as any[]) {const key=JSON.stringify([row.project,row.engine,row.kind]); if(!groups.has(key))groups.set(key,[]);groups.get(key)!.push(row);}
    const throttles=this.throttles();
    return {config:this.config(),hosts,sessions,stats:[...groups.values()].map(rows=>({project:rows[0].project,engine:rows[0].engine,kind:rows[0].kind,samples:rows.length,
      p50_rss_mb:percentile(rows.map(r=>r.peak_rss_mb).filter(finite),50),p90_rss_mb:percentile(rows.map(r=>r.peak_rss_mb).filter(finite),90)})),reaps:this.reaps(),
      throttles,active_throttles:throttles.filter((t:any)=>t.active).length};
  }
}
