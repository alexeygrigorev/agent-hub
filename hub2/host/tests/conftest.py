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


@pytest.fixture(autouse=True)
def _claude_workspace_trust_step(request, monkeypatch):
    """Interactive Claude launches answer the folder-trust dialog by capturing
    the pane (intake 2699b8d7). Launch fakes that predate that step do not
    model `capture`; ClaudeWorkspaceTrust* tests exercise the real method."""
    cls = getattr(request.node, 'cls', None)
    if cls is not None and cls.__name__.startswith('ClaudeWorkspaceTrust'):
        return
    from agent_hub_host import native_launch
    monkeypatch.setattr(native_launch.NativeLauncher, 'accept_claude_workspace_trust',
                        lambda self, native_id, workspace, **kwargs: False)
