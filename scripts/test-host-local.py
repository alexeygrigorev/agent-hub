import os, sys, time, json, base64, hmac, hashlib, urllib.request, threading, subprocess
from pathlib import Path

def wait_for_server(url, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"{url}/api/healthz")
            with urllib.request.urlopen(req) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

def make_admin_cookie():
    payload = {"sub": "admin-1", "email": "admin@fake.local", "isAdmin": True, "csrfToken": "1234", "iat": int(time.time()), "exp": int(time.time()) + 3600}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(b"super_secret_session_key_32_chars_long_for_tests", payload_b64.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"hub_session={payload_b64}.{signature}"

def do_fetch(url, method="GET", headers=None, data=None):
    if headers is None: headers = {}
    if data is not None:
        data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req) as response:
            return response.read(), response.getheader("Set-Cookie")
    except urllib.error.HTTPError as e:
        print(f"HTTPError {e.code}: {e.read().decode()}")
        raise

def main():
    if not os.path.exists(".dev.vars"):
        with open(".dev.vars", "w") as f:
            f.write("SESSION_SECRET=super_secret_session_key_32_chars_long_for_tests\nAUTH_BASE_URL=https://auth.fake.local\nAUTH_CLIENT_ID=fake_client_id\nAUTH_ISSUER=https://auth.fake.local\nAUTH_JWKS_URL=https://auth.fake.local/.well-known/jwks.json\nHUB_ADMINS=admin@fake.local\n")

    port = 18946
    base_url = f"http://localhost:{port}"

    wrangler = subprocess.Popen(["npx", "wrangler", "dev", "--port", str(port), "--local", "--log-level", "none"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for_server(base_url):
            sys.exit(1)
        resp, _ = do_fetch(f"{base_url}/api/device/code", method="POST", data={"client_kind": "host", "name": "test-host", "requested_scopes": ["host"]})
        d_flow = json.loads(resp)
        admin_cookie = make_admin_cookie()
        resp, _ = do_fetch(f"{base_url}/auth/me", method="GET", headers={"Cookie": admin_cookie})
        csrf_token = json.loads(resp)["csrf_token"]
        resp, _ = do_fetch(f"{base_url}/api/device/approve/{d_flow['user_code']}", method="POST", headers={"Cookie": admin_cookie, "x-csrf-token": csrf_token}, data={"granted_scopes": ["host"]})
        resp, _ = do_fetch(f"{base_url}/api/device/token", method="POST", data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": d_flow["device_code"], "client_id": "test-host"})
        token_data = json.loads(resp)
        os.environ["XDG_CONFIG_HOME"] = os.path.abspath(".local/tmp/config")
        config_dir = Path(".local/tmp/config/agent-hub")
        config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config_dir, 0o700)
        with open(config_dir / "host.json", "w") as f: json.dump({"base_url": base_url, "access_token": token_data["access_token"], "refresh_token": token_data["refresh_token"]}, f)
        os.chmod(config_dir / "host.json", 0o600)
        sys.path.insert(0, os.path.abspath("host"))
        from agent_hub_host import api
        ws_called = False
        def fake_ws_handshake(*args, **kwargs):
            nonlocal ws_called
            ws_called = True
            raise Exception("Forced")
        api.ws_handshake = fake_ws_handshake
        agent = api.HostAgent()
        agent_thread = threading.Thread(target=agent.run)
        agent_thread.start()
        time.sleep(3)
        agent.shutdown_requested = True
        agent_thread.join(timeout=5)
        assert ws_called
        from agent_hub_host.auth import get_access_token
        resp, _ = do_fetch(f"{base_url}/api/machines/available", method="GET", headers={"Authorization": f"Bearer {get_access_token()}"})
        machines = json.loads(resp).get("machines", [])
        assert len(machines) == 1
        print("Integration test passed!")
    finally:
        wrangler.terminate()
        wrangler.wait()

if __name__ == "__main__": main()
