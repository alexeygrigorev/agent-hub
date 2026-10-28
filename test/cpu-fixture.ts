/** Explicit fresh, idle CPU evidence for positive placement fixtures. */
export function freshCpu(observed_at: string, cores=8, busy_cores=0, io_psi_full=0) {
  return {busy_percent:busy_cores/cores*100,busy_cores,cores,psi_avg10:0,psi_full_avg10:0,memory_psi_full_avg10:0,io_psi_full_avg10:io_psi_full,oom_kills_delta:0,observed_at,interval_s:1};
}
