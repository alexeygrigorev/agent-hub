#!/usr/bin/env python3
"""Maintain quota receipts and monitor staleness, with opt-in Hub/host health.

No quota details or command stderr are emitted or included in alerts. Only a
validated successful `hub quota publish` receipt advances publication times.
"""
import argparse
import contextlib
import fcntl
import json
import math
import multiprocessing
import re
import urllib.error
import urllib.parse
import urllib.request
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


def fsync_state_dir(parent, create=False):
    if not parent.is_absolute():
        raise Failure('state_directory_untrusted')
    fds = []
    try:
        fds.append(os.open('/', os.O_RDONLY | os.O_DIRECTORY))
        for part in parent.parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fds[-1])
                except FileExistsError:
                    pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
            fds.append(next_fd)
        info = os.fstat(fds[-1])
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Failure('state_directory_untrusted')
        target_dev = info.st_dev
        for fd in reversed(fds):
            if os.fstat(fd).st_dev != target_dev:
                break
            os.fsync(fd)
    finally:
        for fd in reversed(fds):
            os.close(fd)


@contextlib.contextmanager
def locked(path, timeout=1):
    fsync_state_dir(path.parent, create=True)
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(str(path) + '.lock', flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise Failure('state_lock_untrusted')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Failure('state_lock_timeout') from None
                time.sleep(0.02)
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
    except (ValueError, UnicodeError, RecursionError):
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
        fsync_state_dir(path.parent)
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


def alert(args, accounts=(), message=None):
    message = message or 'quota_stale accounts=' + ','.join(sorted(accounts)) + '; inspect the maintained quota publisher.'
    env = {key: value for key, value in os.environ.items()
           if not any(word in key.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY', 'QUSE', 'HUB'))}
    env.update(PRINCIPAL_ALERT_TO=RECIPIENT, ALERT_TO=RECIPIENT, PING_ALERT_MESSAGE=message)
    try:
        result = subprocess.run([str(args.alert_command), message], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=args.alert_timeout)
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


# The child alone loads the enrolled MACHINE credential. The parent receives
# only fixed slugs, never response/config/exception text. Killing this owned
# child bounds DNS, TLS and slow-drip reads as well as socket connection time.
HEALTH_REASONS = frozenset(('ok', 'config_invalid', 'origin_invalid', 'auth_expired',
    'http_rejected', 'response_invalid', 'not_ok', 'identity_mismatch',
    'disconnected', 'transport_failed', 'deadline', 'probe_failed'))
HEALTH_STAGES = frozenset(('config', 'public', 'host', 'probe'))


def private_json(path):
    # Walk from / with O_NOFOLLOW: a safe leaf behind a symlink is unsafe too.
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_STATE_BYTES):
                raise Failure('config_invalid')
            with os.fdopen(fd, 'rb') as stream:
                fd = None
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise Failure('config_invalid')
            try:
                value = json.loads(raw, object_pairs_hook=unique_object)
            except (ValueError, UnicodeError, RecursionError):
                raise Failure('config_invalid') from None
            if not isinstance(value, dict):
                raise Failure('config_invalid')
            return value
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        os.close(directory)


def health_origin(raw, offline):
    if (not isinstance(raw, str) or len(raw) > 2048
            or not re.fullmatch(r'[!-~]+', raw) or any(c in raw for c in ('\\', '%', '?', '#'))):
        raise Failure('origin_invalid')
    try:
        url = urllib.parse.urlsplit(raw)
        port = url.port
        if (not url.hostname or url.username is not None or url.password is not None
                or url.path not in ('', '/') or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        host = url.hostname
        label = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
        if not (re.fullmatch(label + r'(?:\.' + label + r')*', host) or host == '::1'):
            raise ValueError()
        authority = ('[' + host + ']' if ':' in host else host) + (':' + str(port) if port is not None else '')
        if url.netloc.lower() != authority:
            raise ValueError()
        if url.scheme != 'https' and not (offline and url.scheme == 'http' and host in ('127.0.0.1', '::1')):
            raise ValueError()
    except ValueError:
        raise Failure('origin_invalid') from None
    return raw.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health_get(opener, origin, path, token=None):
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(origin + path, headers=headers, method='GET')
    try:
        with opener.open(request, timeout=3) as response:
            if not 200 <= response.status < 300 or response.geturl() != origin + path:
                raise Failure('http_rejected')
            if response.headers.get_content_type() != 'application/json' or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise Failure('response_invalid')
            raw = response.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise Failure('response_invalid')
        body = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(body, dict):
            raise Failure('response_invalid')
        return body
    except urllib.error.HTTPError:
        raise Failure('http_rejected') from None
    except (ValueError, UnicodeError, RecursionError):
        raise Failure('response_invalid') from None
    except (OSError, urllib.error.URLError):
        raise Failure('transport_failed') from None


def health_probe(pipe, config_path, offline):
    stage = 'config'
    try:
        try:
            config = private_json(config_path)
        except (OSError, ValueError, UnicodeError):
            raise Failure('config_invalid') from None
        origin = health_origin(config.get('server'), offline)
        machine, token = config.get('machine_id'), config.get('token')
        if (not isinstance(machine, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', machine)
                or not isinstance(token, str) or not re.fullmatch(r'[!-~]{1,8192}', token)):
            raise Failure('config_invalid')
        expires = config.get('expires_at')
        if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()):
            raise Failure('auth_expired')
        # Explicitly disable all environment proxies. Redirects are never followed,
        # including same-origin redirects, and public health never gets a token.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        stage = 'public'
        public = health_get(opener, origin, '/health')
        if public.get('ok') is not True or public.get('status') == 'bootstrap':
            raise Failure('not_ok')
        stage = 'host'
        body = health_get(opener, origin, '/api/fleet/routing/self', token)
        if body.get('machine_id') != machine:
            raise Failure('identity_mismatch')
        if body.get('connected') is not True:
            raise Failure('disconnected')
        pipe.send((stage, 'ok'))
    except Failure as error:
        pipe.send((stage, str(error) if str(error) in HEALTH_REASONS else 'probe_failed'))
    except Exception:
        pipe.send((stage, 'probe_failed'))
    finally:
        pipe.close()


def sample_health(args):
    context = multiprocessing.get_context('fork')
    reader, writer = context.Pipe(duplex=False)
    child = context.Process(target=health_probe, args=(writer, args.health_config, args.health_offline_loopback))
    deadline = time.monotonic() + args.health_timeout
    try:
        child.start()
        writer.close()
        child.join(max(0, deadline - time.monotonic()))
        if child.is_alive():
            return {'stage': 'probe', 'reason': 'deadline', 'healthy': False}
        try:
            stage, reason = reader.recv() if reader.poll() else ('probe', 'probe_failed')
        except (EOFError, ValueError, TypeError):
            stage, reason = 'probe', 'probe_failed'
        if stage not in HEALTH_STAGES or reason not in HEALTH_REASONS:
            stage, reason = 'probe', 'probe_failed'
        return {'stage': stage, 'reason': reason, 'healthy': reason == 'ok'}
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join()
            child.close()
        writer.close()
        reader.close()


def check_health(args):
    try:
        started = now_ms()
        # Serialize sample + transition + sink so overlapping/older invocations
        # cannot deliver a stale incident after a later recovery. Lock is bounded.
        with locked(args.health_state):
            sample = sample_health(args)
            try:
                data = private_json(args.health_state)
            except FileNotFoundError:
                data = {'version': 1, 'last_sample': 0, 'failures': 0, 'alerted': False}
            except (ValueError, UnicodeError, RecursionError):
                raise Failure('state_invalid') from None
            if (set(data) != {'version', 'last_sample', 'failures', 'alerted'}
                    or type(data['version']) is not int or data['version'] != 1
                    or type(data['last_sample']) is not int or data['last_sample'] < 0
                    or type(data['failures']) is not int or not 0 <= data['failures'] <= 2
                    or type(data['alerted']) is not bool
                    or (data['alerted'] and data['failures'] != 2)):
                raise Failure('state_invalid')
            if started <= data['last_sample']:
                raise Failure('clock_regressed')
            data['last_sample'] = started
            data['failures'] = 0 if sample['healthy'] else min(2, data['failures'] + 1)
            if sample['healthy']:
                data['alerted'] = False
            # Persist failure evidence before invoking the renderer. Sink failure
            # or process death leaves a retryable unsent incident.
            write_state(args.health_state, data)
            unsent = data['failures'] == 2 and not data['alerted']
            delivered = not unsent or alert(args, message='health_critical stage=' + sample['stage'] +
                ' reason=' + sample['reason'] + '; inspect Hub public health and the enrolled host daemon.')
            if unsent and delivered:
                data['alerted'] = True
                write_state(args.health_state, data)
            return {**sample, 'status': 'healthy' if sample['healthy'] else 'health_failed',
                    'failures': data['failures'], 'alert_sent': bool(unsent and delivered), 'sink_failed': not delivered}
    except (Failure, OSError):
        # Unsafe/unavailable durable state cannot prove recovery or dedupe.
        return {'status': 'health_unknown', 'healthy': False, 'stage': 'state',
                'reason': 'state_unavailable', 'alert_sent': False, 'sink_failed': False}


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
    parser.add_argument('--health-check', action='store_true', help='opt-in public and own enrolled host health; requires --check')
    parser.add_argument('--health-config', type=absolute_path, default=Path.home() / '.config/agent-hub/v2.json')
    parser.add_argument('--health-state', type=absolute_path, default=Path.home() / '.local/state/agent-hub-health-monitor/state.json')
    parser.add_argument('--health-timeout', type=int, default=8, help='1..8 seconds total child wall budget including DNS and reads')
    parser.add_argument('--health-offline-loopback', action='store_true', help='allow literal loopback HTTP for explicit offline fixtures')
    args = parser.parse_args(argv)
    if not 1 <= args.health_timeout <= 8 or (args.health_check and not args.check):
        parser.error('health requires --check and timeout1..8 seconds')
    if args.health_check and args.health_state == args.state:
        parser.error('health state must be separate from quota state')
    if args.health_check and args.alert_timeout > 8:
        parser.error('health monitor requires alert timeout1..8 seconds to retain 45s service budget')
    if not 1 <= args.stale_seconds <= 900 or not 1 <= args.publish_timeout <= 120 or not 1 <= args.alert_timeout <= 30:
        parser.error('bounds: stale1..900, publish timeout1..120, alert timeout1..30 seconds')
    health = check_health(args) if args.health_check else None
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
    if health is not None:
        result['health'] = health
        if health['sink_failed']:
            result['sink_failed'] = True
    print(json.dumps(result, sort_keys=True))
    if health is not None and (not health['healthy'] or health['sink_failed']):
        return 3 if result.get('sink_failed') else 2
    if result['status'] in ('fresh', 'published'):
        return 0
    return 3 if result.get('sink_failed') else 2 if result['status'] == 'quota_stale' else 1


if __name__ == '__main__':
    sys.exit(main())
