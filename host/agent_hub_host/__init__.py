"""Agent Hub Host Package"""
__version__ = "0.2.0"

# Intake 6b624e65 AC1: bare-module unittest discovery never imports the tests
# package, so the hermetic test isolation also arms here when a test runner
# is active. Production CLI/agent processes never import unittest or pytest
# and are never isolated. Keep it that way: production code must not import
# unittest or pytest, or test runs of it would scrub HOME.
from . import _test_isolation as _test_isolation

_test_isolation.auto_isolate()

# A dead engine proxy in the user environment (HTTPS_PROXY=http://127.0.0.1:8899,
# codex-vpn) silenced the desktop's controller, supervisor and updater alike
# (2026-11-02). Every Python process that imports this package, and every child it
# starts, therefore bypasses env proxies for the Hub host.
def ensure_hub_no_proxy(environ=None, hosts=('hub.dtcdev.click', '.dtcdev.click')):
    import os
    environ = os.environ if environ is None else environ
    for name in ('NO_PROXY', 'no_proxy'):
        current = [h.strip() for h in environ.get(name, '').split(',') if h.strip()]
        merged = current + [h for h in hosts if h not in current]
        environ[name] = ','.join(merged)


ensure_hub_no_proxy()
