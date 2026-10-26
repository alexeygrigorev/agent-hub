/** Explicit fresh, idle CPU evidence for positive placement fixtures. */
export function freshCpu(observed_at: string, cores=8, busy_cores=0) {
  return {busy_percent:busy_cores/cores*100,busy_cores,cores,psi_avg10:0,psi_full_avg10:0,memory_psi_full_avg10:0,oom_kills_delta:0,observed_at,interval_s:1};
}
