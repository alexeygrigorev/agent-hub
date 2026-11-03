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
  // Match jobs.parseExpiryMs exactly (including the historical 1e11 boundary).
  const expiry = (value: unknown): number | null => {
    if (value === null || value === undefined) return null;
    if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? (value < 1e11 ? value * 1000 : value) : null;
    const text = String(value).trim();
    if (!text || ['Infinity','-Infinity','NaN'].includes(text)) return null;
    if (!text.includes('-') && !text.includes('T')) {
      const n = Number(text);
      return Number.isFinite(n) && n > 0 ? (n < 1e11 ? n * 1000 : n) : null;
    }
    const n = Date.parse(text);
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  return !!lease && lease.holder_job_id === job.id && lease.epoch === job.lease_epoch && (expiry(lease.expires_at) ?? 0) > now;
}
