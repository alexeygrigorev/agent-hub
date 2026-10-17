"""Retain the last explicitly commanded capture, private and bounded per incarnation.

This is historical output, never native exit proof or cleanup authority. Callers
must separately prove retirement and current custody before using it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from . import private_fs
from .native_sessions import NativeTarget, SessionControlError

LIMIT = 65536
META_LIMIT = 4096


def _binding(target: NativeTarget, machine_id: str) -> dict:
    target = target.validated()
    if type(target.created_at_ms) is not int or target.created_at_ms <= 0:
        raise SessionControlError("Capture retention requires native incarnation")
    if not isinstance(machine_id, str) or not machine_id or len(machine_id) > 128:
        raise SessionControlError("Capture retention requires host identity")
    return {"native_id": target.session_id, "created_at_ms": target.created_at_ms,
            "tag": target.tag, "workspace": target.workspace,
            "engine": target.engine, "machine_id": machine_id}


def _paths(journal_dir: str, binding: dict):
    directory = Path(journal_dir)
    if not directory.is_absolute():
        raise SessionControlError("Capture journal must be absolute")
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    stem = directory / ("commanded-capture-" + key)
    return str(stem) + ".json", str(stem) + ".bin", str(stem) + ".lock"


def retain_capture(journal_dir, target, machine_id, command_id, capture):
    binding = _binding(target, machine_id)
    text = capture.get("text")
    if not isinstance(text, str):
        raise SessionControlError("Capture retention requires actual text")
    if not text:
        return False  # No manufactured empty-history evidence.
    if not isinstance(command_id, str) or not command_id or len(command_id) > 128:
        raise SessionControlError("Capture retention requires command identity")
    mode = capture.get("mode", "tail")
    if mode not in ("tail", "screen"):
        raise SessionControlError("Invalid retained capture mode")
    encoded = text.encode("utf-8", "replace")
    raw = encoded[:LIMIT]
    record = {"version": 1, "binding": binding, "command_id": command_id,
              "captured_at_ms": int(time.time() * 1000), "mode": mode,
              "sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw),
              "truncated": bool(capture.get("truncated")) or len(encoded) > LIMIT}
    meta, payload, lock = _paths(journal_dir, binding)
    with private_fs.exclusive_lock(lock):
        private_fs.atomic_write(payload, raw, LIMIT)
        private_fs.atomic_write(meta, json.dumps(record).encode(), META_LIMIT)
    return True


def read_retained_capture(journal_dir, target, machine_id, finished_at_ms):
    binding = _binding(target, machine_id)
    if type(finished_at_ms) is not int:
        raise SessionControlError("Retired capture requires native finish timestamp")
    meta, payload, lock = _paths(journal_dir, binding)
    try:
        with private_fs.exclusive_lock(lock):
            record = json.loads(private_fs.read_bytes(meta, META_LIMIT))
            raw = private_fs.read_bytes(payload, LIMIT)
    except (OSError, ValueError) as exc:
        raise SessionControlError("Authentic commanded capture unavailable") from exc
    at = record.get("captured_at_ms") if isinstance(record, dict) else None
    if (not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1
            or record.get("binding") != binding or type(at) is not int
            or not binding["created_at_ms"] <= at <= finished_at_ms <= int(time.time() * 1000)
            or not isinstance(record.get("command_id"), str) or not record["command_id"]
            or record.get("mode") not in ("tail", "screen") or type(record.get("truncated")) is not bool
            or type(record.get("byte_length")) is not int or record["byte_length"] != len(raw)
            or not raw or record.get("sha256") != hashlib.sha256(raw).hexdigest()):
        raise SessionControlError("Retained capture provenance or digest mismatch")
    return raw, record
