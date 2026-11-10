#!/usr/bin/env python3
"""Maintain quota publication receipts; independently alert on stale accounts.

No quota details or command stderr are emitted or included in alerts. Only a
validated successful `hub quota publish` receipt advances publication times.
"""
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

ACCOUNTS = frozenset(('claude', 'codex', 'codex-orin', 'gemini', 'grok', 'copilot', 'shared-go', 'zai-central'))
RECIPIENT = 'dev@dtcdev.click'
MAX_STATE_BYTES = 65536
DEFAULT_STALE_SECONDS = 840  # 14m + independent 5m cadence + bounded sink < 20m


class Failure(Exception):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def now_ms():
    value = time.time()
    if not math.isfinite(value) or value <= 0:
        raise Failure('clock_invalid')
    return int(value * 1000)


def account_set(raw):
    values = raw.split(',')
    if not values or len(values) != len(set(values)) or any(value not in ACCOUNTS for value in values):
        raise argparse.ArgumentTypeError('expected unique known account labels separated by commas')
    return sorted(values)


def absolute_path(raw):
    path = Path(raw)
    if not path.is_absolute() or '\x00' in raw or any(ord(c) < 32 for c in raw):
        raise argparse.ArgumentTypeError('absolute path required')
    return path


@contextlib.contextmanager
def locked(path):
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Failure('state_directory_untrusted')
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(str(path) + '.lock', flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise Failure('state_lock_untrusted')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def empty_state():
    return {'version': 1, 'accounts': {}, 'alerted': []}


def read_state(path, now):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return empty_state()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_STATE_BYTES:
            raise Failure('state_invalid')
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            fd = None
            data = json.load(stream, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise Failure('state_invalid') from None
    finally:
        if fd is not None:
            os.close(fd)
    if (not isinstance(data, dict) or set(data) != {'version', 'accounts', 'alerted'} or type(data['version']) is not int or data['version'] != 1
            or not isinstance(data['accounts'], dict) or not isinstance(data['alerted'], list)
            or any(not isinstance(a, str) for a in data['alerted']) or len(data['alerted']) != len(set(data['alerted']))):
        raise Failure('state_invalid')
    if any(account not in ACCOUNTS or type(at) is not int or at <= 0 or at > now + 5000 for account, at in data['accounts'].items()):
        raise Failure('state_invalid')
    if any(not isinstance(account, str) or account not in ACCOUNTS for account in data['alerted']):
        raise Failure('state_invalid')
    return data


def write_state(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def receipt(raw):
    if len(raw.encode()) > MAX_STATE_BYTES:
        raise Failure('result_invalid')
    try:
        result = json.loads(raw, object_pairs_hook=unique_object)
    except ValueError:
        raise Failure('result_invalid') from None
    if not isinstance(result, dict) or set(result) != {'published', 'skipped'}:
        raise Failure('result_invalid')
    published, skipped = result['published'], result['skipped']
    if (not isinstance(published, list) or not published or not all(isinstance(a, str) and a in ACCOUNTS for a in published)
            or len(published) != len(set(published)) or not isinstance(skipped, dict)
            or any(a not in ACCOUNTS or not isinstance(reason, str) for a, reason in skipped.items())
            or set(published).intersection(skipped)):
        raise Failure('result_invalid')
    return published


def publish(args):
    # The subprocess does not own the state lock: an independent monitor remains
    # runnable if the publisher is blocked, killed or otherwise stopped.
    started = now_ms()
    try:
        result = subprocess.run([str(args.hub), 'quota', 'publish'], capture_output=True, text=True, timeout=args.publish_timeout)
    except subprocess.TimeoutExpired:
        raise Failure('cli_timeout') from None
    except UnicodeError:
        raise Failure('result_invalid') from None
    except OSError:
        raise Failure('cli_unavailable') from None
    if result.returncode:
        raise Failure('cli_failed')
    published = receipt(result.stdout)
    with locked(args.state):
        current = now_ms()
        if current < started:
            raise Failure('clock_regressed')
        data = read_state(args.state, current)
        for account in published:
            # Use invocation time: a slow or overlapping publisher cannot make
            # old evidence appear newer than it was, nor overwrite a newer receipt.
            data['accounts'][account] = max(data['accounts'].get(account, 0), started)
        data['alerted'] = [account for account in data['alerted']
                           if account not in published or current - data['accounts'][account] > args.stale_seconds * 1000]
        write_state(args.state, data)
    return {'status': 'published', 'accounts': sorted(published)}


def alert(args, accounts):
    message = 'quota_stale accounts=' + ','.join(sorted(accounts)) + '; inspect the maintained quota publisher.'
    env = {key: value for key, value in os.environ.items()
           if not any(word in key.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY', 'QUSE', 'HUB'))}
    env.update(PRINCIPAL_ALERT_TO=RECIPIENT, ALERT_TO=RECIPIENT, PING_ALERT_MESSAGE=message)
    try:
        result = subprocess.run([str(args.alert_command), message], env=env, capture_output=True, text=True, timeout=args.alert_timeout)
        return result.returncode == 0
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return False


def check(args):
    with locked(args.state):
        now = now_ms()
        try:
            data = read_state(args.state, now)
        except (Failure, OSError):
            data = empty_state()  # corrupt data proves no account fresh
        stale = {account for account in args.accounts if account not in data['accounts'] or now - data['accounts'][account] > args.stale_seconds * 1000}
        # Recovery resets incident dedupe. Publication itself never marks an alert sent.
        data['alerted'] = sorted(set(data['alerted']).intersection(stale))
        unsent = stale.difference(data['alerted'])
        delivered = not unsent or alert(args, unsent)
        if unsent and delivered:
            data['alerted'] = sorted(set(data['alerted']).union(unsent))
        write_state(args.state, data)
    return {'status': 'quota_stale' if stale else 'fresh', 'accounts': sorted(stale), 'alert_sent': bool(unsent and delivered),
            'sink_failed': not delivered}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='independent staleness monitor; never invokes hub/quse')
    parser.add_argument('--accounts', type=account_set, default=sorted(ACCOUNTS), help='expected account labels, comma separated (default all known labels)')
    parser.add_argument('--state', type=absolute_path, default=Path.home() / '.local/state/agent-hub-quota-publisher/state.json')
    parser.add_argument('--hub', type=absolute_path, default=Path.home() / '.local/bin/hub')
    parser.add_argument('--alert-command', type=absolute_path, default=Path.home() / '.local/bin/principal-alert.sh')
    parser.add_argument('--stale-seconds', type=int, default=DEFAULT_STALE_SECONDS, help='1..900; default840 leaves delivery allowance within20min drill')
    parser.add_argument('--publish-timeout', type=int, default=120)
    parser.add_argument('--alert-timeout', type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.stale_seconds <= 900 or not 1 <= args.publish_timeout <= 120 or not 1 <= args.alert_timeout <= 30:
        parser.error('bounds: stale1..900, publish timeout1..120, alert timeout1..30 seconds')
    try:
        result = check(args) if args.check else publish(args)
    except (Failure, OSError) as error:
        # No raw CLI, provider, filesystem or credential errors reach logs/mail.
        if args.check:
            delivered = alert(args, args.accounts)
            result = {'status': 'quota_stale', 'accounts': args.accounts, 'reason': 'state_unavailable',
                      'alert_sent': delivered, 'sink_failed': not delivered}
        else:
            slug = str(error) if isinstance(error, Failure) else 'operation_failed'
            result = {'status': 'publish_failed', 'reason': slug if slug in {
                'clock_invalid', 'clock_regressed', 'state_invalid', 'state_directory_untrusted',
                'state_lock_untrusted', 'result_invalid', 'cli_timeout', 'cli_unavailable', 'cli_failed'} else 'operation_failed'}
    print(json.dumps(result, sort_keys=True))
    if result['status'] in ('fresh', 'published'):
        return 0
    return 3 if result.get('sink_failed') else 2 if result['status'] == 'quota_stale' else 1


if __name__ == '__main__':
    sys.exit(main())
