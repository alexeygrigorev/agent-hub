/** CPU admission primitives. Load average is context only, never an admission input. */
export interface CpuUsage {
  busy_percent: number; busy_cores: number; cores: number;
  psi_avg10: number | null; observed_at: string; interval_s: number;
}
export interface CpuConfig {
  cpu_target_utilization: number; cpu_psi_hard_cap_pct: number;
  cpu_default_cores: number; cpu_estimate_multiplier: number;
}
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0;
export function sanitizeCpuUsage(value: any): CpuUsage | null {
  if (!value || !finite(value.busy_percent) || value.busy_percent > 100 || !finite(value.busy_cores)
    || !Number.isSafeInteger(value.cores) || value.cores < 1 || value.cores > 65536
    || value.busy_cores > value.cores || Math.abs(value.busy_cores - value.busy_percent * value.cores / 100) > .01
    || !finite(value.interval_s) || value.interval_s <= 0 || value.interval_s > 180
    || typeof value.observed_at !== 'string' || value.observed_at.length > 64 || !Number.isFinite(Date.parse(value.observed_at))
    || (value.psi_avg10 != null && (!finite(value.psi_avg10) || value.psi_avg10 > 100))) return null;
  return {busy_percent:value.busy_percent,busy_cores:value.busy_cores,cores:value.cores,
    psi_avg10:value.psi_avg10 ?? null,observed_at:value.observed_at,interval_s:value.interval_s};
}
export function cpuVerdict(raw: unknown, cores: number, expected: number, ramping: number, config: CpuConfig, now: number) {
  const usage = sanitizeCpuUsage(raw);
  const known = usage && usage.cores === cores && now - Date.parse(usage.observed_at) <= 180000
    && now - Date.parse(usage.observed_at) >= -30000 && finite(expected) && finite(ramping);
  const numbers = {current_cpu_cores:known ? usage.busy_cores : null,expected_cpu_cores:expected,
    ramping_cpu_cores:ramping,limit_cpu_cores:cores * config.cpu_target_utilization,
    target_utilization:config.cpu_target_utilization,psi_avg10:usage?.psi_avg10 ?? null,observed_at:usage?.observed_at ?? null};
  const reason = !known ? 'cpu_usage_unknown'
    : usage.psi_avg10 !== null && usage.psi_avg10 > config.cpu_psi_hard_cap_pct ? 'cpu_pressure'
    : usage.busy_cores + expected + ramping > numbers.limit_cpu_cores ? 'cpu_headroom' : null;
  return {...numbers,reason,admitted:reason === null};
}
