import json
import os
import sys
import tempfile
import stat
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

def _is_reparse_point(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_symlink():
        return True
    if sys.platform == "win32":
        try:
            st = os.lstat(path)
            if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except Exception:
            return True # fail closed
    return False

def _check_ancestors(path: Path):
    p = path.parent
    while p != p.parent:
        if _is_reparse_point(p):
            raise RuntimeError(f"Unsafe ancestor (symlink or reparse point): {p}")
        p = p.parent

def _set_permissions(path: Path, is_dir: bool = False):
    if sys.platform == "win32":
        import subprocess
        try:
            res_whoami = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10.0)
            if res_whoami.returncode != 0:
                raise RuntimeError("Failed to get current user SID")
            sid = res_whoami.stdout.strip().split(',')[-1].strip('"')
            
            icacls_exe = "icacls"
            res = subprocess.run([icacls_exe, str(path), "/inheritance:r"], capture_output=True, text=True, timeout=10.0)
            if res.returncode != 0:
                raise RuntimeError("Failed to remove inheritance")
            
            # Remove any explicit permissions for Everyone, Users, Authenticated Users
            subprocess.run([icacls_exe, str(path), "/remove:g", "Everyone", "Users", "BUILTIN\\Users", "Authenticated Users"], capture_output=True, text=True, timeout=10.0)
            
            res = subprocess.run([icacls_exe, str(path), "/grant:r", f"*{sid}:(F)"], capture_output=True, text=True, timeout=10.0)
            if res.returncode != 0:
                raise RuntimeError("Failed to grant ACL")
        except subprocess.TimeoutExpired:
            raise RuntimeError("icacls timeout")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"ACL error: {type(e).__name__}")
    else:
        if _is_reparse_point(path):
            raise RuntimeError("Symlinks are not allowed")
        mode = 0o700 if is_dir else 0o600
        os.chmod(path, mode)

def _validate_data(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    res = {}
    for k, v in data.items():
        if k in ("access_token", "refresh_token", "base_url"):
            if not isinstance(v, str) or not v:
                return {}
        res[k] = v
    return res

def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        _check_ancestors(path)
        if _is_reparse_point(path):
            return {}
            
        if sys.platform != "win32":
            st = os.stat(path)
            if st.st_mode & 0o077:
                return {} # unsafe permissions
                
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return _validate_data(data)
    except Exception:
        return {}

def save_config(data: dict):
    path = get_config_path()
    _check_ancestors(path)
    
    p = path.parent
    dirs = []
    while not p.exists():
        dirs.append(p)
        p = p.parent
    for d in reversed(dirs):
        d.mkdir(mode=0o700)
        _set_permissions(d, is_dir=True)
        
    # Ensure existing parent is also restricted
    if path.parent.exists():
        _set_permissions(path.parent, is_dir=True)
    
    # Directory is user-only BEFORE plaintext temporary write
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix="host.json.", text=True)
    try:
        _set_permissions(Path(tmp_path), is_dir=False) # fail closed atomic storage
        
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise RuntimeError("Config save failed")

def update_config(**kwargs):
    config = load_config()
    config.update(kwargs)
    save_config(config)

def clear_config():
    path = get_config_path()
    if path.exists():
        path.unlink()
