"""Agent Hub Host Quota Snapshot Module.

Collects, sanitizes, and posts provider quota snapshots according to
FOUNDER-AUTHORIZED QUOTA-HUB-SPEC-20261014 and contract amendments.
"""

import datetime
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import api, auth, config

SCHEMA_VERSION = "1.1"
COLLECTOR_VERSION = "quota_snapshot/1.1"
ALLOWED_PROVIDERS = (
    "codex",
    # Orin's second Codex account (own CODEX_HOME, own ChatGPT token);
    # reported by the same host collector (quse) as a separate route.
    "codex-orin",
    "claude",
    "gemini",
    "grok",
    "zai",
    "go",
    "copilot",
)
NATIVE_UNIT_LABELS = ("percent", "credits", "usd_cents", "requests", "tokens")
PROFILE_LABEL_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$")
COLLECTOR_VERSION_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+/-]{0,63}$")
MAX_CREDITS_VALUE = 1e12
MAX_SPEND_VALUE = 1e15
WINDOW_NAME_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
MAX_WINDOWS_PER_PROVIDER = 32
MAX_SNAPSHOT_BYTES = 32 * 1024  # 32 KiB
MAX_RAW_STDOUT_BYTES = 64 * 1024  # 64 KiB
MAX_RAW_STDERR_BYTES = 16 * 1024  # 16 KiB
DEFAULT_COLLECT_INTERVAL = 600.0  # 600 seconds (10 minutes)
STALE_AGE_THRESHOLD = 1200.0  # 1200 seconds (20 minutes)
MAX_FUTURE_SKEW_SECONDS = 0.0  # No future timestamps permitted


class QuotaError(ValueError):
    """Base exception for quota collection and validation errors."""


class QuotaHold(QuotaError):
    """Raised when configuration hold prevents execution."""


