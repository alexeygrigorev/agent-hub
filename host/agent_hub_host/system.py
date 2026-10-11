import platform
import socket
import subprocess
import shutil

def get_hostname() -> str:
    return socket.gethostname()

def get_os() -> str:
    return platform.system()

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
        "network": get_network(),
        "roles": get_roles(),
        "capabilities": get_capabilities()
    }
