"""Read Claude's native /goal receipt from an explicitly named conversation.

Claude persists goal_status attachments when installing/evaluating its Stop
hook. A matching user prompt or screen text alone does not prove registration.
"""
import json
import os
import re
import stat
import time
import uuid
from datetime import datetime
from pathlib import Path


def conversation_argv(argv, task_id, operation_id, epoch):
    """Pin a fresh conversation per launch attempt, independent of workspace."""
    identity = json.dumps([task_id, operation_id, epoch], separators=(',', ':'))
    thread = str(uuid.uuid5(uuid.NAMESPACE_URL, 'hub2-claude-goal:' + identity))
    return [*argv, '--session-id', thread]


def conversation_id(command):
    """Read only an unambiguous, canonical explicit conversation selector."""
    if not isinstance(command, list) or command.count('--session-id') != 1:
        return None
    try:
        thread = command[command.index('--session-id') + 1]
        return thread if str(uuid.UUID(thread)) == thread else None
    except (ValueError, TypeError, AttributeError, IndexError):
        return None


def goal_store(row, config_dir, expected_thread, objective_hash):
    """Read only the immutable launch-pinned conversation, never a status replacement."""
    if not expected_thread or conversation_id(row.get('command')) != expected_thread:
        return None
    thread = expected_thread
    try:
        workspace = row['workspace']
        created = row['created_at_ms']
        if not Path(workspace).is_absolute() or type(created) is not int or created <= 0:
            return None
        root = Path(config_dir)
        if not root.is_absolute():
            return None
        path = root / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', workspace) / (thread + '.jsonl')
        if path.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > 8 * 1024 * 1024:
                return None
            raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                return None
            proof = None
            for line in raw.splitlines():
                if len(line) > 512 * 1024:
                    return None
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # A concurrent writer may not have flushed its last line.
                if not isinstance(event, dict):
                    continue
                state = event.get('attachment')
                if (event.get('type') != 'attachment' or not isinstance(state, dict)
                        or state.get('type') != 'goal_status' or event.get('isSidechain') is not False
                        or event.get('cwd') != workspace or event.get('sessionId') != thread):
                    continue
                timestamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
                if timestamp.tzinfo is None or not created - 1000 <= timestamp.timestamp() * 1000 <= time.time() * 1000 + 5000:
                    continue
                condition = state.get('condition')
                if not isinstance(condition, str) or not condition.strip() or type(state.get('met')) is not bool:
                    continue
                status = ('blocked' if state.get('failed') is True else
                          'paused' if state['met'] and state.get('sentinel') is True else
                          'complete' if state['met'] else 'active')
                proof = {'status': status, 'thread_id': thread, 'objective_hash': objective_hash(condition)}
            return proof
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None


def approve_external_includes(config_dir, workspace):
    """Pre-accept Claude's "Allow external CLAUDE.md imports?" dialog for a workspace.

    Hub worktrees live under the repo checkout, so an ancestor CLAUDE.md that
    imports @AGENTS.md is an external import. The dialog swallows the /goal
    send and the session dies of SIGHUP (exit 129). Best effort: unreadable or
    non-object config is left untouched.
    """
    try:
        path = (Path(config_dir) / '.claude.json') if config_dir else (Path.home() / '.claude.json')
        try:
            config = json.loads(path.read_text('utf-8'))
        except FileNotFoundError:
            config = {}
        projects = config.setdefault('projects', {}) if isinstance(config, dict) else None
        if not isinstance(projects, dict):
            return False
        entry = projects.setdefault(str(workspace), {})
        if not isinstance(entry, dict):
            return False
        if entry.get('hasClaudeMdExternalIncludesApproved') is True:
            return True
        entry['hasClaudeMdExternalIncludesApproved'] = True
        temp = path.with_name(path.name + '.hub-%d.tmp' % os.getpid())
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, indent=2)
        os.replace(temp, path)
        return True
    except (OSError, ValueError):
        return False