def parse_iso_utc(ts: Any) -> Optional[datetime.datetime]:
    """Parse an explicitly timezone-aware full ISO timestamp into a UTC datetime."""
    if not isinstance(ts, str) or not ts.strip() or ts != ts.strip():
        return None
    s = ts.strip()
    if "T" not in s and "t" not in s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return None
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def format_iso_utc(dt: datetime.datetime) -> str:
    """Format a timezone-aware UTC datetime into canonical UTC ISO format."""
    if not isinstance(dt, datetime.datetime) or dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("Datetime must be timezone-aware UTC")
    dt_utc = dt.astimezone(datetime.timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_percent(val: Any) -> Optional[float]:
    """Validate finite non-boolean percent remaining in [0.0, 100.0]."""
    if type(val) is bool:
        return None
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return None
        if 0.0 <= val <= 100.0:
            return float(val)
        return None
    return None


def sanitize_bool(val: Any) -> Optional[bool]:
    """Validate strict boolean."""
    if type(val) is bool:
        return val
    return None


def sanitize_native_amount(val: Any, cap: float) -> Optional[float]:
    """Validate a finite non-boolean native usage amount in [0, cap]; else None."""
    if type(val) is bool:
        return None
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return None
        if 0.0 <= float(val) <= cap:
            return float(val)
        return None
    return None


def sanitize_unit_label(val: Any) -> Optional[str]:
    """Allowlisted native unit label; unknown units stay None (unknown)."""
    if isinstance(val, str) and val in NATIVE_UNIT_LABELS:
        return val
    return None


def sanitize_profile_label(val: Any) -> Optional[str]:
    """Bounded provider profile label (e.g. Muse Spark model id); else None."""
    if isinstance(val, str) and PROFILE_LABEL_REGEX.match(val):
        return val
    return None


def extract_native_fields(raw: Any) -> Dict[str, Any]:
    """Pass through ONLY allowlisted provider-native usage fields.

    Unknown keys (tokens, cookies, emails, raw stdout, private config) are
    dropped by allowlist extraction. Invalid native values become None
    (unknown) without disturbing the percent path; no percentage is ever
    converted into dollars or tokens here.
    """
    native: Dict[str, Any] = {
        "credits_remaining": None,
        "spend_used": None,
        "spend_limit": None,
        "unit": None,
        "profile": None,
    }
    if not isinstance(raw, dict):
        return native
    native["credits_remaining"] = sanitize_native_amount(raw.get("credits_remaining"), MAX_CREDITS_VALUE)
    native["spend_used"] = sanitize_native_amount(raw.get("spend_used"), MAX_SPEND_VALUE)
    native["spend_limit"] = sanitize_native_amount(raw.get("spend_limit"), MAX_SPEND_VALUE)
    native["unit"] = sanitize_unit_label(raw.get("unit"))
    native["profile"] = sanitize_profile_label(raw.get("profile"))
    return native


def sanitize_window(
    win_name: str,
    win_data: Any,
    current_time: datetime.datetime
) -> Tuple[Dict[str, Any], bool]:
    """Sanitize a single window and determine if an error was reported."""
    if not isinstance(win_data, dict):
        return {
            "percent_remaining": None,
            "reset_at": None,
            "present": False,
            "rolling": False,
            "error": "unavailable",
        }, True

    has_error = False

    # percent_remaining: number 0..100 | null
    if "percent_remaining" not in win_data:
        percent_remaining = None
        has_error = True
    else:
        raw_pct = win_data.get("percent_remaining")
        if raw_pct is None:
            percent_remaining = None
        else:
            percent_remaining = sanitize_percent(raw_pct)
            if percent_remaining is None:
                has_error = True

    # reset_at: normalized UTC ISO string | null
    raw_reset = win_data.get("reset_at")
    reset_at = None
    if raw_reset is not None:
        parsed_reset = parse_iso_utc(raw_reset)
        if parsed_reset is not None:
            reset_at = format_iso_utc(parsed_reset)
        else:
            has_error = True

    # Upstream explicit window error (including empty string or non-null falsy) marks window/route unavailable failclosed
    if "error" in win_data and win_data["error"] is not None:
        has_error = True

    # rolling: strict bool if provided
    if "rolling" in win_data and win_data["rolling"] is not None:
        if type(win_data["rolling"]) is not bool:
            has_error = True
            rolling = False
        else:
            rolling = win_data["rolling"]
    else:
        rolling = False

    # present: strict bool if provided
    if "present" in win_data and win_data["present"] is not None:
        if type(win_data["present"]) is not bool:
            has_error = True
            raw_present: Optional[bool] = False
        else:
            raw_present = win_data["present"]
    else:
        raw_present = None

    if percent_remaining is None:
        if raw_present is True or reset_at is not None:
            has_error = True
        present = False
    else:
        # Explicit present:false with a numeric percent is contradictory: fail closed
        if raw_present is False:
            has_error = True
        present = True

    if has_error:
        present = False
        window_error: Optional[str] = "unavailable"
    else:
        window_error = None

    # Hub contract: an absent or errored window carries no measurements at all.
    # Native fields are omitted when unknown so percent-only inputs keep their
    # exact legacy shape; non-null natives round-trip verbatim.
    if has_error or not present:
        percent_remaining = None
        reset_at = None
    native: Dict[str, Any] = {}
    if not has_error and present:
        for native_key, native_val in extract_native_fields(win_data).items():
            if native_val is not None:
                native[native_key] = native_val

    return {
        "percent_remaining": percent_remaining,
        "reset_at": reset_at,
        "present": present,
        "rolling": rolling,
        "error": window_error,
        **native,
    }, has_error


def sanitize_provider(
    provider_name: str,
    raw_prov: Any,
    current_time: datetime.datetime
) -> Dict[str, Any]:
    """Sanitize provider data and enforce allowlisted fields and privacy limits."""
    default_details = (
        {"limit_reached": None}
        if provider_name != "grok"
        else {"limit_reached": None, "has_grok_code_access": None}
    )
    if not isinstance(raw_prov, dict):
        return {
            "status": "error",
            "error": "unavailable",
            "observed_at": None,
            "details": default_details,
            "windows": {},
        }

    is_stale_or_invalid = False

    # details: limit_reached (and has_grok_code_access ONLY on grok)
    if "details" in raw_prov and raw_prov["details"] is not None:
        if not isinstance(raw_prov["details"], dict):
            is_stale_or_invalid = True
            raw_details: Dict[str, Any] = {}
        else:
            raw_details = raw_prov["details"]
    else:
        raw_details = {}

    if "limit_reached" in raw_details and raw_details["limit_reached"] is not None:
        if type(raw_details["limit_reached"]) is not bool:
            is_stale_or_invalid = True
            limit_reached = None
        else:
            limit_reached = raw_details["limit_reached"]
    else:
        limit_reached = None

    sanitized_details: Dict[str, Any] = {"limit_reached": limit_reached}
    # Full-reset grants expose expiry/availability, never an inferred token size.
    if isinstance(raw_details.get("banked_resets"), list):
        grants = []
        for grant in raw_details["banked_resets"][:32]:
            if not isinstance(grant, dict) or type(grant.get("available")) is not bool:
                continue
            expiry = parse_iso_utc(grant.get("expires_at"))
            label = grant.get("label", "")
            if expiry is None or not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9 ._-]{0,80}", label):
                continue
            grants.append({"expires_at": format_iso_utc(expiry), "available": grant["available"], "label": label})
        if grants or not raw_details["banked_resets"]:
            sanitized_details["banked_resets"] = grants
    for native_key, native_val in extract_native_fields(raw_details).items():
        if native_val is not None:
            sanitized_details[native_key] = native_val
    if "stale" in raw_details and type(raw_details["stale"]) is bool:
        sanitized_details["stale"] = raw_details["stale"]
    # Known provider backoff deadline (launcher 429 Retry-After authority, mirrored
    # as UTC ISO). Omitted when unknown; malformed stays unknown here, the hub
    # retains an unexpired known hold and fails closed on malformed stored values.
    raw_cooldown = raw_details.get("cooldown_until")
    if raw_cooldown is not None:
        parsed_cooldown = parse_iso_utc(raw_cooldown)
        if parsed_cooldown is not None:
            sanitized_details["cooldown_until"] = format_iso_utc(parsed_cooldown)
    if provider_name == "grok":
        if "has_grok_code_access" in raw_details and raw_details["has_grok_code_access"] is not None:
            if type(raw_details["has_grok_code_access"]) is not bool:
                is_stale_or_invalid = True
                sanitized_details["has_grok_code_access"] = None
            else:
                sanitized_details["has_grok_code_access"] = raw_details["has_grok_code_access"]
        else:
            sanitized_details["has_grok_code_access"] = None

    has_prov_obs = "observed_at" in raw_prov and raw_prov["observed_at"] is not None
    has_det_obs = "observed_at" in raw_details and raw_details["observed_at"] is not None
    had_obs_key = "observed_at" in raw_prov or "observed_at" in raw_details
    observed_at = None
    if has_prov_obs or has_det_obs:
        candidates = []
        if has_prov_obs:
            candidates.append(raw_prov["observed_at"])
        if has_det_obs:
            candidates.append(raw_details["observed_at"])
        parsed_obs = []
        for c in candidates:
            dt = parse_iso_utc(c)
            if dt is None:
                is_stale_or_invalid = True
            else:
                parsed_obs.append(dt)
        if not is_stale_or_invalid and len(parsed_obs) == 2 and parsed_obs[0] != parsed_obs[1]:
            is_stale_or_invalid = True
        if parsed_obs and not is_stale_or_invalid:
            obs_dt = parsed_obs[0]
            age = (current_time - obs_dt).total_seconds()
            if age < 0.0:
                is_stale_or_invalid = True
                observed_at = None
            else:
                observed_at = format_iso_utc(obs_dt)
                if age >= STALE_AGE_THRESHOLD:
                    is_stale_or_invalid = True

    # Native stale flag (strict bool, True or malformed non-bool marks stale/invalid)
    if "stale" in raw_prov and raw_prov["stale"] is not None:
        if raw_prov["stale"] is not False:
            is_stale_or_invalid = True
    if "stale" in raw_details and raw_details["stale"] is not None:
        if raw_details["stale"] is not False:
            is_stale_or_invalid = True

    # windows: data-driven, validated regex, bounded <= 32
    if "windows" in raw_prov and not isinstance(raw_prov["windows"], dict):
        is_stale_or_invalid = True
        raw_windows: Dict[str, Any] = {}
    else:
        raw_windows = raw_prov.get("windows") if isinstance(raw_prov.get("windows"), dict) else {}

    sanitized_windows: Dict[str, Any] = {}
    any_window_error = False

    for win_name, win_val in raw_windows.items():
        if not isinstance(win_name, str) or not WINDOW_NAME_REGEX.match(win_name):
            any_window_error = True
            continue
        if len(sanitized_windows) >= MAX_WINDOWS_PER_PROVIDER:
            any_window_error = True
            continue
        swin, win_err = sanitize_window(win_name, win_val, current_time)
        sanitized_windows[win_name] = swin
        if win_err:
            any_window_error = True

    raw_status = raw_prov.get("status")
    has_raw_error = "error" in raw_prov and raw_prov["error"] is not None

    if raw_status == "error" or has_raw_error or is_stale_or_invalid or any_window_error:
        status = "error"
        error = "unavailable"
        if not (has_prov_obs or has_det_obs):
            observed_at = None
    elif raw_status == "ok":
        status = "ok"
        error = None
        if observed_at is None and not had_obs_key:
            observed_at = format_iso_utc(current_time)
    else:
        status = "unknown"
        error = "unavailable"
        if not (has_prov_obs or has_det_obs):
            observed_at = None

    return {
        "status": status,
        "error": error,
        "observed_at": observed_at,
        "details": sanitized_details,
        "windows": sanitized_windows,
    }


