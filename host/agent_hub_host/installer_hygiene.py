"""Installer-only environment hygiene (2026-11-02 Windows rollout defects).

Pure helpers so they run in tests on Windows and Linux: dead local proxies,
stale release PATH entries and transient file locks must not fail an install.
"""
import os
import re
import socket
import time
from urllib.parse import urlsplit

PROXY_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
              'npm_config_proxy', 'npm_config_https_proxy', 'NPM_CONFIG_PROXY', 'NPM_CONFIG_HTTPS_PROXY')
LOOPBACK = ('127.0.0.1', 'localhost', '::1', '[::1]')


def _listening(host, port):
    try:
        with socket.create_connection((host.strip('[]'), port), timeout=1):
            return True
    except OSError:
        return False


def dead_local_proxy(value, probe=_listening):
    """True when value points at a loopback port with nothing listening."""
    try:
        parts = urlsplit(value if '://' in value else 'http://' + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if not host or host.lower() not in [h.strip('[]') for h in LOOPBACK] or not port:
        return False
    return not probe(host, port)


def drop_dead_local_proxies(environ=None, probe=_listening):
    """Remove proxy variables aimed at a dead loopback port; returns the dropped names.

    A live local proxy is left alone. Called by the installer only, so a proxy
    that starts later still serves processes that were not started by it.
    """
    environ = os.environ if environ is None else environ
    dropped = []
    for name in PROXY_VARS:
        value = environ.get(name)
        if value and dead_local_proxy(value, probe):
            del environ[name]
            dropped.append(name)
    return dropped


_BS = re.escape(chr(92))
_SEP = '[/' + _BS + ']'
_RELEASE = re.compile('(?:^|' + _SEP + ')agent-hub' + _SEP + 'releases' + _SEP + '([^/' + _BS + ']+)(?:' + _SEP + '|$)', re.IGNORECASE)

_STANDALONE_APLEXER = re.compile('(?:^|' + _SEP + ')Programs' + _SEP + 'aplexer(?:' + _SEP + '|$)', re.IGNORECASE)


def prune_stale_release_paths(entries, current_release):
    """Drop PATH entries inside agent-hub/releases/<other>; keep the current release and unrelated entries."""
    keep = []
    for entry in entries:
        if _STANDALONE_APLEXER.search(entry or ''):
            continue  # old standalone aplexer (a 0.1.10) shadows the release's own `a`
        match = _RELEASE.search(entry or '')
        if match and match.group(1).lower() != str(current_release).lower():
            continue
        keep.append(entry)
    return keep


def retry_transient(action, *, attempts=5, base_delay=0.5, sleep=time.sleep, errors=(OSError,)):
    """Run action(); retry transient errors with exponential backoff, then re-raise the last one."""
    for index in range(attempts):
        try:
            return action()
        except errors:
            if index + 1 == attempts:
                raise
            sleep(base_delay * 2 ** index)
