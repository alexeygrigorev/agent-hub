"""Tests and installer E2Es cannot mutate the production machine registry."""

import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agent_hub_host import _prod_guard, api


PROD = "https://hub.dtcdev.click"
PROD_WORKER = "https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev"
LOCAL = "http://127.0.0.1:19487"
PROTECTED_PATHS = (
    "/api/machines/register",
    "/api/host/poll",
    "/api/host/routes",
    "/api/fleet/routing/capabilities",
    "/api/routing/capabilities",
    "/api/device/code",
)

STATIC_SCAN_ALLOWLIST = {
    "host/tests/test_prod_guard_a38dffd9.py":
        "synthetic production requests are refused before the transport opens",
    "host/tests/test_api.py":
        "registration calls use a fake origin; the production hostname is only a proxy assertion",
    "host/tests/test_cli.py":
        "default URL behavior uses patched HTTP transport",
    "host/tests/test_role_dispatch_recovery.py":
        "production URL is a fake dispatcher fixture with mocked transport",
    "host/tests/test_domain_config.py":
        "production URL is used only to test origin parsing",
    "test/proxy-origin.test.ts":
        "Worker requests are in-memory forwarding fixtures, never outbound host writes",
    "test/docker/lifecycle.py":
        "the lifecycle rejects production Hub URLs before its local test requests",
}

PRODUCTION_MARKERS = ("hub.dtcdev.click", "agent-hub.cloudflare-ai-agent-de9ca0.workers.dev")
WRITE_MARKERS = (*PROTECTED_PATHS, "register_machine(", "device_login(")
SOURCE_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".sh"}


def unsafe_test_sources(roots, repo_root):
    offenders = []
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            relative = path.relative_to(repo_root).as_posix()
            if (relative not in STATIC_SCAN_ALLOWLIST
                    and any(host in source for host in PRODUCTION_MARKERS)
                    and any(marker in source for marker in WRITE_MARKERS)):
                offenders.append(relative)
    return sorted(offenders)


class TestProductionHubWriteGuard(unittest.TestCase):
    def test_production_urls_are_recognized_exactly(self):
        self.assertTrue(_prod_guard.is_production_base_url(PROD))
        self.assertTrue(_prod_guard.is_production_base_url(PROD.upper() + ":443/"))
        self.assertTrue(_prod_guard.is_production_base_url(PROD_WORKER))
        self.assertFalse(_prod_guard.is_production_base_url("https://hub.dtcdev.click.attacker.invalid"))
        self.assertFalse(_prod_guard.is_production_base_url(LOCAL))
        self.assertFalse(_prod_guard.is_production_base_url(None))

    def test_test_context_refuses_every_production_enrollment_and_host_write(self):
        with mock.patch.object(_prod_guard, "in_test_context", return_value=True):
            for path in PROTECTED_PATHS:
                with self.subTest(path=path):
                    with self.assertRaises(_prod_guard.ProductionHubWriteError):
                        _prod_guard.assert_test_hub_write(PROD, path, "POST", cfg={"test_machine": False})
            # A test marker is not an exemption: this build uses isolated test Hubs.
            with self.assertRaises(_prod_guard.ProductionHubWriteError):
                _prod_guard.assert_test_hub_write(PROD, "/api/host/poll", "POST", cfg={"test_machine": True})
            with self.assertRaises(_prod_guard.ProductionHubWriteError):
                _prod_guard.assert_test_hub_write(PROD_WORKER, "/api/host/poll", "POST", cfg={})

    def test_shared_http_transport_fails_before_opening_a_production_write(self):
        with mock.patch.object(_prod_guard, "in_test_context", return_value=True), \
                mock.patch.object(api, "_get_opener") as opener:
            request = urllib.request.Request(
                PROD + "/api/host/poll",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(_prod_guard.ProductionHubWriteError):
                api.safe_http_json(request)
            opener.assert_not_called()

    def test_pairing_and_host_mutations_are_blocked_but_reads_and_local_hub_are_allowed(self):
        with mock.patch.object(_prod_guard, "in_test_context", return_value=True):
            with self.assertRaises(_prod_guard.ProductionHubWriteError):
                _prod_guard.assert_test_hub_write(PROD, "/api/device/code", "POST")
            _prod_guard.assert_test_hub_write(PROD, "/api/fleet/routing/machines", "GET")
            _prod_guard.assert_test_hub_write(PROD, "/api/heartbeat", "POST")
            _prod_guard.assert_test_hub_write(LOCAL, "/api/host/poll", "POST")

    def test_non_test_process_is_not_gated(self):
        with mock.patch.object(_prod_guard, "in_test_context", return_value=False):
            _prod_guard.assert_test_hub_write(PROD, "/api/host/poll", "POST")

    def test_child_process_markers_arm_the_guard(self):
        with mock.patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "test_name (call)"}, clear=True), \
                mock.patch.object(_prod_guard.sys, "modules", {}):
            self.assertTrue(_prod_guard.in_test_context())
        with mock.patch.dict(os.environ, {"HUB_E2E": "1"}, clear=True), \
                mock.patch.object(_prod_guard.sys, "modules", {}):
            self.assertTrue(_prod_guard.in_test_context())
        with mock.patch.dict(os.environ, {"HUB_TEST_GUARD_HOME": "/synthetic/test-home"}, clear=True), \
                mock.patch.object(_prod_guard.sys, "modules", {}):
            self.assertTrue(_prod_guard.in_test_context())

    def test_static_guard_finds_a_test_that_targets_a_production_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad = root / "test_leak.py"
            bad.write_text(
                'BASE = "https://hub.dtcdev.click"\n'
                'api.register_machine(BASE)  # direct production enrollment\n',
                encoding="utf-8",
            )
            self.assertEqual(unsafe_test_sources([root], root), ["test_leak.py"])

    def test_repository_test_sources_have_no_unreviewed_production_registry_driver(self):
        repo_root = Path(__file__).resolve().parents[2]
        roots = (repo_root / "host" / "tests", repo_root / "test")
        self.assertEqual(unsafe_test_sources(roots, repo_root), [])

    def test_docker_lifecycle_rejects_production_hub_and_arms_installer_children(self):
        lifecycle = Path(__file__).resolve().parents[2] / "test" / "docker" / "lifecycle.py"
        source = lifecycle.read_text(encoding="utf-8")
        self.assertIn("isolated test Hub URL", source)
        self.assertIn("os.environ['HUB_E2E'] = '1'", source)
        self.assertIn("os.environ['HUB_BASE_URL'] = BASE", source)


if __name__ == "__main__":
    unittest.main()
