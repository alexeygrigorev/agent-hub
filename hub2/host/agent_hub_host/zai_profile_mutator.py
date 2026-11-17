"""Pure opt-in prototype; no file access or executable identity authority."""
import copy
import json
import math
from pathlib import PurePosixPath
import re
import tomllib

MAX_CONFIG_BYTES = 262144
MAX_PROFILE_BYTES = 256
MAX_SDK_PATH_BYTES = 4096
MAX_CANDIDATES = 128
_VALUE = re.compile(
    r"(?m)^[ \t]*(?:executable|\"executable\"|'executable')[ \t]*=[ \t]*"
    r"(?P<value>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*')"
    r"[ \t]*(?:#[^\r\n]*)?\r?$"
)
_HEADER = re.compile(r"(?m)^[ \t]*\[(?!\[)[^\r\n]*\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)")


class SelectorRefused(ValueError):
    """Only fixed refusal codes; never include configuration or parser text."""


def _refuse(code):
    raise SelectorRefused(code) from None


def _same(left, right):
    # Type-exact equality: Python bool/int equality must not hide drift.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and math.isnan(left):
        return math.isnan(right)
    return left == right


def select_profile_executable(config_bytes, profile_id, sdk_path):
    """Return one proven same-profile edit; caller owns all custody/trust checks.

    Only POSIX absolute SDK path strings are supported. This does not read the
    path, follow links, prove its terminal binary, or authorize activation.
    Single-line executable values and explicit table-header insertion are the
    intentionally narrow text styles. Unsupported input refuses opt-in only.
    """
    if type(config_bytes) is not bytes or not 0 < len(config_bytes) <= MAX_CONFIG_BYTES:
        _refuse("config_bounds")
    for value, limit, code in ((profile_id, MAX_PROFILE_BYTES, "profile_id"),
                                (sdk_path, MAX_SDK_PATH_BYTES, "sdk_path")):
        if type(value) is not str or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            _refuse(code)
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            _refuse(code)
        if len(encoded) > limit:
            _refuse(code)
    if not PurePosixPath(sdk_path).is_absolute():
        _refuse("sdk_path")
    try:
        text = config_bytes.decode("utf-8")
        before = tomllib.loads(text)
    except (UnicodeError, ValueError, RecursionError):
        _refuse("invalid_toml")
    profiles = before.get("profiles")
    if type(profiles) is not dict or type(profiles.get(profile_id)) is not dict:
        _refuse("profile_missing")
    profile = profiles[profile_id]
    if "command" in profile:
        _refuse("command_profile")
    if "executable" in profile and type(profile["executable"]) is not str:
        _refuse("unsupported_executable")
    if profile.get("executable") == sdk_path:
        _refuse("no_change")
    try:
        expected = copy.deepcopy(before)
    except RecursionError:
        _refuse("unsupported_nesting")
    expected["profiles"][profile_id]["executable"] = sdk_path
    literal = json.dumps(sdk_path, ensure_ascii=False)
    edits = []
    if "executable" in profile:
        for match in _VALUE.finditer(text):
            edits.append((*match.span("value"), literal))
    else:
        for match in _HEADER.finditer(text):
            header = match.group(0)
            newline = "\r\n" if header.endswith("\r\n") else "\n"
            prefix = "" if header.endswith("\n") else newline
            edits.append((match.end(), match.end(), prefix + "executable = " + literal + newline))
    if not edits or len(edits) > MAX_CANDIDATES:
        _refuse("candidate_bounds")
    accepted = None
    count = 0
    for start, end, replacement in edits:
        candidate = text[:start] + replacement + text[end:]
        try:
            parsed = tomllib.loads(candidate)
        except (ValueError, RecursionError):
            continue
        try:
            equivalent = _same(parsed, expected)
        except RecursionError:
            equivalent = False
        if equivalent:
            count += 1
            accepted = candidate
    if count != 1:
        _refuse("edit_not_unique_or_unsupported")
    return accepted.encode("utf-8")
