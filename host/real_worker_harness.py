"""Reusable startup harness and process manager for real Cloudflare Worker / Miniflare instances.

Provides:
- Bounded monotonic readiness polling
- Fail-fast child termination detection
- Actionable bounded log tail on failure/exit
- Prevention of PIPE deadlock via file-backed logging
- Verified cleanup of owned child process and process group (SIGTERM -> SIGKILL)
- Bounded infrastructure-only pre-seed startup retry with explicit process/HTTP evidence
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional
import urllib.error
import urllib.request


class WorkerStartupError(RuntimeError):
    """Raised when the worker fails to start or become ready within bounds."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: Optional[int] = None,
        log_tail: str = "",
        attempts: int = 1,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.log_tail = log_tail
        self.attempts = attempts
        self.diagnostics = diagnostics or {}


def get_available_cpus(count: int = 2) -> str:
    """Return a comma-separated CPU list for taskset, respecting process affinity."""
    try:
        if hasattr(os, "sched_getaffinity"):
            aff = sorted(os.sched_getaffinity(0))
            if aff:
                return ",".join(str(c) for c in aff[:count])
    except Exception:
        pass
    return "0,1"


def find_available_port(host: str = "127.0.0.1") -> int:
    """Find an available TCP port on the specified host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return s.getsockname()[1]


def resolve_node_binary(
    candidates: Optional[List[str]] = None,
    *,
    include_defaults: bool = True,
) -> str:
    """Find a node binary satisfying the Wrangler requirement (Node >= 22)."""
    search_paths = []
    if candidates:
        search_paths.extend(candidates)

    if include_defaults:
        which_node = shutil.which("node")
        if which_node:
            search_paths.append(which_node)

        fallback_candidates = [
            "/home/alexey/.nvm/versions/node/v24.13.1/bin/node",
            "/home/alexey/.nvm/versions/node/v24.15.0/bin/node",
            os.path.expanduser("~/.nvm/versions/node/v24.13.1/bin/node"),
            os.path.expanduser("~/.nvm/versions/node/v24.15.0/bin/node"),
        ]
        search_paths.extend(fallback_candidates)

    checked = []
    # Only accept a binary with major version >= 22
    for candidate in search_paths:
        if candidate in checked or not os.path.exists(candidate):
            continue
        checked.append(candidate)
        try:
            out = subprocess.check_output([candidate, "-v"], text=True, timeout=5).strip()
            # out looks like 'v24.13.1'
            if out.startswith("v"):
                major = int(out[1:].split(".")[0])
                if major >= 22:
                    return candidate
        except Exception:
            continue

    raise RuntimeError(
        f"Compatible node binary (Node >= 22) not found (Wrangler requires Node >= 22); checked: {checked}"
    )


def read_file_tail(file_path: str, max_lines: int = 80, max_bytes: int = 16384) -> str:
    """Safely read the last bounded portion of a file."""
    if not os.path.exists(file_path):
        return ""
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            seek_bytes = min(size, max_bytes)
            f.seek(-seek_bytes, os.SEEK_END)
            data = f.read(seek_bytes)
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception as exc:
        return f"<Failed to read log tail from {file_path}: {exc}>"


class WorkerProcessHandle:
    """Manages an active worker process group and its logging lifecycle."""

    def __init__(
        self,
        proc: subprocess.Popen,
        log_path: str,
        port: int,
        health_url: str,
        *,
        log_file_handle: Optional[Any] = None,
    ) -> None:
        self.proc = proc
        self.log_path = log_path
        self.port = port
        self.health_url = health_url
        self.log_file_handle = log_file_handle
        self.start_monotonic = time.monotonic()
        try:
            self.pgid = os.getpgid(proc.pid)
        except Exception:
            self.pgid = proc.pid

    def poll(self) -> Optional[int]:
        """Poll the process exit status."""
        return self.proc.poll()

    def is_owned_pgid(self) -> bool:
        """Verify pgid is an isolated owned process group, never inherited or foreign."""
        try:
            own_pgrp = os.getpgrp()
            return self.pgid > 1 and self.pgid != own_pgrp and self.pgid == self.proc.pid
        except Exception:
            return False

    def group_is_alive(self) -> bool:
        """Check if either the direct child process or any process in its owned group is alive."""
        if self.proc.poll() is None:
            return True
        if self.is_owned_pgid():
            try:
                os.killpg(self.pgid, 0)
                return True
            except ProcessLookupError:
                return False
            except Exception:
                return False
        return False

    def is_alive(self) -> bool:
        """Return True if the process or its owned group is currently running."""
        return self.group_is_alive()

    def tail_log(self, max_lines: int = 80, max_bytes: int = 16384) -> str:
        """Return the recent bounded log tail."""
        if self.log_file_handle:
            try:
                self.log_file_handle.flush()
            except Exception:
                pass
        return read_file_tail(self.log_path, max_lines=max_lines, max_bytes=max_bytes)

    def cleanup(self, sigterm_timeout: float = 3.0, sigkill_timeout: float = 2.0) -> None:
        """Verified, deterministic cleanup of process and owned process group.

        Fails explicitly if cleanup cannot be verified. Never signals foreign or inherited process groups.
        """
        is_owned = self.is_owned_pgid()

        # 1. Graceful SIGTERM if child or any process in owned group is alive
        if self.group_is_alive():
            if is_owned:
                try:
                    os.killpg(self.pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except ProcessLookupError:
                    pass

            deadline = time.monotonic() + sigterm_timeout
            while time.monotonic() < deadline and self.group_is_alive():
                if self.proc.poll() is None:
                    try:
                        self.proc.wait(timeout=0.05)
                    except Exception:
                        pass
                else:
                    time.sleep(0.05)

        # 2. Escalate to SIGKILL if still alive
        if self.group_is_alive():
            if is_owned:
                try:
                    os.killpg(self.pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if self.proc.poll() is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass

            kill_deadline = time.monotonic() + sigkill_timeout
            while time.monotonic() < kill_deadline and self.group_is_alive():
                if self.proc.poll() is None:
                    try:
                        self.proc.wait(timeout=0.05)
                    except Exception:
                        pass
                else:
                    time.sleep(0.05)

        # 3. Close open log handle if present
        if self.log_file_handle:
            try:
                self.log_file_handle.close()
            except Exception:
                pass
            self.log_file_handle = None

        # 4. Verified exit assertion: fail explicitly if cleanup cannot be verified
        if self.group_is_alive():
            raise RuntimeError(
                f"Failed to verify cleanup of worker process {self.proc.pid} "
                f"(owned process group {self.pgid}). Processes remain alive."
            )


class RealWorkerHarness:
    """Harness coordinating build, startup, readiness, and bounded infrastructure retry."""

    def __init__(
        self,
        repo_root: str,
        server_dir: str,
        tmp_root: str,
        *,
        readiness_timeout: float = 30.0,
        poll_interval: float = 0.25,
        http_timeout: float = 1.0,
        max_startup_attempts: int = 2,
    ) -> None:
        self.repo_root = os.path.abspath(repo_root)
        self.server_dir = os.path.abspath(server_dir)
        self.tmp_root = os.path.abspath(tmp_root)
        self.readiness_timeout = readiness_timeout
        self.poll_interval = poll_interval
        self.http_timeout = http_timeout
        self.max_startup_attempts = max_startup_attempts
        self.retry_history: List[Dict[str, Any]] = []

    def build_worker(
        self,
        config_path: str,
        out_dir: str,
        *,
        env: Optional[Dict[str, str]] = None,
        cpu_str: Optional[str] = None,
        node_bin: Optional[str] = None,
        timeout: float = 60.0,
    ) -> Path:
        """Run wrangler deploy --dry-run and verify bundle generation."""
        node_bin = node_bin or resolve_node_binary()
        cpu_str = cpu_str or get_available_cpus(2)
        wrangler_js = os.path.join(self.repo_root, "node_modules", "wrangler", "bin", "wrangler.js")
        if not os.path.exists(wrangler_js):
            raise RuntimeError(f"wrangler.js not found at {wrangler_js}")

        build_env = os.environ.copy()
        if env:
            build_env.update(env)
        node_dir = str(Path(node_bin).parent)
        current_path = build_env.get("PATH", "")
        if node_dir not in current_path.split(os.pathsep):
            build_env["PATH"] = f"{node_dir}:{current_path}"

        build = subprocess.run(
            ["taskset", "-c", cpu_str, node_bin, wrangler_js, "deploy", "--dry-run",
             "--config", config_path, "--outdir", out_dir],
            cwd=self.server_dir,
            env=build_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if build.returncode != 0:
            raise RuntimeError(f"Wrangler dry-run build failed: {build.stderr[-2000:]}")

        bundles = list(Path(out_dir).glob("*.js"))
        if len(bundles) != 1:
            raise RuntimeError(f"Expected 1 bundle in {out_dir}, found: {bundles}")
        return bundles[0]

    def launch_command(
        self,
        cmd: List[str],
        port: int,
        health_path: str = "/healthz",
        *,
        env: Optional[Dict[str, str]] = None,
        log_name: str = "worker.log",
        cwd: Optional[str] = None,
    ) -> WorkerProcessHandle:
        """Launch a process in an isolated process group with file-backed logging."""
        work_dir = cwd or self.server_dir
        log_file_path = os.path.join(work_dir, log_name)
        log_file = open(log_file_path, "wb")

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            env=run_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        health_url = f"http://127.0.0.1:{port}{health_path}"
        return WorkerProcessHandle(
            proc=proc,
            log_path=log_file_path,
            port=port,
            health_url=health_url,
            log_file_handle=log_file,
        )

    def wait_for_ready(
        self,
        handle: WorkerProcessHandle,
        *,
        timeout: Optional[float] = None,
        accepted_statuses: tuple = (200, 401),
        custom_probe: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Monotonic readiness polling with fail-fast child exit detection."""
        time_limit = timeout if timeout is not None else self.readiness_timeout
        deadline = time.monotonic() + time_limit

        while time.monotonic() < deadline:
            # 1. Fail-fast check: did child exit unexpectedly?
            code = handle.poll()
            if code is not None:
                tail = handle.tail_log(max_lines=80, max_bytes=16384)
                raise WorkerStartupError(
                    f"Worker process {handle.proc.pid} exited prematurely with exit code {code}. Log tail:\n{tail}",
                    exit_code=code,
                    log_tail=tail,
                )

            # 2. Check custom probe if provided
            if custom_probe is not None:
                try:
                    if custom_probe():
                        return True
                except Exception:
                    pass

            # 3. HTTP readiness probe
            try:
                req = urllib.request.Request(handle.health_url)
                with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                    if resp.status in accepted_statuses:
                        return True
            except urllib.error.HTTPError as err:
                if err.code in accepted_statuses:
                    return True
            except (urllib.error.URLError, ConnectionRefusedError, http.client.RemoteDisconnected, TimeoutError, OSError):
                # Process still starting up
                pass

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.poll_interval, max(0.01, remaining)))

        # Final exit check before timing out
        code = handle.poll()
        tail = handle.tail_log(max_lines=80, max_bytes=16384)
        if code is not None:
            raise WorkerStartupError(
                f"Worker process {handle.proc.pid} exited with exit code {code} before becoming ready. Log tail:\n{tail}",
                exit_code=code,
                log_tail=tail,
            )
        raise WorkerStartupError(
            f"Worker process {handle.proc.pid} failed to become ready within {time_limit}s on {handle.health_url}. Log tail:\n{tail}",
            exit_code=None,
            log_tail=tail,
        )

    def start_with_retry(
        self,
        launcher_fn: Callable[[int, str], WorkerProcessHandle],
        *,
        max_attempts: Optional[int] = None,
        custom_probe: Optional[Callable[[], bool]] = None,
    ) -> WorkerProcessHandle:
        """Start worker with bounded infrastructure-only pre-seed startup retry."""
        attempts = max_attempts if max_attempts is not None else self.max_startup_attempts
        last_error: Optional[WorkerStartupError] = None

        for attempt in range(1, attempts + 1):
            port = find_available_port()
            log_name = f"worker-attempt-{attempt}.log"
            handle: Optional[WorkerProcessHandle] = None
            try:
                handle = launcher_fn(port, log_name)
                self.wait_for_ready(handle, custom_probe=custom_probe)
                return handle
            except WorkerStartupError as exc:
                last_error = exc
                if handle:
                    handle.cleanup()
                evidence = {
                    "attempt": attempt,
                    "port": port,
                    "exit_code": exc.exit_code,
                    "error": str(exc).splitlines()[0],
                    "log_tail": exc.log_tail,
                    "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                self.retry_history.append(evidence)
                if attempt < attempts:
                    # Bounded infrastructure backoff before retry attempt
                    time.sleep(0.5)
                    continue
                else:
                    raise WorkerStartupError(
                        f"Worker failed to become ready after {attempts} attempts. Last error: {exc}",
                        exit_code=exc.exit_code,
                        log_tail=exc.log_tail,
                        attempts=attempts,
                        diagnostics={"retry_history": self.retry_history},
                    ) from exc
            except Exception as exc:
                if handle:
                    handle.cleanup()
                tail = handle.tail_log() if handle else ""
                evidence = {
                    "attempt": attempt,
                    "port": port,
                    "exit_code": handle.poll() if handle else None,
                    "error": str(exc),
                    "log_tail": tail,
                    "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                self.retry_history.append(evidence)
                if attempt < attempts:
                    time.sleep(0.5)
                    continue
                else:
                    raise WorkerStartupError(
                        f"Worker failed with unexpected error after {attempts} attempts: {exc}",
                        exit_code=handle.poll() if handle else None,
                        log_tail=tail,
                        attempts=attempts,
                        diagnostics={"retry_history": self.retry_history},
                    ) from exc

        assert last_error is not None
        raise last_error


def build_fixture_worker_ts(repo_root: str) -> str:
    """Generate fixture worker TypeScript code that overrides Hub fetch with __fixture__/seed_sql."""
    return f"""import worker, {{ Hub as BaseHub }} from "{repo_root}/src/worker";
export class FixtureHub extends BaseHub {{
  override async fetch(request: Request): Promise<Response> {{
    const url = new URL(request.url);
    if (url.pathname === "/__fixture__/seed_sql" && request.method === "POST") {{
      const data = await request.json() as {{ sql: string, params?: any[] }};
      try {{
        const rows = this.sql.exec(data.sql, ...(data.params || [])).toArray();
        return Response.json({{ ok: true, rows }});
      }} catch (err: any) {{
        return Response.json({{ error: String(err?.message || err) }}, {{ status: 400 }});
      }}
    }}
    return super.fetch(request);
  }}
}}
export {{ FixtureHub as Hub }};
export default {{
  async fetch(request: Request, env: any, ctx: any) {{
    const url = new URL(request.url);
    if (url.pathname === "/__fixture__/seed_sql") {{
      const id = env.HUB.idFromName("hub");
      const stub = env.HUB.get(id);
      return stub.fetch(request);
    }}
    return worker.fetch(request, env, ctx);
  }}
}};
"""


def build_wrangler_config(
    port: int,
    main_ts: str = "fixture_worker.ts",
    vars_dict: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Generate wrangler.jsonc configuration for the fixture worker."""
    vars_config = vars_dict or {
        "AUTH_CLIENT_ID": "fake_client_id",
        "AUTH_ISSUER": "https://auth.fake.local",
        "AUTH_BASE_URL": "https://auth.fake.local",
        "AUTH_CALLBACK_URL": f"http://127.0.0.1:{port}/auth/callback",
        "AUTH_JWKS_URL": "https://auth.fake.local/.well-known/jwks.json",
        "SESSION_SECRET": "super_secret_session_key_32_chars_long_for_tests",
    }
    return {
        "name": "agent-hub-fixture",
        "main": main_ts,
        "compatibility_date": "2024-04-05",
        "compatibility_flags": ["nodejs_compat"],
        "durable_objects": {"bindings": [{"name": "HUB", "class_name": "Hub"}]},
        "migrations": [{"tag": "v1", "new_sqlite_classes": ["Hub"]}],
        "vars": vars_config,
    }


def build_miniflare_script(
    repo_root: str,
    server_dir: str,
    bundle_path: str,
    port: int,
    vars_dict: Optional[Dict[str, str]] = None,
) -> str:
    """Generate actual-miniflare.cjs script using convertV4MiniflareOptions."""
    vars_config = vars_dict or {
        "AUTH_CLIENT_ID": "fake_client_id",
        "AUTH_ISSUER": "https://auth.fake.local",
        "AUTH_BASE_URL": "https://auth.fake.local",
        "AUTH_CALLBACK_URL": f"http://127.0.0.1:{port}/auth/callback",
        "AUTH_JWKS_URL": "https://auth.fake.local/.well-known/jwks.json",
        "SESSION_SECRET": "super_secret_session_key_32_chars_long_for_tests",
    }
    worker_opts = {
        "name": "agent-hub-fixture",
        "scriptPath": str(bundle_path),
        "modules": True,
        "compatibilityDate": "2024-04-05",
        "compatibilityFlags": ["nodejs_compat"],
        "bindings": vars_config,
        "durableObjects": {"HUB": {"className": "Hub", "useSQLite": True}},
        "assets": {
            "directory": os.path.join(repo_root, "public"),
            "binding": "ASSETS",
            "run_worker_first": True,
            "routerConfig": {"has_user_worker": True},
        },
    }
    mf_options = {
        "host": "127.0.0.1",
        "port": port,
        "durableObjectsPersist": os.path.join(server_dir, "state"),
        "workers": [worker_opts],
    }
    miniflare_pkg = json.dumps(os.path.join(repo_root, "node_modules", "miniflare"))
    opts_json = json.dumps(mf_options)
    return (
        f"const {{Miniflare,convertV4MiniflareOptions}}=require({miniflare_pkg}); "
        f"const opts={opts_json}; "
        f"const mf=new Miniflare(convertV4MiniflareOptions(opts)); "
        f"mf.ready.then(async u=>{{console.log('ACTUAL_WORKER_READY',String(u));}}).catch(e=>{{console.error(e);process.exit(1);}}); "
        f"process.on('SIGTERM', async()=>{{await mf.dispose();process.exit(0);}});"
    )

