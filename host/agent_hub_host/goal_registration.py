"""Submit Codex-family goals through the native TUI and verify the goal store.

Codex's interactive CLI treats a slash command passed as its initial prompt
argument as ordinary model text. Start the TUI empty, deliver the command
through aplexer's guarded message transport, then verify the exact rollout's
thread_goals row before declaring the launch ready.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from .native_progress import GOAL_STATUSES, NativeTranscriptProgress, stored_goal_status


GOAL_REGISTRATION_TIMEOUT_S = 60.0
_DELIVERY_RETRY_STATUSES = frozenset({'not-ready', 'busy', 'draft', 'menu', 'unknown'})
_DELIVERED_STATUSES = frozenset({'submitted', 'already-submitted', 'recipient-acked'})


class GoalRegistrationError(RuntimeError):
    """The TUI command or its authoritative thread_goals receipt was not proven."""

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
    """Deliver one ``/goal`` envelope and wait for its exact SQLite row.

    Readiness refusals retry the same durable envelope with capped exponential
    backoff. An ambiguous send or delivery is never repeated. Store polling
    reads only the rollout thread's goal row and shares the same 60 s deadline.
    """
    if (engine not in ('codex', 'zcodex') or not isinstance(goal_command, str)
            or not goal_command.startswith('/goal ') or not goal_command[6:].strip()
            or timeout_s <= 0):
        raise GoalRegistrationError()

    progress = progress or NativeTranscriptProgress()
    deadline = monotonic() + min(float(timeout_s), GOAL_REGISTRATION_TIMEOUT_S)
    envelope_id = None
    row = None
    delivered = False
    retry_delay = 0.25

    def call(argv: list[str], **kwargs: Any) -> Any:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GoalRegistrationError()
        # run_json otherwise routes catalog reads through its in-process
        # aplexer client, which has no per-call timeout. An explicit child
        # environment selects the CLI transport, where the remaining deadline
        # is enforced by run_json. Message effects already use that CLI path.
        return runner(argv, timeout=max(0.1, min(3.0, remaining)),
                      env=os.environ.copy(), **kwargs)

    while monotonic() < deadline:
        remaining = deadline - monotonic()
        if row is None:
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
                sleep(min(retry_delay, max(0.0, remaining)))
                retry_delay = min(3.0, retry_delay * 2)
                continue

        if envelope_id is None:
            try:
                receipt = call([
                    executable, 'message', 'send', '--to', tag,
                    '--workspace', workspace, '--kind', 'hub-session-goal', '--json', goal_command,
                ], cwd=workspace)
            except Exception as exc:
                raise GoalRegistrationError() from exc
            envelope_id = receipt.get('id') if isinstance(receipt, dict) else None
            if not isinstance(envelope_id, str) or not envelope_id or len(envelope_id) > 128:
                raise GoalRegistrationError()

        if not delivered:
            # Requalify the exact native tuple before every guarded TUI effect.
            try:
                current = _exact_row(
                    call([executable, 'list', '--all', '--json']),
                    native_id=native_id, tag=tag, workspace=workspace, engine=engine,
                    expected_created_at_ms=expected_created_at_ms)
                if current is None:
                    raise GoalRegistrationError()
                delivery = call([
                    executable, 'message', 'deliver', '--workspace', workspace,
                    '--json', envelope_id,
                ], cwd=workspace)
            except GoalRegistrationError:
                raise
            except Exception as exc:
                raise GoalRegistrationError() from exc
            status = delivery.get('status') if isinstance(delivery, dict) else None
            if status in _DELIVERED_STATUSES:
                delivered = True
                retry_delay = 0.25
            elif status in _DELIVERY_RETRY_STATUSES:
                sleep(min(retry_delay, max(0.0, remaining)))
                retry_delay = min(3.0, retry_delay * 2)
                continue
            else:
                raise GoalRegistrationError()

        try:
            registered = _registered_goal(row, progress)
        except Exception:
            registered = None
        if registered:
            return registered
        sleep(min(0.5, max(0.0, deadline - monotonic())))

    raise GoalRegistrationError()
