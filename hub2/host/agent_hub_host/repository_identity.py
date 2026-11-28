"""Bounded, local-only inspection of a checkout's configured Git origin.

This intentionally does not invoke Git or read user/global Git configuration.
Only the checkout's direct config and the bounded worktree metadata pointers are
observed; include/includeIf directives are never followed.
"""
import hashlib
import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

QUALIFIED_PREFIX = 'repo-'
QUALIFIED_WORKSPACE = re.compile(r'repo-[0-9a-f]{59}')
REPOSITORY_IDENTITY_CAPABILITY = 'repo-identity:v1'
# Bounded to cover registered checkouts with many local branch entries.
MAX_GIT_METADATA_BYTES = 16 * 1024
MAX_CHECKOUT_CANDIDATES = 2048
MAX_ROOT_ENTRIES = 4096

_SCP_ORIGIN = re.compile(r'^(?:[^@/:\s]+@)?([^/:?#\s]+):([^?#]+)$')
_PERCENT_ESCAPE = re.compile(r'%(?![0-9A-Fa-f]{2})')
_SECTION = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)"|\.([A-Za-z0-9._-]+))?\s*\]\s*(?:[#;].*)?$')


def _decode_path(raw_path):
    if not isinstance(raw_path, str) or not raw_path or _PERCENT_ESCAPE.search(raw_path):
        return None
    if re.search(r'%(?:2f|5c)', raw_path, re.IGNORECASE):
        return None
    parts = raw_path.strip('/').split('/')
    if len(parts) < 2 or any(not part for part in parts):
        return None
    decoded = []
    for part in parts:
        try:
            value = unquote(part, encoding='utf-8', errors='strict')
        except (UnicodeDecodeError, ValueError):
            return None
        if (value in {'.', '..'} or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or any(char.isspace() for char in value) or any(char in '/\\?#' for char in value)):
            return None
        decoded.append(value)
    if decoded[-1].lower().endswith('.git'):
        decoded[-1] = decoded[-1][:-4]
    if not decoded[-1]:
        return None
    return decoded


