/** Revalidate presenting host authority and explicit repository consent live. */
export function hostRepositoryAuthorizer(sql: any, caller: any,
  hostAllowed: (machine: string, caller: any) => boolean): () => boolean {
  return () => {
    try {
      if (caller?.kind !== 'host' || !caller.tokenId || !caller.machineId) return false;
      const cred = sql.exec('SELECT * FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
      if (!cred || cred.kind !== 'host' || cred.revoked_at || cred.machine_id !== caller.machineId ||
          cred.token_hash !== caller.tokenHash) return false;
      const expires = Date.parse(cred.expires_at);
      if (!Number.isFinite(expires) || expires <= Date.now()) return false;
      const scopes = typeof cred.scopes === 'string' ? JSON.parse(cred.scopes) : cred.scopes;
      if (!Array.isArray(scopes) || !scopes.includes('host') || !scopes.includes('repositories:read')) return false;
      return hostAllowed(cred.machine_id, caller) === true;
    } catch { return false; }
  };
}
