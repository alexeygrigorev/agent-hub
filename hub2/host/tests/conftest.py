"""Hermetic Codex quota wrapper for the host suite.

Codex starts require the operator's maintained quota wrapper. CI runners and
disposable-HOME runs do not have one, so every test gets a synthetic wrapper
with the canonical body (operator env CODEX_LAUNCH_WRAPPER). Tests that
exercise a missing/unsafe wrapper set the variable themselves."""
import os
import pytest

BODY = ('#!/usr/bin/env bash\nset -euo pipefail\n'
        'python3 /synthetic/scripts/quota-gate.py\n'
        'exec /synthetic/bin/codex "$@"\n')


@pytest.fixture(scope='session')
def _synthetic_codex_wrapper(tmp_path_factory):
    directory = tmp_path_factory.mktemp('codex-wrapper')
    path = directory / 'launch-codex.sh'
    path.write_text(BODY)
    path.chmod(0o700)
    return str(path)


@pytest.fixture(autouse=True)
def _codex_wrapper_env(_synthetic_codex_wrapper, monkeypatch):
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', _synthetic_codex_wrapper)
