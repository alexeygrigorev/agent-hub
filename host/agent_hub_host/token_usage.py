"""Agent Hub Host native token usage collector.

Collects REAL per-hour input/output token counts from the engines' native
session data (no quota percentages, no invented values):

- codex family rollout JSONL ``token_usage_record`` entries under the
  ``sessions/`` trees of ``~/.codex`` (account shared-codex), ``~/.codex-orin``
  (account orin) and ``~/.zcodex`` (engine zcodex, account shared-zai);
- Claude project JSONL ``message.usage`` entries under
  ``~/.claude/projects`` (engine claude, account shared-claude);
- completed OpenCode assistant messages (engine opencode, account shared-go).

Engines without a native per-session token log on the host (gemini, grok,
copilot) are NOT collected here; the server marks them explicitly
unsupported per engine (see ``src/token-history.ts`` TOKEN_SOURCES).

Projection: Codex uses native input/output totals, whose cached/reasoning
fields are subsets. Claude/OpenCode add their separately reported cache
and reasoning categories. Native detail stays in the session logs; only hourly
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
import re
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
MAX_SCAN_BYTES = 128 * 1024 * 1024
WINDOW_HOURS = 26
POST_TIMEOUT_S = 10.0

_CODEX_SOURCES = (
    ("codex", "shared-codex", ".codex"),
    ("codex", "codex-orin", ".codex-orin"),
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
    # Git worktrees carry the canonical repository in their common-dir link.
    # Read only Git path metadata, never a config or remote containing secrets.
    path = Path(cwd)
    for parent in (path, *list(path.parents)[:12]):
        marker = parent / '.git'
        try:
            if marker.is_file():
                pointer = marker.read_text()[:4096].strip()
                if pointer.startswith('gitdir: '):
                    gitdir = (parent / pointer[8:]).resolve()
                    common = (gitdir / 'commondir').read_text()[:4096].strip()
                    canonical = (gitdir / common).resolve()
                    if canonical.name == '.git':
                        cwd = str(canonical.parent)
                        break
        except OSError:
            continue
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


def _source_files(roots, since_ms):
    """One bounded newest-first budget shared by classic and managed homes."""
    candidates = {}
    for root in roots:
        for path in _iter_jsonl_files(Path(root), since_ms):
            try:
                stat = path.stat()
                if stat.st_size <= MAX_BYTES_PER_FILE:
                    candidates[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size, path)
            except OSError:
                continue
    total, files = 0, []
    for _, size, path in sorted(candidates.values(), key=lambda row:row[0], reverse=True):
        if len(files) >= MAX_FILES_PER_SOURCE or total + size > MAX_SCAN_BYTES:
            break
        files.append(path)
        total += size
    return files


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
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return None


def collect_codex(home: str, since_ms: int, now_ms: int,
                   engine: str, account: str, roots=None) -> Dict[Tuple[int, str], List[int]]:
    """Read native cumulative totals, or per-response usage on older rollouts.

    Codex input/output totals already include cached/reasoning subsets. Both
    native event formats can report the same cumulative total; positive deltas
    against a thread high-water mark count that work only once. Records before
    the requested window establish the baseline, never a new in-window charge.
    """
    per_thread = {}
    try:
        files = _source_files([Path(root).expanduser() / "sessions" for root in (roots or [home])], since_ms - 7 * 86400000)
    except Exception:
        return {}
    for path in files:
        try:
            if path.stat().st_size > MAX_BYTES_PER_FILE:
                continue
            project, session_id = "unattributed", str(path)
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle):
                    if lineno >= MAX_LINES_PER_FILE:
                        break
                    try:
                        entry = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    payload = entry.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if entry.get("type") == "session_meta":
                        project = project_for_cwd(payload.get("cwd"))
                        if isinstance(payload.get("id"), str) and payload["id"]:
                            session_id = payload["id"]
                        continue
                    cumulative, response_id = True, None
                    if entry.get("type") == "token_usage_record":
                        usage = payload.get("thread_token_usage")
                        if not isinstance(usage, dict):
                            usage, cumulative = payload.get("usage"), False
                            response_id = payload.get("response_id")
                    elif entry.get("type") == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info")
                        usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    else:
                        continue
                    if not isinstance(usage, dict):
                        continue
                    inp, outp = _finite_int(usage.get("input_tokens")), _finite_int(usage.get("output_tokens"))
                    ts_ms = _parse_ts(entry.get("timestamp"))
                    if inp is None or outp is None or ts_ms is None or ts_ms > now_ms:
                        continue
                    thread = payload.get("thread_id") or payload.get("session_id") or session_id
                    if not isinstance(thread, str):
                        thread = session_id
                    per_thread.setdefault(thread, []).append((ts_ms, project, inp, outp, cumulative, response_id))
        except OSError:
            continue
    out: Dict[Tuple[int, str], List[int]] = {}
    for records in per_thread.values():
        records.sort(key=lambda item: item[0])
        previous = None
        seen_responses = set()
        # Prefer the native cumulative stream when present, so an older
        # per-response duplicate cannot be added to the same thread totals.
        has_totals = any(r[4] for r in records)
        for ts_ms, project, inp, outp, cumulative, response_id in records:
            if has_totals and not cumulative:
                continue
            if cumulative:
                first = previous is None
                delta_in, delta_out = (inp, outp) if first else (max(0, inp - previous[0]), max(0, outp - previous[1]))
                previous = [inp, outp] if first else [max(previous[0], inp), max(previous[1], outp)]
                if not first and delta_in == 0 and delta_out == 0:
                    continue
            else:
                if isinstance(response_id, str) and response_id:
                    if response_id in seen_responses:
                        continue
                    seen_responses.add(response_id)
                delta_in, delta_out = inp, outp
            if ts_ms < since_ms:
                continue
            key = (utc_hour_ms(ts_ms), project)
            values = out.setdefault(key, [0, 0])
            values[0] += delta_in
            values[1] += delta_out
    return out


def collect_claude(projects_dir: str, since_ms: int, now_ms: int, roots=None) -> Dict[Tuple[int, str], List[int]]:
    """Scan Claude project JSONL message usage. Same return shape."""
    out: Dict[Tuple[int, str], List[int]] = {}
    messages = {}
    try:
        root = Path(projects_dir).expanduser()
        files = _source_files(roots or [root], since_ms - 7 * 86400000)
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
                    if ts_ms is None or ts_ms > now_ms:
                        continue
                    inp, outp = _finite_int(usage.get("input_tokens")), _finite_int(usage.get("output_tokens"))
                    if inp is None or outp is None:
                        continue
                    cache = [_finite_int(usage.get(k, 0)) for k in
                             ("cache_read_input_tokens", "cache_creation_input_tokens")]
                    if any(v is None for v in cache):
                        continue
                    inp += sum(cache)
                    project = project_for_cwd(entry.get("cwd"))
                    # Streaming snapshots/content blocks reuse the native message
                    # ID; retain its latest usage once across files/subagents.
                    mid = message.get("id")
                    identity = mid if isinstance(mid, str) and mid else (str(path), lineno)
                    old = messages.get(identity)
                    if old is None:
                        messages[identity] = (ts_ms, ts_ms, project, inp, outp)
                    else:
                        first = min(ts_ms, old[0])
                        messages[identity] = ((first, ts_ms, project, inp, outp)
                                              if ts_ms >= old[1] else (first, *old[1:]))
        except OSError:
            continue
    for ts_ms, _, project, inp, outp in messages.values():
        # A message's original native timestamp fixes its hour. Later stream
        # snapshots correct that hour rather than charging the same request twice.
        if ts_ms < since_ms:
            continue
        slot = out.setdefault((utc_hour_ms(ts_ms), project), [0, 0])
        slot[0] += inp
        slot[1] += outp
    return out


def _opencode_messages(db_path, since_ms, now_ms, messages):
    """Merge only valid completed usage; newest native update wins copies."""
    try:
        uri = "file:" + urllib.request.pathname2url(os.path.abspath(os.path.expanduser(db_path))) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except Exception:
        return
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        message_tables = [t for t in ("message", "session_message") if t in tables]
        if not message_tables:
            return
        session_tables = [t for t in ("session_v2", "session") if t in tables]
        if not session_tables:
            return
        joins = " ".join(f"LEFT JOIN {table} s{i} ON s{i}.id=m.session_id" for i, table in enumerate(session_tables))
        directories = [f"s{i}.directory" for i in range(len(session_tables))]
        directory = directories[0] if len(directories) == 1 else "COALESCE(" + ",".join(directories) + ")"
        queries = []
        for table in message_tables:
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            native_role = 'm.type' if 'type' in columns else 'NULL'
            queries.append(conn.execute(f"SELECT m.id,m.data,{directory},{native_role},m.time_updated FROM {table} m {joins} "
                                        "WHERE m.time_updated>=? AND m.time_created<=? LIMIT 20000", (since_ms, now_ms)))
        for mid, raw, cwd, native_role, updated in (row for query in queries for row in query):
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict) or data.get("role", native_role) != "assistant":
                continue
            times = data.get("time")
            completed = times.get("completed") if isinstance(times, dict) else None
            if type(completed) is not int or not 0 <= completed <= now_ms:
                continue
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue
            cache = tokens.get("cache")
            if not isinstance(cache, dict):
                continue
            values = [_finite_int(tokens.get(k)) for k in ("input", "output", "reasoning")]
            values.extend(_finite_int(cache.get(k)) for k in ("read", "write"))
            if any(v is None for v in values):
                continue
            inp, outp, reasoning, read, write = values
            # Charge the completed native response hour; unfinished usage can
            # still change and must not be represented as a final measurement.
            if completed < since_ms:
                continue
            observation = (updated, completed, project_for_cwd(cwd), inp + read + write, outp + reasoning)
            previous = messages.get(mid)
            if previous is None or observation[:2] > previous[:2]:
                messages[mid] = observation
    except (sqlite3.Error, OSError):
        return
    finally:
        conn.close()


def collect_opencode(db_path: str, since_ms: int, now_ms: int, roots=None) -> Dict[Tuple[int, str], List[int]]:
    """Aggregate latest completed native messages across classic/managed DBs."""
    messages = {}
    for path in (roots or [db_path]):
        _opencode_messages(path, since_ms, now_ms, messages)
    out = {}
    for _, completed, project, inp, outp in messages.values():
        slot = out.setdefault((utc_hour_ms(completed), project), [0, 0])
        slot[0] += inp
        slot[1] += outp
    return out


def build_hours(now_ms: int) -> List[Dict[str, Any]]:
    """Aggregate native tokens for complete hours in the trailing window."""
    current_hour = utc_hour_ms(now_ms)
    since_ms = current_hour - WINDOW_HOURS * 3600000
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

    sources = [(e, a, Path(home) / d) for e, a, d in _CODEX_SOURCES]
    sources.extend([("claude", "shared-claude", Path(home) / '.claude' / 'projects'),
                    ("opencode", "shared-go", Path(home) / '.local/share/opencode/opencode.db')])
    # Retained revisions can contain unique sessions, so collect all installed
    # homes, using only directory identity and the catalog's account aliases.
    try:
        from . import provisioning
        from .model_contract import canonical_account_label
        for dest in sorted(provisioning.root().iterdir())[:256]:
            match = re.fullmatch(r'([a-z][a-z0-9_-]{0,47})-([a-f0-9-]{36})', dest.name)
            if not match or not dest.is_dir():
                continue
            account = canonical_account_label(match[1])
            managed = dest / 'home'
            for engine, relative in [('codex', '.codex'), ('zcodex', '.zcodex'),
                                     ('claude', '.claude/projects'),
                                     ('opencode', '.local/share/opencode/opencode.db')]:
                source = managed / relative
                if source.exists():
                    sources.append((engine, account, source))
    except (OSError, ValueError, TypeError):
        pass
    grouped = {}
    for engine, account, source in sources:
        paths = grouped.setdefault((engine, account), set())
        paths.add(str(source.resolve()))
    for (engine, account), paths in grouped.items():
        try:
            paths = sorted(paths)
            if engine in ('codex', 'zcodex'):
                buckets = collect_codex(paths[0], since_ms, now_ms, engine, account, roots=paths)
                merge(engine, account, buckets)
            elif engine == 'claude':
                merge(engine, account, collect_claude(paths[0], since_ms, now_ms, roots=paths))
            else:
                merge(engine, account, collect_opencode(paths[0], since_ms, now_ms, roots=paths))
        except Exception:
            continue
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
            # Respect the server bucket limit independently of body byte size.
            result = None
            for offset in range(0, len(hours), 48):
                batch = hours[offset:offset + 48]
                if self.http_client:
                    result = self.http_client(target, batch, token=access_token)
                else:
                    result = post_token_hours(target, batch, token=access_token)
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
