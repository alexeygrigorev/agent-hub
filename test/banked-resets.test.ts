import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { BankedResets, berlinDeadline, expiryThreshold } from '../src/banked-resets';
import { createSessionCookie } from '../src/auth/session';

const HOUR = 3_600_000;
const NOW = Date.UTC(2026, 9, 28, 3);
const operator = { isAdmin: true, kind: 'human', actor: 'human:test' };
const inDO: any = runInDurableObject;
const post = (value: unknown) => new Request('https://hub/api/quota/resets', { method: 'POST', body: JSON.stringify(value) });
const grant = (hours = 48, label = 'Weekly reset') => ({ label, expires_at: new Date(NOW + hours * HOUR).toISOString() });
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('reset-' + crypto.randomUUID()));
const setup = (h: any) => {
  h.sql.exec('DELETE FROM fleet_accounts');
  h.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex','codex',1,15,1)");
  h.sql.exec("INSERT INTO fleet_accounts VALUES ('codex-orin','codex-orin',1,15,1)");
  h.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-zai','zai',1,15,1)");
};
const quota = (grants: any[]) => ({ status: 'ok', details: { banked_resets: grants.map(g => ({ ...g, available: true })) } });

describe('reset expiry thresholds and Berlin deadlines', () => {
  it('warns at 48h with >=36h margin, again strictly below24h, never after expiry', () => {
    expect(expiryThreshold(NOW + 48 * HOUR + 1, NOW)).toBeNull();
    expect(expiryThreshold(NOW + 48 * HOUR, NOW)).toBe('early');
    expect(expiryThreshold(NOW + 36 * HOUR, NOW)).toBe('early');
    expect(expiryThreshold(NOW + 24 * HOUR, NOW)).toBe('early');
    expect(expiryThreshold(NOW + 24 * HOUR - 1, NOW)).toBe('urgent');
    expect(expiryThreshold(NOW, NOW)).toBeNull();
    expect(expiryThreshold(NOW - 1, NOW)).toBeNull();
    expect(expiryThreshold(NaN, NOW)).toBeNull();
  });
  it('uses Europe/Berlin DST instead of the server timezone', () => {
    expect(berlinDeadline(Date.UTC(2026, 0, 1, 12))).toContain('13:00');
    expect(berlinDeadline(Date.UTC(2026, 6, 1, 12))).toContain('14:00');
    expect(berlinDeadline(NOW)).toContain('Europe/Berlin');
  });
});

