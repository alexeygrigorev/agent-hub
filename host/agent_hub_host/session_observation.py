"""Bounded model metadata from the existing native transcript binding.

Never read profile credentials or infer a provider default. Only the model
identifier leaves the host; transcript contents and paths stay local.
"""
import json
import re
import time

from .model_contract import native_model

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,159}\Z")
_SCAN_BYTES = 256 * 1024
_CACHE = {}


def _model(value):
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def native_session_model(row, locator=None):
    if row.get('engine') == 'shell':
        return 'not_applicable'
    published = _model(row.get('model')) or _model(native_model(row.get('command')))
    if published:
        return published
    if (type(row.get('created_at_ms')) is not int or row['created_at_ms'] <= 0
            or not isinstance(row.get('id'), str)):
        return None
    from .native_progress import NativeTranscriptProgress
    locator = locator or NativeTranscriptProgress()
    try:
        path = locator.locate(row)
        if path is None:
            return None
        stat = path.stat()
        key = (row['id'], row['created_at_ms'], stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if key in _CACHE:
            return _CACHE[key]
        deadline = time.monotonic() + .05
        latest = None
        with path.open('rb') as stream:
            if stat.st_size > _SCAN_BYTES:
                stream.seek(-_SCAN_BYTES, 2)
                stream.readline(_SCAN_BYTES)
            raw = stream.read(_SCAN_BYTES)
        for line in raw.splitlines():
            if not line:
                continue
            if time.monotonic() > deadline:
                return None
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get('payload')
            candidate = None
            if event.get('type') in ('turn_context', 'session_meta') and isinstance(payload, dict):
                candidate = _model(payload.get('model'))
            elif event.get('type') == 'assistant' and isinstance(event.get('message'), dict):
                candidate = _model(event['message'].get('model'))
            if candidate:
                latest = candidate
        if len(_CACHE) >= 500:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = latest
        return latest
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None
