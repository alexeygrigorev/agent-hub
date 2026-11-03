import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { generateTokenPair } from '../src/auth/token';

it('refreshes a host credential and its binding despite extra trigger writes', async () => {
  const stub=(env as any).HUB.get((env as any).HUB.idFromName('auth-cas-trigger-'+crypto.randomUUID()));
  await (runInDurableObject as any)(stub,async(hub:any,state:any)=>{
    const raw=state.storage.sql,pair=await generateTokenPair(),now=Date.now(),at=new Date(now).toISOString();
    raw.exec("INSERT INTO machines(id,name,hostname,registered_at,credential_id) VALUES('host','host','host',?,'old-host')",at);
    raw.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at)
      VALUES('old-host','host',?,?,'family','host','host','machine:host','["host"]','test',?,?)`,pair.accessHash,pair.refreshHash,at,new Date(now+3600000).toISOString());
    raw.exec('CREATE TABLE auth_cas_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT)');
    raw.exec("CREATE TRIGGER auth_cas_credential AFTER UPDATE OF revoked_at ON credentials BEGIN INSERT INTO auth_cas_audit(value) VALUES('credential-a'),('credential-b'); END");
    raw.exec("CREATE TRIGGER auth_cas_machine AFTER UPDATE OF credential_id ON machines BEGIN INSERT INTO auth_cas_audit(value) VALUES('machine-a'),('machine-b'); END");
    const response=await hub.fetch(new Request('https://hub.test/api/token/refresh',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({grant_type:'refresh_token',refresh_token:pair.refreshToken})}));
    expect(response.status).toBe(200);
    const body=await response.json();expect(body.access_token).toMatch(/^hubt_/);
    const machine=raw.exec("SELECT credential_id FROM machines WHERE id='host'").one();
    expect(machine.credential_id).not.toBe('old-host');
    expect(raw.exec('SELECT revoked_at FROM credentials WHERE id=?',machine.credential_id).one().revoked_at).toBeNull();
    expect(raw.exec('SELECT COUNT(*) AS n FROM auth_cas_audit').one().n).toBe(4);
  });
});
