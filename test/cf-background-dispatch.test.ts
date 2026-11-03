import { describe, expect, it, vi } from 'vitest';
import { env, runInDurableObject, createExecutionContext } from 'cloudflare:test';
import worker from '../src/worker';

const fixture = (fn: (hub: any, state: any) => Promise<void>) => (runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('dispatch-' + crypto.randomUUID())), fn);

describe('strict queue background dispatch', () => {
  it('cron does not instantiate or call an idle Durable Object', async () => {
    const get = vi.fn(() => { throw new Error('cron touched Hub'); });
    for (const cron of ['*/5 * * * *','*/30 * * * *']) {
      await worker.scheduled({ cron } as any, { HUB: { get, idFromName: get } } as any, createExecutionContext());
    }
    expect(get).not.toHaveBeenCalled();
  });

  it('empty alarms never call maintenance or rearm themselves', () => fixture(async (hub, state) => {
    hub.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
    hub.backgroundWork.cancel('root-slot');
    await hub.refreshBackgroundJobs();
    await state.storage.deleteAlarm();
    const spies = [vi.spyOn(hub.jobsService,'processAlarms'),vi.spyOn(hub.webhookService,'pump'),
      vi.spyOn(hub.sessionControlService,'processDueWakes'),vi.spyOn(hub.sessionAudit,'tick'),
      vi.spyOn(hub.hostHistoryService,'sample')];
    await hub.meter.run('empty-background', async () => { await hub.alarm(); await hub.alarm(); });
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    expect(await state.storage.getAlarm()).toBeNull();
    expect(hub.meter.snapshot()['empty-background'].rows_written).toBe(0);
    // Each explicitly invoked alarm may read its single empty index head.
    // No alarm is scheduled, so a truly idle Hub performs zero such calls.
    expect(hub.meter.snapshot()['empty-background'].rows_read).toBeLessThanOrEqual(2);
    vi.restoreAllMocks();
  }));

  it('one invocation consumes only the first pending entity', () => fixture(async (hub, state) => {
    hub.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
    hub.backgroundWork.cancel('root-slot');
    await hub.refreshBackgroundJobs();
    const now = Date.now();
    hub.backgroundWork.enqueue({id:'first',kind:'job-deadline',dueAt:now-2000,payload:{entity_id:'a'}});
    hub.backgroundWork.enqueue({id:'second',kind:'job-deadline',dueAt:now-1000,payload:{entity_id:'b'}});
    const process = vi.spyOn(hub.jobsService,'processBackground').mockReturnValue(null);
    await hub.alarm();
    expect(process.mock.calls).toEqual([['a']]);
    expect(hub.sql.exec("SELECT id FROM hub_background_work WHERE id='second'").toArray()).toHaveLength(1);
    expect(await state.storage.getAlarm()).not.toBeNull();
    vi.restoreAllMocks();
  }));
});
