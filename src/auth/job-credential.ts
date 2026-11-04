type JobAuthoritySql = { exec(query: string, ...bindings: any[]): { toArray(): any[] } };

/** Exact legacy/role lease identity, shared by auth and deadline producers. */
export function jobLeaseName(job:any):string {
  if(job.role_name)return job.role_name;
  let payload:any={};try{payload=JSON.parse(job.payload||'{}');}catch{}
  return job.kind==='root-run'?'root':job.kind==='writer-run'&&payload?.date?`writer:${payload.date}`
    :job.kind==='head-recovery'?`head:${payload?.project||'default'}`:job.lease_name||`job:${job.id}`;
}
export function jobLeaseNameSql(alias:string):string {
  const json=`CASE WHEN json_valid(${alias}.payload) THEN ${alias}.payload ELSE '{}' END`;
  return `COALESCE(NULLIF(${alias}.role_name,''),CASE WHEN ${alias}.kind='root-run' THEN 'root'
    WHEN ${alias}.kind='writer-run' AND NULLIF(NULLIF(json_extract(${json},'$.date'),''),0) IS NOT NULL THEN 'writer:'||json_extract(${json},'$.date')
    WHEN ${alias}.kind='head-recovery' THEN 'head:'||COALESCE(NULLIF(NULLIF(json_extract(${json},'$.project'),''),0),'default')
    ELSE 'job:'||${alias}.id END)`;
}

/** The job and its current lease are the authority for a job bearer. Old
 * generations need no scan of retained credentials to become unusable. */
export function jobCredentialAllowed(sql: JobAuthoritySql, credential: any, now = Date.now()): boolean {
  if (credential.kind !== 'job') return true;
  const job: any = sql.exec('SELECT * FROM jobs WHERE id=?', credential.subject).toArray()[0];
  if (!job || !['claimed', 'running'].includes(job.status) || !job.claim_token_hash ||
      job.claim_token_hash !== credential.token_hash || !credential.machine_id ||
      credential.machine_id !== (job.holder_machine_id || job.holder_host_id)) return false;
  const name = jobLeaseName(job);
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

/** A terminal role token may only present its already-recorded finish receipt.
 * This grants no generic job authority: the HTTP caller must additionally
 * match POST /api/jobs/<subject>/finish, and finishJob rechecks configuration,
 * generation, host binding, token/epoch and the exact receipt contents. */
export function roleFinishReceiptAllowed(sql:JobAuthoritySql,credential:any,subject:string,now=Date.now()):boolean {
  if(credential.kind!=='job'||credential.subject!==subject||credential.revoked_at!=null||!(Date.parse(credential.expires_at)>now))return false;
  const job:any=sql.exec('SELECT * FROM jobs WHERE id=?',subject).toArray()[0];
  if(!job?.role_name||!job.role_finish||!['succeeded','finished','failed','cancelled'].includes(job.status)
    ||job.claim_token_hash!==credential.token_hash||!credential.machine_id
    ||credential.machine_id!==(job.holder_machine_id||job.holder_host_id))return false;
  const lease:any=sql.exec('SELECT epoch,holder_job_id FROM leases WHERE name=?',job.role_name).toArray()[0];
  return !!lease&&lease.epoch===job.lease_epoch&&(lease.holder_job_id===null||lease.holder_job_id===job.id);
}
