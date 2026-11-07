"""Deliver Hub2's durable dev-mail queue through the existing inbox helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
from typing import Any, Callable, Optional

from . import api, private_fs

HUB_URL = "https://hub.dtcdev.click"
CONFIG_NAME = "oncall-alerts.json"
CONFIG_LIMIT = 4096
DELIVERY_RE = re.compile(r"^[0-9a-f-]{36}$", re.IGNORECASE)
HELPER_TIMEOUT_S = 150
HELPERS = (
    ("oncall", "oncall-send"),
    ("orchestrator", "orch-send"),
)


def _load_config(journal_dir: str) -> Optional[dict[str, str]]:
    path = Path(journal_dir) / CONFIG_NAME
    try:
        raw = private_fs.read_bytes(path, CONFIG_LIMIT)
    except FileNotFoundError:
        return None
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {"poll_token"}:
        raise ValueError("invalid oncall alert config")
    token = value.get("poll_token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("invalid oncall alert config")
    return {"poll_token": token}


def _request(method: str, path: str, payload: Optional[dict[str, Any]], token: str) -> Any:
    if not path.startswith("/api/oncall-alerts/"):
        raise ValueError("invalid oncall request path")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        HUB_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "agent-hub-oncall-alerts/1",
        },
    )
    return api.safe_http_json(request, timeout=10)


def _field(payload: dict[str, Any], key: str, limit: int = 500) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "")[:limit]


def _render_alert(payload: Any, delivery_id: str) -> str:
    if not isinstance(payload, dict):
        raise ValueError("invalid oncall alert payload")
    marker = f"[hub-oncall-alert:{delivery_id}]"
    lines = [
        "Dapier dev@ infrastructure alert",
        marker,
        "From: " + _field(payload, "from"),
        "To: " + _field(payload, "to"),
        "Subject: " + _field(payload, "subject"),
        "Date: " + _field(payload, "date"),
        "Message-ID: " + _field(payload, "message_id"),
        "",
        _field(payload, "text_body", 60_000) or _field(payload, "text", 60_000),
    ]
    attachments = payload.get("attachments")
    if isinstance(attachments, list) and attachments:
        lines.extend(["", "Attachments:"])
        for item in attachments[:30]:
            if isinstance(item, dict):
                name = item.get("filename") if isinstance(item.get("filename"), str) else "attachment"
                content_type = item.get("content_type") if isinstance(item.get("content_type"), str) else ""
                lines.append(f"- {name[:300]} ({content_type[:120]})")
    return "\n".join(lines).strip() + "\n"


def _already_saved(inbox: Path, marker: str) -> bool:
    for directory in (inbox, inbox / "processed"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.md"):
            try:
                if marker in path.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
    return False


def _run_helper(role: str, helper: str, body: str) -> bool:
    inbox = Path.home() / "tmp" / "agent-reports" / role / "inbox"
    marker = body.splitlines()[1] if len(body.splitlines()) > 1 else ""
    if marker and _already_saved(inbox, marker):
        return True
    helper_path = Path.home() / "bin" / helper
    env = os.environ.copy()
    env["SENDER"] = "dapier"
    try:
        result = subprocess.run(
            [str(helper_path)], input=body, text=True, capture_output=True,
            timeout=HELPER_TIMEOUT_S, env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Exit 2 is oncall-send's durable saved-but-session-unavailable result.
    return result.returncode in (0, 2) and "INBOX=saved " in result.stdout


def poll_once(
    journal_dir: str,
    *,
    request: Optional[Callable[[str, str, Optional[dict[str, Any]], str], Any]] = None,
    helper_runner: Optional[Callable[[str, str, str], bool]] = None,
) -> dict[str, Any]:
    """Claim a bounded batch, save to both role inboxes, then acknowledge."""
    config = _load_config(journal_dir)
    if config is None:
        return {"disabled": True, "offers": 0, "saved": 0, "errors": 0}
    request = request or _request
    helper_runner = helper_runner or _run_helper
    token = config["poll_token"]
    offers = request("GET", "/api/oncall-alerts/offers", None, token).get("offers", [])
    if not isinstance(offers, list):
        raise ValueError("invalid oncall offers response")
    saved = 0
    errors = 0
    for offer in offers[:10]:
        if not isinstance(offer, dict):
            errors += 1
            continue
        delivery_id = offer.get("delivery_id")
        lease_token = offer.get("lease_token")
        if not isinstance(delivery_id, str) or not DELIVERY_RE.fullmatch(delivery_id) or \
                not isinstance(lease_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", lease_token):
            errors += 1
            continue
        try:
            body = _render_alert(offer.get("payload"), delivery_id)
        except ValueError:
            errors += 1
            try:
                request("POST", f"/api/oncall-alerts/{delivery_id}/nack", {"lease_token": lease_token}, token)
            except Exception:
                pass
            continue
        helper_saved = True
        for role, helper in HELPERS:
            if not helper_runner(role, helper, body):
                helper_saved = False
        if not helper_saved:
            errors += 1
            try:
                request("POST", f"/api/oncall-alerts/{delivery_id}/nack", {"lease_token": lease_token}, token)
            except Exception:
                pass
            continue
        try:
            request("POST", f"/api/oncall-alerts/{delivery_id}/ack", {"lease_token": lease_token}, token)
            saved += 1
        except Exception:
            # Inbox markers make the next lease replay safe after a lost ACK.
            errors += 1
    return {"disabled": False, "offers": min(len(offers), 10), "saved": saved, "errors": errors}
