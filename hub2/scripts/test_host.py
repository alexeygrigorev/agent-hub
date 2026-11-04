#!/usr/bin/env python3
"""Run Hub 2 host unit tests from any working directory and on both OSes."""

from __future__ import annotations

import sys
import importlib
import unittest
from pathlib import Path


HUB2 = Path(__file__).resolve().parents[1]
HOST = HUB2 / "host"
TESTS = HOST / "tests"
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(TESTS))

suite = unittest.TestSuite()
loader = unittest.defaultTestLoader
for path in sorted(TESTS.glob("test_*.py")):
    suite.addTests(loader.loadTestsFromModule(importlib.import_module(path.stem)))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