describe('scheduled reset alerts and operator fallback', () => {
  it('exposes no apply/redeem/approval API, even to an authenticated administrator', async () => {
    const target = stub();
    await inDO(target, async (h: any) => {
      setup(h);
      const now = new Date().toISOString();
      h.sql.exec("INSERT INTO users(sub,email,is_admin,first_seen_at,last_seen_at) VALUES ('reset-admin','admin@fake.local',1,?,?)", now, now);
    });
    const cookie = (await createSessionCookie(env as any, { sub: 'reset-admin', email: 'admin@fake.local',
      isAdmin: true, csrfToken: 'reset-csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
    const headers = { Cookie: cookie, 'X-CSRF-Token':'reset-csrf', 'Content-Type':'application/json' };
    for (const verb of ['apply','redeem','approve','consume']) {
      const response = await target.fetch(new Request('https://hub/api/quota/resets/' + verb, { method:'POST', headers, body:'{}' }));
      expect(response.status).toBe(404);
    }
    const r = await target.fetch(new Request('https://hub/api/quota/resets', { method:'POST', headers,
      body:JSON.stringify({ account:'shared-zai',grants:[],action:'apply' }) }));
    expect(r.status).toBe(400);
  });
  it('reads all accounts, creates actionable unknown reminders only once', async () => {
    await inDO(stub(), async (h: any) => {
      setup(h);
      h.sql.exec("UPDATE fleet_accounts SET enabled=0 WHERE label='codex-orin'");
      const service = () => new BankedResets(h.sql, h.livenessService, () => NOW);
      const first = service().run();
      expect(first).toHaveLength(3);
      expect(first.map(a => a.subject).sort()).toEqual(['codex-orin','shared-codex','shared-zai']);
      expect(first.find(a => a.subject === 'codex-orin').detail).toContain('codex-orin (codex)');
      const listing = await (await service().handle(new Request('https://hub/api/quota/resets'), operator)).json() as any;
      expect(listing.accounts).toContainEqual({label:'codex-orin',provider:'codex'});
      expect(first.find(a => a.subject === 'shared-zai').detail).toContain('record');
      expect(service().run()).toHaveLength(0);
    });
  });
  it('manual grants trigger one alert per threshold across restarts and repeated saves', async () => {
    await inDO(stub(), async (h: any) => {
      setup(h);
      let now = NOW;
      const service = () => new BankedResets(h.sql, h.livenessService, () => now);
      for (const account of ['shared-codex','codex-orin','shared-zai']) {
        expect((await service().handle(post({ account, grants: [grant(48, account === 'shared-zai' ? 'Weekly reset' : 'Full reset')] }), operator)).status).toBe(200);
      }
      let alerts = service().run();
      expect(alerts).toHaveLength(3);
      expect(alerts.every(a => a.detail.includes('at least 36 hours'))).toBe(true);
      const codex = alerts.find(a => a.detail.startsWith('shared-codex'));
      expect(codex.detail).toContain('Full reset');
      expect(codex.detail).toContain('credits unchanged');
      expect(alerts.every(a => a.detail.includes('Alexey, please approve'))).toBe(true);
      expect(alerts.every(a => a.detail.includes('Hub and agents must not redeem it'))).toBe(true);
      expect(codex.detail).toContain('Europe/Berlin');
      expect(alerts.find(a => a.detail.startsWith('shared-zai')).detail).toContain('Aliyun');
      expect(service().run()).toHaveLength(0);
      await service().handle(post({ account: 'shared-zai', grants: [grant()] }), operator);
      expect(service().run()).toHaveLength(0);
      now += 24 * HOUR;
      expect(service().run()).toHaveLength(0); // exactly24h is not urgent
      now += 1;
      alerts = service().run();
      expect(alerts).toHaveLength(3);
      expect(alerts.every(a => a.detail.includes('URGENT'))).toBe(true);
      expect(service().run()).toHaveLength(0);
      expect(h.sql.exec('SELECT * FROM banked_reset_notices').toArray()).toHaveLength(6);
      // Retention/removal of the alert history never resets the receipt ledger.
      h.sql.exec("DELETE FROM liveness_alerts WHERE subject_kind='banked_reset_expiry'");
      expect(service().run()).toHaveLength(0);
    });
  });
  it('keeps duplicate expiry grants distinct and ignores reorderings/API recovery', async () => {
    await inDO(stub(), async (h: any) => {
      setup(h);
      let now = NOW;
      const service = () => new BankedResets(h.sql, h.livenessService, () => now);
      const grants = [grant(), grant(), grant(40, 'Full reset')];
      await service().handle(post({ account:'shared-codex', grants }), operator);
      expect(service().run().filter(a => a.subject_kind === 'banked_reset_expiry')).toHaveLength(3);
      now += 1000;
      h.sql.exec('INSERT OR REPLACE INTO server_quota_snapshots VALUES (?,?,?,?)', 'shared-codex','codex',new Date(now).toISOString(),JSON.stringify(quota([...grants].reverse())));
      expect(service().run().filter(a => a.subject_kind === 'banked_reset_expiry')).toHaveLength(0);
      expect(h.sql.exec('SELECT * FROM banked_reset_notices').toArray()).toHaveLength(3);
    });
  });
  it('preserves known deadlines on failed/missing reads and resolves used grants', async () => {
    await inDO(stub(), async (h: any) => {
      setup(h);
      let now = NOW;
      const service = () => new BankedResets(h.sql, h.livenessService, () => now);
      const save = (q: any) => h.sql.exec('INSERT OR REPLACE INTO server_quota_snapshots VALUES (?,?,?,?)', 'shared-zai','zai',new Date(now).toISOString(),JSON.stringify(q));
      save(quota([grant()]));
      expect(service().run().filter(a => a.subject_kind === 'banked_reset_expiry')).toHaveLength(1);
      now += HOUR;
      save({ status:'error', details: {} });
      service().run();
      expect(h.sql.exec('SELECT id FROM banked_reset_grants WHERE active=1').toArray()).toHaveLength(1);
      now += HOUR;
      save(quota([]));
      service().run();
      expect(h.sql.exec('SELECT id FROM banked_reset_grants WHERE active=1').toArray()).toHaveLength(0);
      expect(h.sql.exec("SELECT id FROM liveness_alerts WHERE subject_kind='banked_reset_expiry' AND resolved_at IS NULL").toArray()).toHaveLength(0);
    });
  });
  it('uses account-specific subscription probes for alternate Codex homes', async () => {
    await inDO(stub(), async (h: any) => {
      setup(h);
      h.sql.exec('INSERT INTO fleet_subscriptions VALUES (?,?,?,?,?,?)', 'host', 'codex-orin', 'codex', 'pro', new Date(NOW).toISOString(), JSON.stringify(quota([grant()])));
      const service = new BankedResets(h.sql, h.livenessService, () => NOW);
      const alerts = service.run().filter(a => a.subject_kind === 'banked_reset_expiry');
      expect(alerts).toHaveLength(1);
      expect(alerts[0].detail).toContain('codex-orin');
    });
  });
  it('records explicit none, rejects unauthorised and invalid manual values', async () => {
    await inDO(stub(), async (h: any) => {
      setup(h);
      const service = new BankedResets(h.sql, h.livenessService, () => NOW);
      expect((await service.handle(post({ account:'shared-zai', grants:[] }), {})).status).toBe(403);
      expect((await service.handle(post({ account:'shared-zai', grants:[] }), { isAdmin:true,kind:'host' })).status).toBe(403);
      for (const data of [null, { account:'unknown',grants:[] }, { account:'shared-zai',grants:[{ label:'Weekly reset', expires_at:'2026-10-30T03:00:00' }] },
        { account:'shared-zai',grants:[grant(-1)] }, { account:'shared-zai',grants:[{ ...grant(),label:'<script>' }] },
        { account:'shared-zai',grants:[],action:'apply' }, { account:'shared-zai',grants:[],approve:true }]) {
        expect((await service.handle(post(data), operator)).status).toBe(400);
      }
      service.run();
      await service.handle(post({ account:'shared-zai',grants:[] }), operator);
      expect(service.run()).toHaveLength(0);
      expect(h.sql.exec("SELECT id FROM liveness_alerts WHERE subject_kind='banked_reset_unknown' AND subject='shared-zai' AND resolved_at IS NULL").toArray()).toHaveLength(0);
    });
  });
  it('reports late discovery honestly and emits only the urgent threshold for <24h', async () => {
    await inDO(stub(), async (h: any) => {
      setup(h);
      const service = new BankedResets(h.sql, h.livenessService, () => NOW);
      await service.handle(post({ account:'shared-zai',grants:[grant(23)] }), operator);
      const alerts = service.run().filter(a => a.subject_kind === 'banked_reset_expiry');
      expect(alerts).toHaveLength(1);
      expect(alerts[0].detail).toContain('URGENT');
      expect(service.run()).toHaveLength(0);
    });
  });
});
