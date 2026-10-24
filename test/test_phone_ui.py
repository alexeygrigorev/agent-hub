#!/usr/bin/env python3
"""F7: phone UI static contract on the file bytes (stdlib only).

Runs in full Python discovery (test-full-local.sh) and in CI. Complements
test/intake-phone.test.ts (live API + wiring) and test/phone-ui.browser.mjs
(390x844 layout in both themes): Workers turn CSS `?raw` imports empty, so
stylesheet rule content is pinned here instead.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ["xterm", "node-pty", "ssh2", "pocketshell"]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


class TestPhoneUiStatic(unittest.TestCase):
    def test_phone_media_queries(self):
        for name in ["public/intake.css", "public/engines.css", "public/sessions.css",
                     "public/fleet.css", "public/metrics.css", "public/auth.css"]:
            body = read(name)
            self.assertRegex(body, r"@media\s*\(max-width:\s*820px\)", f"{name} needs a phone breakpoint")

    def test_viewport_and_shell(self):
        for name in ["public/intake.html", "public/engines.html", "public/index.html",
                     "public/sessions.html", "public/fleet.html",
                     "public/conversations.html", "public/metrics.html"]:
            body = read(name)
            self.assertIn('name="viewport"', body, name)
            self.assertIn("width=device-width", body, name)
            self.assertIn("data-dk-drawer-toggle", body, name)
            self.assertIn("shell.js", body, name)

    def test_intake_sidebar_everywhere(self):
        for name in ["public/index.html", "public/sessions.html", "public/fleet.html",
                     "public/conversations.html", "public/metrics.html",
                     "public/engines.html", "public/intake.html"]:
            self.assertIn('/intake.html', read(name), f"{name} links Intake")

    def test_no_terminal_baggage_in_product_paths(self):
        for name in ["public/intake.html", "public/intake.js", "public/intake.css",
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
                      "'/fleet'", "'/conversations'", "'/engines'"]:
            self.assertIn(route, body)
        for rewrite in ["url.pathname = '/intake.html'", "url.pathname = '/fleet.html'",
                        "url.pathname = '/conversations.html'", "url.pathname = '/engines.html'"]:
            self.assertIn(rewrite, body)


if __name__ == "__main__":
    unittest.main()
