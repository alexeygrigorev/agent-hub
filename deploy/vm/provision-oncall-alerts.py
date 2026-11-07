#!/usr/bin/env python3
"""Provision the one-time oncall token pair and emit it encrypted to stdin's RSA key."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import argparse
import re
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BOOTSTRAP_ENV = Path("/opt/agent-hub/.dev.vars")
CONFIG_URL = "http://127.0.0.1:8787/api/oncall-alerts/config"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
ONCALL_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")
TOKEN_SIZE = 64  # Hub2 randomToken(32), lowercase hexadecimal.


def bootstrap_token(env_path: Path = BOOTSTRAP_ENV) -> str:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("Hub bootstrap configuration is unavailable") from exc
    matches = [line.partition("=")[2] for line in lines if line.partition("=")[0] == "HUB2_BOOTSTRAP_TOKEN"]
    if len(matches) != 1 or not TOKEN_RE.fullmatch(matches[0]):
        raise RuntimeError("Hub bootstrap configuration is invalid")
    return matches[0]


def token_bundle(bootstrap: str, opener: Callable | None = None, rotate: bool = False) -> bytes:
    opener = opener or urlopen
    body = b'{"rotate":true}' if rotate else b"{}"
    request = Request(
        CONFIG_URL,
        data=body,
        headers={"Authorization": f"Bearer {bootstrap}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=10) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"Hub token provisioning returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Hub token provisioning request failed") from None
    if status != 201:
        raise RuntimeError(f"Hub token provisioning returned HTTP {status}")
    try:
        result = json.loads(body)
    except (ValueError, TypeError):
        raise RuntimeError("Hub token provisioning response is invalid") from None
    if not isinstance(result, dict) or result.get("configured") is not True:
        raise RuntimeError("Hub token provisioning response is invalid")
    tokens = {key: result.get(key) for key in ("ingest_token", "poll_token")}
    if any(not isinstance(value, str) or not ONCALL_TOKEN_RE.fullmatch(value) for value in tokens.values()):
        raise RuntimeError("Hub token provisioning response is invalid")
    if any(len(value) != TOKEN_SIZE for value in tokens.values()):
        raise RuntimeError("Hub token provisioning response has an unexpected token size")
    return json.dumps(tokens, separators=(",", ":")).encode("ascii")


def encrypt_for_recipient(payload: bytes, public_key: bytes) -> bytes:
    if len(public_key) > 8192 or b"-----BEGIN PUBLIC KEY-----" not in public_key:
        raise ValueError("recipient must provide a PEM RSA public key")
    with tempfile.TemporaryDirectory(prefix="oncall-token-envelope-") as temp_dir:
        key_path = Path(temp_dir) / "recipient.pub"
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(public_key)
        result = subprocess.run(
            [
                "openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(key_path),
                "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
                "-pkeyopt", "rsa_mgf1_md:sha256",
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    if result.returncode != 0:
        raise ValueError("recipient key cannot encrypt the oncall token bundle")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="replace a configured token pair; use only to recover a lost encrypted artifact",
    )
    args = parser.parse_args()
    public_key = sys.stdin.buffer.read(8193)
    if not public_key or len(public_key) > 8192:
        raise RuntimeError("recipient public key is missing or too large")

    # Validate the recipient and RSA-OAEP payload limit before the one-time POST.
    dummy_payload = json.dumps(
        {"ingest_token": "1" * TOKEN_SIZE, "poll_token": "2" * TOKEN_SIZE}, separators=(",", ":")
    ).encode("ascii")
    encrypt_for_recipient(dummy_payload, public_key)

    bundle = token_bundle(bootstrap_token(), rotate=args.rotate)
    encrypted = encrypt_for_recipient(bundle, public_key)
    sys.stdout.write(base64.b64encode(encrypted).decode("ascii") + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
