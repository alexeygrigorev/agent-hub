import json
import subprocess
import time
import os

_gate_cache = None
_gate_cache_time = 0.0

def get_gate_status(clock=None) -> dict:
    global _gate_cache, _gate_cache_time
    now = clock.time() if clock else time.monotonic()
    
    if _gate_cache and now - _gate_cache_time < 300.0:
        return _gate_cache

    try:
        quota_gate_path = os.path.expanduser("~/git/cloudflare-agent-git/scripts/quota-gate.py")
        if not os.path.exists(quota_gate_path):
            _gate_cache = {"launch_allowed": False, "reason": "no launcher"}
            _gate_cache_time = now
            return _gate_cache

        res = subprocess.run(
            ["python3", quota_gate_path],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False
        )
        
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout)
                allowed = data.get("launch_allowed", False)
                reason = data.get("reason", "unknown")
            except Exception:
                allowed = False
                reason = "malformed"
        else:
            allowed = False
            reason = "failed"
            
        if "remaining" in reason or "%" in reason:
            reason = "quota ok" if allowed else "quota low"
            
        _gate_cache = {"launch_allowed": allowed, "reason": reason}
    except subprocess.TimeoutExpired:
        _gate_cache = {"launch_allowed": False, "reason": "timeout"}
    except Exception:
        _gate_cache = {"launch_allowed": False, "reason": "error"}
        
    _gate_cache_time = now
    return _gate_cache
