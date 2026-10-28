"""Test-package import guards.

Hermetic-by-default (intake 6b624e65 AC1): importing this package - which
unittest package-mode discovery and module runs do before any test module -
scrubs HOME/XDG and arms the config hard guard before agent_hub_host is
imported. Bare-module discovery (discover -s tests) is covered by
agent_hub_host._test_isolation.auto_isolate() in the package.

Launch-identity guard (incident edb71968): a disposable HOME does not
isolate the per-launch worker credential - worker_auth.config_path()
follows native launch identity (APLEXER_*, HUB_WORKER_CONFIG_FILE) instead
of HOME/XDG. Any test process importing this package must not inherit the
launching session's identity, or tests that save fixtures
(config.save_config, auth.device_login, ...) rewrite that session's real
hub-worker-auth file and destroy its credential. Tests that need an
identity opt back in explicitly via patch.dict on a tmp workspace.
"""
import os

for _var in list(os.environ):
    if _var.startswith("APLEXER_") or _var == "HUB_WORKER_CONFIG_FILE":
        del os.environ[_var]

from agent_hub_host._test_isolation import isolate as _isolate_hermetic_home

_isolate_hermetic_home()
