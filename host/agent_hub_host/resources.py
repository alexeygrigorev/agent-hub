"""Public `hub resources` client: utilization, per-session ledger, reaps.

Read-only projections over the Hub resource-management endpoints:
  GET /api/fleet/resources            overview (admin)
  GET /api/fleet/resources/sessions   per-session ledger with filters (admin)
  GET /api/fleet/resources/reaps      watchdog reap log (admin)

No tokens, transcripts or private config ever travel here. Filters are
allowlisted; unknown values are refused client-side.
"""
from __future__ import annotations

import json
import re
import urllib.parse

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Bounded workspace-absolute project filter (mirrors the server workspace
# rule): absolute path, <=512 chars, segments confined to [A-Za-z0-9._-].
WORKSPACE_SEG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TASK_KINDS = ("implement", "review", "ask", "goal")


def add_commands(sub) -> None:
    parser = sub.add_parser("resources", help="Show resource utilization, per-session usage and watchdog reaps")
    parser.add_argument("view", nargs="?", choices=["sessions", "reaps"], default=None,
                        help="Ledger view (default: utilization overview)")
    parser.add_argument("--project", default=None, help="Filter sessions by project label or workspace absolute path")
    parser.add_argument("--engine", default=None, help="Filter sessions by engine")
    parser.add_argument("--kind", default=None, help="Filter sessions by task kind")
    parser.add_argument("--task-kind", dest="task_kind", default=None, choices=list(TASK_KINDS),
                        help="Filter sessions by task kind (implement/review/ask/goal)")
    parser.add_argument("--json", dest="json", action="store_true", help="Emit raw JSON instead of text")


def _label(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not LABEL_RE.match(value):
        raise ValueError(f"Invalid {what} filter")
    return value


def _project(value: str | None) -> str | None:
    """Project filter: a label or a bounded workspace absolute path.

    Absolute paths are accepted (percent-encoded by urlencode at send time)
    only when canonical and bounded: POSIX absolute, <=512 chars, no
    NUL/control/backslash, no `.`/`..`/empty segments, segments confined to
    [A-Za-z0-9._-]{1,64}. Anything else is refused client-side.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid project filter")
    if LABEL_RE.match(value):
        return value
    if len(value) > 512 or value.startswith("-"):
        raise ValueError("Invalid project filter")
    if not value.startswith("/") or "\\" in value:
        raise ValueError("Invalid project filter")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid project filter")
    parts = value.split("/")
    if parts[0] != "" or len(parts) < 2:
        raise ValueError("Invalid project filter")
    for seg in parts[1:]:
        if not seg or seg in (".", "..") or not WORKSPACE_SEG_RE.match(seg):
            raise ValueError("Invalid project filter")
    return value


def _kind(engine_kind: str | None, task_kind: str | None, what: str) -> str | None:
    chosen = task_kind if task_kind is not None else engine_kind
    if chosen is None:
        return None
    if not isinstance(chosen, str) or not LABEL_RE.match(chosen):
        raise ValueError(f"Invalid {what} filter")
    return chosen


def fetch_overview() -> dict:
    from . import bus
    return bus._do_request("GET", "/api/fleet/resources")


def fetch_sessions(project=None, engine=None, kind=None, task_kind=None) -> dict:
    from . import bus
    params: dict[str, str] = {}
    if project is not None:
        params["project"] = _project(project)
    if engine is not None:
        params["engine"] = _label(engine, "engine")
    resolved_kind = _kind(kind, task_kind, "kind")
    if resolved_kind is not None:
        params["kind"] = resolved_kind
    path = "/api/fleet/resources/sessions"
    if params:
        path += "?" + urllib.parse.urlencode(params)
    return bus._do_request("GET", path)


def fetch_reaps() -> dict:
    from . import bus
    return bus._do_request("GET", "/api/fleet/resources/reaps")


def execute(args) -> dict:
    view = getattr(args, "view", None)
    if view == "sessions":
        return fetch_sessions(project=getattr(args, "project", None),
                              engine=getattr(args, "engine", None),
                              kind=getattr(args, "kind", None),
                              task_kind=getattr(args, "task_kind", None))
    if view == "reaps":
        return fetch_reaps()
    return fetch_overview()


def _fmt_mb(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if f != f or f < 0:
        return "unknown"
    return "%d MiB" % int(round(f))


def _fmt_cpu(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if f != f or f < 0:
        return "unknown"
    return "%.1fs" % f


def _fmt_avg(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if f != f or f < 0:
        return "unknown"
    return "%.2f" % f


def _fmt_runtime(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if f != f or f < 0:
        return "unknown"
    total = int(f)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return "%dh%dm" % (hours, minutes)
    if minutes:
        return "%dm%ds" % (minutes, seconds)
    return "%ds" % seconds


def render_text(data: dict, view=None) -> str:
    if not isinstance(data, dict):
        raise ValueError("Invalid resources payload")
    if view == "sessions":
        rows = data.get("sessions")
        if not isinstance(rows, list) or not rows:
            return "No sessions reported."
        lines = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            flag = " FLAGGED" if row.get("rss_flagged") else ""
            state = str(row.get("state", "?"))
            if row.get("finished_at"):
                state = "%s finished=%s" % (state, row.get("finished_at"))
            lines.append("%s project=%s engine=%s model=%s tier=%s kind=%s state=%s "
                         "peak_rss=%s peak_pss=%s cpu=%s avg_cpu=%s runtime=%s%s" % (
                             row.get("session_id", "?"), row.get("project", "?"),
                             row.get("engine", "?"), row.get("model", "?"),
                             row.get("tier", "?"), row.get("kind", "?"), state,
                             _fmt_mb(row.get("peak_rss_mb")), _fmt_mb(row.get("peak_pss_mb")),
                             _fmt_cpu(row.get("cpu_seconds")), _fmt_avg(row.get("avg_cpu_cores")),
                             _fmt_runtime(row.get("runtime_s")), flag))
        return "\n".join(lines) if lines else "No sessions reported."
    if view == "reaps":
        rows = data.get("reaps")
        if not isinstance(rows, list) or not rows:
            return "No reaps reported."
        lines = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            lines.append("%s reason=%s freed=%s at=%s actor=%s intake=%s evidence=%s" % (
                row.get("session_id", "?"), row.get("reason", "?"),
                _fmt_mb(row.get("freed_mb")), row.get("at", "?"),
                row.get("actor", "?"), row.get("intake_id", "?"),
                row.get("evidence_id", "?")))
        return "\n".join(lines) if lines else "No reaps reported."
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    hosts = data.get("hosts") if isinstance(data.get("hosts"), list) else []
    reaps = data.get("reaps") if isinstance(data.get("reaps"), list) else []
    lines = []
    lines.append("floor=%s knobs(idle=%smin rss_flag=%sGB watchdog=%ss)" % (
        _fmt_mb(config.get("memory_floor_mb")),
        config.get("idle_minutes", "?"), config.get("rss_flag_gb", "?"),
        config.get("watchdog_interval_s", "?")))
    if not hosts:
        lines.append("hosts: none")
    for host in hosts:
        if not isinstance(host, dict):
            continue
        lines.append("host %s mem_used=%s/%s load5=%s cores=%s running=%s queued=%s" % (
            host.get("machine_id", "?"), _fmt_mb(host.get("mem_used_mb")),
            _fmt_mb(host.get("mem_total_mb")), host.get("load5", "?"),
            host.get("cores", "?"), host.get("running", "?"), host.get("queued", "?")))
    stats = data.get("stats") if isinstance(data.get("stats"), list) else []
    for group in stats[:20]:
        if not isinstance(group, dict):
            continue
        lines.append("p90 %s/%s/%s n=%s p50=%s p90=%s" % (
            group.get("project", "?"), group.get("engine", "?"), group.get("kind", "?"),
            group.get("samples", "?"), _fmt_mb(group.get("p50_rss_mb")),
            _fmt_mb(group.get("p90_rss_mb"))))
    lines.append("recent reaps: %d" % len(reaps))
    return "\n".join(lines)
