"""AGENT_HUB_HOST_PATH for launcher subprocesses only (Hub intake 86254a30 AC6-8).

The maintained launcher imports managed quota/admission probes in-process only
from a validated AGENT_HUB_HOST_PATH (launcher 61dea0f/41ee04c); it has no PATH
or well-known fallback. The host passes the exact directory its own
agent_hub_host package was loaded from, after proving it is an absolute real
directory whose package is this running package. The value goes into a copy
of the environment handed to launcher subprocesses; the host process
environment and sys.path are never modified. When the proof fails the
variable is removed instead, so the launcher fails closed with its own reason.
"""
import os
from pathlib import Path

VAR = "AGENT_HUB_HOST_PATH"


def host_path():
    """The validated real directory containing this agent_hub_host package, or None."""
    try:
        package_init = Path(__file__).resolve().parent / "__init__.py"
        root = package_init.parent.parent
        if not (root.is_absolute() and root.is_dir() and package_init.is_file()):
            return None
        real_root = Path(os.path.realpath(root))
        if real_root != root:
            return None
        loaded = __import__("agent_hub_host").__file__
        if not loaded or os.path.realpath(loaded) != os.path.realpath(package_init):
            return None
        return str(real_root)
    except (OSError, ValueError, ImportError, AttributeError):
        return None


def launcher_environment(base=None):
    """A copy of base (default os.environ) carrying only a validated host path."""
    env = dict(os.environ if base is None else base)
    path = host_path()
    if path:
        env[VAR] = path
    else:
        env.pop(VAR, None)
    return env
