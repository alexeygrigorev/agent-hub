"""Unprofiled native engines must not send bare account_label to launcher."""

from __future__ import annotations

from pathlib import Path


def test_unprofiled_headless_payload_omits_bare_account_label():
    src = Path(__file__).resolve().parents[1] / "agent_hub_host" / "fleet.py"
    text = src.read_text()
    # Regression lock: the old one-liner caused launcher named-profile rejection.
    bad = '"account_label": sub["account_label"], "profile": "model-task"'
    assert bad not in text
    assert "must omit account_label" in text
    # Profiled path still sets both fields together.
    assert 'payload["account_label"] = sub["account_label"]' in text
    assert 'payload["account_profile"] = profile' in text
