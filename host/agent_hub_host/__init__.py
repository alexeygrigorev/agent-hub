"""Agent Hub Host Package"""
__version__ = "0.2.0"

# Intake 6b624e65 AC1: bare-module unittest discovery never imports the tests
# package, so the hermetic test isolation also arms here when a test runner
# is active. Production CLI/agent processes never import unittest or pytest
# and are never isolated. Keep it that way: production code must not import
# unittest or pytest, or test runs of it would scrub HOME.
from . import _test_isolation as _test_isolation

_test_isolation.auto_isolate()
