import json
import time
import urllib.request
import urllib.error
import urllib.parse
from . import config
from . import api

def device_login(base_url: str, client_kind: str, name: str, hostname: str, requested_scopes: list):
    api.check_url(base_url)
    
    code_url = f"{base_url}/api/device/code"
    code_payload = json.dumps({
        "client_kind": client_kind,
        "name": name,
        "hostname": hostname,
        "requested_scopes": requested_scopes,
    }).encode("utf-8")
    
    req = urllib.request.Request(code_url, data=code_payload, headers={"Content-Type": "application/json"})
    try:
        code_res = api.safe_http_json(req)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error during device code request: {e}")
    
    device_code = code_res["device_code"]
    user_code = code_res["user_code"]
    verification_uri = code_res["verification_uri"]
    interval = code_res.get("interval", 5)
    expires_in = code_res["expires_in"]
    
    print(f"Please visit: {verification_uri}")
    print(f"And enter the code: {user_code}")
    
    token_url = f"{base_url}/api/device/token"
    start_time = time.time()
    
    while True:
        if time.time() - start_time > expires_in:
            raise RuntimeError("Device code expired.")
            
        time.sleep(interval)
        
        token_payload = json.dumps({
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": "host-agent"
        }).encode("utf-8")
        
        treq = urllib.request.Request(token_url, data=token_payload, headers={"Content-Type": "application/json"})
        try:
            token_res = api.safe_http_json(treq)
            config.update_config(
                access_token=token_res["access_token"],
                refresh_token=token_res.get("refresh_token"),
                base_url=base_url
            )
            print("Login successful.")
            return
        except urllib.error.HTTPError as e:
            if e.code == 400:
                try:
                    err_res = json.loads(e.read().decode("utf-8"))
                    err_code = err_res.get("error")
                except Exception:
                    raise RuntimeError("Token error: malformed response")
                if err_code == "authorization_pending":
                    continue
                elif err_code == "slow_down":
                    interval += 5
                    continue
                elif err_code == "access_denied":
                    raise RuntimeError("Access denied by user.")
                elif err_code == "expired_token":
                    raise RuntimeError("Device code expired.")
                else:
                    raise RuntimeError(f"Token error: {err_code}")
            else:
                raise RuntimeError(f"HTTP error during token request: {e.code}")
        except urllib.error.URLError as e:
            print(f"Network error, retrying... ({e})")
            continue

import threading
_refresh_lock = threading.Lock()

def refresh_token():
    with _refresh_lock:
        cfg = config.load_config()
        refresh = cfg.get("refresh_token")
        base_url = cfg.get("base_url")
        if not refresh or not base_url:
            raise RuntimeError("No refresh token or base URL available. Please login.")
            
        token_url = f"{base_url}/api/token/refresh"
        token_payload = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh
        }).encode("utf-8")
        
        req = urllib.request.Request(token_url, data=token_payload, headers={"Content-Type": "application/json"})
        try:
            token_res = api.safe_http_json(req)
            config.update_config(
                access_token=token_res["access_token"],
                refresh_token=token_res.get("refresh_token", refresh)
            )
            return token_res["access_token"]
        except urllib.error.HTTPError as e:
            should_repair = False
            if e.code == 401:
                should_repair = True
            elif e.code == 400:
                try:
                    body = e.read(1024 * 1024)
                    err_res = json.loads(body.decode("utf-8"))
                    if err_res.get("error") == "invalid_grant":
                        should_repair = True
                except Exception:
                    pass
            
            if should_repair:
                config.clear_config()
                config.update_config(base_url=base_url)
                from . import system
                print(f"Refresh failed (code {e.code} revocation), pairing required again.")
                device_login(base_url, "host", "agent-hub-host", system.get_hostname(), ["host_agent"])
                return get_access_token()
            else:
                raise RuntimeError(f"Transient HTTP error during refresh: {e.code}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error during refresh: {e}")

def get_access_token() -> str:
    cfg = config.load_config()
    token = cfg.get("access_token")
    if not token:
        raise RuntimeError("Not logged in.")
    return token
