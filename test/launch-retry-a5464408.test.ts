import {describe, it, expect} from 'vitest';
import {env, runInDurableObject} from 'cloudflare:test';
import {FleetService, UNCLAIMED_OFFER_TTL_MS} from '../src/fleet';
import type {Hub} from '../src/hub';
import type {Env} from '../src/env';

describe('unclaimed offer expiry (a5464408)', () => {
  it('attests an exact terminal lock owner only to its assigned host', async () => {
    const binding = env as unknown as Env;
    const stub = binding.HUB.get(binding.HUB.idFromName('worktree-owner-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb));
      service.init();
      const launch = crypto.randomUUID(), owner = crypto.randomUUID(), intake = crypto.randomUUID();
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,
        machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        VALUES(?,'test','human','owner',?,'{}','host','codex','account','interactive','demo','','failed',?,?)`,launch,launch,at,at);
      hub.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,
        machine_id,engine,mode,state,created_at,updated_at)
        VALUES(?,'test',?,?,?,'host','codex','interactive','failed',?,?)`,owner,owner,JSON.stringify({intake_task_id:intake}),launch,at,at);
      const read = (machine: string) => service.handle(new Request('https://hub.test/api/fleet/worktree-owner/' + owner),
        {caller:{kind:'host',actor:machine,machineId:machine},scopes:['host']});
      expect((await read('other-host')).status).toBe(404);
      expect(await (await read('host')).json()).toEqual({session_id:owner,intake_id:intake,terminal:true});
      hub.sql.exec("UPDATE fleet_launches SET state='uncertain' WHERE id=?",launch);
      expect((await (await read('host')).json() as any).terminal).toBe(false);
      hub.sql.exec("UPDATE fleet_launches SET state='failed' WHERE id=?",launch);
      hub.sql.exec("UPDATE session_control_sessions SET state='running' WHERE id=?",owner);
      expect((await (await read('host')).json() as any).terminal).toBe(false);
    });
  });
  it('enriches only a proven legacy no-start worktree failure without changing lifecycle', async () => {
    const binding = env as unknown as Env;
    const stub = binding.HUB.get(binding.HUB.idFromName('launch-diagnostic-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb));
      service.init();
      for (const native of [null, 'already-started']) {
        const id = crypto.randomUUID();
        const task = 'hub-' + id;
        const at = new Date().toISOString();
        hub.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,
          machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,
          verification_status,exit_reason,launcher_task_id,native_id)
          VALUES(?,'test','human','legacy',?,'{}','host','opencode','account','headless','demo','',
            'failed',?,?,'not_started','launcher_command_failed',?,?)`,id,id,at,at,task,native);
        const diagnostic = {reason:'launcher_worktree_lock_failed',
          message:'The checkout could not be protected; an existing session or operator lock is preserved.',
          error_type:'ValueError',stage:'dispatch_headless'};
        const result = await service.handle(new Request('https://hub.test/api/fleet/launches/' + id + '/result',
          {method:'POST',body:JSON.stringify({state:'failed',launcher_task_id:task,confirmed_exit:true,
            reason:diagnostic.reason,diagnostic})}),
          {caller:{kind:'host',actor:'host',machineId:'host'},scopes:['host']});
        expect(result.status).toBe(native === null ? 200 : 409);
        const row: any = hub.sql.exec('SELECT * FROM fleet_launches WHERE id=?',id).one();
        expect(row.state).toBe('failed');
        expect(row.native_id).toBe(native);
        expect(row.exit_reason).toBe(native === null ? diagnostic.reason : 'launcher_command_failed');
      }
    });
  });
  it('releases stale unclaimed offers, preserves claims/effects, and fences a late claim', async () => {
    const binding = env as unknown as Env;
    const stub = binding.HUB.get(binding.HUB.idFromName('launch-expiry-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const now = Date.now();
      const at = new Date(now).toISOString();
      const old = new Date(now - UNCLAIMED_OFFER_TTL_MS - 1).toISOString();
      const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      service.init();
      const ids: Record<string,string> = {};
      for (const kind of ['stale','fresh','claimed','uncertain','running','native','task']) {
        const id = ids[kind] = crypto.randomUUID();
        const phase = ['uncertain','running'].includes(kind) ? kind : kind === 'claimed' ? 'starting' : 'offered';
        hub.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,
          machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,claim_key,native_id)
          VALUES(?,'test','human','expiry',?,'{}','host','codex','account','interactive','demo','',?,?,?,?,?)`,
          id,id,phase,old,kind === 'fresh' ? at : old, kind === 'claimed' ? 'claim-key' : null,
          kind === 'native' ? 'native-id' : null);
        hub.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,
          machine_id,engine,mode,state,created_at,updated_at)
          VALUES(?,'test',?,'{}',?,'host','codex','interactive','offered',?,?)`,crypto.randomUUID(),id,id,old,old);
      }
      // Routed task outcomes have their own result protocol; expiry cannot bypass it.
      const job = crypto.randomUUID();
      hub.sql.exec(`INSERT INTO routing_jobs(id,actor,request_key,request_json,state,
        created_at,updated_at,launch_id) VALUES(?,'test',?,'{}','offered',?,?,?)`,
        job,job,old,old,ids.task);
      expect(service.expireUnclaimedOffers()).toBe(1);
      const expired: any = hub.sql.exec('SELECT * FROM fleet_launches WHERE id=?',ids.stale).one();
      expect(expired.state).toBe('failed');
      expect(expired.verification_status).toBe('not_started');
      expect(JSON.parse(expired.launch_error_json).reason).toBe('launcher_offer_expired');
      const retried: any = hub.sql.exec("SELECT * FROM fleet_launches WHERE state='queued'").one();
      expect(retried.id).not.toBe(ids.stale);
      expect(retried.request_key).toBe(ids.stale);
      expect(retried.machine_id).toBe('');
      expect(retried.native_id).toBeNull();
      expect((hub.sql.exec('SELECT state FROM session_control_sessions WHERE fleet_launch_id=?',retried.id).one() as any).state).toBe('queued');
      for (const kind of ['fresh','native','task'])
        expect((hub.sql.exec('SELECT state FROM fleet_launches WHERE id=?',ids[kind]).one() as any).state).toBe('offered');
      for (const kind of ['claimed','uncertain','running'])
        expect((hub.sql.exec('SELECT state FROM fleet_launches WHERE id=?',ids[kind]).one() as any).state).toBe(kind === 'claimed' ? 'starting' : kind);
      expect(service.expireUnclaimedOffers()).toBe(0);
      const response = await service.handle(new Request('https://hub.test/api/fleet/launches/' + ids.stale + '/claim',
        {method:'POST',body:JSON.stringify({claim_key:'late-key'})}),
        {caller:{kind:'host',actor:'host',machineId:'host'},scopes:['host']});
      expect(response.status).toBe(409);
      expect((await response.json() as any).error).toBe('already_claimed');
    });
  });
});
