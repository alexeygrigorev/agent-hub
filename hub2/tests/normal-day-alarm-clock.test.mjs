import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const require = createRequire(resolve(import.meta.dirname, '../package.json'));
const { Miniflare, convertV4MiniflareOptions, Log, LogLevel } = require('miniflare');

const script = `
export class AlarmClock {
  constructor(ctx) {
    this.ctx = ctx;
    this.count = 0;
    this.shift = 0;
    this.virtual = Date.now() - 3_600_000;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === '/arm') {
      this.shift = Number(url.searchParams.get('shift'));
      await this.ctx.storage.setAlarm(this.virtual + 1_000 + this.shift);
    }
    return Response.json({
      count: this.count,
      virtual_due: this.virtual + 1_000,
      stored: await this.ctx.storage.getAlarm(),
    });
  }

  async alarm() {
    this.count += 1;
    if (this.count < 3) {
      await this.ctx.storage.setAlarm(this.virtual + 1_000 + this.shift);
    } else {
      await this.ctx.storage.deleteAlarm();
    }
  }
}

export default {
  fetch(request, env) {
    const id = new URL(request.url).searchParams.get('id');
    return env.C.get(env.C.idFromName(id)).fetch(request);
  },
};
`;

const mf = new Miniflare(convertV4MiniflareOptions({
  log: new Log(LogLevel.ERROR),
  workers: [{
    name: 'alarm-clock-only',
    script,
    modules: true,
    compatibilityDate: '2024-04-05',
    durableObjects: { C: { className: 'AlarmClock', useSQLite: true } },
  }],
}));

try {
  const base = (await mf.ready).origin;
  await fetch(`${base}/arm?id=broken&shift=0`);
  await fetch(`${base}/arm?id=fixed&shift=${7 * 86_400_000}`);

  const deadline = performance.now() + 5_000;
  let broken;
  do {
    broken = await (await fetch(`${base}/?id=broken`)).json();
    if (broken.count === 3) break;
    await new Promise(resolve => setTimeout(resolve, 25));
  } while (performance.now() < deadline);

  const fixed = await (await fetch(`${base}/?id=fixed`)).json();
  assert.equal(broken.count, 3, 'past-due virtual alarms should complete all three bounded firings');
  assert.equal(fixed.count, 0, 'future physical alarm must not fire for the translated virtual deadline');
  assert.equal(fixed.stored - 7 * 86_400_000, fixed.virtual_due);
  console.log(JSON.stringify({
    passed: true,
    gate: 'native workerd physical/virtual alarm mismatch',
    broken_virtual_future_autofires: broken.count,
    fixed_virtual_future_autofires: fixed.count,
    inverse_deadline_preserved: true,
    note: 'Synthetic isolated clock test, not production cost proof',
  }));
} finally {
  await mf.dispose();
}
