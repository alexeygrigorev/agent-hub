"""Register Codex-family goals through the native interactive TUI.

Codex treats ``/goal`` passed as its initial prompt argument as ordinary model
text. Start the TUI empty, submit the command once through Aplexer's guarded
interactive-input path, and verify the exact rollout's ``thread_goals`` row
before declaring the launch ready.

The ``send --enter`` call is deliberately limited to this feature: goal
registration is a native slash command, not Hub mailbox messaging. Aplexer
uses Codex's bracketed-paste input for this multiline command. Its receipt must
prove a ready composer, landed text, and submission. Ambiguous outcomes are
never retried.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from .native_progress import GOAL_STATUSES, NativeTranscriptProgress, stored_goal_status


GOAL_REGISTRATION_TIMEOUT_S = 60.0
_TUI_READY_TIMEOUT_S = 25
_TUI_SEND_TIMEOUT_S = 30.0
_CONFIRMED_SEND_STATUSES = frozenset({'submitted', 'delivered_working'})


class GoalRegistrationError(RuntimeError):
    """The native TUI or its authoritative thread_goals receipt was not proven."""

    reason = 'goal_registration_failed'

    def __init__(self):
        super().__init__(self.reason)


def _rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get('sessions')
    else:
        rows = None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _exact_row(raw: Any, *, native_id: str, tag: str, workspace: str, engine: str,
               expected_created_at_ms: Optional[int]) -> Optional[dict[str, Any]]:
    tagged = [row for row in _rows(raw) if row.get('tag') == tag and row.get('workspace') == workspace]
    if not tagged:
        # A matching tag in another workspace or an absent tag is not a target.
        return None
    if len(tagged) != 1:
        raise GoalRegistrationError()
    row = tagged[0]
    if row.get('id') != native_id or row.get('engine') != engine:
        raise GoalRegistrationError()
    created = row.get('created_at_ms', row.get('created_ms'))
    if type(created) is not int or created <= 0:
        raise GoalRegistrationError()
    if expected_created_at_ms is not None and created != expected_created_at_ms:
        raise GoalRegistrationError()
    if row.get('phase', row.get('state')) in ('exited', 'dead', 'pruned', 'forgotten', 'stopped'):
        raise GoalRegistrationError()
    return row


def _registered_goal(row: dict[str, Any], progress: Any) -> Optional[dict[str, str]]:
    path = progress.locate(row)
    if path is None:
        return None
    status, thread_id, _read_ok = stored_goal_status(path)
    if status in GOAL_STATUSES and isinstance(thread_id, str) and thread_id:
        return {'status': status, 'thread_id': thread_id}
    return None


def _verified_delivery(receipt: Any, *, native_id: str, engine: str) -> bool:
    if not isinstance(receipt, dict) or receipt.get('id') != native_id:
        return False
    delivery = receipt.get('delivery')
    if not isinstance(delivery, dict):
        return False
    sends = delivery.get('sends')
    enters = delivery.get('enter_presses')
    return (
        delivery.get('engine') == engine
        and delivery.get('ready') is True
        and delivery.get('landed') is True
        and delivery.get('submitted') is True
        and delivery.get('status') in _CONFIRMED_SEND_STATUSES
        and type(sends) is int and sends == 1
        and type(enters) is int and 1 <= enters <= 3
    )


def register_goal(
    executable: str,
    *,
    native_id: str,
    tag: str,
    workspace: str,
    engine: str,
    goal_command: str,
    runner: Callable[..., Any],
    expected_created_at_ms: Optional[int] = None,
    timeout_s: float = GOAL_REGISTRATION_TIMEOUT_S,
    progress: Any = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Submit one native ``/goal`` command and verify its exact SQLite row.

    The ready-composer/send receipt and store lookup share one 60-second
    deadline. There is exactly one text submission: unclear delivery, a
    missing receipt, or a missing goal row fails closed rather than resending.
    """
    if (engine not in ('codex', 'zcodex') or not isinstance(goal_command, str)
            or not goal_command.startswith('/goal ') or not goal_command[6:].strip()
            or timeout_s <= 0):
        raise GoalRegistrationError()

    progress = progress or NativeTranscriptProgress()
    deadline = monotonic() + min(float(timeout_s), GOAL_REGISTRATION_TIMEOUT_S)

    def call(argv: list[str], *, call_timeout: float = 3.0, **kwargs: Any) -> Any:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GoalRegistrationError()
        # run_json otherwise routes list through its in-process Aplexer client,
        # which has no per-call timeout. An explicit child environment selects
        # the CLI transport so each native operation stays inside the deadline.
        return runner(argv, timeout=max(0.1, min(call_timeout, remaining)),
                      env=os.environ.copy(), **kwargs)

    retry_delay = 0.25
    row = None
    while row is None and monotonic() < deadline:
        try:
            row = _exact_row(
                call([executable, 'list', '--all', '--json']),
                native_id=native_id, tag=tag, workspace=workspace, engine=engine,
                expected_created_at_ms=expected_created_at_ms)
        except GoalRegistrationError:
            raise
        except Exception:
            row = None
        if row is None:
            sleep(min(retry_delay, max(0.0, deadline - monotonic())))
            retry_delay = min(3.0, retry_delay * 2)
    if row is None:
        raise GoalRegistrationError()

    # A thread can only belong to this just-started native id, and checking
    # first avoids duplicate slash commands if a caller reconciles a prior run.
    try:
        registered = _registered_goal(row, progress)
    except Exception:
        registered = None
    if registered:
        return registered

    # Requalify the exact session tuple immediately before the one TUI effect.
    current = _exact_row(
        call([executable, 'list', '--all', '--json']),
        native_id=native_id, tag=tag, workspace=workspace, engine=engine,
        expected_created_at_ms=expected_created_at_ms)
    if current is None:
        raise GoalRegistrationError()

    try:
        receipt = call([
            executable, 'send', native_id, goal_command,
            '--enter', '--json', '--wait-ack', '0', '--resends', '0',
            '--ready-timeout', str(_TUI_READY_TIMEOUT_S),
        ], call_timeout=_TUI_SEND_TIMEOUT_S, cwd=workspace)
    except Exception as exc:
        # The command may have reached the TUI. Its effects are ambiguous, so
        # the caller fails/cleans up once; this helper never retries the send.
        raise GoalRegistrationError() from exc
    if not _verified_delivery(receipt, native_id=native_id, engine=engine):
        raise GoalRegistrationError()

    retry_delay = 0.25
    while monotonic() < deadline:
        try:
            registered = _registered_goal(current, progress)
        except Exception:
            registered = None
        if registered:
            return registered
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(0.5, remaining))

    raise GoalRegistrationError()
