import os
import sys
import time
import json
import base64
import hmac
import hashlib
import urllib.request
import urllib.error
import threading
import subprocess
from pathlib import Path

PORT = 18951
BASE_URL = f"http://127.0.0.1:{PORT}"
SESSION_SECRET = b"super_secret_session_key_32_chars_long_for_tests"

def make_admin_cookie(csrf_token="csrf_net_123"):
    payload = {
        "sub": "admin-1",
        "email": "admin@fake.local",
        "isAdmin": True,
        "csrfToken": csrf_token,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(SESSION_SECRET, payload_b64.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"hub_session={payload_b64}.{signature}"

def do_fetch(url, method="GET", headers=None, data=None):
    if headers is None:
        headers = {}
    encoded_data = None
    if data is not None:
        if isinstance(data, (dict, list)):
            encoded_data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif isinstance(data, (bytes, str)):
            encoded_data = data if isinstance(data, bytes) else data.encode("utf-8")
    req = urllib.request.Request(url, method=method, headers=headers, data=encoded_data)
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status, response.read(), dict(response.headers)

def wait_for_server(url, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            status, body, _ = do_fetch(f"{url}/api/healthz")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

def main():
    if not os.path.exists(".dev.vars"):
        with open(".dev.vars", "w") as f:
            f.write(f"SESSION_SECRET={SESSION_SECRET.decode()}\nAUTH_BASE_URL=https://auth.fake.local\nAUTH_CLIENT_ID=fake_client_id\nAUTH_ISSUER=https://auth.fake.local\nAUTH_JWKS_URL=https://auth.fake.local/.well-known/jwks.json\nHUB_ADMINS=admin@fake.local\n")

    print(f"=== Starting Real Local Network Integration Test on Port {PORT} ===")
    wrangler_cmd = [
        "npx", "wrangler", "dev",
        "--port", str(PORT),
        "--ip", "127.0.0.1",
        "--local",
        "--log-level", "none"
    ]

    wrangler = subprocess.Popen(wrangler_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = {}

    try:
        print("[1/6] Waiting for Wrangler Dev server to spin up...")
        if not wait_for_server(BASE_URL, timeout=30):
            print("ERROR: Wrangler dev failed to start within 30s")
            sys.exit(1)
        print("  -> Wrangler Dev is alive and responding on /api/healthz")

        print("[2/6] Executing Device Flow Pairing (Production RFC 8628 shape)...")
        # 1. Device code request
        status, resp_bytes, _ = do_fetch(
            f"{BASE_URL}/api/device/code",
            method="POST",
            data={"client_kind": "host", "name": "net-host-1", "hostname": "nethost1", "requested_scopes": ["host"]}
        )
        assert status == 200, f"Expected 200 from device code, got {status}"
        d_flow = json.loads(resp_bytes)
        user_code = d_flow["user_code"]
        device_code = d_flow["device_code"]
        print(f"  -> Initiated device code. User Code: {user_code}")

        # 2. Admin approval with CSRF
        csrf_token = "csrf_net_test_123"
        admin_cookie = make_admin_cookie(csrf_token)
        status, appr_bytes, _ = do_fetch(
            f"{BASE_URL}/api/device/approve/{user_code}",
            method="POST",
            headers={"Cookie": admin_cookie, "x-csrf-token": csrf_token},
            data={"granted_scopes": ["host"]}
        )
        assert status == 200, f"Expected 200 from approval, got {status}"
        appr_data = json.loads(appr_bytes)
        machine_id = appr_data["machine_id"]
        print(f"  -> Approved pairing. Machine ID: {machine_id}")

        # 3. Device token poll
        status, token_bytes, _ = do_fetch(
            f"{BASE_URL}/api/device/token",
            method="POST",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": "net-host-1"
            }
        )
        assert status == 200, f"Expected 200 from device token, got {status}"
        token_data = json.loads(token_bytes)
        access_token = token_data["access_token"]
        refresh_token = token_data["refresh_token"]
        print("  -> Successfully received access_token and refresh_token")

        # Configure host config
        tmp_config_dir = Path(".local/tmp/net_config/agent-hub")
        tmp_config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp_config_dir, 0o700)
        config_path = tmp_config_dir / "host.json"
        with open(config_path, "w") as f:
            json.dump({
                "base_url": BASE_URL,
                "access_token": access_token,
                "refresh_token": refresh_token
            }, f)
        os.chmod(config_path, 0o600)
        os.environ["XDG_CONFIG_HOME"] = os.path.abspath(".local/tmp/net_config")

        sys.path.insert(0, os.path.abspath("host"))
        from agent_hub_host import api, auth
        from agent_hub_host.ws import WSConnection

        print("[3/6] Testing Channel 1: Real WebSocket Primary Connection...")
        # Machine registers first
        api.register_machine(BASE_URL)
        print("  -> Machine registered via /api/machines/register")

        # Real WS Handshake
        rng = api.RNG()
        sock, rest = api.ws_handshake("127.0.0.1", PORT, "/api/host/connect", access_token, False, rng)
        print("  -> Real WebSocket handshake succeeded (HTTP 101 Switching Protocols)")

        # Send heartbeat over WebSocket
        ws = WSConnection(sock, rest, rng)
        hb_payload = api.get_heartbeat_payload()
        ws.send_frame(1, json.dumps(hb_payload).encode("utf-8"))
        print("  -> Heartbeat sent over real WebSocket frame")

        # Verify availability
        status, avail_bytes, _ = do_fetch(
            f"{BASE_URL}/api/machines/available",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        assert status == 200
        avail_data = json.loads(avail_bytes)
        machines = avail_data.get("machines", [])
        assert any(m["id"] == machine_id for m in machines), f"Machine {machine_id} not available via WS"
        print("  -> Machine verified available in registry via WebSocket heartbeat")

        # Close WebSocket cleanly
        ws.send_frame(8, b"")
        sock.close()
        results["ws_primary"] = "PASS"

        print("[4/6] Testing Channel 2: Real Short HTTP Poll Fallback...")
        # Test HTTP fallback endpoint directly
        status, poll_bytes, _ = do_fetch(
            f"{BASE_URL}/api/host/poll",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            data=hb_payload
        )
        assert status == 200
        poll_data = json.loads(poll_bytes)
        assert "offers" in poll_data, "Poll response missing protocol-compatible 'offers' list"
        print("  -> HTTP Poll fallback succeeded, returned protocol offers: []")
        results["http_fallback"] = "PASS"

        print("[5/6] Testing Admin Drain & Undrain Boundaries...")
        # Drain machine
        status, drain_bytes, _ = do_fetch(
            f"{BASE_URL}/api/machines/{machine_id}/drain",
            method="POST",
            headers={"Cookie": admin_cookie, "x-csrf-token": csrf_token}
        )
        assert status == 200, f"Drain failed with {status}"
        assert json.loads(drain_bytes).get("status") == "drained"

        # Check available (should be excluded)
        status, avail_bytes, _ = do_fetch(
            f"{BASE_URL}/api/machines/available",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        avail_data = json.loads(avail_bytes)
        assert not any(m["id"] == machine_id for m in avail_data.get("machines", [])), "Drained machine still available!"
        print("  -> Drained machine successfully excluded from available list")

        # Undrain machine
        status, undrain_bytes, _ = do_fetch(
            f"{BASE_URL}/api/machines/{machine_id}/undrain",
            method="POST",
            headers={"Cookie": admin_cookie, "x-csrf-token": csrf_token}
        )
        assert status == 200, f"Undrain failed with {status}"
        assert json.loads(undrain_bytes).get("status") == "undrained"

        # Check available (restored)
        status, avail_bytes, _ = do_fetch(
            f"{BASE_URL}/api/machines/available",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        avail_data = json.loads(avail_bytes)
        assert any(m["id"] == machine_id for m in avail_data.get("machines", [])), "Undrained machine not restored!"
        print("  -> Undrained machine successfully restored to available list")
        results["drain_undrain"] = "PASS"

        print("[6/6] Testing Token Rotation and Revocation...")
        # Refresh token rotation
        status, refresh_bytes, _ = do_fetch(
            f"{BASE_URL}/api/token/refresh",
            method="POST",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        assert status == 200, f"Refresh failed: {status}"
        refresh_data = json.loads(refresh_bytes)
        new_access_token = refresh_data["access_token"]
        new_refresh_token = refresh_data["refresh_token"]
        assert new_access_token != access_token
        assert new_refresh_token != refresh_token
        print("  -> Token rotation succeeded, issued new access and refresh tokens")

        # Revoke token
        status, revoke_bytes, _ = do_fetch(
            f"{BASE_URL}/api/token/revoke",
            method="POST",
            headers={"Authorization": f"Bearer {new_access_token}"},
            data={"token": new_access_token}
        )
        assert status == 200, f"Revoke failed: {status}"
        assert json.loads(revoke_bytes).get("status") == "revoked"

        # Verify old token rejected with 401
        try:
            do_fetch(
                f"{BASE_URL}/api/machines/available",
                headers={"Authorization": f"Bearer {new_access_token}"}
            )
            assert False, "Revoked token was accepted!"
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"Expected 401 for revoked token, got {e.code}"
        print("  -> Revoked token correctly rejected with HTTP 401")
        results["token_rotation_revoke"] = "PASS"

        print("\n=== All Real Local Network Integration Tests Completed Successfully! ===")
        with open(".local/network-integration-results.json", "w") as f:
            json.dump({"status": "SUCCESS", "results": results, "port": PORT}, f, indent=2)

    finally:
        print("Cleaning up Wrangler process...")
        try:
            wrangler.terminate()
            wrangler.wait(timeout=10)
        except Exception:
            wrangler.kill()
        print("Cleanup complete.")

if __name__ == "__main__":
    main()
