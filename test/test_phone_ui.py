#!/usr/bin/env python3
"""F7: phone UI static contract on the file bytes (stdlib only).

Runs in full Python discovery (test-full-local.sh) and in CI. Complements
test/intake-phone.test.ts (live API + wiring) and test/phone-ui.browser.mjs
(390x844 layout in both themes): Workers turn CSS `?raw` imports empty, so
stylesheet rule content is pinned here instead.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ["xterm", "node-pty", "ssh2", "pocketshell"]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


class TestPhoneUiStatic(unittest.TestCase):
    def test_phone_media_queries(self):
        for name in ["public/intake.css", "public/engines.css", "public/sessions.css",
                     "public/fleet.css", "public/machines.css", "public/quota.css",
                     "public/settings.css", "public/metrics.css", "public/auth.css"]:
            body = read(name)
            self.assertRegex(body, r"@media\s*\(max-width:\s*820px\)", f"{name} needs a phone breakpoint")

    def test_viewport_and_shell(self):
        for name in ["public/intake.html", "public/engines.html", "public/index.html",
                     "public/sessions.html", "public/fleet.html",
                     "public/machines.html", "public/quota.html", "public/settings.html",
                     "public/conversations.html", "public/metrics.html"]:
            body = read(name)
            self.assertIn('name="viewport"', body, name)
            self.assertIn("width=device-width", body, name)
            self.assertIn("data-dk-drawer-toggle", body, name)
            self.assertIn("shell.js", body, name)

    def test_intake_sidebar_everywhere(self):
        for name in ["public/index.html", "public/sessions.html", "public/fleet.html",
                     "public/machines.html", "public/quota.html", "public/settings.html",
                     "public/conversations.html", "public/metrics.html",
                     "public/engines.html", "public/intake.html"]:
            self.assertIn('/intake.html', read(name), f"{name} links Intake")

    def test_no_terminal_baggage_in_product_paths(self):
        for name in ["public/intake.html", "public/intake.js", "public/intake.css",
                     "public/machines.html", "public/machines.js", "public/machines.css",
                     "public/quota.html", "public/quota.js", "public/quota.css",
                     "public/settings.html", "public/settings.js", "public/settings.css",
                     "public/engines.html", "public/engines.css", "src/worker.ts"]:
            body = read(name)
            for token in FORBIDDEN + ["PTY", "attach-ws"]:
                self.assertNotIn(token, body, f"{name} must not contain {token}")

    def test_intake_client_reads_only_on_open(self):
        body = read("public/intake.js")
        for forbidden in ["/assign", "/continue", "api/sessions", "fleet/launches", "startSession"]:
            self.assertNotIn(forbidden, body)
        self.assertIn("/api/intake/tasks", body)
        self.assertIn("scope_flag", body)

    def test_delivery_never_accepted(self):
        self.assertNotIn("accepted", read("public/sessions.js"))
        conv = read("public/conversations.js")
        for stage in ["delivered_at", "accepted_at", "acked_at"]:
            self.assertNotIn(stage, conv)

    def test_worker_maps_clean_phone_urls(self):
        body = read("src/worker.ts")
        for route in ["'/intake'", "'/intake.html'", "'/intake.js'", "'/intake.css'",
                      "'/fleet'", "'/machines'", "'/quota'", "'/settings'",
                      "'/conversations'", "'/engines'"]:
            self.assertIn(route, body)
        for rewrite in ["url.pathname = '/intake.html'", "url.pathname = '/fleet.html'",
                        "url.pathname = '/machines.html'", "url.pathname = '/quota.html'",
                        "url.pathname = '/settings.html'",
                        "url.pathname = '/conversations.html'", "url.pathname = '/engines.html'"]:
            self.assertIn(rewrite, body)

    def test_chunk3_machines_quota_settings_and_provider_usage(self):
        quota_html = read("public/quota.html")
        quota_js = read("public/quota.js")
        quota_css = read("public/quota.css")
        for col in ["Provider", "Window", "Remaining", "Resets"]:
            self.assertIn(f"<span>{col}</span>", quota_html)
        self.assertIn("<h2>Provider usage</h2>", quota_html)
        for win in ["'5h'", "'7d'", "'monthly'"]:
            self.assertIn(win, quota_js)
        self.assertIn('role\', \'progressbar\'', quota_js)
        self.assertIn('quota-badge--error', quota_js)
        self.assertIn('quota-provider__resets', quota_js)
        self.assertIn("if (incoming.present !== true) return;", quota_js)
        self.assertIn("if (!Number.isFinite(ts)) return remaining === 100 ? '' : 'reset time unknown';", quota_js)
        for cls in [".quota-meter--high", ".quota-meter--mid", ".quota-meter--low", ".quota-badge--error"]:
            self.assertIn(cls, quota_css)

        machines_js = read("public/machines.js")
        for field in ["Capacity", "Blocker", "Last check-in", "Diagnostics"]:
            self.assertIn(field, machines_js)
        self.assertNotIn("/api/fleet/sessions", machines_js)

        fleet_js = read("public/fleet.js")
        for target in ["/quota.html", "/machines.html", "/settings.html"]:
            self.assertIn(target, fleet_js)

    @unittest.skipUnless(shutil.which("node"), "node is required for JS runtime check")
    def test_quota_and_fleet_runtime_behavior(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function makeNode(tag) {
  return {
    tagName: String(tag).toUpperCase(),
    textContent: '',
    className: '',
    href: '',
    disabled: false,
    hidden: false,
    open: false,
    style: {},
    dataset: {},
    attributes: {},
    children: [],
    classList: {
      add(...cls) {
        const cur = new Set((this._owner.className || '').split(/\s+/).filter(Boolean));
        cls.forEach(c => cur.add(c));
        this._owner.className = [...cur].join(' ');
      }
    },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k]; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
    addEventListener(type, fn) { this._listeners = this._listeners || {}; this._listeners[type] = fn; },
    querySelector() { return makeNode('span'); }
  };
}
function createEl(tag) {
  const n = makeNode(tag);
  n.classList._owner = n;
  return n;
}
function textOf(node) {
  return (node.textContent || '') + (node.children || []).map(textOf).join(' ');
}
function findAll(node, pred, out = []) {
  if (pred(node)) out.push(node);
  for (const c of node.children || []) findAll(c, pred, out);
  return out;
}

const byId = new Map();
const getEl = id => {
  if (!byId.has(id)) byId.set(id, createEl('div'));
  return byId.get(id);
};

const now = Date.now();
const quotaPayload = {
  as_of: new Date(now).toISOString(),
  canonical: { providers: {}, accounts: {} },
  coverage: { fresh: ['m1'], machines_total: 1 },
  account_coverage: { total: 4, fresh: ['claude-main', 'codex-main', 'zai-main'], stale: [], unavailable: ['codex-orin'] },
  pools: [
    {
      provider: 'claude',
      account: 'claude-main',
      state: 'fresh',
      source: 'server',
      observed_at: new Date(now - 5000).toISOString(),
      remaining_percent: 0,
      windows: {
        '5h': { present: true, percent_remaining: 100, reset_at: null, rolling: true },
        '7d': { present: true, percent_remaining: 0, reset_at: new Date(now + 2 * 86400000 + 6 * 3600000).toISOString(), rolling: false },
        monthly: { present: false, percent_remaining: null, reset_at: null, rolling: false }
      }
    },
    {
      provider: 'codex',
      account: 'codex-main',
      state: 'fresh',
      source: 'server',
      observed_at: new Date(now - 5000).toISOString(),
      remaining_percent: 42,
      banked_resets: [
        { available: true, expires_at: new Date(now + 27 * 86400000 + 12 * 3600000).toISOString(), label: 'Full reset' },
        { available: true, expires_at: new Date(now + 30 * 86400000).toISOString(), label: 'Full reset' }
      ],
      windows: {
        '5h': { present: false, percent_remaining: null, reset_at: null, rolling: false },
        '7d': { present: true, percent_remaining: 42, reset_at: new Date(now + 4 * 86400000 + 18 * 3600000).toISOString(), rolling: false }
      }
    },
    {
      provider: 'codex',
      account: 'codex-orin',
      state: 'unavailable',
      source: 'server',
      observed_at: null,
      remaining_percent: null,
      windows: {
        '5h': { present: false, percent_remaining: null, reset_at: null, rolling: false, error: 'unavailable' },
        '7d': { present: false, percent_remaining: null, reset_at: null, rolling: false, error: 'unavailable' }
      }
    },
    {
      provider: 'zai',
      account: 'zai-main',
      state: 'fresh',
      source: 'machine-fallback',
      observed_at: new Date(now - 5000).toISOString(),
      remaining_percent: 3,
      windows: {
        '5h': { present: true, percent_remaining: 100, reset_at: null, rolling: true },
        '7d': { present: true, percent_remaining: 3, reset_at: new Date(now + 3 * 86400000 + 23 * 3600000).toISOString(), rolling: false }
      }
    }
  ],
  hosts: []
};
const fleetPayload = {
  accounts: [
    { label: 'claude-main', active: 1, max_concurrency: 4, placeable_slots: 3 },
    { label: 'codex-main', active: 2, max_concurrency: 8, placeable_slots: 6 },
    { label: 'codex-orin', active: 0, max_concurrency: 4, placeable_slots: 0 },
    { label: 'zai-main', active: 0, max_concurrency: 6, placeable_slots: 6 }
  ]
};
const resetsPayload = {
  observations: [{ account: 'zai-main' }],
  grants: [
    { account: 'zai-main', expires_at: now + 49 * 86400000 + 7 * 3600000, label: 'Weekly reset 1' },
    { account: 'zai-main', expires_at: now + 50 * 86400000, label: 'Weekly reset 2' },
    { account: 'zai-main', expires_at: now + 51 * 86400000, label: 'Weekly reset 3' }
  ]
};
const enginesPayload = {
  credentials: [
    { label: 'codex-orin', provider: 'codex', status: 'invalid', status_reason: 'quota_unauthorized' }
  ]
};

const avatar = createEl('span');
const ctx = {
  document: {
    getElementById: getEl,
    createElement: createEl,
    querySelector: () => avatar,
    hidden: false
  },
  location: { origin: 'https://hub.test', search: '', hash: '', assign() {}, replace() {} },
  fetch: async (url) => ({
    ok: true,
    status: 200,
    json: async () => {
      if (url === '/auth/me') return { email: 'admin@fake.local', csrf_token: 'csrf' };
      if (url.startsWith('/api/quota?view=fleet')) return quotaPayload;
      if (url.startsWith('/api/fleet')) return fleetPayload;
      if (url === '/api/quota/resets') return resetsPayload;
      if (url === '/api/engines') return enginesPayload;
      return {};
    }
  }),
  setInterval: () => 0,
  Date,
  Math,
  Number,
  String,
  Object,
  Array,
  Map,
  Set,
  Promise,
  URL,
  URLSearchParams,
   encodeURIComponent
};
vm.runInNewContext(fs.readFileSync('public/quota.js', 'utf8'), ctx);

setImmediate(() => {
  const list = getEl('quota-list');
  assert.equal(list.children.length, 4, 'renders all 4 provider/account pools');
  const allText = textOf(list);
  assert.ok(allText.includes('Claude'));
  assert.ok(allText.includes('2 resets available · expires in 27d'));
  assert.ok(allText.includes('3 resets available · expires in 49d'));
  assert.ok(allText.includes('HTTP Error 401: Unauthorized'));

  // Claude has 5h (100%, empty reset) and 7d (0%), no absent monthly row.
  const claudeCard = list.children[0];
  const claudeRows = findAll(claudeCard, n => n.className === 'quota-provider__row');
  assert.equal(claudeRows.length, 2);
  assert.equal(claudeRows[0].children[0].textContent, '5h');
  assert.equal(claudeRows[0].children[2].textContent, '100%');
  assert.equal(claudeRows[0].children[3].textContent, '');
  assert.equal(claudeRows[1].children[0].textContent, '7d');
  assert.equal(claudeRows[1].children[2].textContent, '0%');
  assert.ok(claudeRows[1].children[3].textContent.startsWith('in 2d'));

  // Codex-main has only 7d (absent 5h is omitted) with amber mid level.
  const codexCard = list.children[1];
  const codexRows = findAll(codexCard, n => n.className === 'quota-provider__row');
  assert.equal(codexRows.length, 1);
  assert.equal(codexRows[0].children[0].textContent, '7d');
  assert.equal(codexRows[0].children[2].textContent, '42%');
  assert.ok(codexRows[0].children[1].className.includes('quota-meter--mid'));
  assert.ok(codexRows[0].children[2].className.includes('quota-provider__percent--mid'));

  // Codex-orin is unavailable with error badge and no broken window bars.
  const orinCard = list.children[2];
  const orinRows = findAll(orinCard, n => n.className === 'quota-provider__row');
  assert.equal(orinRows.length, 0);
  const errorBadges = findAll(orinCard, n => (n.className || '').includes('quota-badge--error'));
  assert.equal(errorBadges.length, 1);
  assert.equal(errorBadges[0].textContent, 'error');
});
"""
        proc = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
