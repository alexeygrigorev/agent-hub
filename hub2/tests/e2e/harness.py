"""Real-process harness for the Hub2 start-path e2e test.

Boots the production Node entry (hub2/node/main.ts) on a temp SQLite data
dir, enrolls a machine through the public pairing API, runs the real Hub2
host daemon (hub2/host, `agent_hub_host.cli serve`) with the stub `a` and
`codex` binaries first on PATH, and drives the real `hub` CLI zipapp built
from host/ by scripts/build-hub-cli.py. Nothing here talks to production,
a provider, or the real aplexer.
"""
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
E2E = Path(__file__).resolve().parent
STUB_BIN = E2E / "bin"
OPERATOR_TOKEN = "e2e-operator-token-" + "0" * 32
PROJECT = "e2e-owner/e2e-repo"
ACCOUNT = "e2e-codex"
MODEL = "gpt-e2e-stub"
MACHINE = "e2e-host"


def scratch_root():
    """Temp dirs live under <repo>/.local/tmp (never /tmp) unless TMPDIR says otherwise."""
    base = Path(os.environ.get("HUB2_E2E_TMP") or REPO / ".local" / "tmp")
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="hub2-e2e-", dir=base))


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, timeout, interval=0.25, label="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError("timed out after %.0fs waiting for %s (last=%r)" % (timeout, label, last))


class HttpResult:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __repr__(self):
        return "HTTP %s %s" % (self.status, json.dumps(self.body)[:400])


