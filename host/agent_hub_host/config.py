import json
import os
import sys
import tempfile
from pathlib import Path

def get_config_path() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not set on Windows")
        base = Path(appdata)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "agent-hub" / "host.json"

def _set_permissions(path: Path):
    if sys.platform == "win32":
        import ctypes
        import win32api
        import win32security
        import ntsecuritycon as con
        try:
            # Set user-only ACL
            user, domain, type = win32security.LookupAccountName("", win32api.GetUserName())
            sd = win32security.SECURITY_DESCRIPTOR()
            dacl = win32security.ACL()
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, con.FILE_ALL_ACCESS, user)
            sd.SetSecurityDescriptorDacl(1, dacl, 0)
            win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, sd)
        except ImportError:
            # fallback or failclosed if required, but standard library only so win32security might not be available
            # Let's try standard ctypes or cacls
            import subprocess
            res = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME')}:F"], capture_output=True)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to set ACL on Windows: {res.stderr.decode()}")
    else:
        os.chmod(path, 0o600)

def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data: dict):
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix="host.json.", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    
    _set_permissions(Path(tmp_path))
    os.replace(tmp_path, path)

def update_config(**kwargs):
    config = load_config()
    config.update(kwargs)
    save_config(config)

def clear_config():
    path = get_config_path()
    if path.exists():
        path.unlink()
