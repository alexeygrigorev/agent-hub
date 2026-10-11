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
        import subprocess
        import shlex
        # get sid via whoami
        res_whoami = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True)
        if res_whoami.returncode != 0:
            raise RuntimeError("Failed to get current user SID")
        sid = res_whoami.stdout.strip().split(',')[-1].strip('"')
        
        icacls_exe = "icacls"
        # For testing, we might want to mock this if a dummy executable is in path
        res = subprocess.run([icacls_exe, str(path), "/inheritance:r"], capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to remove inheritance: {res.stderr}")
            
        res = subprocess.run([icacls_exe, str(path), "/grant:r", f"*{sid}:F"], capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to grant ACL: {res.stderr}")
    else:
        if path.is_symlink():
            raise RuntimeError("Symlinks are not allowed for config")
        os.chmod(path, 0o600)

def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        return {}
    if path.is_symlink():
        raise RuntimeError("Symlinks are not allowed for config")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return data
    except Exception:
        return {}

def save_config(data: dict):
    path = get_config_path()
    
    # Safely create directory tree with 0o700
    p = path.parent
    dirs = []
    while not p.exists():
        dirs.append(p)
        p = p.parent
    for d in reversed(dirs):
        d.mkdir(mode=0o700)
        
    if path.parent.is_symlink():
        raise RuntimeError("Symlinks are not allowed for config parent")
    
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix="host.json.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        _set_permissions(Path(tmp_path))
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise

def update_config(**kwargs):
    config = load_config()
    config.update(kwargs)
    save_config(config)

def clear_config():
    path = get_config_path()
    if path.exists():
        path.unlink()
