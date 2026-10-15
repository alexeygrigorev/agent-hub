import json
import subprocess
import time
import os

_gate_cache = None
_gate_cache_time = 0.0

def get_gate_status(clock=None) -> dict:
    global _gate_cache, _gate_cache_time
    now = clock.time() if clock else time.monotonic()
    
    if _gate_cache and now - _gate_cache_time <= 300.0:
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
        
        if res.returncode in (0, 75):
            try:
                data = json.loads(res.stdout)
                allowed = data.get("launch_allowed", False)
                if not isinstance(allowed, bool):
                    allowed = False
                reason = str(data.get("reason", "unknown"))
            except Exception:
                allowed = False
                reason = "malformed"
        else:
            allowed = False
            reason = "failed"
            
        if "remaining" in reason or "%" in reason or "balance" in reason.lower() or "$" in reason:
            reason = "quota ok" if allowed else "quota low"
            
        # The quota-gate bool is codex-only and does NOT prove an executable Root profile.
        # launch_allowed stays denied (pending#59) unless the host's own genuine operational
        # proof holds: installed launcher Root boundary, accepted pins, trusted entrypoint.
        # The proof takes no caller input; a missing SDK or any exception denies.
        if allowed:
            try:
                from .runtime import operational_root_profile_proof
                proven, _ = operational_root_profile_proof()
            except Exception:
                proven = False
            if not proven:
                allowed = False
                reason = "pending#59"
            
        _gate_cache = {"launch_allowed": allowed, "reason": reason}
    except subprocess.TimeoutExpired:
        _gate_cache = {"launch_allowed": False, "reason": "timeout"}
    except Exception:
        _gate_cache = {"launch_allowed": False, "reason": "error"}
        
    _gate_cache_time = now
    return _gate_cache
