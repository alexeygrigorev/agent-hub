/** Live host admission and durable per-session usage; token usage joins by native identity. */
import type { SqlStorage } from '@cloudflare/workers-types';
import defaults from '../config/resource-management.json';
import { IntakeService } from './intake';
import { policy } from './model-routing';
export const MEMORY_FLOOR_MB = defaults.memory_floor_mb;
export const NEXT_SLOT_MB = defaults.ramp_reserve_default_mb;
export const CPU_LOAD_FACTOR = defaults.cpu_load_factor;
export type ResourceConfig = typeof defaults;
const parse = (s: any): any => { try { return JSON.parse(s || '{}'); } catch { return {}; } };
const finite = (n: any) => typeof n === 'number' && Number.isFinite(n) && n >= 0;
const TERMINAL = new Set(['exited', 'finished', 'failed', 'stopped', 'dead', 'refused']);
export const RESOURCE_SCHEMA = `
CREATE TABLE IF NOT EXISTS resource_resident_samples (launch_id TEXT PRIMARY KEY, resident_mb REAL NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_ramps (launch_id TEXT PRIMARY KEY, started_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_resource_usage (
 session_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL,
 fleet_launch_id TEXT, intake_id TEXT, project TEXT, engine TEXT, model TEXT, tier TEXT, kind TEXT,
 started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
 peak_rss_mb REAL, peak_pss_mb REAL, cpu_seconds REAL, runtime_s REAL, avg_cpu_cores REAL,
 observed_at TEXT NOT NULL, UNIQUE(machine_id,native_id));
CREATE INDEX IF NOT EXISTS session_resource_estimate ON session_resource_usage(engine,project,kind,finished_at);
CREATE TABLE IF NOT EXISTS resource_evidence (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, native_id TEXT NOT NULL, reason TEXT NOT NULL, saved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_reaps (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, intake_id TEXT, actor TEXT NOT NULL, reason TEXT NOT NULL, freed_mb REAL, at TEXT NOT NULL, evidence_id TEXT NOT NULL);
`;
export function percentile(values: number[], p: number): number | null {
  if (!values.length) return null;
  const sorted = values.slice().sort((a,b) => a-b);
  return sorted[Math.max(0, Math.ceil(sorted.length * p / 100) - 1)];
}
export function resourceConfig(value: any = {}): ResourceConfig {
  const result = { ...defaults };
  const bounds: Record<string, number[]> = {
    memory_floor_mb: [128, 1048576], ramp_reserve_default_mb: [64, 65536], estimate_percentile: [50,100],
    estimate_multiplier: [1,4], ramp_seconds: [30,900], cpu_load_factor: [.5,4],
    watchdog_interval_s: [300,600], idle_minutes: [10,10080], rss_flag_gb: [.125,1024],
  };
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_resource_config');
  for (const [k,v] of Object.entries(value)) {
    if (!bounds[k] || !finite(v) || (v as number) < bounds[k][0] || (v as number) > bounds[k][1]) throw new Error('invalid_resource_config');
    if (!['cpu_load_factor','estimate_multiplier','rss_flag_gb'].includes(k) && !Number.isSafeInteger(v)) throw new Error('invalid_resource_config');
    (result as any)[k] = v;
  }
  return result;
}
export function memoryVerdict(available: number, ramp: number, estimate: number, config: ResourceConfig) {
  return finite(available) && finite(ramp) && finite(estimate) && available - ramp - estimate >= config.memory_floor_mb;
}
export class ResourceManagement {
  constructor(private sql: SqlStorage, private now: () => number = Date.now) {}
  init() { for (const statement of RESOURCE_SCHEMA.split(';')) if (statement.trim()) this.sql.exec(statement); }
  config(): ResourceConfig { return resourceConfig(parse(this.sql.exec('SELECT data FROM resource_settings WHERE id=1').toArray()[0]?.data)); }
  configure(value: any) {
    const config = resourceConfig(value);
    this.sql.exec('INSERT INTO resource_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', JSON.stringify(config));
    return config;
  }
  estimate(project: string | null, engine: string | null, kind: string | null) {
    const config = this.config();
    // Only actual measurements, never a cgroup cap or fabricated zero. Bounded recent history.
    const rows = this.sql.exec('SELECT project,kind,peak_rss_mb FROM session_resource_usage WHERE engine IS ? AND finished_at IS NOT NULL AND peak_rss_mb > 0 ORDER BY observed_at DESC LIMIT 500', engine).toArray() as any[];
    const specific = rows.filter(r => r.project === project && r.kind === kind);
    const measured = percentile((specific.length ? specific : rows).map(r => Number(r.peak_rss_mb)), config.estimate_percentile);
    return Math.ceil(Math.max(64, measured === null ? config.ramp_reserve_default_mb : measured * config.estimate_multiplier));
  }
  headroom(machine: string, input: any = {}, engine: string | null = null) {
    const machineRow: any = this.sql.exec('SELECT last_heartbeat FROM machines WHERE id=?', machine).toArray()[0];
    const hb = parse(machineRow?.last_heartbeat); const config = this.config();
    const cutoff = new Date(this.now() - config.ramp_seconds * 1000).toISOString();
    // Only launch ramps are additional commitments. Resident usage is already in MemAvailable.
    const pending = this.sql.exec("SELECT id,engine,workspace,request_json FROM fleet_launches WHERE machine_id=? AND (state IN ('offered','starting','uncertain') OR (state='running' AND COALESCE((SELECT started_at FROM resource_ramps WHERE launch_id=fleet_launches.id),created_at) >= ?))", machine, cutoff).toArray() as any[];
    let reserved = 0;
    for (const row of pending) {
      const req = parse(row.request_json);
      const estimate = this.estimate(req.project || row.workspace, row.engine, req.task_kind || req.kind || 'goal');
      const measured: any = this.sql.exec('SELECT resident_mb FROM resource_resident_samples WHERE launch_id=? AND observed_at>=?', row.id, cutoff).toArray()[0];
      reserved += Math.max(0, estimate - (finite(measured?.resident_mb) ? measured.resident_mb : 0));
    }
    return { mem_available_mb: finite(hb.mem_avail_mb) ? hb.mem_avail_mb : null,
      reserved_mb: reserved, floor_mb: config.memory_floor_mb,
      requested_mb: this.estimate(input.project || input.workspace || null, engine, input.task_kind || input.kind || 'goal'),
      observed_at: hb.launcher_resources?.observed_at ?? hb.at ?? null };
  }
  sessions(machine?: string) {
    // Canonical binding, not tag matching; external interactive sessions can be sampled but never reaped.
    const rows = this.sql.exec(`SELECT s.*,l.request_json AS launch_request,l.state AS launch_state,l.created_at AS launch_created,
      l.finished_at AS launch_finished,l.model_selection_json,l.workspace AS launch_workspace
      FROM session_control_sessions s LEFT JOIN fleet_launches l ON l.id=s.fleet_launch_id
      ${machine ? 'WHERE s.machine_id=?' : ''} ORDER BY s.updated_at DESC LIMIT 1000`, ...(machine ? [machine] : [])).toArray() as any[];
    return rows.flatMap(s => {
      const binding = parse(s.binding_json); if (!binding.native_id || !binding.workspace || !binding.tag) return [];
      const req = parse(s.request_json), launch = parse(s.launch_request), model = parse(s.model_selection_json);
      const intakeId=req.intake_task_id || launch.intake_task_id || null;
      const intakeRow:any=intakeId ? this.sql.exec('SELECT data FROM intake_tasks WHERE id=?',intakeId).toArray()[0] : null;
      const project=parse(intakeRow?.data).project || req.project || launch.project || s.launch_workspace || binding.workspace;
      const observed: any = this.sql.exec('SELECT last_activity_at,created_at_ms,phase FROM fleet_sessions WHERE machine_id=? AND native_id=?',s.machine_id,binding.native_id).toArray()[0];
      const protection = this.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_protections'").toArray().length
        ? this.sql.exec('SELECT 1 FROM session_control_protections WHERE session_id=?',s.id).toArray().length > 0 : false;
      return [{ session_id: s.id, machine_id:s.machine_id, fleet_launch_id:s.fleet_launch_id,
        intake_id:intakeId,
        native_id:binding.native_id, tag:binding.tag, workspace:binding.workspace,
        native_created_at_ms:binding.native_created_at_ms ?? observed?.created_at_ms ?? null,
        engine:s.engine, model:model.model ?? null, project,
        tier:req.tier || launch.tier || (s.fleet_launch_id ? policy.default_tier : null), kind:req.task_kind || launch.task_kind || 'goal',
        state:TERMINAL.has(s.state) ? s.state : s.launch_state || s.state, keep:protection || req.keep === true || launch.keep === true,
        managed:!!s.fleet_launch_id && (s.mode === 'headless' || launch.with_goal === true || req.with_goal === true),
        last_activity_at:observed?.last_activity_at ?? null, started_at:s.launch_created || s.created_at,
        finished_at:s.launch_finished || (TERMINAL.has(s.state) ? s.updated_at : null) }];
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
    if (!session?.managed || session.keep || !session.intake_id) throw new Error('session_not_reapable');
    if (value.reason === 'terminal' && !TERMINAL.has(session.state)) throw new Error('session_not_reapable');
    if (!['idle','terminal','dead_pid'].includes(value.reason) || typeof value.id!=='string' || !/^[\w:-]{1,128}$/.test(value.id)
      || typeof value.output!=='string' || new TextEncoder().encode(value.output).byteLength > 6000) throw new Error('invalid_evidence');
    const prior: any = this.sql.exec('SELECT * FROM resource_evidence WHERE id=?',value.id).toArray()[0];
    if (prior && (prior.session_id!==session.session_id || prior.native_id!==session.native_id || prior.reason!==value.reason)) throw new Error('evidence_conflict');
    if (!prior) {
      this.note(session,value.id,`Resource watchdog saved output before ${value.reason} stop. Session ${session.session_id}. Restart unfinished work after reap.\n${value.output}`);
      this.sql.exec('INSERT INTO resource_evidence VALUES(?,?,?,?,?)',value.id,session.session_id,session.native_id,value.reason,new Date(this.now()).toISOString());
    }
    return {saved:true,evidence_id:value.id};
  }
  report(machine: string, value: any) {
    const sessions = new Map(this.sessions(machine).map(s => [s.session_id,s]));
    if (!value || !Array.isArray(value.samples) || !Array.isArray(value.reaps) || value.samples.length>200 || value.reaps.length>100) throw new Error('invalid_resource_report');
    for (const sample of value.samples) {
      const session = sessions.get(sample.session_id);
      if (!session || sample.native_id!==session.native_id) throw new Error('resource_binding_mismatch');
      for (const k of ['current_rss_mb','peak_rss_mb','peak_pss_mb','cpu_seconds','runtime_s','avg_cpu_cores']) if (sample[k]!=null && (!finite(sample[k]) || sample[k]>1e12)) throw new Error('invalid_resource_sample');
      const at=Date.parse(sample.observed_at);
      if (!Number.isFinite(at) || at>this.now()+30000 || at<this.now()-86400000) throw new Error('invalid_resource_timestamp');
      if (session.fleet_launch_id && finite(sample.current_rss_mb)) this.sql.exec('INSERT INTO resource_resident_samples VALUES(?,?,?) ON CONFLICT(launch_id) DO UPDATE SET resident_mb=excluded.resident_mb,observed_at=excluded.observed_at WHERE excluded.observed_at>=resource_resident_samples.observed_at',session.fleet_launch_id,sample.current_rss_mb,sample.observed_at);
      const old: any = this.sql.exec('SELECT * FROM session_resource_usage WHERE session_id=?',session.session_id).toArray()[0];
      const max = (k:string) => finite(sample[k]) ? Math.max(old?.[k] ?? 0,sample[k]) : old?.[k] ?? null;
      const cpu=max('cpu_seconds'),runtime=max('runtime_s');
      this.sql.exec(`INSERT INTO session_resource_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET finished_at=excluded.finished_at,state=excluded.state,
        peak_rss_mb=excluded.peak_rss_mb,peak_pss_mb=excluded.peak_pss_mb,cpu_seconds=excluded.cpu_seconds,
        runtime_s=excluded.runtime_s,avg_cpu_cores=excluded.avg_cpu_cores,observed_at=excluded.observed_at`,
        session.session_id,machine,session.native_id,session.fleet_launch_id,session.intake_id,session.project,session.engine,session.model,session.tier,session.kind,
        session.started_at,old?.finished_at ?? session.finished_at,old?.finished_at ? old.state : session.state,max('peak_rss_mb'),max('peak_pss_mb'),cpu,runtime,runtime>0 && cpu!==null ? cpu/runtime : null,sample.observed_at);
    }
    for (const reap of value.reaps) {
      const session=sessions.get(reap.session_id);
      const evidence: any = this.sql.exec('SELECT * FROM resource_evidence WHERE id=?',reap.evidence_id ?? '').toArray()[0];
      if (!session?.managed || session.keep || !evidence || evidence.session_id!==session.session_id || evidence.native_id!==reap.native_id || evidence.reason!==reap.reason || reap.stopped!==true || (reap.freed_mb!==null && !finite(reap.freed_mb))
        || reap.freed_mb>1e9 || Date.parse(reap.at)>this.now()+30000 || Date.parse(reap.at)<Date.parse(evidence.saved_at)-1000
        || typeof reap.id!=='string' || !/^[\w:-]{1,128}$/.test(reap.id) || !Number.isFinite(Date.parse(reap.at))) throw new Error('invalid_reap');
      const prior:any=this.sql.exec('SELECT * FROM resource_reaps WHERE id=?',reap.id).toArray()[0];
      if (prior) { if(prior.session_id!==session.session_id || prior.evidence_id!==reap.evidence_id) throw new Error('reap_conflict'); continue; }
      this.note(session,reap.id,`Resource watchdog reaped session ${session.session_id}: ${reap.reason}; freed ${reap.freed_mb ?? 'unknown'} MiB; confirmed stopped at ${reap.at}. Output saved as ${reap.evidence_id}. Unfinished intake remains open for restart.`);
      this.sql.exec('INSERT INTO resource_reaps VALUES(?,?,?,?,?,?,?,?,?,?)',reap.id,session.session_id,machine,reap.native_id,session.intake_id,'machine:'+machine,reap.reason,reap.freed_mb,reap.at,reap.evidence_id);
      this.sql.exec("UPDATE session_resource_usage SET finished_at=COALESCE(finished_at,?),state='stopped' WHERE session_id=?",reap.at,session.session_id);
      this.sql.exec("UPDATE session_control_sessions SET state='stopped',updated_at=? WHERE id=?",reap.at,session.session_id);
      this.sql.exec("UPDATE fleet_launches SET state=CASE WHEN state IN ('exited','failed','stopped') THEN state ELSE 'failed' END,finished_at=COALESCE(finished_at,?),exit_reason=COALESCE(exit_reason,?),updated_at=? WHERE id=?",reap.at,'watchdog_'+reap.reason,reap.at,session.fleet_launch_id);
      this.sql.exec("UPDATE fleet_sessions SET phase='exited',observed_at=? WHERE machine_id=? AND native_id=?",reap.at,machine,session.native_id);
    }
    return {reported:true,samples:value.samples.length,reaps:value.reaps.length};
  }
  usage(filters: any = {}) {
    const clauses:string[]=[], args:string[]=[];
    for (const k of ['project','engine','kind']) if(filters[k]) { clauses.push(k+'=?');args.push(filters[k]); }
    return this.sql.exec('SELECT * FROM session_resource_usage'+(clauses.length?' WHERE '+clauses.join(' AND '):'')+' ORDER BY observed_at DESC LIMIT 500',...args).toArray().map((r:any)=>({...r,rss_flagged:r.peak_rss_mb>=this.config().rss_flag_gb*1024,token_join_key:r.machine_id+':'+r.native_id}));
  }
  reaps() { return this.sql.exec('SELECT * FROM resource_reaps ORDER BY at DESC LIMIT 100').toArray(); }
  overview() {
    const hosts=this.sql.exec('SELECT id,capabilities,last_heartbeat,last_checkin_at FROM machines WHERE drained=0').toArray().map((m:any)=>{
      const hb=parse(m.last_heartbeat),caps=parse(m.capabilities),headroom=this.headroom(m.id);
      return {machine_id:m.id,mem_total_mb:hb.mem_total_mb??null,
        mem_used_mb:finite(hb.mem_total_mb)&&finite(hb.mem_avail_mb)?hb.mem_total_mb-hb.mem_avail_mb:null,
        load5:hb.load?.[1]??null,cores:caps.cpu_count??null,...headroom,host_observed_at:m.last_checkin_at,
        running:Number(this.sql.exec("SELECT COUNT(*) AS n FROM fleet_sessions WHERE machine_id=? AND phase='running'",m.id).one().n),
        queued:Number(this.sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE state='queued' AND (machine_id=? OR machine_id='')",m.id).one().n)};
    });
    const sessions=this.usage(),groups=new Map<string,any[]>();
    for (const row of sessions as any[]) {const key=JSON.stringify([row.project,row.engine,row.kind]); if(!groups.has(key))groups.set(key,[]);groups.get(key)!.push(row);}
    return {config:this.config(),hosts,sessions,stats:[...groups.values()].map(rows=>({project:rows[0].project,engine:rows[0].engine,kind:rows[0].kind,samples:rows.length,
      p50_rss_mb:percentile(rows.map(r=>r.peak_rss_mb).filter(finite),50),p90_rss_mb:percentile(rows.map(r=>r.peak_rss_mb).filter(finite),90)})),reaps:this.reaps()};
  }
}
