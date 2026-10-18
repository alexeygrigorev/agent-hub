"""Public `hub quota` client: authenticated fleet-wide/per-host quota reads.

Read-only projection over GET /api/quota?view=fleet. The DTO is usage-only
(percentages, native amounts with unit labels, resets, freshness states);
no tokens, cookies, emails, transcripts or private config ever travel here.
"""
import json
import re
import urllib.parse

ALLOWED_PROVIDERS = ("codex", "codex-orin", "claude", "gemini", "grok", "zai", "go", "copilot")
LABEL_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
HOST_ID_REGEX = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def add_commands(sub):
    parser = sub.add_parser("quota", help="Show sanitized provider quota (fleet-wide or per host)")
    parser.add_argument("--host", default=None, help="Limit to one host id")
    parser.add_argument("--provider", default=None, choices=list(ALLOWED_PROVIDERS))
    parser.add_argument("--engine", default=None, help="Limit to one engine label")
    parser.add_argument("--account", default=None, help="Limit to one account label")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of text")


def _label(value, what):
    if not isinstance(value, str) or not LABEL_REGEX.match(value):
        raise ValueError("Invalid %s filter" % what)
    return value


def _host_id(value):
    if not isinstance(value, str) or not HOST_ID_REGEX.match(value):
        raise ValueError("Invalid host filter")
    return value


def fetch_quota(host=None, provider=None, engine=None, account=None):
    """GET /api/quota?view=fleet with optional filters via the stored credential."""
    from . import bus
    params = {"view": "fleet"}
    if host is not None:
        params["host_id"] = _host_id(host)
    if provider is not None:
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError("Invalid provider filter")
        params["provider"] = provider
    if engine is not None:
        params["engine"] = _label(engine, "engine")
    if account is not None:
        params["account"] = _label(account, "account")
    return bus._do_request("GET", "/api/quota?" + urllib.parse.urlencode(params))


def execute(args):
    return fetch_quota(host=args.host, provider=args.provider, engine=args.engine, account=args.account)


def render_text(data):
    """Human-readable usage table; unknown stays unknown, never zero."""
    if not isinstance(data, dict):
        raise ValueError("Invalid quota payload")
    lines = []
    pools = data.get("pools") if isinstance(data.get("pools"), list) else []
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        remaining = pool.get("remaining_percent")
        left = "unknown" if remaining is None else "%s%%" % remaining
        lines.append("%s/%s: %s (%s left, observed %s by %s)" % (
            pool.get("provider", "?"), pool.get("account", "?"), pool.get("state", "unknown"),
            left, pool.get("observed_at") or "never",
            ", ".join(pool.get("observed_by_hosts") or []) or "no host"))
    coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
    if coverage:
        lines.append("coverage: %s fresh, %s stale, %s missing of %s machines" % (
            len(coverage.get("fresh") or []), len(coverage.get("stale") or []),
            len(coverage.get("missing") or []), coverage.get("machines_total")))
    if not lines:
        return "No quota pools reported."
    return "\n".join(lines)
