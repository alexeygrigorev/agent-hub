"""Keep Codex/ZCode hook trust in step with a rewritten hooks.json.

A regenerated hooks.json otherwise makes the engine show "hook needs review"
and block the first prompt until someone presses `t` (zai-central desktop
sessions, intake 5814e109). The engine stores the approved hash of every hook
handler in config.toml under [hooks.state."<hooks.json>:<event>:<group>:<handler>"].
"""
import hashlib
import json
import re
from pathlib import Path

from . import private_fs

EVENTS = {'PreToolUse': 'pre_tool_use', 'PostToolUse': 'post_tool_use', 'SessionStart': 'session_start',
          'UserPromptSubmit': 'user_prompt_submit', 'Stop': 'stop', 'PermissionRequest': 'permission_request',
          'PreCompact': 'pre_compact', 'PostCompact': 'post_compact'}

_HEADER = re.compile(r'^\[hooks\.state\.("(?:[^"\\]|\\.)*")\]$')


def entries(hooks_path, hooks_doc):
    """{state key: 'sha256:<hex>'} computed as the engine does.

    Verified against hashes the engine itself stored after `t` was pressed: sha256 of the
    sorted-key compact JSON {event_name (snake_case), [matcher], hooks: [{type, command,
    timeout (default 600), async}]} for each handler."""
    found = {}
    events = hooks_doc.get('hooks') if isinstance(hooks_doc, dict) else None
    if not isinstance(events, dict):
        return found
    for name, groups in events.items():
        event = EVENTS.get(name)
        if event is None or not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for hi, handler in enumerate(handlers):
                if (not isinstance(handler, dict) or handler.get('type', 'command') != 'command'
                        or not isinstance(handler.get('command'), str)):
                    continue
                identity = {'event_name': event, 'hooks': [{
                    'type': 'command', 'command': handler['command'],
                    'timeout': handler.get('timeout', 600), 'async': bool(handler.get('async', False))}]}
                if isinstance(group.get('matcher'), str) and group['matcher']:
                    identity['matcher'] = group['matcher']
                digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
                found['%s:%s:%d:%d' % (hooks_path, event, gi, hi)] = 'sha256:' + digest
    return found


def render(current, hooks_path, hooks_doc):
    """Replace trust tables for one generated hooks file in a TOML document."""
    trusted = entries(str(hooks_path), hooks_doc)
    prefix = str(hooks_path) + ':'
    kept = []
    skipping = False
    removed = False
    for line in current.splitlines():
        header = _HEADER.match(line.strip())
        if header:
            try:
                skipping = json.loads(header.group(1)).startswith(prefix)
                removed = removed or skipping
            except ValueError:
                skipping = False
        elif line.lstrip().startswith('['):
            skipping = False
        if not skipping:
            kept.append(line)
    if not trusted and not removed:
        return current
    text = '\n'.join(kept).rstrip('\n')
    for key, value in sorted(trusted.items()):
        text += '\n\n[hooks.state.%s]\ntrusted_hash = %s' % (json.dumps(key), json.dumps(value))
    text = text.lstrip('\n')
    return text + '\n' if text else ''


def refresh(config_path, hooks_path, hooks_doc):
    """Rewrite the [hooks.state] tables of hooks_path in config.toml."""
    config_path = Path(config_path)
    try:
        current = config_path.read_text(encoding='utf-8')
    except OSError:
        current = ''
    text = render(current, hooks_path, hooks_doc)
    if text == current:
        return False
    with private_fs.parent(config_path, create=True):
        pass
    private_fs.atomic_write(config_path, text.encode('utf-8'), 262144, engine_owned=True)
    return True
