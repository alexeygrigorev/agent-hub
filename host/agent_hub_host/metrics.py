"""Allowlisted Hub metrics projection and POST adapter.

Reads operator-supplied Dashboard report JSON files, projects a strict
sanitized snapshot, validates it, and posts with the existing Hub HTTP
client. Never opens Dashboard default private paths. Never logs tokens.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import api, config

SCHEMA_VERSION = "1.0"
MAX_SNAPSHOT_BYTES = 32 * 1024
MAX_INPUT_BYTES = 256 * 1024
REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
CANONICAL_PROJECT_IDS = (
    "agent-branches",
    "agent-dashboard",
    "quota-launcher",
    "agent-coordination",
)
PROJECT_ALIASES = {
    "agent-quota-launcher": "quota-launcher",
    "agent_quota_launcher": "quota-launcher",
    "agent_branches": "agent-branches",
    "agent_dashboard": "agent-dashboard",
    "agent_coordination": "agent-coordination",
}
UNATTRIBUTED_PROJECT_ID = "unattributed"
COVERAGE_ENUM = frozenset({"observed", "unknown", "missing", "unsupported", "partial"})
GAP_CODES = frozenset({
    "missing",
    "unsupported_project",
    "unattributed",
    "partial_coverage",
    "producer_unknown",
    "empty_window",
})
PRIVATE_KEYS = frozenset({
    "path", "paths", "prompt", "prompts", "text", "content", "summary",
    "feature_summary", "user_content", "session_id", "session_uuid",
    "conversation_id", "credential", "credentials", "token", "account",
    "accounts", "address", "addresses", "email", "raw", "transcript",
    "transcripts", "quota", "percent_remaining", "cost", "money", "file",
    "filename", "source_path", "owner_tag", "reviewer", "pr_url", "commit",
    "sha", "total_quota_delta_percent", "total_cost", "quota_delta",
    "shared_agent_ids",
})
HOUR = datetime.timedelta(hours=1)

ALLOWED_PROJECT_IDS = frozenset((*CANONICAL_PROJECT_IDS, UNATTRIBUTED_PROJECT_ID))
# R4 F2: receiver UTC grammar mirror (Worker parseUtcIso). Extended spelling
# only; compact YYYYMMDDTHHMMSSZ and non-UTC designators never validate.
# ASCII-only full-string match: \d must not accept Unicode digits and the
# terminator must not permit a trailing newline (receiver Date.parse rejects
# both with 400 invalid_as_of).
_UTC_STRICT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|\+00:00)\Z",
    re.ASCII,
)
_TOP_KEYS = frozenset({
    "schema_version", "as_of", "host_id", "request_key", "coverage", "gaps",
    "utilization", "usage", "accepted",
})
_COVERAGE_KEYS = frozenset({"utilization", "usage", "accepted"})
_GAP_KEYS = frozenset({"code"})
_UTIL_KEYS = frozenset({"unknown", "projects"})
_UTIL_PROJECT_KEYS = frozenset({
    "unknown", "unique_agents", "total_agent_hours", "coverage",
    "unattributed_agent_hours", "hourly_buckets",
})
_BUCKET_KEYS = frozenset({"bucket_start", "bucket_end", "active_agents", "agent_hours"})
_USAGE_KEYS = frozenset({"unknown", "projects"})
_USAGE_PROJECT_KEYS = frozenset({
    "unknown", "total_input_tokens", "total_output_tokens",
    "total_cache_read_tokens", "total_cache_creation_tokens",
    "total_reasoning_tokens", "total_noncache_input_tokens",
    "has_unknown_tokens", "has_unknown_cache", "has_unknown_reasoning",
    "cache_coverage", "reasoning_coverage", "event_count",
})
_ACCEPTED_KEYS = frozenset({
    "unknown", "accepted_outcomes", "task_count", "commit_count",
    "review_count", "projects",
})
_ACCEPTED_PROJECT_KEYS = frozenset({
    "unknown", "accepted_outcomes", "task_count", "commit_count", "review_count",
})


class MetricsError(ValueError):
    """Validation or projection failure. Message never includes file contents."""


def canonical_project_id(project_id: Any) -> str:
    if project_id in PROJECT_ALIASES:
        return PROJECT_ALIASES[project_id]
    if project_id in CANONICAL_PROJECT_IDS or project_id == UNATTRIBUTED_PROJECT_ID:
        return str(project_id)
    return UNATTRIBUTED_PROJECT_ID


def parse_utc(ts: Any) -> Optional[datetime.datetime]:
    # R4 F2: match the receiver's UTC grammar exactly (Worker parseUtcIso):
    # extended YYYY-MM-DDTHH:MM:SS with optional 1-9 digit fraction and a Z or
    # +00:00 designator only. Compact basic spellings, lowercase separators,
    # nonzero offsets and naive stamps are rejected before config/HTTP, just
    # as the Worker answers 400 invalid_as_of.
    if not isinstance(ts, str) or not ts:
        return None
    match = _UTC_STRICT_RE.match(ts)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 59):
        return None
    fraction = match.group(7) or ""
    # R5 R3: the receiver (Date.parse) keeps millisecond precision only.
    # Truncate sub-millisecond digits so as_of/bucket-boundary comparison
    # matches the Worker instead of rejecting valid sub-millisecond DTOs.
    microsecond = (int((fraction + "000000")[:6]) // 1000) * 1000
    try:
        return datetime.datetime(year, month, day, hour, minute, second, microsecond, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def format_utc(dt: datetime.datetime) -> str:
    dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}Z"


def _finite_nn(val: Any, integer: bool) -> Optional[float]:
    if val is None:
        return None
    if type(val) is bool:
        raise MetricsError("invalid_count")
    if isinstance(val, (int, float)):
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            raise MetricsError("invalid_count")
        if val < 0:
            raise MetricsError("invalid_count")
        if integer:
            if isinstance(val, float) and not val.is_integer():
                raise MetricsError("invalid_count")
            return int(val)
        return float(val)
    raise MetricsError("invalid_count")


def _unit(val: Any) -> Optional[float]:
    if val is None:
        return None
    n = _finite_nn(val, False)
    if n is not None and n > 1:
        raise MetricsError("invalid_coverage")
    return n


def _bool(val: Any) -> bool:
    if type(val) is bool:
        return val
    raise MetricsError("invalid_flag")


def _scan_private(obj: Any) -> bool:
    if isinstance(obj, list):
        return any(_scan_private(v) for v in obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in PRIVATE_KEYS or k in ("__proto__", "constructor", "prototype"):
                return True
            if _scan_private(v):
                return True
    return False


def _strict_exact_keys(obj: Any, allowed: frozenset, required: Optional[frozenset] = None) -> None:
    if not isinstance(obj, dict):
        raise MetricsError("invalid_snapshot")
    keys = set(obj.keys())
    if not keys.issubset(allowed) or (required is not None and not required.issubset(keys)):
        raise MetricsError("unknown_field")


def _strict_validate_buckets(buckets_raw: Any, as_of: datetime.datetime, unknown: bool) -> None:
    # R5 R3: mirror the receiver's unknown-bucket rule (it fills an unknown
    # empty list instead of rejecting it). Only the empty list is accepted;
    # a missing key stays a key error and known projects still need 24 rows.
    if unknown and buckets_raw == []:
        return
    if not isinstance(buckets_raw, list) or len(buckets_raw) != 24:
        raise MetricsError("invalid_buckets")
    window_start = as_of - datetime.timedelta(hours=24)
    for i, b in enumerate(buckets_raw):
        if not isinstance(b, dict) or set(b.keys()) != _BUCKET_KEYS:
            raise MetricsError("unknown_field")
        bs = parse_utc(b.get("bucket_start"))
        be = parse_utc(b.get("bucket_end"))
        expect_s = window_start + datetime.timedelta(hours=i)
        expect_e = window_start + datetime.timedelta(hours=i + 1)
        if bs is None or be is None or bs != expect_s or be != expect_e:
            raise MetricsError("invalid_buckets")
        try:
            active = _finite_nn(b.get("active_agents"), True)
            hours = _finite_nn(b.get("agent_hours"), False)
        except MetricsError:
            raise MetricsError("invalid_count") from None
        if unknown and (active is not None or hours is not None):
            raise MetricsError("invalid_buckets")
        if not unknown and (active is None or hours is None):
            raise MetricsError("invalid_buckets")


def _strict_validate_util_project(raw: Any, as_of: datetime.datetime) -> None:
    _strict_exact_keys(raw, _UTIL_PROJECT_KEYS, frozenset({"unknown", "hourly_buckets"}))
    # R4 F2: receiver requires every nested key present; a missing counter is
    # not explicit null (Worker 400 invalid_usage/invalid_utilization parity).
    if not _UTIL_PROJECT_KEYS.issubset(set(raw.keys())):
        raise MetricsError("invalid_snapshot")
    if type(raw.get("unknown")) is not bool:
        raise MetricsError("invalid_snapshot")
    unknown = bool(raw.get("unknown"))
    try:
        unique = _finite_nn(raw.get("unique_agents"), True)
        hours = _finite_nn(raw.get("total_agent_hours"), False)
        coverage = _unit(raw.get("coverage"))
        unattr = _finite_nn(raw.get("unattributed_agent_hours"), False)
    except MetricsError:
        raise MetricsError("invalid_count") from None
    if unknown:
        if unique is not None or hours is not None or coverage is not None or unattr is not None:
            raise MetricsError("invalid_snapshot")
    else:
        if unique is None or hours is None or coverage is None or unattr is None:
            raise MetricsError("invalid_snapshot")
    _strict_validate_buckets(raw.get("hourly_buckets"), as_of, unknown)


def _strict_validate_usage_project(raw: Any) -> None:
    _strict_exact_keys(raw, _USAGE_PROJECT_KEYS, frozenset({"unknown"}))
    # R4 F2: missing usage counter keys are not explicit null. The receiver
    # rejects a DTO without total_reasoning_tokens (400 invalid_usage); the
    # helper must reject before config/credentials/HTTP.
    if not _USAGE_PROJECT_KEYS.issubset(set(raw.keys())):
        raise MetricsError("invalid_snapshot")
    if type(raw.get("unknown")) is not bool:
        raise MetricsError("invalid_snapshot")
    unknown = bool(raw.get("unknown"))
    try:
        vals = {}
        for key in (
            "total_input_tokens", "total_output_tokens", "total_cache_read_tokens",
            "total_cache_creation_tokens", "total_reasoning_tokens",
            "total_noncache_input_tokens", "event_count",
        ):
            vals[key] = _finite_nn(raw.get(key), True)
        flags = {}
        for key in ("has_unknown_tokens", "has_unknown_cache", "has_unknown_reasoning"):
            flags[key] = _bool(raw.get(key))
        cache_cov = _unit(raw.get("cache_coverage"))
        reason_cov = _unit(raw.get("reasoning_coverage"))
    except MetricsError:
        raise MetricsError("invalid_count") from None
    if unknown:
        for key in (
            "total_input_tokens", "total_output_tokens", "total_cache_read_tokens",
            "total_cache_creation_tokens", "total_reasoning_tokens",
            "total_noncache_input_tokens",
        ):
            if vals[key] is not None:
                raise MetricsError("invalid_snapshot")


def _strict_validate_accepted_project(raw: Any) -> None:
    _strict_exact_keys(raw, _ACCEPTED_PROJECT_KEYS, frozenset({"unknown"}))
    # R4 F2: same missing-vs-null rule as usage/utilization projects.
    if not _ACCEPTED_PROJECT_KEYS.issubset(set(raw.keys())):
        raise MetricsError("invalid_snapshot")
    if type(raw.get("unknown")) is not bool:
        raise MetricsError("invalid_snapshot")
    unknown = bool(raw.get("unknown"))
    try:
        accepted = _finite_nn(raw.get("accepted_outcomes"), True)
        tasks = _finite_nn(raw.get("task_count"), True)
        commits = _finite_nn(raw.get("commit_count"), True)
        reviews = _finite_nn(raw.get("review_count"), True)
    except MetricsError:
        raise MetricsError("invalid_count") from None
    if unknown and (accepted is not None or tasks is not None or commits is not None or reviews is not None):
        raise MetricsError("invalid_snapshot")
    # Unknown=False keeps known outcomes/tasks/commits; review_count may stay null
    # when no agreed review-event identity exists (F3). Do not force reviews non-null.
    if not unknown and (accepted is None or tasks is None or commits is None):
        raise MetricsError("invalid_snapshot")


def _strict_validate_project_map(raw: Any, as_of: datetime.datetime, kind: str) -> None:
    if not isinstance(raw, dict):
        raise MetricsError("invalid_snapshot")
    for proj, value in raw.items():
        if proj not in ALLOWED_PROJECT_IDS:
            raise MetricsError("unknown_field")
        if kind == "utilization":
            _strict_validate_util_project(value, as_of)
        elif kind == "usage":
            _strict_validate_usage_project(value)
        else:
            _strict_validate_accepted_project(value)


def load_json_file(path: str) -> Any:
    if not isinstance(path, str) or not path or path != path.strip():
        raise MetricsError("invalid_report_path")
    if not os.path.isfile(path):
        raise MetricsError("report_unreadable")
    try:
        size = os.path.getsize(path)
    except OSError:
        raise MetricsError("report_unreadable") from None
    if size > MAX_INPUT_BYTES:
        raise MetricsError("report_too_large")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise MetricsError("report_unreadable") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise MetricsError("report_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise MetricsError("invalid_report_json") from None


def _empty_buckets(as_of: datetime.datetime) -> List[Dict[str, Any]]:
    start = as_of - datetime.timedelta(hours=24)
    out = []
    for i in range(24):
        b0 = start + datetime.timedelta(hours=i)
        b1 = start + datetime.timedelta(hours=i + 1)
        out.append({
            "bucket_start": format_utc(b0),
            "bucket_end": format_utc(b1),
            "active_agents": None,
            "agent_hours": None,
        })
    return out


def _project_utilization(report: Optional[Dict[str, Any]], as_of: datetime.datetime) -> Tuple[Dict[str, Any], str, List[str]]:
    gaps: List[str] = []
    if not isinstance(report, dict):
        return {"unknown": True, "projects": {p: {
            "unknown": True, "unique_agents": None, "total_agent_hours": None,
            "coverage": None, "unattributed_agent_hours": None,
            "hourly_buckets": _empty_buckets(as_of),
        } for p in CANONICAL_PROJECT_IDS}}, "missing", ["missing"]
    projects_in = report.get("projects") if isinstance(report.get("projects"), dict) else {}
    out_projects: Dict[str, Any] = {}
    saw_unsupported = False
    for raw_id, raw in projects_in.items():
        cid = canonical_project_id(raw_id)
        if raw_id not in CANONICAL_PROJECT_IDS and raw_id != UNATTRIBUTED_PROJECT_ID and cid == UNATTRIBUTED_PROJECT_ID:
            saw_unsupported = True
            continue
        if not isinstance(raw, dict):
            continue
        unknown = bool(raw.get("unknown")) or report.get("unknown") is True
        buckets_raw = raw.get("hourly_buckets") if isinstance(raw.get("hourly_buckets"), list) else []
        buckets = []
        window_start = as_of - datetime.timedelta(hours=24)
        if unknown or len(buckets_raw) != 24:
            buckets = _empty_buckets(as_of)
            unknown = True
        else:
            ok = True
            for i, b in enumerate(buckets_raw):
                if not isinstance(b, dict):
                    ok = False
                    break
                bs = parse_utc(b.get("bucket_start"))
                be = parse_utc(b.get("bucket_end"))
                expect_s = window_start + datetime.timedelta(hours=i)
                expect_e = window_start + datetime.timedelta(hours=i + 1)
                if bs is None or be is None or bs != expect_s or be != expect_e:
                    ok = False
                    break
                if unknown:
                    buckets.append({
                        "bucket_start": format_utc(bs),
                        "bucket_end": format_utc(be),
                        "active_agents": None,
                        "agent_hours": None,
                    })
                else:
                    buckets.append({
                        "bucket_start": format_utc(bs),
                        "bucket_end": format_utc(be),
                        "active_agents": _finite_nn(b.get("active_agents"), True),
                        "agent_hours": _finite_nn(b.get("agent_hours"), False),
                    })
            if not ok:
                buckets = _empty_buckets(as_of)
                unknown = True
                gaps.append("partial_coverage")
        slot = out_projects.setdefault(cid, {
            "unknown": True,
            "unique_agents": None,
            "total_agent_hours": None,
            "coverage": None,
            "unattributed_agent_hours": None,
            "hourly_buckets": _empty_buckets(as_of),
        })
        if not unknown:
            slot.update({
                "unknown": False,
                "unique_agents": _finite_nn(raw.get("unique_agents"), True),
                "total_agent_hours": _finite_nn(raw.get("total_agent_hours"), False),
                "coverage": _unit(raw.get("coverage")),
                "unattributed_agent_hours": _finite_nn(raw.get("unattributed_agent_hours"), False),
                "hourly_buckets": buckets,
            })
        elif slot["unknown"]:
            slot["hourly_buckets"] = buckets
    for p in CANONICAL_PROJECT_IDS:
        if p not in out_projects:
            out_projects[p] = {
                "unknown": True,
                "unique_agents": None,
                "total_agent_hours": None,
                "coverage": None,
                "unattributed_agent_hours": None,
                "hourly_buckets": _empty_buckets(as_of),
            }
            gaps.append("missing")
    if saw_unsupported:
        gaps.append("unsupported_project")
        if UNATTRIBUTED_PROJECT_ID not in out_projects:
            out_projects[UNATTRIBUTED_PROJECT_ID] = {
                "unknown": True,
                "unique_agents": None,
                "total_agent_hours": None,
                "coverage": None,
                "unattributed_agent_hours": None,
                "hourly_buckets": _empty_buckets(as_of),
            }
    if UNATTRIBUTED_PROJECT_ID in out_projects:
        gaps.append("unattributed")
    unknown_all = all(v.get("unknown") for v in out_projects.values() if True)
    coverage = "missing" if unknown_all and not projects_in else ("partial" if any(v.get("unknown") for v in out_projects.values()) and not unknown_all else ("unknown" if unknown_all else "observed"))
    if report.get("unknown") is True:
        coverage = "unknown"
        unknown_all = True
    incomplete = _producer_incomplete(report)
    for code in incomplete:
        if code not in gaps:
            gaps.append(code)
    if incomplete and coverage == "observed":
        coverage = "partial"
    return {"unknown": unknown_all, "projects": out_projects}, coverage, gaps


def _project_usage(report: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], str, List[str]]:
    gaps: List[str] = []
    empty = {
        "unknown": True,
        "total_input_tokens": None,
        "total_output_tokens": None,
        "total_cache_read_tokens": None,
        "total_cache_creation_tokens": None,
        "total_reasoning_tokens": None,
        "total_noncache_input_tokens": None,
        "has_unknown_tokens": True,
        "has_unknown_cache": True,
        "has_unknown_reasoning": True,
        "cache_coverage": None,
        "reasoning_coverage": None,
        "event_count": 0,
    }
    if not isinstance(report, dict):
        return {"unknown": True, "projects": {p: dict(empty) for p in CANONICAL_PROJECT_IDS}}, "missing", ["missing"]
    projects_in = report.get("projects") if isinstance(report.get("projects"), dict) else {}
    out: Dict[str, Any] = {}
    saw_unsupported = False
    for raw_id, raw in projects_in.items():
        cid = canonical_project_id(raw_id)
        if raw_id not in CANONICAL_PROJECT_IDS and raw_id != UNATTRIBUTED_PROJECT_ID and cid == UNATTRIBUTED_PROJECT_ID:
            saw_unsupported = True
            continue
        if not isinstance(raw, dict):
            continue
        unknown = bool(raw.get("unknown")) or report.get("unknown") is True
        slot = {
            "unknown": unknown,
            "total_input_tokens": None if unknown else _finite_nn(raw.get("total_input_tokens"), True),
            "total_output_tokens": None if unknown else _finite_nn(raw.get("total_output_tokens"), True),
            "total_cache_read_tokens": None if unknown else _finite_nn(raw.get("total_cache_read_tokens"), True),
            "total_cache_creation_tokens": None if unknown else _finite_nn(raw.get("total_cache_creation_tokens"), True),
            "total_reasoning_tokens": None if unknown else _finite_nn(raw.get("total_reasoning_tokens"), True),
            "total_noncache_input_tokens": None if unknown else _finite_nn(raw.get("total_noncache_input_tokens"), True),
            "has_unknown_tokens": True if unknown else _bool(raw.get("has_unknown_tokens", False)),
            "has_unknown_cache": True if unknown else _bool(raw.get("has_unknown_cache", False)),
            "has_unknown_reasoning": True if unknown else _bool(raw.get("has_unknown_reasoning", False)),
            "cache_coverage": None if unknown else _unit(raw.get("cache_coverage")),
            "reasoning_coverage": None if unknown else _unit(raw.get("reasoning_coverage")),
            "event_count": 0 if unknown else int(_finite_nn(raw.get("event_count") or 0, True) or 0),
        }
        out[cid] = slot
    for p in CANONICAL_PROJECT_IDS:
        if p not in out:
            out[p] = dict(empty)
            gaps.append("missing")
    if saw_unsupported:
        gaps.append("unsupported_project")
        if UNATTRIBUTED_PROJECT_ID not in out:
            out[UNATTRIBUTED_PROJECT_ID] = dict(empty)
    unknown_all = all(v.get("unknown") for v in out.values())
    coverage = "missing" if not projects_in else ("unknown" if unknown_all else ("partial" if any(v.get("unknown") or v.get("has_unknown_tokens") for v in out.values()) else "observed"))
    if report.get("unknown") is True:
        coverage = "unknown"
    incomplete = _producer_incomplete(report)
    for code in incomplete:
        if code not in gaps:
            gaps.append(code)
    if incomplete and coverage == "observed":
        coverage = "partial"
    return {"unknown": unknown_all or report.get("unknown") is True, "projects": out}, coverage, gaps


_COUNT_ID_FIELDS = ("feature_id", "task_id", "commit", "commit_sha", "sha", "pr_url")


def _valid_evidence_id(value: Any) -> bool:
    # R4 F3: identifier evidence must be a primitive non-blank string per the
    # read-only producer contract. Bool/object (or other non-string) values
    # are never coerced with str() into proof.
    return isinstance(value, str) and bool(value.strip())


def _count_features(entries: Any) -> Tuple[int, int, int, Optional[int], bool, frozenset]:
    if not isinstance(entries, list):
        return 0, 0, 0, None, True, frozenset()
    # Malformed non-object entries prevent a total; do not silently ignore them.
    for item in entries:
        if not isinstance(item, dict):
            return 0, 0, 0, None, True, frozenset()
    features: set[str] = set()
    tasks: set[str] = set()
    commits: set[str] = set()
    review_ids: set[str] = set()
    complete_proof = True
    for item in entries:
        item_reviews: set[str] = set()
        has_evidence = False
        for key in (*_COUNT_ID_FIELDS, "review_id"):
            if key not in item or item[key] is None:
                continue
            value = item[key]
            if isinstance(value, str):
                if not value.strip():
                    continue
                has_evidence = True
                if key == "review_id":
                    item_reviews.add(value)
                continue
            # Present non-string identifiers (bool/object/...) are malformed
            # evidence, not proof.
            return 0, 0, 0, None, True, frozenset()
        extra_ids = item.get("review_ids")
        if extra_ids is not None:
            if not isinstance(extra_ids, list):
                return 0, 0, 0, None, True, frozenset()
            for extra in extra_ids:
                if extra is None or (isinstance(extra, str) and not extra.strip()):
                    continue
                if not _valid_evidence_id(extra):
                    return 0, 0, 0, None, True, frozenset()
                has_evidence = True
                item_reviews.add(extra)
        if not has_evidence:
            # An empty or reviewer-only object is not observed empty; without
            # any identifier evidence the total stays unknown, not zero.
            return 0, 0, 0, None, True, frozenset()
        fid = item.get("feature_id") or item.get("task_id")
        tid = item.get("task_id")
        if _valid_evidence_id(fid):
            features.add(fid)
        if _valid_evidence_id(tid):
            tasks.add(tid)
        commit = item.get("commit") or item.get("commit_sha") or item.get("sha") or item.get("pr_url")
        if _valid_evidence_id(commit):
            commits.add(commit)
        if item_reviews:
            review_ids.update(item_reviews)
        else:
            # A valid entry without review-event proof keeps the review total
            # unknown even when sibling entries carry valid event IDs.
            complete_proof = False
    # Distinct reviews require agreed review-event identity (review_id/review_ids).
    # Reviewer labels alone do not establish distinct review events; keep null.
    # Known zero is reserved for genuinely empty validated input (no entries);
    # nonempty evidence without any feature/review proof keeps review unknown.
    # R5 R4: commit-only nonempty evidence cannot invent review 0.
    if review_ids and complete_proof:
        reviews: Optional[int] = len(review_ids)
    elif not features and not review_ids and not entries:
        reviews = 0
    else:
        reviews = None
    return len(features), len(tasks), len(commits), reviews, False, frozenset(review_ids)


def _project_accepted(report: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], str, List[str]]:
    gaps: List[str] = []
    if not isinstance(report, dict):
        return {
            "unknown": True,
            "accepted_outcomes": None,
            "task_count": None,
            "commit_count": None,
            "review_count": None,
            "projects": {p: {
                "unknown": True, "accepted_outcomes": None, "task_count": None,
                "commit_count": None, "review_count": None,
            } for p in CANONICAL_PROJECT_IDS},
        }, "missing", ["missing"]
    if report.get("unknown") is True:
        gaps.append("producer_unknown")
        return {
            "unknown": True,
            "accepted_outcomes": None,
            "task_count": None,
            "commit_count": None,
            "review_count": None,
            "projects": {p: {
                "unknown": True, "accepted_outcomes": None, "task_count": None,
                "commit_count": None, "review_count": None,
            } for p in CANONICAL_PROJECT_IDS},
        }, "unknown", gaps
    raw_projects = report.get("projects")
    if not isinstance(raw_projects, dict):
        # Absent/malformed projects object is not proof of an empty window.
        unknown_projects = {p: {
            "unknown": True, "accepted_outcomes": None, "task_count": None,
            "commit_count": None, "review_count": None,
        } for p in CANONICAL_PROJECT_IDS}
        gaps.append("partial_coverage")
        for code in _producer_incomplete(report):
            if code not in gaps:
                gaps.append(code)
        return {
            "unknown": True,
            "accepted_outcomes": None,
            "task_count": None,
            "commit_count": None,
            "review_count": None,
            "projects": unknown_projects,
        }, "partial", gaps
    projects_in = raw_projects
    has_entries = False
    malformed_any = False
    for raw in projects_in.values():
        if isinstance(raw, list):
            if any(not isinstance(item, dict) for item in raw):
                malformed_any = True
            if any(isinstance(item, dict) for item in raw):
                has_entries = True
        elif raw is None:
            malformed_any = True
        else:
            malformed_any = True
    if malformed_any:
        # Malformed entries prevent a total; preserve unknown/partial, not zero.
        unknown_projects = {p: {
            "unknown": True, "accepted_outcomes": None, "task_count": None,
            "commit_count": None, "review_count": None,
        } for p in CANONICAL_PROJECT_IDS}
        gaps.append("partial_coverage")
        for code in _producer_incomplete(report):
            if code not in gaps:
                gaps.append(code)
        return {
            "unknown": True,
            "accepted_outcomes": None,
            "task_count": None,
            "commit_count": None,
            "review_count": None,
            "projects": unknown_projects,
        }, "partial", gaps
    if not has_entries:
        # Genuine empty only when validated without unresolved acceptance coverage.
        incomplete = _producer_incomplete(report)
        if incomplete:
            unknown_projects = {p: {
                "unknown": True, "accepted_outcomes": None, "task_count": None,
                "commit_count": None, "review_count": None,
            } for p in CANONICAL_PROJECT_IDS}
            gaps.append("empty_window")
            for code in incomplete:
                if code not in gaps:
                    gaps.append(code)
            return {
                "unknown": True,
                "accepted_outcomes": None,
                "task_count": None,
                "commit_count": None,
                "review_count": None,
                "projects": unknown_projects,
            }, "partial", gaps
        zeros = {
            p: {
                "unknown": False,
                "accepted_outcomes": 0,
                "task_count": 0,
                "commit_count": 0,
                "review_count": 0,
            }
            for p in CANONICAL_PROJECT_IDS
        }
        gaps.append("empty_window")
        return {
            "unknown": False,
            "accepted_outcomes": 0,
            "task_count": 0,
            "commit_count": 0,
            "review_count": 0,
            "projects": zeros,
        }, "observed", gaps
    out: Dict[str, Any] = {}
    total_f = total_t = total_c = 0
    total_r: Optional[int] = 0
    all_review_ids: set[str] = set()
    saw_unsupported = False
    for raw_id, raw in projects_in.items():
        cid = canonical_project_id(raw_id)
        if raw_id not in CANONICAL_PROJECT_IDS and raw_id != UNATTRIBUTED_PROJECT_ID and cid == UNATTRIBUTED_PROJECT_ID:
            saw_unsupported = True
            continue
        f, t, c, r, malformed, project_review_ids = _count_features(raw)
        if malformed:
            out[cid] = {
                "unknown": True,
                "accepted_outcomes": None,
                "task_count": None,
                "commit_count": None,
                "review_count": None,
            }
            gaps.append("partial_coverage")
            # R5 R4: a malformed project may hide review events, so the
            # aggregate review total stays unknown even when a sibling
            # project proves event r1. Independently proven
            # feature/task/commit counts still accumulate below.
            total_r = None
            continue
        out[cid] = {
            "unknown": False,
            "accepted_outcomes": f,
            "task_count": t,
            "commit_count": c,
            "review_count": r,
        }
        total_f += f
        total_t += t
        total_c += c
        if r is None or total_r is None:
            total_r = None
        else:
            # R4 F3: the same agreed review event attached in several
            # canonical projects counts once in the aggregate, not once per
            # project. Track the global identity set alongside the sum guard.
            all_review_ids.update(project_review_ids)
    if total_r is not None:
        # Every known project proved its events: the aggregate is the number
        # of distinct agreed review-event identities, deduplicated globally.
        total_r = len(all_review_ids)
    for p in CANONICAL_PROJECT_IDS:
        if p not in out:
            out[p] = {
                "unknown": True,
                "accepted_outcomes": None,
                "task_count": None,
                "commit_count": None,
                "review_count": None,
            }
            gaps.append("missing")
    if saw_unsupported:
        gaps.append("unsupported_project")
        if UNATTRIBUTED_PROJECT_ID not in out:
            out[UNATTRIBUTED_PROJECT_ID] = {
                "unknown": True,
                "accepted_outcomes": None,
                "task_count": None,
                "commit_count": None,
                "review_count": None,
            }
    unknown_all = all(v.get("unknown") for v in out.values())
    coverage = "unknown" if unknown_all else ("partial" if any(v.get("unknown") for v in out.values()) else "observed")
    for code in _producer_incomplete(report):
        if code not in gaps:
            gaps.append(code)
        if coverage == "observed":
            coverage = "partial"
    return {
        "unknown": unknown_all,
        "accepted_outcomes": None if unknown_all else total_f,
        "task_count": None if unknown_all else total_t,
        "commit_count": None if unknown_all else total_c,
        "review_count": None if unknown_all else total_r,
        "projects": out,
    }, coverage, gaps


def _producer_incomplete(report: Optional[Dict[str, Any]]) -> List[str]:
    codes: List[str] = []
    if not isinstance(report, dict):
        return codes
    gaps = report.get("coverage_gaps")
    if isinstance(gaps, list) and gaps:
        codes.append("partial_coverage")
    for key in (
        "records_missing_provenance",
        "records_missing_timestamp",
        "records_missing_response_id",
        "invalid_records",
    ):
        val = report.get(key)
        if isinstance(val, bool) or val is None:
            continue
        try:
            if int(val) > 0:
                codes.append("partial_coverage")
                break
        except (TypeError, ValueError):
            continue
    return codes


def _supplied_as_of(report: Dict[str, Any]) -> datetime.datetime:
    dt = parse_utc(report.get("as_of"))
    if dt is None:
        raise MetricsError("missing_as_of")
    window_start_raw, window_end_raw = report.get("window_start"), report.get("window_end")
    if window_start_raw is not None or window_end_raw is not None:
        window_start, window_end = parse_utc(window_start_raw), parse_utc(window_end_raw)
        if window_start is None or window_end is None:
            raise MetricsError("invalid_window")
        if window_start != dt - datetime.timedelta(hours=24) or window_end != dt:
            raise MetricsError("window_mismatch")
    return dt


def _choose_as_of(*reports: Optional[Dict[str, Any]]) -> datetime.datetime:
    stamps = []
    for report in reports:
        if report is None:
            continue
        if not isinstance(report, dict):
            raise MetricsError("invalid_report")
        stamps.append(_supplied_as_of(report))
    if not stamps:
        raise MetricsError("missing_as_of")
    first = stamps[0]
    for other in stamps[1:]:
        if other != first:
            raise MetricsError("as_of_mismatch")
    return first


def project_snapshot(
    hourly: Optional[Dict[str, Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
    features: Optional[Dict[str, Any]] = None,
    host_id: Optional[str] = None,
    request_key: Optional[str] = None,
) -> Dict[str, Any]:
    as_of = _choose_as_of(hourly, usage, features)
    util, util_cov, util_gaps = _project_utilization(hourly, as_of)
    use, use_cov, use_gaps = _project_usage(usage)
    acc, acc_cov, acc_gaps = _project_accepted(features)
    codes: List[str] = []
    for code in util_gaps + use_gaps + acc_gaps:
        if code in GAP_CODES and code not in codes:
            codes.append(code)
    snapshot: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "as_of": format_utc(as_of),
        "request_key": request_key or "",
        "coverage": {
            "utilization": util_cov if util_cov in COVERAGE_ENUM else "unknown",
            "usage": use_cov if use_cov in COVERAGE_ENUM else "unknown",
            "accepted": acc_cov if acc_cov in COVERAGE_ENUM else "unknown",
        },
        "gaps": [{"code": c} for c in codes[:16]],
        "utilization": util,
        "usage": use,
        "accepted": acc,
    }
    if host_id:
        snapshot["host_id"] = host_id
    if not snapshot["request_key"]:
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        snapshot["request_key"] = f"metrics:{format_utc(as_of)}:{digest}"
    if not REQUEST_KEY_RE.match(snapshot["request_key"]):
        raise MetricsError("invalid_request_key")
    body = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise MetricsError("snapshot_too_large")
    if _scan_private(snapshot):
        raise MetricsError("private_field")
    return validate_snapshot_payload(snapshot, host_id=host_id)


def snapshot_from_files(
    hourly_path: Optional[str] = None,
    usage_path: Optional[str] = None,
    features_path: Optional[str] = None,
    combined_path: Optional[str] = None,
    host_id: Optional[str] = None,
    request_key: Optional[str] = None,
) -> Dict[str, Any]:
    hourly = usage = features = None
    if combined_path:
        combined = load_json_file(combined_path)
        if not isinstance(combined, dict):
            raise MetricsError("invalid_report_json")
        hourly = combined.get("hourly") if isinstance(combined.get("hourly"), dict) else combined.get("utilization")
        usage = combined.get("usage")
        features = combined.get("features") if isinstance(combined.get("features"), dict) else combined.get("accepted")
        if hourly is None and usage is None and features is None:
            hourly = combined
    if hourly_path:
        loaded = load_json_file(hourly_path)
        if not isinstance(loaded, dict):
            raise MetricsError("invalid_report_json")
        hourly = loaded
    if usage_path:
        loaded = load_json_file(usage_path)
        if not isinstance(loaded, dict):
            raise MetricsError("invalid_report_json")
        usage = loaded
    if features_path:
        loaded = load_json_file(features_path)
        if not isinstance(loaded, dict):
            raise MetricsError("invalid_report_json")
        features = loaded
    return project_snapshot(hourly, usage, features, host_id=host_id, request_key=request_key)


def _resolve_origin_and_token(base_url: Optional[str], token: Optional[str]) -> Tuple[str, str]:
    cfg = config.load_config()
    cfg_base = cfg.get("base_url")
    cfg_tok = cfg.get("access_token")
    if base_url:
        clean = base_url.strip().rstrip("/")
    else:
        if not isinstance(cfg_base, str) or not cfg_base.strip():
            raise MetricsError("missing_base_url")
        clean = cfg_base.strip().rstrip("/")
    api.check_url(clean)
    if token is None:
        if not isinstance(cfg_tok, str) or not cfg_tok.strip():
            raise MetricsError("missing_token")
        access = cfg_tok
    else:
        if not isinstance(token, str) or not token.strip():
            raise MetricsError("missing_token")
        access = token
    return clean, access


def validate_snapshot_payload(snapshot: Any, host_id: Optional[str] = None) -> Dict[str, Any]:
    """Strict Hub DTO check used by projection and POST. Never logs contents."""
    if not isinstance(snapshot, dict):
        raise MetricsError("invalid_snapshot")
    if _scan_private(snapshot):
        raise MetricsError("private_field")
    if not set(snapshot.keys()).issubset(_TOP_KEYS):
        raise MetricsError("unknown_field")
    for required in ("schema_version", "as_of", "request_key", "coverage", "gaps",
                     "utilization", "usage", "accepted"):
        if required not in snapshot:
            raise MetricsError("invalid_snapshot")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise MetricsError("invalid_schema_version")
    request_key = snapshot.get("request_key")
    if not isinstance(request_key, str) or not REQUEST_KEY_RE.match(request_key):
        raise MetricsError("invalid_request_key")
    if host_id is not None and snapshot.get("host_id") not in (None, host_id):
        raise MetricsError("host_id_mismatch")
    # R5 R3: explicit null host identity is universally different from omitted
    # host identity on this host-bound receiver (Worker 403 host_id_mismatch).
    # Reject before config/HTTP; omission stays valid (credential binding).
    if "host_id" in snapshot and snapshot["host_id"] is None:
        raise MetricsError("invalid_snapshot")
    if "host_id" in snapshot and snapshot.get("host_id") is not None and not isinstance(snapshot.get("host_id"), str):
        raise MetricsError("invalid_snapshot")
    as_of = parse_utc(snapshot.get("as_of"))
    if as_of is None:
        raise MetricsError("invalid_as_of")
    coverage = snapshot.get("coverage")
    if not isinstance(coverage, dict) or set(coverage.keys()) != _COVERAGE_KEYS:
        raise MetricsError("invalid_coverage")
    for key in ("utilization", "usage", "accepted"):
        if coverage.get(key) not in COVERAGE_ENUM:
            raise MetricsError("invalid_coverage")
    gaps = snapshot.get("gaps")
    if not isinstance(gaps, list) or len(gaps) > 16:
        raise MetricsError("invalid_gaps")
    for gap in gaps:
        if not isinstance(gap, dict) or set(gap.keys()) != _GAP_KEYS or gap.get("code") not in GAP_CODES:
            raise MetricsError("invalid_gaps")
    util_body = snapshot.get("utilization")
    if not isinstance(util_body, dict) or set(util_body.keys()) != _UTIL_KEYS:
        raise MetricsError("unknown_field")
    if type(util_body.get("unknown")) is not bool:
        raise MetricsError("invalid_snapshot")
    if not isinstance(util_body.get("projects"), dict):
        raise MetricsError("invalid_snapshot")
    _strict_validate_project_map(util_body.get("projects"), as_of, "utilization")
    usage_body = snapshot.get("usage")
    if not isinstance(usage_body, dict) or set(usage_body.keys()) != _USAGE_KEYS:
        raise MetricsError("unknown_field")
    if type(usage_body.get("unknown")) is not bool:
        raise MetricsError("invalid_snapshot")
    if not isinstance(usage_body.get("projects"), dict):
        raise MetricsError("invalid_snapshot")
    _strict_validate_project_map(usage_body.get("projects"), as_of, "usage")
    accepted_body = snapshot.get("accepted")
    if not isinstance(accepted_body, dict) or not set(accepted_body.keys()).issubset(_ACCEPTED_KEYS):
        raise MetricsError("unknown_field")
    for required in ("unknown", "accepted_outcomes", "task_count", "commit_count", "review_count"):
        if required not in accepted_body:
            raise MetricsError("invalid_snapshot")
    if type(accepted_body.get("unknown")) is not bool:
        raise MetricsError("invalid_snapshot")
    try:
        acc_outcomes = _finite_nn(accepted_body.get("accepted_outcomes"), True)
        acc_tasks = _finite_nn(accepted_body.get("task_count"), True)
        acc_commits = _finite_nn(accepted_body.get("commit_count"), True)
        acc_reviews = _finite_nn(accepted_body.get("review_count"), True)
    except MetricsError:
        raise MetricsError("invalid_count") from None
    acc_unknown = bool(accepted_body.get("unknown"))
    if acc_unknown and (acc_outcomes is not None or acc_tasks is not None or acc_commits is not None or acc_reviews is not None):
        raise MetricsError("invalid_snapshot")
    # Top-level unknown=False may still carry null review_count (partial coverage
    # without agreed review-event identity); only unknown=True forces all nulls.
    accepted_projects = accepted_body.get("projects", {})
    if not isinstance(accepted_projects, dict):
        raise MetricsError("invalid_snapshot")
    _strict_validate_project_map(accepted_projects, as_of, "accepted")
    encoded = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise MetricsError("snapshot_too_large")
    return snapshot


def post_metrics_snapshot(
    snapshot: Dict[str, Any],
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise MetricsError("invalid_snapshot")
    validate_snapshot_payload(snapshot)
    clean_base, access_token = _resolve_origin_and_token(base_url, token)
    body = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise MetricsError("snapshot_too_large")
    url = f"{clean_base}/api/metrics"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        return api.safe_http_json(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP error {exc.code} during metrics post") from None
    except urllib.error.URLError:
        raise RuntimeError("Network error during metrics post") from None
    except Exception:
        raise RuntimeError("Metrics POST failed") from None


def get_metrics_snapshot(
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    clean_base, access_token = _resolve_origin_and_token(base_url, token)
    url = f"{clean_base}/api/metrics"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        return api.safe_http_json(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP error {exc.code} during metrics get") from None
    except urllib.error.URLError:
        raise RuntimeError("Network error during metrics get") from None
    except Exception:
        raise RuntimeError("Metrics GET failed") from None


def add_commands(sub):
    """Metrics-only CLI seam (owned by platform-metrics-live)."""
    parser = sub.add_parser("metrics", help="Post or read sanitized 24h operational metrics (JSON)")
    msub = parser.add_subparsers(dest="metrics_cmd", required=True)
    post = msub.add_parser("post", help="Project allowlisted Dashboard reports and POST /api/metrics")
    post.add_argument("--hourly", default=None, help="Existing Dashboard hourly JSON report file")
    post.add_argument("--usage", default=None, help="Existing Dashboard usage JSON report file")
    post.add_argument("--features", default=None, help="Existing Dashboard accepted-features JSON report file")
    post.add_argument("--from-json", dest="from_json", default=None, help="Combined already-computed local report JSON")
    post.add_argument("--key", default=None, help="Stable idempotency key; reuse after a lost response")
    post.add_argument("--dry-run", action="store_true", help="Print projected snapshot JSON without posting")
    msub.add_parser("get", help="GET this host's sanitized metrics snapshot")


def execute(args):
    import json as _json
    from . import config as _config
    if getattr(args, "metrics_cmd", None) == "get":
        return get_metrics_snapshot()
    if not (args.hourly or args.usage or args.features or args.from_json):
        raise MetricsError("Metrics post requires --hourly/--usage/--features or --from-json.")
    cfg = _config.load_config()
    host_id = cfg.get("machine_id") if isinstance(cfg, dict) else None
    snap = snapshot_from_files(
        hourly_path=args.hourly,
        usage_path=args.usage,
        features_path=args.features,
        combined_path=args.from_json,
        host_id=host_id if isinstance(host_id, str) else None,
        request_key=args.key,
    )
    if args.dry_run:
        print(_json.dumps(snap, indent=2, sort_keys=True))
        return snap
    return post_metrics_snapshot(snap)
