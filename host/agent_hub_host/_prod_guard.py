"""Fail closed when tests try to mutate production Hub host/account registries.

Installer E2Es must point the installed host at an isolated test Hub. Setting
``HUB_E2E=1`` for the whole process tree arms this guard even after the
installer starts a child process. Pytest and the hermetic test harness arm it
automatically as well.
"""

import os
import sys
import urllib.parse


PRODUCTION_HUB_HOSTS = frozenset({
    "hub.dtcdev.click",
    "agent-hub.cloudflare-ai-agent-de9ca0.workers.dev",
})
E2E_ENV = "HUB_E2E"
TEST_GUARD_HOME_ENV = "HUB_TEST_GUARD_HOME"
PROTECTED_PREFIXES = (
    "/api/machines/",
    "/api/host/",
    "/api/fleet/routing/",
    "/api/fleet/accounts/",
    "/api/routing/",
    "/api/device/",
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ProductionHubWriteError(RuntimeError):
    """A test attempted a production Hub enrollment or registry mutation."""


def is_production_base_url(base_url):
    if not isinstance(base_url, str) or not base_url:
        return False
    try:
        hostname = urllib.parse.urlsplit(base_url).hostname
    except ValueError:
        return False
    return (hostname or "").lower().rstrip(".") in PRODUCTION_HUB_HOSTS


def in_test_context():
    """Recognize pytest, the hermetic suite, and explicit installer E2Es."""
    if os.environ.get(E2E_ENV) == "1" or os.environ.get(TEST_GUARD_HOME_ENV):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return any(name == "pytest" or name.startswith("_pytest") for name in sys.modules)


def is_machine_registry_write(path, method):
    if not isinstance(path, str) or method not in WRITE_METHODS:
        return False
    normalized = path.rstrip("/") or "/"
    return any(normalized == prefix[:-1] or normalized.startswith(prefix)
               for prefix in PROTECTED_PREFIXES)


def assert_test_hub_write(base_url, path, method="POST", *, cfg=None):
    """Reject all test-context writes to production enrollment, host, or account APIs.

    The test path is isolation-only: a test credential is not an exemption.
    This prevents an installer E2E from creating a production machine even if
    it has a stale or incorrectly marked credential.
    """
    if (is_production_base_url(base_url) and in_test_context()
            and is_machine_registry_write(path, method.upper())):
        raise ProductionHubWriteError(
            "Test context refused a production Hub enrollment, host-registry or account-registry "
            "write; point the test at an isolated Hub."
        )


def assert_request_allowed(request):
    """Guard the shared Hub HTTP transport, covering every host caller."""
    try:
        parsed = urllib.parse.urlsplit(request.full_url)
        method = request.get_method().upper()
    except (AttributeError, TypeError, ValueError):
        return
    if is_machine_registry_write(parsed.path, method):
        assert_test_hub_write(request.full_url, parsed.path, method)
