"""Public `hub quota` client: authenticated fleet-wide/per-host quota reads.

Read-only projection over GET /api/quota?view=fleet. The DTO is usage-only
(percentages, native amounts with unit labels, resets, freshness states);
no tokens, cookies, emails, transcripts or private config ever travel here.

Canonical provider model: `codex-orin` is not a provider choice — it is the
second Codex account (provider `codex`, account `codex-orin`). Filter it with
`--provider codex --account codex-orin`. The Go pool account is `shared-go`.
"""
import json
import re
import urllib.parse

# Canonical output providers only: legacy wire route keys (e.g. `codex-orin`,
# still collected from quse snapshots) never appear as --provider choices.
ALLOWED_PROVIDERS = ("codex", "claude", "gemini", "grok", "zai", "go", "copilot")
LABEL_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
HOST_ID_REGEX = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def add_commands(sub):
    parser = sub.add_parser("quota", help="Show sanitized provider quota (fleet-wide or per host)")
    parser.add_argument("--host", default=None, help="Limit to one host id")
    parser.add_argument("--provider", default=None, choices=list(ALLOWED_PROVIDERS))
    parser.add_argument("--engine", default=None, help="Limit to one engine label")
    parser.add_argument("--account", default=None, help="Limit to one account label")
    parser.add_argument("--admin-token-file", default=None, help="Use an existing private Hub operator token for this read")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of text")
    parser.add_argument("--enable", action="store_true", help="publish: also enable existing disabled quota accounts for routing")
    parser.add_argument("tbm", nargs="?", default=None, choices=["history", "pacing", "metrics", "banked", "kill-switch", "publish"],
                        help="TBM view: pacing, metrics, banked, or kill-switch (fleet filters do not apply)")
    parser.add_argument("kill_action", nargs="?", default=None, choices=["on", "off"],
                        help="kill-switch write: on|off (read when omitted)")
    parser.add_argument("--reason", default=None, help="Reason for kill-switch on|off")
    parser.add_argument("--range", default="24h", choices=["24h", "7d", "30d"], help="Usage history range")
    parser.add_argument("--kind", default="quota_window", choices=["quota_window", "quota", "tokens"], help="Usage history dimension")


def _label(value, what):
    if not isinstance(value, str) or not LABEL_REGEX.match(value):
        raise ValueError("Invalid %s filter" % what)
    return value


def _host_id(value):
    if not isinstance(value, str) or not HOST_ID_REGEX.match(value):
        raise ValueError("Invalid host filter")
    return value


def fetch_quota(host=None, provider=None, engine=None, account=None, admin_token_file=None):
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
    path = "/api/quota?" + urllib.parse.urlencode(params)
    if admin_token_file is not None:
        from . import config, quota_server
        base = config.load_config().get('base_url') or config.DEFAULT_BASE_URL
        opener, _ = quota_server.admin_opener(base.rstrip('/'), admin_token_file)
        with opener.open(base.rstrip('/') + path, timeout=20) as response:
            return json.loads(response.read(1024 * 1024))
    return bus._do_request("GET", path)


def execute(args):
    tbm_view = getattr(args, "tbm", None)
    if tbm_view is None:
        kwargs = dict(host=args.host, provider=args.provider, engine=args.engine, account=args.account)
        if getattr(args, 'admin_token_file', None) is not None:
            kwargs['admin_token_file'] = args.admin_token_file
        if getattr(args, "kill_action", None) is not None or getattr(args, "reason", None) is not None:
            raise ValueError("Invalid quota arguments")
        return fetch_quota(**kwargs)
    if tbm_view == 'publish':
        from . import quota_publish
        return quota_publish.publish(enable=getattr(args, 'enable', False))
    if tbm_view == 'history':
        if any(getattr(args, key, None) is not None for key in ('provider', 'engine', 'account', 'admin_token_file', 'kill_action', 'reason')):
            raise ValueError('History accepts --range, --kind and --host only')
        from . import bus
        params = {'range': args.range, 'kind': args.kind}
        if args.host is not None:
            params['machine'] = _host_id(args.host)
        return bus._do_request('GET', '/api/monitoring/history?' + urllib.parse.urlencode(params))
    # TBM views do not take fleet filters; fail loudly instead of silently ignoring them.
    for key in ("host", "provider", "engine", "account", "admin_token_file"):
        if getattr(args, key, None) is not None:
            raise ValueError("Invalid quota arguments: filters do not apply to '%s'" % tbm_view)
    from . import tbm as tbm_cli
    if tbm_view == "metrics":
        if getattr(args, "kill_action", None) is not None or getattr(args, "reason", None) is not None:
            raise ValueError("Invalid quota arguments")
        return tbm_cli.get_metrics()
    if tbm_view == "pacing":
        if getattr(args, "kill_action", None) is not None or getattr(args, "reason", None) is not None:
            raise ValueError("Invalid quota arguments")
        return tbm_cli.get_pacing()
    if tbm_view == "banked":
        if getattr(args, "kill_action", None) is not None or getattr(args, "reason", None) is not None:
            raise ValueError("Invalid quota arguments")
        return tbm_cli.get_banked()
    if tbm_view == "kill-switch":
        action = getattr(args, "kill_action", None)
        reason = getattr(args, "reason", None)
        if action is None:
            if reason is not None:
                raise ValueError("Invalid quota arguments")
            return tbm_cli.get_kill_switch()
        return tbm_cli.set_kill_switch(tbm_cli.parse_kill_action(action), reason)
    raise ValueError("Invalid quota view")