def create_quota_snapshot(
    raw_data: Any,
    host_id: str,
    current_time: Optional[datetime.datetime] = None
) -> Dict[str, Any]:
    """Assemble a validated, sanitized quota snapshot adhering to schema 1.0."""
    if not config.is_valid_machine_id(host_id):
        raise ValueError("host_id must be a valid non-empty string")

    if current_time is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    else:
        if (
            not isinstance(current_time, datetime.datetime)
            or current_time.tzinfo is None
            or current_time.tzinfo.utcoffset(current_time) is None
        ):
            raise ValueError("current_time must be a timezone-aware UTC datetime")
        now = current_time.astimezone(datetime.timezone.utc)

    captured_at = format_iso_utc(now)

    if not isinstance(raw_data, dict):
        return {
            "schema_version": SCHEMA_VERSION,
            "collector_version": COLLECTOR_VERSION,
            "captured_at": captured_at,
            "host_id": host_id,
            "status": "error",
            "error": "unavailable",
            "routes": {},
        }

    routes: Dict[str, Any] = {}
    for prov in ALLOWED_PROVIDERS:
        if prov in raw_data:
            routes[prov] = sanitize_provider(prov, raw_data[prov], now)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "captured_at": captured_at,
        "host_id": host_id,
        "status": "ok",
        "error": None,
        "routes": routes,
    }

    raw_bytes = json.dumps(snapshot).encode("utf-8")
    if len(raw_bytes) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot payload exceeds maximum bounded size of 32 KiB")

    return snapshot


