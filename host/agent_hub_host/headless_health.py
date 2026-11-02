"""Bounded first-turn health checks for exact host-owned launcher executions."""
from __future__ import annotations
import json
import re
from pathlib import Path

REASON = 'engine_tool_permission_refused'
MESSAGE = 'Headless engine refused write or network tools; route held until a successful execution probe.'
PROMPT_REASON = 'engine_interactive_prompt'

# Match the short, explicit choice screens that require a person before the
# engine accepts work. Keep this separate from output/log inspection: task text
# can mention login, trust, update, or onboarding without presenting a prompt.
_PROMPT_PATTERNS = (
    ('folder trust', re.compile(
        r'(?is)(?:do you trust (?:the )?(?:contents|files) (?:of|in) this (?:folder|directory)\?'
        r'|trust this (?:folder|directory)\?)(?=.{0,300}(?:yes|no|trust and continue|select))')),
    ('login', re.compile(
        r'(?im)^\s*(?:not logged in.{0,160}(?:run\s+)?/?login|login required|'
        r'please\s+(?:log in|login|sign in)|sign in to continue|'
        r'(?:sign in|continue) with (?:chatgpt|google)|'
        r'open (?:this )?(?:url|link) to (?:log in|sign in)|authentication required)\b')),
    ('update', re.compile(
        r'(?im)^\s*(?:update available|new version available).{0,300}'
        r'(?:restart|relaunch|install|update now|press enter|yes|later)|'
        r'^\s*(?:restart|relaunch) to (?:install|apply) (?:the )?update')),
    ('first-run setup', re.compile(
        r'(?im)^\s*(?:choose|select) (?:a |your )?(?:theme|text style|login method)\b'
        r'|^\s*welcome to (?:claude code|codex|opencode|gemini cli)[.!]?\s*'
        r'(?:press enter|continue|choose|select|sign in|log in)\b')),
)

HEALTH_CAPTURE_TIMEOUT = 10.0


def interactive_prompt(text):
    """Return a bounded prompt kind only for recognizable engine choice screens."""
    if not isinstance(text, str):
        return None
    screen = text[-32768:]
    screen = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', screen)
    screen = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', screen).replace('\r', '')
    for kind, pattern in _PROMPT_PATTERNS:
        if pattern.search(screen):
            return kind
    return None


def prompt_failure(engine, kind):
    """Safe typed diagnostic: never include the screen or task text."""
    engine_name = str(engine)[:32] if isinstance(engine, str) else 'engine'
    kind = kind if kind in {'folder trust', 'login', 'update', 'first-run setup'} else 'first-run'
    return {
        'reason': PROMPT_REASON,
        'message': (f'The {engine_name} session stopped at an interactive {kind} prompt. '
                    'No human input is available; the session was stopped and capacity '
                    'will be released after exit is confirmed.'),
        'error_type': 'InteractivePromptError',
        'stage': 'dispatch_headless',
    }


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
    # Choice screens do not reliably appear in native logs. Capture a scoped
    # UUID/tag/workspace screen first while this launch is still in its startup
    # window. Bound each identity check and native call within the 60-second
    # startup health budget.
    try:
        screen = capture_output(executable, target, limit_bytes=32768, mode='screen',
                                timeout=HEALTH_CAPTURE_TIMEOUT).text
    except Exception:
        screen = ''
    kind = interactive_prompt(screen)
    if kind:
        return prompt_failure(target.engine, kind)
    try:
        output = capture_output(executable, target, limit_bytes=131072,
                                timeout=HEALTH_CAPTURE_TIMEOUT).text
    except Exception:
        return None
    return failure() if permission_refusal(output) else None
