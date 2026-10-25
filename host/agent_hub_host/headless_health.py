"""Bounded first-turn health checks for exact host-owned launcher executions."""
from __future__ import annotations
import json
import re
from pathlib import Path

REASON = 'engine_tool_permission_refused'
MESSAGE = 'Headless engine refused write or network tools; route held until a successful execution probe.'


def permission_refusal(text):
    """Inspect engine events/errors, never echoed user goals or command arguments.

    ZCodex omits failed tool events from JSON output; its agent_message first-turn
    refusal report is also evidence. Require a specific permission-client failure,
    not a generic discussion of sandbox modes or the name of a failing test.
    """
    for line in str(text)[-131072:].splitlines():
        line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        try:
            event = json.loads(line)
        except ValueError:
            if re.search(r'^(?:[0-9]{4}-[0-9T:.+Z-]+\s+)?ERROR\s+codex_core::tools::router\b.*unsupported call: (?:Write|Bash)\b', line):
                return True
            if re.fullmatch(r'(?:Error: |PermissionError: )?No permission client configured(?: for (?:Bash|Write))?[.!]?', line):
                return True
            continue
        if not isinstance(event, dict):
            continue
        item = event.get('item')
        if event.get('type') != 'item.completed' or not isinstance(item, dict):
            continue
        if item.get('type') == 'agent_message':
            message = item.get('text', '')
            if not isinstance(message, str):
                continue
            # Do not accept a review, quoted task, or suggested test as a refusal.
            if re.search(r'(?:acceptance criteria|Task:|Prompt:|test_|fixture|for example)', message, re.I):
                continue
            if ('No permission client configured' in message and
                    re.search(r'(?:refused|blocked|failed|cannot|can.t|error)', message, re.I)):
                return True
        elif item.get('type') == 'command_execution' and item.get('exit_code') not in (None, 0):
            if re.search(r'No permission client configured(?: for (?:Bash|Write))?', str(item.get('aggregated_output', ''))):
                return True
    return False


def failure():
    return dict(reason=REASON, message=MESSAGE, error_type='PermissionError', stage='dispatch_headless')


def launch_target(workspace, task_id, engine):
    """Only the launch's own start receipt can authorize capture or cleanup."""
    from . import private_fs
    from .native_sessions import NativeTarget
    workspace = Path(workspace).resolve(strict=True)
    if not re.fullmatch(r'hub-[a-f0-9-]{36}', task_id):
        raise ValueError('Invalid owned task')
    path = workspace / '.local' / f'launch-{task_id}-start.json'
    doc = json.loads(private_fs.read_bytes(path, 65536, private=False))
    if isinstance(doc.get('wrapper_start_json'), dict):
        doc = doc['wrapper_start_json']
    if (doc.get('tag') != 'task-' + task_id or doc.get('workspace') != str(workspace)
            or doc.get('engine') != engine or type(doc.get('created_at_ms')) is not int):
        raise ValueError('Owned start identity mismatch')
    return NativeTarget(session_id=doc['id'], tag=doc['tag'], workspace=str(workspace),
                        engine=engine, created_at_ms=doc['created_at_ms']).validated()


def inspect(executable, target):
    from .native_sessions import capture_output
    return failure() if permission_refusal(capture_output(executable, target, limit_bytes=131072).text) else None
