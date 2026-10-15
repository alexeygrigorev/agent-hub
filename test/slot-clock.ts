import { afterEach } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';

/**
 * Ticks refuse a 30-minute slot once it is 25 minutes old. Tests that tick "now" shift the jobs
 * clock to just after the next slot start when real time falls in that late window, so they do
 * not fail during minutes 25-29 and 55-59 of every hour. Past timestamps stay in the past, and
 * the shift is undone after each test.
 */
export function tickSafeOffset(now = Date.now()): number {
  const slot = Math.floor(now / 1800000) * 1800000;
  return now - slot < 20 * 60000 ? 0 : slot + 1800000 + 60000 - now;
}

const shifted = new Set<any>();
afterEach(async () => {
  for (const stub of shifted) await runInDurableObject(stub, async (instance: any) => { instance.jobsService.setClock(() => new Date()); });
  shifted.clear();
});

export async function pinJobsClock(stub: any, offset: number = tickSafeOffset()): Promise<Date> {
  await runInDurableObject(stub, async (instance: any) => { instance.jobsService.setClock(() => new Date(Date.now() + offset)); });
  shifted.add(stub);
  return new Date(Date.now() + offset);
}
