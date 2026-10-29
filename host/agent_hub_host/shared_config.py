"""Hub-managed shared config bundle (non-secret, versioned in shared-config/).

Syncs the repo's shared-config/ bundle into the Hub-owned per-host path
~/.config/agent-hub/shared/ (Windows: %APPDATA%/agent-hub/shared/), so managed
engine homes (which isolate HOME) can still reach Hub-owned config such as the
AWS Gate shared config via AWS_CONFIG_FILE.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

SHARED_DIR_NAME = 'shared'
MANIFEST_NAME = 'manifest.json'

# Versioned bundle entries (relative to shared-config/). To add a folder:
# add files under shared-config/<tool>/, list them in manifest.json, and
# extend this tuple (see shared-config/README.md).
SHARED_CONFIG_FILES = ('aws/config',)


def _host_config_base() -> Path:
    # A host credential override may live in a runtime journal. Shared config
    # belongs to the host config root, independently of that credential file.
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not set on Windows")
        return Path(appdata) / "agent-hub"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agent-hub"


def dest_dir() -> Path:
    return _host_config_base() / SHARED_DIR_NAME


def hub_aws_config_file() -> Path:
    return dest_dir() / 'aws' / 'config'


def hub_aws_credentials_file(home) -> Path:
    """Nonexistent credentials file inside the managed home.

    Points at the standard <home>/.aws/credentials location, which the Hub
    never creates, so per-machine static keys (e.g. main-temp) stay
    unreachable from managed workers.
    """
    return Path(home) / '.aws' / 'credentials'


def _candidate_sources():
    cands = []
    override = os.environ.get('HUB_SHARED_CONFIG_SOURCE')
    if override:
        cands.append(Path(override).expanduser())
    try:
        # Inline release-root form so test_release_completeness can scan it.
        cands.append(Path(__file__).resolve().parents[2] / 'shared-config')
    except Exception:
        pass
    return cands


def source_dir() -> Path | None:
    for cand in _candidate_sources():
        try:
            if cand.is_dir() and (cand / MANIFEST_NAME).is_file():
                return cand
        except Exception:
            continue
    return None


def manifest_files(src: Path) -> list[str]:
    try:
        data = json.loads((src / MANIFEST_NAME).read_text(encoding='utf-8'))
        files = data.get('files')
        if isinstance(files, list) and all(isinstance(f, str) for f in files):
            return [f for f in files if f in SHARED_CONFIG_FILES]
    except Exception:
        pass
    return [f for f in SHARED_CONFIG_FILES if (src / f).is_file()]


def _embedded_files():
    """Bundle embedded in hub.pyz at build time (install-only machines)."""
    try:
        from . import _shared_bundle
        files = getattr(_shared_bundle, 'FILES', None)
        if isinstance(files, dict) and files:
            return {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        pass
    return None


def bundle_texts(source=None):
    """Return {relative_path: text} from override, checkout/release, or pyz embed."""
    if source:
        src = Path(source).expanduser()
        if not src.is_dir():
            raise ValueError('shared config source bundle missing')
        files = manifest_files(src)
        if not files:
            raise ValueError('shared config manifest lists no known files')
        return {rel: (src / rel).read_text(encoding='utf-8') for rel in files}
    src = source_dir()
    if src is not None:
        files = manifest_files(src)
        if files:
            return {rel: (src / rel).read_text(encoding='utf-8') for rel in files}
    embedded = _embedded_files()
    if embedded:
        manifest = None
        try:
            manifest = json.loads(embedded.get('manifest.json', '{}'))
        except Exception:
            manifest = None
        wanted = None
        if isinstance(manifest, dict):
            listed = manifest.get('files')
            if isinstance(listed, list):
                wanted = [f for f in listed if f in SHARED_CONFIG_FILES and f in embedded]
        texts = {rel: embedded[rel] for rel in (wanted or [])}
        if not texts:
            texts = {rel: embedded[rel] for rel in SHARED_CONFIG_FILES if rel in embedded}
        if texts:
            return texts
    raise ValueError('shared config source bundle missing')


def _resolve_hub_bin(explicit=None) -> str:
    if explicit:
        return str(explicit)
    override = os.environ.get('HUB_BIN')
    if override:
        return override
    found = shutil.which('hub')
    return found or 'hub'


def _render_aws_config(text: str, hub_bin: str) -> str:
    """Render the bundled aws/config with a concrete hub binary path.

    The versioned bundle ships `credential_process = hub aws-gate
    credential-process` (PATH-based, portable). At sync time the Hub-owned
    copy pins the discovered absolute hub path so managed workers do not
    depend on PATH. Values are never secret.
    """
    prog = hub_bin.strip() or 'hub'
    if prog != 'hub' and (' ' in prog or '\t' in prog):
        prog = '"%s"' % prog.replace('"', '')
    elif os.name != 'nt' and prog != 'hub':
        prog = shlex.quote(prog)
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('credential_process') and '=' in line:
            key, _ = line.split('=', 1)
            line = '%s= %s aws-gate credential-process' % (key.rstrip(), prog)
        out_lines.append(line)
    rendered = '\n'.join(out_lines)
    if text.endswith('\n'):
        rendered += '\n'
    return rendered


def sync_shared_config(source=None, dest=None, hub_bin=None, dry_run=False):
    """Copy the versioned bundle into the Hub-owned dest (idempotent).

    Returns {'status': ..., 'dest': ..., 'files': [...]}. Never copies
    secrets: bundle files must not contain static AWS keys (fail closed).
    Up-to-date files are left untouched so heartbeat ticks stay cheap.
    """
    texts = bundle_texts(source=source)
    dst = Path(dest).expanduser() if dest else dest_dir()
    hub_prog = _resolve_hub_bin(hub_bin)
    written = []
    for rel, raw in texts.items():
        if '..' in rel.split('/') or not rel or rel.startswith('/'):
            raise ValueError('unsafe shared config entry')
        if len(raw) > 65536:
            raise ValueError('shared config file unexpectedly large')
        lowered = raw.lower()
        if 'aws_access_key_id' in lowered or 'aws_secret_access_key' in lowered:
            raise ValueError('shared config bundle must not contain static keys')
        if 'akia' in lowered:
            raise ValueError('shared config bundle must not contain static keys')
        if rel == 'aws/config':
            raw = _render_aws_config(raw, hub_prog)
        target = dst / rel
        written.append(str(target))
        if dry_run:
            continue
        try:
            if target.is_file() and target.read_text(encoding='utf-8') == raw:
                continue
        except Exception:
            pass
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            try:
                os.chmod(target.parent, 0o700)
            except OSError:
                pass
        from . import private_fs
        private_fs.atomic_write(target, raw.encode('utf-8'), 65536)
        if os.name != 'nt':
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
    return {'status': 'synced' if not dry_run else 'dry-run', 'dest': str(dst), 'files': written}


def add_commands(sub):
    parser = sub.add_parser('shared-config', help='Sync versioned Hub-owned shared config')
    cmds = parser.add_subparsers(dest='shared_config_cmd', required=True)
    sync_p = cmds.add_parser('sync', help='Copy shared-config/ bundle to the Hub-owned path')
    sync_p.add_argument('--source', default=None)
    sync_p.add_argument('--dest', default=None)
    sync_p.add_argument('--hub-bin', default=None)
    sync_p.add_argument('--dry-run', action='store_true')


def command(args):
    if args.shared_config_cmd == 'sync':
        return sync_shared_config(source=args.source, dest=args.dest, hub_bin=args.hub_bin, dry_run=args.dry_run)
    raise ValueError('unknown shared-config command')