class Server:
    def __init__(self, root):
        self.root = root
        self.data_dir = root / "hub2-db"
        self.data_dir.mkdir()
        self.port = free_port()
        self.origin = "http://127.0.0.1:%d" % self.port
        self.log_path = root / "server.log"
        self.process = None

    def start(self):
        env = dict(os.environ, HUB2_DEV_OPERATOR_TOKEN=OPERATOR_TOKEN, HUB2_CREDENTIAL_KEY="ab" * 32,
                   HUB2_RELEASE_MODE="active", HUB2_DEFAULT_ACCOUNT_ID=ACCOUNT)
        log = open(self.log_path, "ab")
        self.process = subprocess.Popen(
            ["node", "--experimental-transform-types", str(REPO / "hub2/node/main.ts"),
             "--host", "127.0.0.1", "--port", str(self.port), "--data-dir", str(self.data_dir), "--dev-vars", "none"],
            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        wait_for(lambda: self.alive() and self.health(), 60, label="hub2 server /health")

    def alive(self):
        if self.process.poll() is not None:
            raise AssertionError("hub2 server exited with %s; log:\n%s" % (self.process.returncode, self.tail()))
        return True

    def health(self):
        try:
            with urllib.request.urlopen(self.origin + "/health", timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()

    def restart(self):
        self.stop()
        self.start()

    def tail(self, lines=60):
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def api(self, method, path, body=None, token=OPERATOR_TOKEN):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.origin + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw[:400].decode(errors="replace")}
        return HttpResult(status, parsed)

    def ok(self, method, path, body=None, token=OPERATOR_TOKEN, expect=(200, 201)):
        result = self.api(method, path, body, token)
        assert result.status in expect, "%s %s -> %r" % (method, path, result)
        return result.body


def synthetic_codex_auth():
    """A structurally valid, provider-unusable Codex auth.json (fake JWT, far expiry)."""
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 20 * 86400}).encode()).decode().rstrip("=")
    return json.dumps({"tokens": {"access_token": "e2e." + claims + ".signature", "refresh_token": "e2e-synthetic-refresh",
                                  "account_id": "e2e-account"},
                       "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


def configure_hub(server):
    """Operator setup a fresh Hub needs before a model-less start: repo, account, policy, machine."""
    server.ok("PATCH", "/api/repos/" + PROJECT.replace("/", "%2F"), {
        "canonical_url": "https://github.com/" + PROJECT, "purpose": "Hub2 e2e start-path fixture",
        "status": "active", "aliases": [], "domains": []})
    account = "/api/accounts/" + ACCOUNT + "/api/account"
    server.ok("PATCH", account, {"provider": "codex", "enabled": True, "min_remaining": 0})
    server.ok("PUT", account + "/credentials", {"expected_generation": 0, "files": {".codex/auth.json": synthetic_codex_auth()}})
    now = int(time.time() * 1000)
    server.ok("PUT", account + "/quota", {"remaining": 80, "captured_at": now, "valid_until": now + 3_000_000,
                                          "source": "hub2-e2e-synthetic-quota"})
    server.ok("PUT", account + "/hosts", {"hosts": [MACHINE]})
    server.ok("PUT", "/api/model-policy", {"expected_version": 0, "candidates": [
        {"id": "e2e-codex", "engine": "codex", "model": MODEL, "account_id": ACCOUNT, "weight": 1,
         "capabilities": [], "enabled": True}]})
    pair = server.ok("POST", "/api/enrollments/pair", {"machine_id": MACHINE, "alias": MACHINE})
    enrolled = server.ok("POST", "/api/enrollments/complete", {"code": pair["pairing_code"]}, token=None)
    return enrolled["token"]


class Host:
    """The real Hub2 host daemon (hub2/host) with stub `a`/`codex` first on PATH."""

    def __init__(self, root, server, token):
        self.root, self.server = root, server
        self.workspace_root = root / "workspaces"
        self.workspace = self.workspace_root / "e2e-repo"
        self.workspace.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "remote", "add", "origin",
                        "https://github.com/" + PROJECT + ".git"], check=True)
        self.state = root / "aplexer-state"
        self.state.mkdir()
        self.config_dir = root / "host-config"
        self.config = self.config_dir / "v2.json"
        self.config_dir.mkdir(mode=0o700)
        self.config.write_text(json.dumps({
            "server": server.origin, "token": token, "machine_id": MACHINE,
            "roots": [str(self.workspace_root)], "engines": ["codex"],
            "launcher": {"routes": {"codex": {"provider": "codex"}}, "aplexer_state_dir": str(self.state)}}))
        self.config.chmod(0o600)
        self.log_path = root / "host.log"
        self.process = None
        self.source = REPO / "hub2/host"

    def env(self):
        env = dict(os.environ, PATH=str(STUB_BIN) + os.pathsep + os.environ.get("PATH", ""),
                   PYTHONPATH=os.pathsep.join(filter(None, [str(self.source), os.environ.get("PYTHONPATH")])), APLEXER_STATE_DIR=str(self.state), PYTHONUNBUFFERED="1")
        for name in ("HUB_TOKEN", "HUB_CONFIG_FILE", "HUB2_TOKEN", "HUB_URL"):
            env.pop(name, None)
        return env

    def start(self):
        log = open(self.log_path, "ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "agent_hub_host.cli", "--config", str(self.config), "serve", "--label", "e2e"],
            cwd=self.root, env=self.env(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def freeze(self):
        """SIGSTOP the daemon: its socket stays open but nothing is received or claimed."""
        os.killpg(self.process.pid, signal.SIGSTOP)

    def kill(self):
        if self.alive():
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()

    def control(self, **settings):
        (self.state / "stub-control.json").write_text(json.dumps(settings))

    def native_rows(self):
        out = subprocess.run([str(STUB_BIN / "a"), "list", "--json"], env=self.env(), capture_output=True, text=True, timeout=10)
        return json.loads(out.stdout or "[]")

    def kill_all_native(self):
        for row in self.native_rows():
            if row.get("phase") == "running":
                subprocess.run([str(STUB_BIN / "a"), "kill", row["id"]], env=self.env(), capture_output=True, timeout=15)

    def tail(self, lines=60):
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""


def stale_heartbeat_host_copy(root):
    """Copy of hub2/host whose Client.heartbeat has the stale `resources=` signature of the
    tracked build/lib copy (RC2), for the harness mutation test. Never touches the source tree."""
    target = root / "stale-host"
    shutil.copytree(REPO / "hub2/host/agent_hub_host", target / "agent_hub_host",
                    ignore=shutil.ignore_patterns("__pycache__"))
    client = target / "agent_hub_host" / "client.py"
    text = client.read_text()
    mutated = text.replace("capabilities=None, native_sessions=None):", "capabilities=None, resources=None):")
    mutated = mutated.replace('if native_sessions is not None:\n            body["native_sessions"] = native_sessions',
                              'if resources is not None:\n            body["resources"] = resources')
    assert mutated != text and "native_sessions" not in mutated, "heartbeat signature changed; update the mutation"
    client.write_text(mutated)
    return target


def build_cli(out):
    """Build the real `hub` zipapp from host/ exactly as scripts/build-hub-cli.py publishes it."""
    subprocess.run([sys.executable, str(REPO / "scripts/build-hub-cli.py"), "--out-dir", str(out)],
                   check=True, capture_output=True, timeout=180)
    return out / "hub.pyz"


class Cli:
    """Runs the real `hub` CLI zipapp against one Server with an isolated HOME."""

    def __init__(self, zipapp, root, server):
        self.zipapp, self.server = zipapp, server
        self.home = root / "cli-home"
        (self.home / ".config").mkdir(parents=True)

    def run(self, *args, timeout=120):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("HUB", "AGENT_HUB"))}
        env.update(HUB_URL=self.server.origin, HUB_TOKEN=OPERATOR_TOKEN, HOME=str(self.home),
                   XDG_CONFIG_HOME=str(self.home / ".config"), HUB_CONFIG_FILE=str(self.home / ".config/agent-hub/cli.json"))
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(self.zipapp), *args], env=env, capture_output=True,
                                text=True, timeout=timeout)
        result.elapsed = time.monotonic() - started
        return result

    def json(self, *args, timeout=120):
        result = self.run(*args, timeout=timeout)
        assert result.returncode == 0, "hub %s failed rc=%s\nstdout=%s\nstderr=%s" % (
            " ".join(args), result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        return json.loads(result.stdout)


def new_key():
    return "e2e-" + uuid.uuid4().hex[:16]
