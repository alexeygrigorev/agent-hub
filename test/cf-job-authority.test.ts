import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { jobCredentialAllowed } from '../src/auth/job-credential';
import { generateTokenPair, verifyBearerScope } from '../src/auth/token';
import { RowsMeter } from '../src/rows-meter';

describe('indexed credential authority and timestamp coalescing', () => {
  it('denies old job generations and released leases using two point lookups', async () => {
    const stub = (env as any).HUB.get((env as any).HUB.idFromName('job-authority-' + crypto.randomUUID()));
    await (runInDurableObject as any)(stub, async (_hub: any, state: any) => {
      const raw = state.storage.sql, now = Date.now(), iso = new Date(now).toISOString();
      raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,claim_token_hash,holder_host_id,holder_machine_id,lease_epoch)
        VALUES('current','root-run','authority','{}','running',?,?,'test','current-hash','host','host',7)`, iso, new Date(now + 60000).toISOString());
      raw.exec("UPDATE leases SET holder_job_id='current',epoch=7,expires_at=? WHERE name='root'", new Date(now + 60000).toISOString());
      const cred = { kind: 'job', subject: 'current', token_hash: 'current-hash', machine_id: 'host' };
      const meter = new RowsMeter(() => now), sql = meter.wrap(raw);
      expect(meter.runSync('authority', () => jobCredentialAllowed(sql, cred, now))).toBe(true);
      expect(meter.snapshot().authority.rows_read).toBeLessThanOrEqual(4);
      expect(jobCredentialAllowed(sql, { ...cred, token_hash: 'old-hash' }, now)).toBe(false);
      expect(jobCredentialAllowed(sql, { ...cred, machine_id: 'other' }, now)).toBe(false);
      raw.exec("UPDATE leases SET epoch=8 WHERE name='root'");
      expect(jobCredentialAllowed(sql, cred, now)).toBe(false);
      raw.exec("UPDATE leases SET epoch=7 WHERE name='root'");
      raw.exec("UPDATE jobs SET status='succeeded' WHERE id='current'");
      expect(jobCredentialAllowed(sql, cred, now)).toBe(false);
    });
  });

  it('host authentication does not write on each successful poll', async () => {
    const stub = (env as any).HUB.get((env as any).HUB.idFromName('auth-coalesce-' + crypto.randomUUID()));
    await (runInDurableObject as any)(stub, async (_hub: any, state: any) => {
      const raw = state.storage.sql, token = await generateTokenPair(), now = Date.now();
      raw.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at)
        VALUES('host-auth','host',?,'host-auth','host','host','["host"]','test',?,?)`, token.accessHash, new Date(now).toISOString(), new Date(now + 3600000).toISOString());
      const meter = new RowsMeter(() => now), sql = meter.wrap(raw);
      expect(await verifyBearerScope(sql, 'Bearer ' + token.accessToken, 'host')).not.toBeNull();
      await meter.run('polls', async () => {
        for (let i = 0; i < 50; i++) expect(await verifyBearerScope(sql, 'Bearer ' + token.accessToken, 'host')).not.toBeNull();
      });
      expect(meter.snapshot().polls.rows_written).toBe(0);
      expect(meter.snapshot().polls.rows_read).toBeLessThanOrEqual(100);
    });
  });
});
