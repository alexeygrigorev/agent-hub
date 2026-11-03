import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

test('pool-wide minimum is not assigned to a window without provenance', async () => {
  const source = await readFile(new URL('../public/quota.js', import.meta.url), 'utf8');
  const marker = "  $('quota-refresh').addEventListener('click', refresh);";
  assert.ok(source.includes(marker), 'quota script initialization marker exists');
  const instrumented = source.replace(marker, '  globalThis.quotaTest = { windowsFor, unattributedMinimum, render }; return;');
  class Element {
    constructor() { this.children = []; this.style = {}; this.attributes = {}; this.classList = { add() {} }; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
  }
  const elements = new Map();
  const document = {
    createElement: () => new Element(),
    getElementById: id => {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
  };
  const context = vm.createContext({ document });
  vm.runInContext(instrumented, context);
  const { windowsFor, unattributedMinimum, render } = context.quotaTest;
  const pool = {
    provider: 'codex', account: 'example', source: 'machine-fallback', state: 'fresh',
    remaining_percent: 20,
    windows: {
      '5h': { present: true, percent_remaining: 80, reset_at: '2026-11-04T00:00:00Z' },
      '7d': { present: true, percent_remaining: 60, reset_at: '2026-11-10T00:00:00Z' },
    },
  };
  // The pool minimum comes from a second observation whose window details are
  // unavailable to the client; it must not inherit the displayed 7d reset.
  const windows = windowsFor({ hosts: [] }, pool, [pool]);
  assert.deepEqual(Array.from(windows, ([name, win]) => [name, win.percent_remaining, win.reset_at]), [
    ['5h', 80, '2026-11-04T00:00:00Z'],
    ['7d', 60, '2026-11-10T00:00:00Z'],
  ]);
  assert.equal(unattributedMinimum(pool, windows), 20);
  assert.equal(unattributedMinimum({ ...pool, remaining_percent: 60 }, windows), null);
  assert.equal(unattributedMinimum({ ...pool, state: 'stale' }, windows), null);

  render({ pools: [pool], hosts: [] }, { accounts: [] }, null, null);
  const all = element => [element, ...element.children.flatMap(all)];
  const rendered = all(elements.get('quota-list'));
  assert.ok(rendered.some(element => element.textContent === 'Pool minimum: 20% remaining (window not identified)'));
  const weeklyRow = rendered.find(element => element.className === 'quota-provider__row' &&
    element.children.some(child => child.textContent === '7d'));
  assert.ok(weeklyRow, 'weekly row rendered');
  assert.ok(weeklyRow.children.some(child => child.textContent === '60%'), 'weekly row retains its observed percentage');
  assert.ok(!weeklyRow.children.some(child => child.textContent === '20%'), 'pool minimum is not assigned to the weekly row');
});
