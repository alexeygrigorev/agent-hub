import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { hostRepositoryAuthorizer } from '../src/host-repo-authority';
import { sha256 } from '../src/auth/crypto';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';

async function fixture(fn: (hub: Hub, token: string, caller: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('host-repos-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => {
    const token = 'synthetic-host-token-' + crypto.randomUUID();
    const hash = await sha256(token);
    hub.sql.exec(`INSERT INTO credentials
      (id,kind,token_hash,family_id,subject,created_by_sub,machine_id,scopes,created_at,expires_at,approval_seq)
      VALUES ('repo-host','host',?,'repo-family','repo-machine','fixture','repo-machine',?,?,?,1)`, hash,
      JSON.stringify(['host', 'repositories:read']), new Date().toISOString(),
      new Date(Date.now() + 3600000).toISOString());
    hub.sql.exec(`INSERT INTO machines (id,name,hostname,credential_id,approval_seq,registered_at)
      VALUES ('repo-machine','repo-machine','fixture','repo-host',1,?)`, new Date().toISOString());
    await fn(hub, token, { kind: 'host', tokenId: 'repo-host', tokenHash: hash, machineId: 'repo-machine' });
  });
}

describe('host repository enrollment authority', () => {
  it('rechecks scope, expiry, revocation and host family after every async boundary', async () => fixture(async (hub, token, caller) => {
    let hostAllowed = true;
    const authorize = hostRepositoryAuthorizer(hub.sql, caller, () => hostAllowed);
    expect(authorize()).toBe(true);
    hostAllowed = false;
    expect(authorize()).toBe(false);
    hostAllowed = true;
    for (const [column, value] of [['scopes', '["host"]'], ['revoked_at', new Date().toISOString()],
      ['expires_at', 'not-a-date'], ['machine_id', 'other-machine'], ['token_hash', 'changed-hash']]) {
      const before: any = hub.sql.exec('SELECT * FROM credentials WHERE id=?', caller.tokenId).toArray()[0];
      hub.sql.exec(`UPDATE credentials SET ${column}=? WHERE id=?`, value, caller.tokenId);
      expect(authorize(), column).toBe(false);
      hub.sql.exec(`UPDATE credentials SET ${column}=? WHERE id=?`, before[column], caller.tokenId);
    }
    expect(authorize()).toBe(true);
  }));

  it('mounts the route only for current host credentials with repository consent', async () => fixture(async (hub, token) => {
    const request = () => new Request('https://hub.test/api/host/repositories', { headers: { Authorization: 'Bearer ' + token } });
    const available = await hub.fetch(request());
    expect(available.status).toBe(503);
    expect(await available.json()).toEqual({ error: 'repository_access_not_configured' });
    hub.sql.exec("UPDATE credentials SET scopes='[\"host\"]' WHERE id='repo-host'");
    expect((await hub.fetch(request())).status).toBe(403);
    hub.sql.exec("UPDATE credentials SET scopes='[\"host\",\"repositories:read\"]' WHERE id='repo-host'");
    hub.sql.exec("UPDATE machines SET credential_id='replacement',approval_seq=2 WHERE id='repo-machine'");
    expect((await hub.fetch(request())).status).toBe(403);
  }));

  it('includes repository access in the single host pairing and refuses it for agents', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('repo-pair-' + crypto.randomUUID()));
    const pair = (client_kind: string) => stub.fetch(new Request('https://hub.test/api/device/code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind, name: 'repo-pair-fixture', hostname: 'fixture',
        requested_scopes: ['host', 'bus:read', 'bus:send', 'health:report', 'repositories:read'] }),
    }));
    expect((await pair('host')).status).toBe(200);
    expect((await pair('agent')).status).toBe(400);
  });
});
