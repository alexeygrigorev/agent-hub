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


def add_commands(sub) -> None:
    parser = sub.add_parser("resources", help="Show resource utilization, per-session usage and watchdog reaps")
    parser.add_argument("view", nargs="?", choices=["sessions", "reaps"], default=None,
                        help="Ledger view (default: utilization overview)")
    parser.add_argument("--project", default=None, help="Filter sessions by project")
    parser.add_argument("--engine", default=None, help="Filter sessions by engine")
    parser.add_argument("--kind", default=None, help="Filter sessions by task kind")
    parser.add_argument("--json", dest="json", action="store_true", help="Emit raw JSON instead of text")


def _label(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not LABEL_RE.match(value):
        raise ValueError(f"Invalid {what} filter")
    return value


def fetch_overview() -> dict:
    from . import bus
    return bus._do_request("GET", "/api/fleet/resources")


def fetch_sessions(project=None, engine=None, kind=None) -> dict:
    from . import bus
    params: dict[str, str] = {}
    if project is not None:
        params["project"] = _label(project, "project")
    if engine is not None:
        params["engine"] = _label(engine, "engine")
    if kind is not None:
        params["kind"] = _label(kind, "kind")
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
                              kind=getattr(args, "kind", None))
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
            lines.append("%s %s/%s/%s peak=%s state=%s%s" % (
                row.get("session_id", "?"), row.get("project", "?"),
                row.get("engine", "?"), row.get("kind", "?"),
                _fmt_mb(row.get("peak_rss_mb")), row.get("state", "?"), flag))
        return "\n".join(lines) if lines else "No sessions reported."
    if view == "reaps":
        rows = data.get("reaps")
        if not isinstance(rows, list) or not rows:
            return "No reaps reported."
        lines = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            lines.append("%s %s freed=%s at=%s" % (
                row.get("session_id", "?"), row.get("reason", "?"),
                _fmt_mb(row.get("freed_mb")), row.get("at", "?")))
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
