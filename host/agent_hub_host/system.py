import os
import platform
import socket
import subprocess
import shutil

def get_hostname() -> str:
    return socket.gethostname()

def get_os() -> str:
    name = platform.system()
    return "windows" if name == "Windows" else name

def _normalize_arch(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    v = value.strip().lower()
    if v in ("arm64", "aarch64", "arm64_8a"):
        return "arm64"
    if v in ("amd64", "x86_64", "x64", "x86-64"):
        return "amd64"
    if v in ("386", "i386", "i686", "x86"):
        return "386"
    return None

def get_arch() -> str:
    """Normalized CPU arch (arm64/amd64), native even under x64 emulation on ARM64 Windows.

    On Windows under Prism/x64 emulation PROCESSOR_ARCHITECTURE reads AMD64
    while PROCESSOR_ARCHITEW6432 carries the native arch (ARM64). Check the
    latter first, then PROCESSOR_ARCHITECTURE, then platform.machine().
    Returns "unknown" when nothing normalizes.
    """
    for key in (("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE") if platform.system() == "Windows" else ()):
        try:
            norm = _normalize_arch(os.environ.get(key, ""))
            if norm:
                return norm
        except Exception:
            pass
    try:
        norm = _normalize_arch(platform.machine())
        if norm:
            return norm
    except Exception:
        pass
    return "unknown"

def get_network() -> str:
    return "default"

def get_roles() -> list:
    return ["work"]

def _get_git_sha(path: str) -> str:
    try:
        res = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return "unknown"

def get_capabilities() -> dict:
    capabilities = {}
    
    # Check launcher
    launcher_path = shutil.which("agent-quota-launcher")
    if launcher_path:
        capabilities["launcher"] = True
        try:
            res = subprocess.run(["agent-quota-launcher", "--version"], capture_output=True, text=True)
            capabilities["launcher_version"] = res.stdout.strip()
        except Exception:
            pass
    
    # Check aplexer
    if shutil.which("aplexer"):
        capabilities["aplexer"] = True
    
    return capabilities

def get_system_info() -> dict:
    return {
        "hostname": get_hostname(),
        "os": get_os(),
        "arch": get_arch(),
        "network": get_network(),
        "roles": get_roles(),
        "capabilities": get_capabilities()
    }