def _terminate_owned_popen(proc: subprocess.Popen) -> None:
    """Safely terminate and wait ONLY the owned Popen handle."""
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
    except Exception:
        pass


def _read_stream_capped(
    stream: Any,
    max_bytes: int,
    chunks: List[bytes],
    state: Dict[str, bool],
    proc: subprocess.Popen,
    retain: bool = True,
) -> None:
    """Read from stream in small chunks, capping memory and terminating proc on overflow."""
    total = 0
    try:
        while True:
            read_size = min(4096, max_bytes - total + 1)
            chunk = stream.read(read_size)
            if not isinstance(chunk, (bytes, bytearray)) or len(chunk) == 0:
                break
            total += len(chunk)
            if total > max_bytes:
                state["overflow"] = True
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            if retain:
                chunks.append(bytes(chunk))
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def execute_quse(
    quse_cmd: Union[str, List[str]] = "quse",
    timeout: float = 30.0,
    max_bytes: int = MAX_RAW_STDOUT_BYTES,
    runner: Optional[Callable[..., Any]] = None
) -> Dict[str, Any]:
    """Execute bounded quse (or a trusted JSON adapter argv) without a shell."""
    if not isinstance(quse_cmd, str):
        if (not isinstance(quse_cmd, list) or not 1 <= len(quse_cmd) <= 32 or
                any(not isinstance(a, str) or not a or len(a) > 4096 or "\0" in a for a in quse_cmd)):
            raise ValueError("Invalid JSON adapter argv")
    if type(timeout) is bool or not isinstance(timeout, (int, float)) or math.isnan(timeout) or math.isinf(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    effective_timeout = min(float(timeout), 30.0)

    if type(max_bytes) is bool or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    effective_max_bytes = min(int(max_bytes), MAX_RAW_STDOUT_BYTES)

    if runner is not None:
        res = runner(quse_cmd, timeout=effective_timeout, max_bytes=effective_max_bytes)
        if isinstance(res, dict):
            encoded_dict = json.dumps(res).encode("utf-8")
            if len(encoded_dict) > effective_max_bytes:
                raise QuotaError("quse stdout exceeds byte limit")
            return res
        if isinstance(res, (bytes, str)):
            raw_bytes = res.encode("utf-8") if isinstance(res, str) else res
            if len(raw_bytes) > effective_max_bytes:
                raise QuotaError("quse stdout exceeds byte limit")
            try:
                parsed = json.loads(raw_bytes.decode("utf-8"))
            except Exception as e:
                raise QuotaError(f"Failed to parse quse JSON: {type(e).__name__}") from None
            if not isinstance(parsed, dict):
                raise QuotaError("quse output must be a JSON object")
            return parsed
        raise TypeError("runner must return dict, bytes, or str")

    proc: Optional[subprocess.Popen] = None
    stdout_chunks: List[bytes] = []
    stdout_state = {"overflow": False}
    stderr_state = {"overflow": False}
    try:
        proc = subprocess.Popen(
            [quse_cmd, "--json"] if isinstance(quse_cmd, str) else quse_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False
        )
        t_out = threading.Thread(
            target=_read_stream_capped,
            args=(proc.stdout, effective_max_bytes, stdout_chunks, stdout_state, proc, True),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_read_stream_capped,
            args=(proc.stderr, MAX_RAW_STDERR_BYTES, [], stderr_state, proc, False),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        proc.wait(timeout=effective_timeout)
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
    except subprocess.TimeoutExpired:
        if proc is not None:
            _terminate_owned_popen(proc)
        raise TimeoutError("quse subprocess timed out") from None
    except Exception as e:
        if proc is not None:
            _terminate_owned_popen(proc)
        raise QuotaError(f"quse subprocess failed: {type(e).__name__}") from None
    finally:
        if proc is not None:
            _terminate_owned_popen(proc)

    if stdout_state["overflow"]:
        raise QuotaError("quse stdout exceeded bounded bytes limit")
    if stderr_state["overflow"]:
        raise QuotaError("quse stderr exceeded bounded bytes limit")

    stdout_bytes = b"".join(stdout_chunks)
    if len(stdout_bytes) > effective_max_bytes:
        raise QuotaError("quse stdout exceeded bounded bytes limit")

    if proc.returncode != 0:
        raise QuotaError(f"quse failed with exit code {proc.returncode}")

    try:
        data = json.loads(stdout_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("quse output must be a JSON object")
        return data
    except Exception as e:
        raise QuotaError(f"Failed to parse quse JSON: {type(e).__name__}") from None


def _resolve_verified_origin_and_token(
    base_url: str,
    token: Optional[str] = None
) -> Tuple[str, str, Dict[str, Any]]:
    """Validate base_url and ensure canonical origin matches paired host config before token use."""
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
            raise QuotaHold("Missing paired base_url in host config for token access")
        try:
            cfg_origin = api.canonical_origin(cfg_base)
        except Exception:
            raise QuotaHold("Invalid base_url in host config") from None
        if target_origin != cfg_origin:
            raise QuotaHold("Target base_url origin does not match paired host config")

    if token is None:
        if not isinstance(cfg_tok, str) or not cfg_tok.strip() or cfg_tok != cfg_tok.strip():
            raise QuotaHold("Missing access token for quota request")
        access_token = cfg_tok
    else:
        if not isinstance(token, str) or not token.strip() or token != token.strip():
            raise QuotaHold("Missing or invalid access token for quota request")
        access_token = token

    return base_url.rstrip("/"), access_token, cfg


def post_quota_snapshot(
    base_url: str,
    snapshot: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0
) -> Dict[str, Any]:
    """POST validated quota snapshot to /api/quota without token leaks or redirects."""
    clean_base, access_token, cfg = _resolve_verified_origin_and_token(base_url, token)

    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a dict")
    snap_host_id = snapshot.get("host_id")
    if not config.is_valid_machine_id(snap_host_id):
        raise ValueError("snapshot host_id must be a valid machine_id")
    cfg_mid = cfg.get("machine_id")
    cfg_tok = cfg.get("access_token")
    if (token is None or (cfg_tok is not None and token == cfg_tok)) and cfg_mid is not None:
        if snap_host_id != cfg_mid:
            raise QuotaHold("snapshot host_id does not match paired config machine_id")

    body = json.dumps(snapshot).encode("utf-8")
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Payload exceeds 32 KiB limit")

    url = f"{clean_base}/api/quota"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }
    )

    try:
        return api.safe_http_json(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP error {e.code} during quota post") from None
    except urllib.error.URLError:
        raise RuntimeError("Network error during quota post") from None
    except Exception as e:
        raise RuntimeError(f"Quota POST error: {type(e).__name__}") from None


def get_quota_snapshot(
    base_url: str,
    token: Optional[str] = None,
    timeout: float = 10.0
) -> Dict[str, Any]:
    """GET quota snapshot from /api/quota without token leaks."""
    clean_base, access_token, _ = _resolve_verified_origin_and_token(base_url, token)

    url = f"{clean_base}/api/quota"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}"
        }
    )

    try:
        return api.safe_http_json(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP error {e.code} during quota get") from None
    except urllib.error.URLError:
        raise RuntimeError("Network error during quota get") from None
    except Exception as e:
        raise RuntimeError(f"Quota GET error: {type(e).__name__}") from None


class QuotaCollector:
    """Manages 600s periodic quota collection and POSTs for HostAgent."""

    def __init__(
        self,
        clock: Any = None,
        base_url: Optional[str] = None,
        quse_cmd: Union[str, List[str]] = "quse",
        runner: Optional[Callable[..., Any]] = None,
        http_client: Optional[Callable[..., Any]] = None,
        interval: float = DEFAULT_COLLECT_INTERVAL
    ):
        self.clock = clock or api.Clock()
        self.base_url = base_url
        self.quse_cmd = quse_cmd
        self.runner = runner
        self.http_client = http_client
        self.interval = interval
        self.last_collect_monotonic: Optional[float] = None
        self.last_snapshot: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None
        self.last_hold: Optional[str] = None

    def _get_monotonic(self) -> float:
        if hasattr(self.clock, "monotonic"):
            m = self.clock.monotonic()
        elif hasattr(self.clock, "time"):
            m = self.clock.time()
        else:
            m = time.monotonic()
        if type(m) is bool or not isinstance(m, (int, float)) or math.isnan(m) or math.isinf(m):
            raise ValueError("Invalid monotonic clock reading")
        return float(m)

    def _get_wall_time(self) -> datetime.datetime:
        if hasattr(self.clock, "now"):
            dt = self.clock.now()
            if not isinstance(dt, datetime.datetime) or dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                raise ValueError("Clock.now() must return a timezone-aware datetime")
            return dt.astimezone(datetime.timezone.utc)
        if hasattr(self.clock, "time"):
            t = self.clock.time()
            if type(t) is bool or not isinstance(t, (int, float)) or math.isnan(t) or math.isinf(t):
                raise ValueError("Invalid wall clock reading")
            return datetime.datetime.fromtimestamp(float(t), tz=datetime.timezone.utc)
        return datetime.datetime.now(datetime.timezone.utc)

    def step(self) -> Optional[Dict[str, Any]]:
        """Regular tick called from HostAgent loops. Only runs when due."""
        now_mono = self._get_monotonic()
        if self.last_collect_monotonic is not None:
            if now_mono - self.last_collect_monotonic < self.interval:
                return None

        return self.collect_and_post()

    def collect_and_post(self) -> Optional[Dict[str, Any]]:
        """Execute one collection attempt and post. Updates monotonic timestamp to prevent duplicates."""
        now_mono = self._get_monotonic()
        # Immediately record attempt on monotonic clock to prevent duplicate quse calls on failure
        self.last_collect_monotonic = now_mono

        cfg = config.load_config()
        if not isinstance(cfg, dict):
            self.last_hold = "invalid host config"
            return None

        machine_id = cfg.get("machine_id")
        access_token = cfg.get("access_token")
        cfg_base_url = cfg.get("base_url")

        if (
            not config.is_valid_machine_id(machine_id)
            or not isinstance(access_token, str)
            or not access_token.strip()
            or access_token != access_token.strip()
            or not isinstance(cfg_base_url, str)
            or not cfg_base_url.strip()
        ):
            self.last_hold = "missing machine_id, access_token, or base_url in host config"
            return None

        try:
            cfg_origin = api.canonical_origin(cfg_base_url)
        except Exception:
            self.last_hold = "invalid base_url in host config"
            return None

        target_base_url = self.base_url if self.base_url is not None else cfg_base_url
        try:
            target_origin = api.canonical_origin(target_base_url)
        except Exception:
            self.last_hold = "invalid target base_url"
            return None

        if target_origin != cfg_origin:
            self.last_hold = "base_url origin mismatch with paired host config"
            return None

        if self.runner is None and isinstance(self.quse_cmd, str) and not os.path.isabs(self.quse_cmd) \
                and shutil.which(self.quse_cmd) is None:
            # quse is not installed on this host: no quota evidence to report,
            # a hold rather than a failed collection every cycle.
            self.last_hold = "quse_unavailable"
            return None

        self.last_hold = None

        try:
            wall_now = self._get_wall_time()
            raw_data = execute_quse(
                quse_cmd=self.quse_cmd,
                timeout=30.0,
                max_bytes=MAX_RAW_STDOUT_BYTES,
                runner=self.runner
            )
            wall_now = self._get_wall_time()
            snapshot = create_quota_snapshot(
                raw_data=raw_data,
                host_id=machine_id,
                current_time=wall_now
            )

            if self.http_client:
                self.http_client(target_base_url, snapshot, token=access_token)
            else:
                post_quota_snapshot(target_base_url, snapshot, token=access_token)

            self.last_snapshot = snapshot
            self.last_error = None
            return snapshot
        except Exception as e:
            self.last_error = f"collection failed: {type(e).__name__}"
            return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent Hub Host Quota Collector")
    parser.add_argument("--once", action="store_true", help="Run quota collection once and exit")
    parser.add_argument("--interval", type=float, default=DEFAULT_COLLECT_INTERVAL, help="Collection interval in seconds")
    args = parser.parse_args()

    collector = QuotaCollector(interval=args.interval)
    if args.once:
        res = collector.collect_and_post()
        if res:
            print(json.dumps(res, indent=2))
        else:
            print(f"Collection held or failed: hold={collector.last_hold}, error={collector.last_error}")
    else:
        while True:
            collector.step()
            time.sleep(1.0)