def normalize_git_origin(value):
    """Return a credential-free, scheme-independent repository origin key."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value.encode('utf-8')) > 2048:
            return None
    except UnicodeError:
        return None
    raw = value.strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None
    host = port = raw_path = None
    if '://' not in raw:
        match = _SCP_ORIGIN.fullmatch(raw)
        if not match:
            return None
        host, raw_path = match.groups()
        scheme = 'ssh'
    else:
        try:
            parsed = urlsplit(raw)
            if parsed.scheme.lower() not in {'http', 'https', 'ssh', 'git+ssh'} or parsed.query or parsed.fragment:
                return None
            scheme = 'ssh' if parsed.scheme.lower() == 'git+ssh' else parsed.scheme.lower()
            host = parsed.hostname
            port = parsed.port
            raw_path = parsed.path
        except (ValueError, UnicodeError):
            return None
    if not host:
        return None
    try:
        host = host.encode('idna').decode('ascii').lower().rstrip('.')
    except (UnicodeError, ValueError):
        return None
    if not host or any(char.isspace() for char in host) or any(char in '/\\?#@' for char in host):
        return None
    default_port = {'http': 80, 'https': 443, 'ssh': 22}[scheme]
    if port == default_port:
        port = None
    if isinstance(port, bool) or (port is not None and not 1 <= port <= 65535):
        return None
    parts = _decode_path(raw_path)
    if not parts:
        return None
    if host == 'github.com':
        parts = [part.lower() for part in parts]
    authority = f'[{host}]' if ':' in host and not host.startswith('[') else host
    if port is not None:
        authority += ':' + str(port)
    return authority + '/' + '/'.join(parts)


def workspace_token_from_origin(normalized_origin):
    if not isinstance(normalized_origin, str) or not normalized_origin:
        return None
    digest = hashlib.sha256(normalized_origin.encode('utf-8')).hexdigest()
    return QUALIFIED_PREFIX + digest[:59]


def qualified_workspace(value):
    origin = normalize_git_origin(value)
    return workspace_token_from_origin(origin) if origin else None


def _read_regular(path):
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return None
    except (OSError, RuntimeError):
        return None
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0))
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(descriptor)
        after = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino)
                or info.st_size > MAX_GIT_METADATA_BYTES):
            return None
        data = bytearray()
        while len(data) <= MAX_GIT_METADATA_BYTES:
            chunk = os.read(descriptor, min(1024, MAX_GIT_METADATA_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_GIT_METADATA_BYTES:
            return None
        return bytes(data).decode('utf-8', 'strict')
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(descriptor)


def _inside(path, roots):
    return any(path.is_relative_to(root) for root in roots)


def _metadata_directory(path, roots):
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return None
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if _inside(resolved, roots) else None


def _single_pointer(path, base, roots):
    text = _read_regular(path)
    if text is None or len(text.splitlines()) != 1:
        return None
    value = text.strip()
    if not value or '\x00' in value or '\n' in value or '\r' in value:
        return None
    target = Path(value)
    if not target.is_absolute():
        target = base / target
    return _metadata_directory(target, roots)


def _unquote_git_value(value):
    value = value.strip()
    if not value:
        return None
    if value.startswith('"'):
        result = []
        escaped = False
        closed = False
        index = 1
        while index < len(value):
            char = value[index]
            if escaped:
                translated = {'n': '\n', 't': '\t', 'b': '\b', '"': '"', '\\': '\\'}.get(char)
                if translated is None:
                    return None
                result.append(translated)
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                closed = True
                index += 1
                break
            else:
                result.append(char)
            index += 1
        if not closed or escaped:
            return None
        tail = value[index:].strip()
        if tail and not tail.startswith(('#', ';')):
            return None
        return ''.join(result)
    # Git comments begin only when the marker follows whitespace.
    return re.split(r'\s[;#]', value, maxsplit=1)[0].strip()


def _origin_values(config_text):
    """Return supported remote.origin.url values, or None for unsafe syntax.

    Includes are deliberately unsupported: even an otherwise direct origin can
    be changed by include/includeIf, and following arbitrary include paths is
    outside this bounded metadata reader.
    """
    section = None
    urls = []
    for line in config_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', ';')):
            continue
        header = _SECTION.fullmatch(line)
        if header:
            name, quoted_subsection, dotted_subsection = header.groups()
            section = (name.casefold(), quoted_subsection or dotted_subsection)
            if section[0] in {'include', 'includeif'}:
                return None
            continue
        if stripped.startswith('['):
            return None
        if section != ('remote', 'origin'):
            continue
        setting = re.match(r'^\s*([A-Za-z][A-Za-z0-9.-]*)\s*=\s*(.*?)\s*$', line)
        if setting and setting.group(1).casefold() == 'url':
            raw_url = setting.group(2)
            url = '' if not raw_url else _unquote_git_value(raw_url)
            if url is None:
                return None
            urls.append(url)
    return urls


def _worktree_config_enabled(config_text):
    """Read the one Git boolean that controls config.worktree loading."""
    section = None
    values = []
    for line in config_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', ';')):
            continue
        header = _SECTION.fullmatch(line)
        if header:
            name, quoted_subsection, dotted_subsection = header.groups()
            section = (name.casefold(), quoted_subsection or dotted_subsection)
            if section[0] in {'include', 'includeif'}:
                return None
            continue
        if stripped.startswith('['):
            return None
        if section != ('extensions', None):
            continue
        setting = re.match(r'^\s*([A-Za-z][A-Za-z0-9.-]*)\s*=\s*(.*?)\s*$', line)
        if setting and setting.group(1).casefold() == 'worktreeconfig':
            value = _unquote_git_value(setting.group(2))
            if value is None:
                return None
            values.append(value.casefold())
    if not values:
        return False
    if len(values) != 1:
        return None
    if values[0] in {'true', 'yes', 'on', '1'}:
        return True
    if values[0] in {'false', 'no', 'off', '0'}:
        return False
    return None


def read_git_origin(checkout, roots):
    """Read a checkout origin without invoking Git or following arbitrary paths."""
    try:
        checkout = Path(checkout).resolve(strict=True)
        roots = tuple(Path(root).resolve(strict=True) for root in roots)
        if not checkout.is_dir() or not _inside(checkout, roots):
            return None
        marker = checkout / '.git'
        marker_info = marker.lstat()
    except (OSError, RuntimeError):
        return None

    if stat.S_ISDIR(marker_info.st_mode):
        gitdir = _metadata_directory(marker, roots)
    elif stat.S_ISREG(marker_info.st_mode):
        # The .git file uses a required "gitdir: " prefix; parse it separately
        # so arbitrary text cannot be treated as a filesystem path.
        content = _read_regular(marker)
        match = re.fullmatch(r'\s*gitdir:\s*(\S+)\s*', content or '')
        if not match:
            return None
        raw_gitdir = Path(match.group(1))
        if not raw_gitdir.is_absolute():
            raw_gitdir = checkout / raw_gitdir
        gitdir = _metadata_directory(raw_gitdir, roots)
    else:
        # Symlinks and special files are never Git metadata roots.
        return None
    if gitdir is None:
        return None

    common_marker = gitdir / 'commondir'
    try:
        common_info = common_marker.lstat()
    except FileNotFoundError:
        common = gitdir
    except OSError:
        return None
    else:
        if not stat.S_ISREG(common_info.st_mode):
            return None
        common = _single_pointer(common_marker, gitdir, roots)
        if common is None:
            return None

    common_config = common / 'config'
    try:
        if not stat.S_ISREG(common_config.lstat().st_mode):
            return None
    except OSError:
        return None
    common_text = _read_regular(common_config)
    if common_text is None:
        return None
    configs = [common_text]
    worktree_config = gitdir / 'config.worktree'
    try:
        worktree_info = worktree_config.lstat()
    except FileNotFoundError:
        worktree_info = None
    except OSError:
        return None
    if worktree_info is not None:
        worktree_enabled = _worktree_config_enabled(common_text)
        if worktree_enabled is None:
            return None
        if not worktree_enabled:
            worktree_info = None  # Git ignores this file without the extension.
    if worktree_info is not None:
        if not stat.S_ISREG(worktree_info.st_mode):
            return None
        worktree_text = _read_regular(worktree_config)
        if worktree_text is None:
            return None
        configs.append(worktree_text)

    urls = []
    for config in configs:
        values = _origin_values(config)
        if values is None:
            return None
        for value in values:
            # Empty or multiple remote.origin.url entries have layer-dependent
            # Git precedence; fail closed as unknown rather than guessing.
            if not value:
                return None
            urls.append(value)
    if len(urls) != 1:
        return None
    return normalize_git_origin(urls[0])


def canonical_repo_basename(normalized_origin):
    if not isinstance(normalized_origin, str) or '/' not in normalized_origin:
        return None
    return normalized_origin.rsplit('/', 1)[-1]
