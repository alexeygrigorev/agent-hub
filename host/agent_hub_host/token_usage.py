"""Agent Hub Host native token usage collector.

Collects REAL per-hour input/output token counts from the engines' native
session data (no quota percentages, no invented values):

- codex family rollout JSONL ``token_usage_record`` entries under the
  ``sessions/`` trees of ``~/.codex`` (account shared-codex), ``~/.codex-orin``
  (account orin) and ``~/.zcodex`` (engine zcodex, account shared-zai);
- Claude project JSONL ``message.usage`` entries under
  ``~/.claude/projects`` (engine claude, account shared-claude);
- the OpenCode session table (engine opencode, account opencode-go).

Engines without a native per-session token log on the host (gemini, grok,
copilot) are NOT collected here; the server marks them explicitly
unsupported per engine (see ``src/token-history.ts`` TOKEN_SOURCES).

Projection: input = native input-side tokens (input + cache read/creation),
output = native output-side tokens (output + reasoning where the engine
reports it separately). Native detail stays in the session logs; only hourly
sums cross into the Hub. Project attribution maps a session cwd under
``/home/<user>/git/<repo>/...`` to the ``<repo>`` basename, else
``unattributed``. No prompts, transcripts, costs, emails or account
identifiers are read or posted. Every failure is isolated; ``step`` never
raises for collection problems.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import api, config

SCHEMA_VERSION = "1.0"
MAX_BODY_BYTES = 32 * 1024
MAX_FILES_PER_SOURCE = 512
MAX_BYTES_PER_FILE = 32 * 1024 * 1024
MAX_LINES_PER_FILE = 100_000
WINDOW_HOURS = 26
POST_TIMEOUT_S = 10.0

_CODEX_SOURCES = (
    ("codex", "shared-codex", ".codex"),
    ("codex", "orin", ".codex-orin"),
    ("zcodex", "shared-zai", ".zcodex"),
)


def utc_hour_ms(ts_ms: int) -> int:
    return (ts_ms // 3600000) * 3600000


def hour_iso(hour_ms: int) -> str:
    return datetime.datetime.fromtimestamp(hour_ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:00:00.000Z")


def project_for_cwd(cwd: Any) -> str:
    """Map a session cwd to a bounded project label, else unattributed."""
    if not isinstance(cwd, str) or not cwd:
        return "unattributed"
    parts = cwd.strip("/").split("/")
    # /home/<user>/git/<repo>/... -> <repo>
    if len(parts) >= 4 and parts[0] == "home" and parts[2] == "git" and parts[3]:
        label = parts[3]
        if len(label) <= 64 and all(c.isalnum() or c in "-_." for c in label):
            return label
    return "unattributed"


def _finite_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0 and value < 1e12:
        return int(value)
    return None


def _parse_ts(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return None
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _iter_jsonl_files(root: Path, since_ms: int) -> List[Path]:
    """Bounded newest-first JSONL file list under root (mtime >= since)."""
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []
    found: List[Tuple[float, Path]] = []
    try:
        for path in root.rglob("*.jsonl"):
            if len(found) >= MAX_FILES_PER_SOURCE * 4:
                break
            try:
                if path.is_file() and path.stat().st_mtime * 1000 >= since_ms:
                    found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except Exception:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:MAX_FILES_PER_SOURCE]]


_SESSION_TOKEN_CACHE = {}


def native_session_tokens(row, locator=None):
    """Return bounded numeric telemetry for one exact native incarnation.

    Reuse the progress adapter's sidecar/cwd/time binding. Never infer identity
    from the newest transcript, and never send transcript paths or contents.
    """
    if row.get('engine') not in ('codex', 'zcodex', 'opencode'):
        return None
    if type(row.get('created_at_ms')) is not int or not isinstance(row.get('id'), str):
        return None
    from .native_progress import NativeTranscriptProgress
    locator = locator or NativeTranscriptProgress()
    try:
        if row['engine'] == 'opencode':
            environment = row.get('env') or {}
            db = Path(environment.get('OPENCODE_DB') or Path(locator.opencode_data) / 'opencode/opencode.db')
            if not db.is_absolute() or not db.is_file():
                return None
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=.05) as conn:
                for table in ('session_v2', 'session'):
                    try:
                        rows = conn.execute(f'''SELECT tokens_input,tokens_output,tokens_cache_read,tokens_cache_write,tokens_reasoning
                            FROM {table} WHERE directory=? AND parent_id IS NULL AND time_created BETWEEN ? AND ? LIMIT 2''',
                            (row.get('cwd') or row.get('workspace'), row['created_at_ms'] - 10000, row['created_at_ms'] + 10000)).fetchall()
                    except sqlite3.Error:
                        continue
                    if len(rows) != 1:
                        return None
                    values = rows[0]
                    if any(_finite_int(v) is None for v in values):
                        return None
                    inp, out, cache_read, cache_write, reasoning = values
                    return {'input_tokens': inp + cache_read + cache_write, 'output_tokens': out + reasoning}
            return None
        path = locator.locate(row)
        if path is None:
            return None
        stat = path.stat()
        key = (row['id'], row['created_at_ms'], stat.st_size, stat.st_mtime_ns)
        if key in _SESSION_TOKEN_CACHE:
            return _SESSION_TOKEN_CACHE[key]
        latest = None
        with path.open('rb') as stream:
            if stat.st_size > 4 * 1024 * 1024:
                stream.seek(-4 * 1024 * 1024, 2)
                stream.readline()
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                payload = event.get('payload') or {}
                if event.get('type') == 'token_usage_record':
                    usage = payload.get('thread_token_usage')
                elif event.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                    usage = (payload.get('info') or {}).get('total_token_usage')
                else:
                    continue
                if not isinstance(usage, dict):
                    continue
                inp, out = _finite_int(usage.get('input_tokens')), _finite_int(usage.get('output_tokens'))
                if inp is not None and out is not None:
                    # Codex cached/reasoning counts are subsets of these totals.
                    latest = {'input_tokens': inp, 'output_tokens': out}
        if len(_SESSION_TOKEN_CACHE) >= 500:
            _SESSION_TOKEN_CACHE.clear()
        _SESSION_TOKEN_CACHE[key] = latest
        return latest
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def collect_codex(home: str, since_ms: int, now_ms: int,
                   engine: str, account: str) -> Dict[Tuple[int, str], List[int]]:
    """Scan one codex-family sessions tree. Returns {(hour_ms, project): [in, out]}.

    Rollout ``token_usage_record`` entries carry cumulative per-thread input
    snapshots (monotonic within a thread) with per-turn outputs, so hourly
    input is the sum of per-thread positive deltas (first record counts
    fully) attributed to each record's own hour. This approximates billed
    totals; compaction restarts are floored at zero, never negative.
    """
    per_thread: Dict[str, Tuple[Optional[str], List[Tuple[int, Dict[str, Any]]]]] = {}
    try:
        sessions = Path(home).expanduser() / "sessions"
        files = _iter_jsonl_files(sessions, since_ms - 7 * 86400000)
    except Exception:
        return {}
    for path in files:
        try:
            if path.stat().st_size > MAX_BYTES_PER_FILE:
                continue
        except OSError:
            continue
        project: Optional[str] = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle):
                    if lineno >= MAX_LINES_PER_FILE:
                        break
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if project is None and entry.get("type") == "session_meta":
                        payload = entry.get("payload")
                        if isinstance(payload, dict):
                            project = project_for_cwd(payload.get("cwd"))
                    if entry.get("type") != "token_usage_record":
                        continue
                    payload = entry.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    usage = payload.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    ts_ms = _parse_ts(entry.get("timestamp"))
                    if ts_ms is None or ts_ms < since_ms or ts_ms > now_ms:
                        continue
                    thread = payload.get("thread_id") or payload.get("session_id") or str(path)
                    if not isinstance(thread, str):
                        thread = str(path)
                    slot = per_thread.get(thread)
                    if slot is None:
                        per_thread[thread] = (project or "unattributed", [(ts_ms, usage)])
                    else:
                        if project is not None and slot[0] == "unattributed":
                            per_thread[thread] = (project, slot[1])
                        slot[1].append((ts_ms, usage))
        except OSError:
            continue
    out: Dict[Tuple[int, str], List[int]] = {}
    for proj, records in per_thread.values():
        records.sort(key=lambda item: item[0])
        prev_in: Optional[int] = None
        for ts_ms, usage in records:
            current_in = (_finite_int(usage.get("input_tokens")) or 0)
            current_in += (_finite_int(usage.get("cached_input_tokens")) or 0)
            current_in += (_finite_int(usage.get("cache_write_input_tokens")) or 0)
            outp = _finite_int(usage.get("output_tokens")) or 0
            if prev_in is None:
                delta = current_in
            else:
                delta = current_in - prev_in
                if delta < 0:
                    delta = 0
            prev_in = current_in
            key = (utc_hour_ms(ts_ms), proj or "unattributed")
            have = out.get(key)
            if have is None:
                out[key] = [delta, outp]
            else:
                have[0] += delta
                have[1] += outp
    return out


def collect_claude(projects_dir: str, since_ms: int, now_ms: int) -> Dict[Tuple[int, str], List[int]]:
    """Scan Claude project JSONL message usage. Same return shape."""
    out: Dict[Tuple[int, str], List[int]] = {}
    try:
        root = Path(projects_dir).expanduser()
        files = _iter_jsonl_files(root, since_ms - 7 * 86400000)
    except Exception:
        return out
    for path in files:
        try:
            if path.stat().st_size > MAX_BYTES_PER_FILE:
                continue
        except OSError:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle):
                    if lineno >= MAX_LINES_PER_FILE:
                        break
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    message = entry.get("message")
                    if not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    ts_ms = _parse_ts(entry.get("timestamp"))
                    if ts_ms is None or ts_ms < since_ms or ts_ms > now_ms:
                        continue
                    inp = (_finite_int(usage.get("input_tokens")) or 0) + \
                        (_finite_int(usage.get("cache_read_input_tokens")) or 0) + \
                        (_finite_int(usage.get("cache_creation_input_tokens")) or 0)
                    outp = (_finite_int(usage.get("output_tokens")) or 0)
                    project = project_for_cwd(entry.get("cwd"))
                    key = (utc_hour_ms(ts_ms), project)
                    slot = out.get(key)
                    if slot is None:
                        out[key] = [inp, outp]
                    else:
                        slot[0] += inp
                        slot[1] += outp
        except OSError:
            continue
    return out


def collect_opencode(db_path: str, since_ms: int, now_ms: int) -> Dict[Tuple[int, str], List[int]]:
    """Read-only scan of the OpenCode session table. Same return shape."""
    out: Dict[Tuple[int, str], List[int]] = {}
    try:
        uri = "file:" + urllib.request.pathname2url(os.path.abspath(os.path.expanduser(db_path))) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except Exception:
        return out
    try:
        try:
            rows = conn.execute(
                "SELECT time_created, directory, tokens_input, tokens_output,"
                " tokens_reasoning, tokens_cache_read, tokens_cache_write"
                " FROM session WHERE time_created >= ? AND time_created <= ? LIMIT 20000",
                (since_ms, now_ms),
            ).fetchall()
        except Exception:
            return out
        for created, directory, ti, to, tr, tcr, tcw in rows:
            if not isinstance(created, (int, float)):
                continue
            ts_ms = int(created)
            if ts_ms < since_ms or ts_ms > now_ms:
                continue
            inp = (_finite_int(ti) or 0) + (_finite_int(tcr) or 0) + (_finite_int(tcw) or 0)
            outp = (_finite_int(to) or 0) + (_finite_int(tr) or 0)
            key = (utc_hour_ms(ts_ms), project_for_cwd(directory))
            slot = out.get(key)
            if slot is None:
                out[key] = [inp, outp]
            else:
                slot[0] += inp
                slot[1] += outp
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def build_hours(now_ms: int) -> List[Dict[str, Any]]:
    """Aggregate native tokens for complete hours in the trailing window."""
    current_hour = utc_hour_ms(now_ms)
    since_ms = now_ms - WINDOW_HOURS * 3600000
    home = os.path.expanduser("~")
    per_key: Dict[Tuple[str, str, str, int], List[int]] = {}

    def merge(engine: str, account: str, buckets: Dict[Tuple[int, str], List[int]]) -> None:
        for (hour_ms, project), (inp, outp) in buckets.items():
            if hour_ms >= current_hour or hour_ms < since_ms:
                continue
            key = (engine, account, project, hour_ms)
            slot = per_key.get(key)
            if slot is None:
                per_key[key] = [inp, outp]
            else:
                slot[0] += inp
                slot[1] += outp

    for engine, account, dirname in _CODEX_SOURCES:
        try:
            merge(engine, account, collect_codex(os.path.join(home, dirname), since_ms, now_ms, engine, account))
        except Exception:
            continue
    try:
        merge("claude", "shared-claude", collect_claude(os.path.join(home, ".claude", "projects"), since_ms, now_ms))
    except Exception:
        pass
    try:
        merge("opencode", "opencode-go",
              collect_opencode(os.path.join(home, ".local", "share", "opencode", "opencode.db"), since_ms, now_ms))
    except Exception:
        pass
    hours = [{
        "hour_start": hour_iso(hour_ms),
        "engine": engine,
        "account": account,
        "project": project,
        "input_tokens": values[0],
        "output_tokens": values[1],
    } for (engine, account, project, hour_ms), values in sorted(per_key.items())]
    return hours


def post_token_hours(base_url: str, hours: List[Dict[str, Any]],
                     token: Optional[str] = None, timeout: float = POST_TIMEOUT_S) -> Dict[str, Any]:
    """POST hourly token aggregates to /api/monitoring/tokens."""
    clean_base, access_token, _ = _resolve_origin_and_token(base_url, token)
    body = json.dumps({"schema_version": SCHEMA_VERSION, "hours": hours}).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("Payload exceeds 32 KiB limit")
    req = urllib.request.Request(
        clean_base + "/api/monitoring/tokens",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + access_token},
    )
    try:
        return api.safe_http_json(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP error %s during token post" % exc.code) from None
    except urllib.error.URLError:
        raise RuntimeError("Network error during token post") from None
    except Exception as exc:
        raise RuntimeError("Token POST error: %s" % type(exc).__name__) from None


def _resolve_origin_and_token(base_url: str, token: Optional[str] = None) -> Tuple[str, str, Dict[str, Any]]:
    api.check_url(base_url)
    target_origin = api.canonical_origin(base_url)
    cfg = config.load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    cfg_base = cfg.get("base_url")
    cfg_tok = cfg.get("access_token")
    using_cfg_token = token is None or (cfg_tok is not None and token == cfg_tok)
    if using_cfg_token:
        if not cfg_base:
            raise ValueError("Missing paired base_url in host config for token access")
        try:
            cfg_origin = api.canonical_origin(cfg_base)
        except Exception:
            raise ValueError("Invalid base_url in host config") from None
        if target_origin != cfg_origin:
            raise ValueError("Target base_url origin does not match paired host config")
    if token is None:
        if not isinstance(cfg_tok, str) or not cfg_tok.strip() or cfg_tok != cfg_tok.strip():
            raise ValueError("Missing access token for token request")
        access_token = cfg_tok
    else:
        if not isinstance(token, str) or not token.strip() or token != token.strip():
            raise ValueError("Missing or invalid access token for token request")
        access_token = token
    return base_url.rstrip("/"), access_token, cfg


class TokenCollector:
    """Hourly native token collection and POST for HostAgent.

    Cadence floor 1800 s (complete hours only; reposts are idempotent
    server-side for unchanged totals). A JSON state file records posted
    totals so genuine late-arriving corrections repost with a newer
    observation instant and actually overwrite.
    """

    def __init__(self, clock: Any = None, base_url: Optional[str] = None,
                 http_client: Optional[Callable[..., Any]] = None,
                 interval: float = 3600.0, state_path: Optional[str] = None) -> None:
        self.clock = clock or api.Clock()
        self.base_url = base_url
        self.http_client = http_client
        self.interval = interval
        self.state_path = state_path
        self.last_collect_monotonic: Optional[float] = None
        self.last_snapshot: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None
        self.last_hold: Optional[str] = None

    def _monotonic(self) -> float:
        try:
            value = self.clock.monotonic()
        except Exception:
            value = time.monotonic()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Invalid monotonic clock reading")
        return float(value)

    def _wall_ms(self) -> int:
        try:
            now = self.clock.now()
            if isinstance(now, datetime.datetime) and now.tzinfo is not None:
                return int(now.timestamp() * 1000)
        except Exception:
            pass
        return int(time.time() * 1000)

    def _load_state(self) -> Dict[str, List[int]]:
        if not self.state_path:
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 2}
        except Exception:
            pass
        return {}

    def _save_state(self, state: Dict[str, List[int]]) -> None:
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def step(self) -> Optional[Dict[str, Any]]:
        now_mono = self._monotonic()
        if self.last_collect_monotonic is not None and now_mono - self.last_collect_monotonic < self.interval:
            return None
        return self.collect_and_post()

    def collect_and_post(self) -> Optional[Dict[str, Any]]:
        now_mono = self._monotonic()
        self.last_collect_monotonic = now_mono
        cfg = config.load_config()
        if not isinstance(cfg, dict):
            self.last_hold = "invalid host config"
            return None
        machine_id = cfg.get("machine_id")
        access_token = cfg.get("access_token")
        cfg_base_url = cfg.get("base_url")
        if (not config.is_valid_machine_id(machine_id) or not isinstance(access_token, str)
                or not access_token.strip() or not isinstance(cfg_base_url, str) or not cfg_base_url.strip()):
            self.last_hold = "missing machine_id, access_token, or base_url in host config"
            return None
        try:
            cfg_origin = api.canonical_origin(cfg_base_url)
        except Exception:
            self.last_hold = "invalid base_url in host config"
            return None
        target = self.base_url if self.base_url is not None else cfg_base_url
        try:
            if api.canonical_origin(target) != cfg_origin:
                self.last_hold = "base_url origin mismatch with paired host config"
                return None
        except Exception:
            self.last_hold = "invalid target base_url"
            return None
        self.last_hold = None
        try:
            hours = build_hours(self._wall_ms())
            if not hours:
                self.last_snapshot = {"hours": 0}
                return self.last_snapshot
            if self.http_client:
                result = self.http_client(target, hours, token=access_token)
            else:
                result = post_token_hours(target, hours, token=access_token)
            self.last_snapshot = {"hours": len(hours)}
            self.last_error = None
            try:
                state = {(h["engine"], h["account"], h["project"], h["hour_start"]): [h["input_tokens"], h["output_tokens"]] for h in hours}
                self._save_state({"|".join(k): v for k, v in state.items()})
            except Exception:
                pass
            return {"posted": result, "hours": len(hours)}
        except Exception as exc:
            self.last_error = "collection failed: %s" % type(exc).__name__
            return None