def render_text(data):
    """Human-readable usage table; unknown stays unknown, never zero."""
    if not isinstance(data, dict):
        raise ValueError("Invalid quota payload")
    if isinstance(data.get('windows'), dict):
        from . import tbm as tbm_cli
        return tbm_cli.render_metrics_text(data)
    if isinstance(data.get('series'), list):
        return '\n'.join('%s / %s / %s: %s known points' % (row.get('kind'), row.get('entity'), row.get('metric'), sum(isinstance(p.get('value'), (int, float)) for p in row.get('points', []))) for row in data['series']) or 'No usage history in this range.'
    lines = []
    pools = data.get("pools") if isinstance(data.get("pools"), list) else []
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        remaining = pool.get("remaining_percent")
        left = "unknown" if remaining is None else "%s%%" % remaining
        windows = pool.get("windows")
        if remaining is None and isinstance(windows, dict):
            measured = ["%s=%s%%" % (name, window["percent_remaining"])
                        for name, window in windows.items()
                        if isinstance(window, dict) and window.get("percent_remaining") is not None]
            if measured:
                left += "; windows " + ", ".join(measured)
        harness = pool.get("harness") or pool.get("engines") or []
        harness_text = ", ".join(h for h in harness if isinstance(h, str)) if isinstance(harness, list) else "unknown"
        decision_suffix = (" — %s" % pool["decision_reason"]) if pool.get("decision_reason") else ""
        # 4.3: print the real observer or nothing; "by no host" was noise when
        # no host reported the reading.
        observers = ", ".join(pool.get("observed_by_hosts") or [])
        if observers:
            observation = "observed %s by %s" % (pool.get("observed_at") or "never", observers)
        elif pool.get("observed_at"):
            observation = "observed %s" % pool["observed_at"]
        else:
            observation = "not observed"
        lines.append("%s/%s/%s: %s (%s left, %s)%s" % (
            harness_text or "unknown", pool.get("provider", "?"), pool.get("account", "?"),
            pool.get("state", "unknown") + (" [DISABLED: excluded from routing]" if pool.get("routing") == "disabled" else ""),
            left, observation,
            (decision_suffix + (", resets %s" % pool["reset_at"] if pool.get("reset_at") else ""))))
    coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
    account_coverage = data.get("account_coverage")
    if isinstance(account_coverage, dict):
        lines.append("accounts: %s fresh, %s stale, %s unavailable of %s (server probes; machine reports are fallback)" % (
            len(account_coverage.get("fresh") or []), len(account_coverage.get("stale") or []),
            len(account_coverage.get("unavailable") or []), account_coverage.get("total")))
    if isinstance(coverage, dict) and coverage.get("machines_total") is not None:
        line = "coverage: %s fresh, %s stale, %s missing of %s machines" % (
            len(coverage.get("fresh") or []), len(coverage.get("stale") or []),
            len(coverage.get("missing") or []), coverage.get("machines_total"))
        not_fleet = coverage.get("not_fleet")
        if isinstance(not_fleet, list) and not_fleet:
            line += " (+%s not in fleet: drained or never checked in)" % len(not_fleet)
        lines.append(line)
    if not lines:
        return "No quota pools reported."
    return "\n".join(lines)
