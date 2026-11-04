"""Versioned non-secret shared-config bundle, synced to every Hub host (0043dadb).

The bundle lives in the agent-hub repo under ``shared-config/<folder>/``; the
Hub installer/updater path (``provisioning.sync``, called on startup and each
machine heartbeat) materializes every registered folder into the Hub-owned
shared root ``<agent-hub config dir>/shared/<folder>/``. Managed sessions run
with a private HOME, so a machine's own ``~/.aws/config`` is invisible by
design: the bundle is the single source for shared tool config, and it never
carries secrets (no static keys).

The first folder is AWS: profile ``default`` authenticates through AWS Gate
via ``credential_process``. The template holds a ``{aws_gate_credential_process}``
placeholder that each host substitutes from its own ``aws_gate_credential_process``
host setting; a host without that setting skips the folder (fail closed) rather
than installing a default profile that cannot authenticate. Adding a folder is
one repo file plus one ``SYNC_FOLDERS`` entry (see shared-config/README.md).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from . import config, private_fs

STATE_FILE = 'config-state.json'
MAX_FILE = 65536

SYNC_FOLDERS = {
    'aws': {'placeholder': 'aws_gate_credential_process'},
}


def shared_root():
    """Hub-owned shared root beside the host config (never inside a repo)."""
    return Path(config.get_config_path()).parent / 'shared'


def aws_config_path():
    try:
        return shared_root() / 'aws' / 'config'
    except (OSError, RuntimeError, ValueError):
        return None


def aws_credentials_path(home):
    """Managed-home static-key path; the host never writes it, so lookups fail closed."""
    return Path(home) / '.aws' / 'credentials'


def _sync_folder(name, spec, repo_root, settings, shared, dry_run=False):
    """Sync one folder; returns a non-secret per-folder status dict."""
    source = Path(repo_root) / 'shared-config' / name
    if not source.is_dir():
        return {'folder': name, 'status': 'missing_source'}
    setting_key = spec.get('placeholder')
    substitution = None
    if setting_key:
        substitution = settings.get(setting_key) if isinstance(settings, dict) else None
        if not isinstance(substitution, str) or not substitution.strip():
            return {'folder': name, 'status': 'skipped_setting_missing', 'setting': setting_key}
    files = {}
    for path in sorted(source.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        if len(rel) > 200 or any(part in ('..',) for part in path.relative_to(source).parts):
            return {'folder': name, 'status': 'invalid_layout'}
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            return {'folder': name, 'status': 'unreadable_source'}
        if len(text) > MAX_FILE:
            return {'folder': name, 'status': 'source_too_large'}
        files[rel] = text.replace('{' + setting_key + '}', substitution) if setting_key else text
    if not files:
        return {'folder': name, 'status': 'empty_source'}
    if dry_run:
        return {'folder': name, 'status': 'dry_run', 'files': sorted(files)}
    for rel, text in files.items():
        target = shared / name / rel
        data = text.encode('utf-8')
        try:
            if not (target.is_file() and private_fs.read_bytes(target, MAX_FILE, private=False) == data):
                private_fs.atomic_write(target, data, MAX_FILE, engine_owned=True)
                if os.name != 'nt':
                    try:
                        os.chmod(target, 0o600 if rel.endswith(('id_ed25519', 'credentials')) else 0o644)
                    except OSError:
                        pass
        except PermissionError:
            return {'folder': name, 'status': 'write_refused'}
        except OSError:
            return {'folder': name, 'status': 'write_failed'}
    return {'folder': name, 'status': 'synced', 'files': sorted(files)}


def sync_shared_config(repo_root=None, settings=None, dry_run=False):
    """Materialize every registered shared-config folder; never raises for content.

    ``repo_root`` defaults to the host checkout this module ships in; ``settings``
    defaults to the host's stored settings. The result carries folder statuses
    and a version digest over the synced bytes; it never carries file content.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
    if settings is None:
        from .fleet import load_settings
        try:
            settings = load_settings()
        except (OSError, RuntimeError, ValueError):
            settings = {}
    try:
        shared = shared_root()
    except (OSError, RuntimeError, ValueError):
        return {'status': 'no_shared_root', 'folders': [], 'version': None}
    folders = [_sync_folder(name, spec, repo_root, settings, shared, dry_run=dry_run) for name, spec in SYNC_FOLDERS.items()]
    synced = {f['folder']: f for f in folders if f['status'] in ('synced', 'dry_run')}
    version = None
    if not dry_run and synced:
        digest = hashlib.sha256()
        for name in sorted(synced):
            source = Path(repo_root) / 'shared-config' / name
            for path in sorted(source.rglob('*')):
                if path.is_file():
                    digest.update(str(path.relative_to(source)).encode())
                    digest.update(b'\0')
                    digest.update(path.read_bytes())
        state = {'version': digest.hexdigest(), 'folders': sorted(synced), 'synced_at': int(time.time())}
        try:
            private_fs.atomic_write(shared / STATE_FILE, json.dumps(state).encode(), MAX_FILE, engine_owned=True)
            version = digest.hexdigest()
        except OSError:
            version = None
    failed = [f for f in folders if f['status'] not in ('synced', 'dry_run')]
    return {'status': 'synced' if not failed and not dry_run else ('dry_run' if dry_run else 'partial'), 'folders': folders, 'version': version}
