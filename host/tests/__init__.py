"""Test-package import guard (incident edb71968).

A disposable HOME does not isolate the per-launch worker credential:
worker_auth.config_path() follows native launch identity (APLEXER_*,
HUB_WORKER_CONFIG_FILE) instead of HOME/XDG. Any test process importing
this package must not inherit the launching session's identity, or tests
that save fixtures (config.save_config, auth.device_login, ...) rewrite
that session's real hub-worker-auth file and destroy its credential.

This covers runners that bypass the pytest harness in conftest.py (e.g. a
bare `python -m unittest` from a session shell). Tests that need an
identity opt back in explicitly via patch.dict on a tmp workspace.
"""
import os

for _var in list(os.environ):
    if _var.startswith("APLEXER_") or _var == "HUB_WORKER_CONFIG_FILE":
        del os.environ[_var]
