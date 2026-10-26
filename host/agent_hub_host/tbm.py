"""TBM AC4 CLI: pacing, banked and kill-switch reads/writes.

Read-only projections plus an explicit kill-switch write, over the Hub REST
endpoints the head owns:

- GET /api/quota/pacing -> {as_of, pools:[...]}
- GET /api/quota/banked -> {as_of, tranches:[...]}
- GET /api/quota/kill-switch -> {active, reason}
- POST /api/quota/kill-switch {active:boolean, reason:string} -> {active, reason}

Auth and error handling reuse session_control.request (admin operator Bearer
credential with refresh). No tokens, cookies or secrets are ever printed;
errors are sanitized by session_control.diagnostic at the cli boundary.
"""

PACING_PATH = "/api/quota/pacing"
BANKED_PATH = "/api/quota/banked"
KILL_SWITCH_PATH = "/api/quota/kill-switch"

MAX_REASON_CHARS = 280


def _require_dict(data, what):
    if not isinstance(data, dict):
        raise ValueError("Invalid %s payload" % what)
    return data


def get_pacing():
    from . import session_control
    return _require_dict(session_control.request("GET", PACING_PATH), "pacing")


def get_banked():
    from . import session_control
    return _require_dict(session_control.request("GET", BANKED_PATH), "banked")


def get_kill_switch():
    from . import session_control
    data = _require_dict(session_control.request("GET", KILL_SWITCH_PATH), "kill-switch")
    return _normalize_kill_switch(data)


def set_kill_switch(active, reason=None):
    from . import session_control
    if not isinstance(active, bool):
        raise ValueError("Invalid kill-switch state")
    text = "" if reason is None else reason
    if not isinstance(text, str):
        raise ValueError("Invalid kill-switch reason")
    if len(text) > MAX_REASON_CHARS:
        raise ValueError("Invalid kill-switch reason")
    payload = {"active": active, "reason": text}
    data = _require_dict(session_control.request("POST", KILL_SWITCH_PATH, payload), "kill-switch")
    return _normalize_kill_switch(data)


def _normalize_kill_switch(data):
    if not isinstance(data, dict) or not isinstance(data.get("active"), bool):
        raise ValueError("Invalid kill-switch payload")
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("Invalid kill-switch payload")
    return {"active": data["active"], "reason": reason if reason is not None else ""}


def parse_kill_action(value):
    if value == "on":
        return True
    if value == "off":
        return False
    raise ValueError("Invalid kill-switch action (expected on|off)")


def render_pacing_text(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid pacing payload")
    lines = []
    as_of = data.get("as_of")
    if isinstance(as_of, str) and as_of:
        lines.append("as_of: %s" % as_of)
    pools = data.get("pools") if isinstance(data.get("pools"), list) else []
    count = 0
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        count += 1
        pid = pool.get("pool_id") or "%s/%s" % (
            pool.get("provider", "?"), pool.get("account", "?"))
        target = pool.get("target_burn_rate_tpm")
        ratio = pool.get("pacing_ratio")
        target_s = "unknown" if target is None else str(target)
        ratio_s = "unknown" if ratio is None else str(ratio)
        reset = pool.get("reset_at_ms")
        reset_s = "unknown" if reset is None else str(reset)
        unit = "%/min" if pool.get("capacity_known") == 0 or pool.get("quota_unit") == "percent" else "tokens/min"
        lines.append("%s: target %s %s, pacing %s, reset %s" % (pid, target_s, unit, ratio_s, reset_s))
    if count == 0:
        lines.append("No pacing pools reported.")
    return "\n".join(lines)


def render_banked_text(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid banked payload")
    lines = []
    as_of = data.get("as_of")
    if isinstance(as_of, str) and as_of:
        lines.append("as_of: %s" % as_of)
    tranches = data.get("tranches") if isinstance(data.get("tranches"), list) else []
    count = 0
    for tranche in tranches:
        if not isinstance(tranche, dict):
            continue
        count += 1
        tid = tranche.get("tranche_id", "?")
        status = tranche.get("status", "unknown")
        remaining = tranche.get("remaining_tokens")
        left = "unknown" if remaining is None or tranche.get("quantity_known") == 0 else str(remaining)
        expires = tranche.get("expires_at_ms")
        exp_s = "unknown" if expires is None else str(expires)
        lines.append("%s: %s (%s tokens left, expires %s)" % (tid, status, left, exp_s))
    if count == 0:
        lines.append("No banked tranches reported.")
    return "\n".join(lines)


def render_kill_switch_text(data):
    normalized = _normalize_kill_switch(data) if isinstance(data, dict) else None
    if normalized is None:
        raise ValueError("Invalid kill-switch payload")
    state = "ON" if normalized["active"] else "OFF"
    reason = normalized.get("reason") or ""
    if reason:
        return "kill-switch: %s (reason: %s)" % (state, reason)
    return "kill-switch: %s" % state


def render_text(kind, data):
    if kind == "pacing":
        return render_pacing_text(data)
    if kind == "banked":
        return render_banked_text(data)
    if kind == "kill-switch":
        return render_kill_switch_text(data)
    raise ValueError("Invalid TBM view")
