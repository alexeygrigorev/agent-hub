/** CPU and fresh host load admission primitives shared by placement and claims. */
export interface CpuUsage {
  busy_percent: number | null; busy_cores: number | null; cores: number | null;
  busy_percent_window: number | null; window_s: number | null;
  psi_avg10: number | null; observed_at: string; interval_s: number | null;
  psi_full_avg10: number | null; memory_psi_full_avg10: number | null; io_psi_full_avg10: number | null; oom_kills_delta: number | null; host_oom_kills_delta: number | null;
}
export interface CpuConfig {
  cpu_target_utilization: number; cpu_psi_hard_cap_pct: number;
  cpu_default_cores: number; cpu_estimate_multiplier: number;
  cpu_psi_full_hard_cap_pct: number; memory_psi_full_hard_cap_pct: number;
  cpu_load_factor?: number; swap_max_used_percent?: number; host_max_sessions?: number;
}
/** A host is CPU-full only when measured busy CPU averaged over >=10s reaches this (Alexey t2362u-t2363u; never from load average). */
export const CPU_SATURATED_PCT = 98;
/** MemAvailable below this leaves too little host-wide memory headroom. */
export const MEMORY_PRESSURE_FLOOR_MB = 2048;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0;
export function sanitizeCpuAdmission(value:any):Record<string,any>|null {
  const required=['expected_cpu_cores','ramping_cpu_cores','limit_cpu_cores','target_utilization'];
  const optional=['current_cpu_cores','psi_avg10','psi_full_avg10','memory_psi_full_avg10','io_psi_full_avg10','oom_kills_delta','host_oom_kills_delta','observed_at'];
  if(!value || typeof value!=='object' || Array.isArray(value) || required.some(k=>!Object.hasOwn(value,k))) return null;
  for(const [k,v] of Object.entries(value)) {
    if(!required.includes(k)&&!optional.includes(k)) return null;
    if(k==='observed_at') {if(v!==null&&(typeof v!=='string'||v.length>64||!/(?:Z|[+-][0-9]{2}:[0-9]{2})$/.test(v)||!Number.isFinite(Date.parse(v)))) return null;}
    else if(v===null&&optional.includes(k)) continue;
    else if(!finite(v)||v>(k.includes('psi')?100:k==='target_utilization'?1:1e9)
      || (k==='target_utilization'&&v<.1) || (k.endsWith('oom_kills_delta')&&!Number.isSafeInteger(v))) return null;
  }
  return {...value};
}
export function sanitizeCpuUsage(value: any): CpuUsage | null {
  if (!value || typeof value!=='object' || typeof value.observed_at!=='string'
    || value.observed_at.length>64 || !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/.test(value.observed_at) || !Number.isFinite(Date.parse(value.observed_at))) return null;
  const cores=Number.isSafeInteger(value.cores)&&value.cores>=1&&value.cores<=65536 ? value.cores : null;
  const measured=cores!==null&&finite(value.busy_percent)&&value.busy_percent<=100&&finite(value.busy_cores)
    &&value.busy_cores<=cores&&Math.abs(value.busy_cores-value.busy_percent*cores/100)<=.01;
  // Average busy CPU over a window of at least 10s (host snapshots). Unknown stays null.
  const windowed=measured&&finite(value.busy_percent_window)&&value.busy_percent_window<=100&&finite(value.window_s)&&value.window_s>=10&&value.window_s<=180;
  const pressure=(key:string)=>finite(value[key])&&value[key]<=100 ? value[key] : null;
  return {busy_percent:measured?value.busy_percent:null,busy_cores:measured?value.busy_cores:null,cores,
    busy_percent_window:windowed?value.busy_percent_window:null,window_s:windowed?value.window_s:null,
    psi_avg10:pressure('psi_avg10'),psi_full_avg10:pressure('psi_full_avg10'),memory_psi_full_avg10:pressure('memory_psi_full_avg10'),
    io_psi_full_avg10:pressure('io_psi_full_avg10'),
    oom_kills_delta:Number.isSafeInteger(value.oom_kills_delta)&&value.oom_kills_delta>=0&&value.oom_kills_delta<=1e9 ? value.oom_kills_delta : null,
    host_oom_kills_delta:Number.isSafeInteger(value.host_oom_kills_delta)&&value.host_oom_kills_delta>=0&&value.host_oom_kills_delta<=1e9 ? value.host_oom_kills_delta : null,
    observed_at:value.observed_at,interval_s:finite(value.interval_s)&&value.interval_s>0&&value.interval_s<=180 ? value.interval_s : null};
}
export function cpuVerdict(raw: unknown, cores: number, expected: number, ramping: number, config: CpuConfig, now: number,
  memAvailableMb: number | null = null, memoryFloorMb = MEMORY_PRESSURE_FLOOR_MB, memObservedAt: string | null = null,
  host: {load1?: unknown; swap_used_percent?: unknown; observed_at?: unknown; active_sessions?: unknown} = {}) {
  const usage = sanitizeCpuUsage(raw);
  const fresh=usage&&now-Date.parse(usage.observed_at)<=180000&&now-Date.parse(usage.observed_at)>=-30000;
  const memoryAt = typeof memObservedAt === 'string' ? Date.parse(memObservedAt) : NaN;
  const freshMemory = Boolean(fresh) || (Number.isFinite(memoryAt) && now-memoryAt<=180000 && now-memoryAt>=-30000);
  const known=fresh&&usage.cores===cores&&usage.busy_cores!==null&&usage.interval_s!==null&&finite(expected)&&finite(ramping);
  const hostAt=typeof host.observed_at==='string'?Date.parse(host.observed_at):NaN;
  const hostFresh=Number.isFinite(hostAt)&&now-hostAt<=180000&&now-hostAt>=-30000;
  const hostCores=Number.isSafeInteger(cores)&&cores>=1&&cores<=65536?cores:usage?.cores;
  const hostLoad=hostFresh&&finite(host.load1)&&host.load1<=1e9?host.load1:null;
  const loadFactor=finite(config.cpu_load_factor)?config.cpu_load_factor:2;
  const loadLimit=hostCores!==null&&hostCores!==undefined?hostCores*loadFactor:null;
  const swap=hostFresh&&finite(host.swap_used_percent)&&host.swap_used_percent<=100?host.swap_used_percent:null;
  const swapLimit=finite(config.swap_max_used_percent)?config.swap_max_used_percent:80;
  const active=Number.isSafeInteger(host.active_sessions)&&Number(host.active_sessions)>=0?Number(host.active_sessions):null;
  const sessionLimit=Number.isSafeInteger(config.host_max_sessions)&&Number(config.host_max_sessions)>=1?Number(config.host_max_sessions):24;
  const numbers = {current_cpu_cores:known ? usage.busy_cores : null,expected_cpu_cores:expected,
    ramping_cpu_cores:ramping,limit_cpu_cores:cores * config.cpu_target_utilization,
    target_utilization:config.cpu_target_utilization,psi_avg10:usage?.psi_avg10 ?? null,
    psi_full_avg10:usage?.psi_full_avg10 ?? null,memory_psi_full_avg10:usage?.memory_psi_full_avg10 ?? null,
    io_psi_full_avg10:usage?.io_psi_full_avg10 ?? null,
    oom_kills_delta:usage?.oom_kills_delta ?? null,host_oom_kills_delta:usage?.host_oom_kills_delta ?? null,observed_at:usage?.observed_at ?? null,
    predicted_over_target:known ? usage.busy_cores!+expected+ramping>cores*config.cpu_target_utilization : null,
    busy_percent_window:known ? usage.busy_percent_window : null,window_s:known ? usage.window_s : null,
    cpu_saturated:known && usage.busy_percent_window!==null ? usage.busy_percent_window>=CPU_SATURATED_PCT : null,
    // CPU busy is measured on every OS; PSI exists on Linux only and is not required.
    telemetry_state:known ? 'measured' : 'unknown',
    advisory:known ? null : 'cpu_usage_unknown'};
  const hostReason = hostLoad!==null&&loadLimit!==null&&hostLoad>loadLimit ? 'host_load_high'
    : swap!==null&&swap>swapLimit ? 'memory_pressure'
    : active!==null&&active>=sessionLimit ? 'machine_capacity' : null;
  const cpuReason = known && usage.busy_percent_window!==null && usage.busy_percent_window>=CPU_SATURATED_PCT ? 'cpu_pressure'
    : fresh && usage.psi_full_avg10!==null && usage.psi_full_avg10 > config.cpu_psi_full_hard_cap_pct ? 'cpu_pressure'
    : freshMemory && finite(memAvailableMb) && memAvailableMb < memoryFloorMb ? 'memory_pressure'
    // PSI is a secondary signal: only a high 10-second full-stall average can
    // refuse a host when the primary MemAvailable reading is not low.
    : fresh && usage.memory_psi_full_avg10!==null && usage.memory_psi_full_avg10 >= Math.max(10, config.memory_psi_full_hard_cap_pct) ? 'memory_pressure'
    : fresh && (usage.host_oom_kills_delta ?? 0)>0 ? 'host_oom' : null;
  const reason=hostReason??cpuReason;
  return {...numbers,host_load1:hostLoad,host_load_limit:loadLimit,swap_used_percent:swap,
    swap_max_used_percent:swapLimit,host_session_count:active,host_max_sessions:sessionLimit,
    reason,admitted:reason === null};
}
