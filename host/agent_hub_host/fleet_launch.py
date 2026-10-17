"""Fleet OpenCode Muse-brief launch helper (platform-muse-brief slice).

Exact source/config contract for head integration. No native effects on
import; all launches go through the maintained launcher CLI (aplexer
backend for real UUID/first-action evidence). No second dispatcher or
controller is created here.
"""
from __future__ import annotations

import json
from pathlib import Path

OPENCODE_PROVIDER = "opencode"
OPENCODE_MODEL = "opencode-go/muse-spark-1.3-contributor"
OPENCODE_BIN = "/home/alexey/.nvm/versions/node/v24.13.1/bin/opencode"
OPENCODE_ARGV_PREFIX = [OPENCODE_BIN, "run", "--pure", "--auto",
                        "--model", OPENCODE_MODEL]


def opencode_subscription(account_label, *, engine="opencode", profile=None,
                          admission_argv, launcher_argv, launcher_root,
                          launcher_config_dir):
    """Build the exact Hub fleet subscription dict for a Muse brief.

    admission_argv is the maintained fresh gate; launcher_* is the maintained
    launcher CLI (argv prefix, checkout root, config dir). Model is pinned;
    callers cannot substitute a paid/free alternative.
    """
    if not account_label or not admission_argv or not launcher_argv:
        raise ValueError("account_label, admission_argv and launcher_argv are required")
    return {
        "service": "go",
        "account_label": account_label,
        "engine": engine,
        "profile": profile,
        "admission_argv": list(admission_argv),
        "launcher_provider": OPENCODE_PROVIDER,
        "launcher_model": OPENCODE_MODEL,
        "launcher": {"argv": list(launcher_argv), "root": launcher_root,
                     "config_dir": launcher_config_dir},
    }


def opencode_offer(offer_id, workspace_label, prompt, *, engine="opencode",
                   profile=None, account, cwd=None, task_id=None):
    """Build a Fleet offer dict for an arbitrary Muse brief.

    prompt is passed through verbatim as a separate argv element downstream
    (launcher build_adapter_argv appends it; no shell interpolation anywhere).
    """
    if not offer_id or not workspace_label or prompt is None:
        raise ValueError("offer_id, workspace_label and prompt are required")
    offer = {"id": offer_id, "engine": engine, "profile": profile,
             "account": account, "mode": "headless",
             "workspace": workspace_label, "prompt": str(prompt)}
    if cwd is not None:
        offer["cwd"] = cwd
    if task_id is not None:
        offer["task_id"] = task_id
    return offer


def launcher_payload(target_cwd, goal, account_label, profile=None,
                     timeout=1800):
    """Exact maintained-launcher submit payload for an opencode headless run."""
    return {"goal": goal, "owner": "hub-fleet", "cwd": str(target_cwd),
            "timeout": timeout,
            "model_requirements": {"providers": [OPENCODE_PROVIDER],
                                   "models": [OPENCODE_MODEL]},
            "account_label": account_label, "profile": profile}


def launcher_run_argv(launcher_argv, config_dir, task_id, target_cwd,
                      workspace):
    """Exact maintained-launcher run argv for opencode (aplexer backend).

    Real native UUID + first-action evidence; task-units (unit_name) is never
    used for opencode because it cannot bind/control M12 sessions.
    """
    prefix = list(launcher_argv) + ["--config-dir", str(config_dir)]
    return prefix + ["run", "--backend", "aplexer",
                     "--id", task_id, "--cwd", str(target_cwd),
                     "--tmpdir", str(Path(workspace) / ".local" / "tmp")]


def validate_native_receipt(receipt):
    """Validate a launcher aplexer-backend JSON receipt (no fakes).

    Returns the native UUID string. Raises ValueError on anything incomplete.
    """
    details = (receipt or {}).get("receipt", {})
    native_id = details.get("session_id")
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("No confirmed native execution receipt")
    if details.get("state") != "running":
        raise ValueError("Native lifecycle receipt is incomplete")
    if details.get("first_action_verified") is not True:
        raise ValueError("Native lifecycle receipt is incomplete")
    if details.get("engine") != "opencode":
        raise ValueError("Native engine receipt mismatch (must be opencode)")
    return native_id
