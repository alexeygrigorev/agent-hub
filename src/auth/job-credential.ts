type JobAuthoritySql = { exec(query: string, ...bindings: any[]): { toArray(): any[] } };

/** The job and its current lease are the authority for a job bearer. Old
 * generations need no scan of retained credentials to become unusable. */
export function jobCredentialAllowed(sql: JobAuthoritySql, credential: any, now = Date.now()): boolean {
  if (credential.kind !== 'job') return true;
  const job: any = sql.exec('SELECT * FROM jobs WHERE id=?', credential.subject).toArray()[0];
  if (!job || !['claimed', 'running'].includes(job.status) || !job.claim_token_hash ||
      job.claim_token_hash !== credential.token_hash || !credential.machine_id ||
      credential.machine_id !== (job.holder_machine_id || job.holder_host_id)) return false;
  let payload: any = {};
  try { payload = JSON.parse(job.payload || '{}'); } catch {}
  const name = job.role_name || (job.kind === 'root-run' ? 'root'
    : job.kind === 'writer-run' && payload?.date ? `writer:${payload.date}`
    : job.kind === 'head-recovery' ? `head:${payload?.project || 'default'}`
    : job.lease_name || `job:${job.id}`);
  const lease: any = sql.exec('SELECT * FROM leases WHERE name=?', name).toArray()[0];
  const expiry = (value: unknown): number => {
    if (typeof value === 'number' || typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value)) {
      const n = Number(value); return n < 1e12 ? n * 1000 : n;
    }
    return typeof value === 'string' ? Date.parse(value) : NaN;
  };
  return !!lease && lease.holder_job_id === job.id && lease.epoch === job.lease_epoch && expiry(lease.expires_at) > now;
}
