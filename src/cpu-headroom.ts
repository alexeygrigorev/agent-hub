/** CPU admission primitives. Load average is context only, never an admission input. */
export interface CpuUsage {
  busy_percent: number | null; busy_cores: number | null; cores: number | null;
  psi_avg10: number | null; observed_at: string; interval_s: number | null;
  psi_full_avg10: number | null; memory_psi_full_avg10: number | null; io_psi_full_avg10: number | null; oom_kills_delta: number | null; host_oom_kills_delta: number | null;
}
export interface CpuConfig {
  cpu_target_utilization: number; cpu_psi_hard_cap_pct: number;
  cpu_default_cores: number; cpu_estimate_multiplier: number;
  cpu_psi_full_hard_cap_pct: number; memory_psi_full_hard_cap_pct: number;
}
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
  const pressure=(key:string)=>finite(value[key])&&value[key]<=100 ? value[key] : null;
  return {busy_percent:measured?value.busy_percent:null,busy_cores:measured?value.busy_cores:null,cores,
    psi_avg10:pressure('psi_avg10'),psi_full_avg10:pressure('psi_full_avg10'),memory_psi_full_avg10:pressure('memory_psi_full_avg10'),
    io_psi_full_avg10:pressure('io_psi_full_avg10'),
    oom_kills_delta:Number.isSafeInteger(value.oom_kills_delta)&&value.oom_kills_delta>=0&&value.oom_kills_delta<=1e9 ? value.oom_kills_delta : null,
    host_oom_kills_delta:Number.isSafeInteger(value.host_oom_kills_delta)&&value.host_oom_kills_delta>=0&&value.host_oom_kills_delta<=1e9 ? value.host_oom_kills_delta : null,
    observed_at:value.observed_at,interval_s:finite(value.interval_s)&&value.interval_s>0&&value.interval_s<=180 ? value.interval_s : null};
}
export function cpuVerdict(raw: unknown, cores: number, expected: number, ramping: number, config: CpuConfig, now: number) {
  const usage = sanitizeCpuUsage(raw);
  const fresh=usage&&now-Date.parse(usage.observed_at)<=180000&&now-Date.parse(usage.observed_at)>=-30000;
  const known=fresh&&usage.cores===cores&&usage.busy_cores!==null&&usage.interval_s!==null&&finite(expected)&&finite(ramping);
  const numbers = {current_cpu_cores:known ? usage.busy_cores : null,expected_cpu_cores:expected,
    ramping_cpu_cores:ramping,limit_cpu_cores:cores * config.cpu_target_utilization,
    target_utilization:config.cpu_target_utilization,psi_avg10:usage?.psi_avg10 ?? null,
    psi_full_avg10:usage?.psi_full_avg10 ?? null,memory_psi_full_avg10:usage?.memory_psi_full_avg10 ?? null,
    io_psi_full_avg10:usage?.io_psi_full_avg10 ?? null,
    oom_kills_delta:usage?.oom_kills_delta ?? null,host_oom_kills_delta:usage?.host_oom_kills_delta ?? null,observed_at:usage?.observed_at ?? null,
    predicted_over_target:known ? usage.busy_cores!+expected+ramping>cores*config.cpu_target_utilization : null,
    telemetry_state:known && usage.psi_full_avg10!==null && usage.memory_psi_full_avg10!==null ? 'measured' : 'unknown',
    advisory:known ? null : 'cpu_usage_unknown'};
  const reason = fresh && usage.psi_full_avg10!==null && usage.psi_full_avg10 > config.cpu_psi_full_hard_cap_pct ? 'cpu_pressure'
    : fresh && usage.memory_psi_full_avg10!==null && usage.memory_psi_full_avg10 > config.memory_psi_full_hard_cap_pct ? 'memory_pressure'
    : fresh && (usage.host_oom_kills_delta ?? 0)>0 ? 'host_oom' : null;
  return {...numbers,reason,admitted:reason === null};
}
