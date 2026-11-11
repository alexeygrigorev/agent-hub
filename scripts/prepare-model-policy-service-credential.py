#!/usr/bin/env python3
"""Exchange a fresh, private bootstrap for one scoped deployment credential.

Default preflight never writes or issues. `exchange --post-health` is an explicit
caller attestation: the protected deploy transaction has verified candidate HEAD
against --target-sha and passed health. /health itself exposes no source SHA.
An interrupted issuance requires operator recovery of its known service ID, never
a retry that silently mints another credential. No human refresh is performed.
"""
import argparse
import contextlib
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

BASE_URL = 'http://127.0.0.1:8787'
BOOTSTRAP = 'model-policy-bootstrap.json'
OUTPUT = 'model-policy-admin.json'
INTENT = 'model-policy-service-intent.json'
LABEL = 'agent-hub-model-policy-deploy'
SCOPES = ['model-policy:read', 'model-policy:write']
SERVICE_SCHEMA = 'hub2-model-policy-service-credential-v1'
LIMIT = 65536  # Private files and bootstrap stdin stay independently bounded.
HTTP_LIMIT = 128000  # Match the maintained model-policy response contract.
OWNER = 0
_TEST_OWNER = ContextVar("policy_service_owner", default=None)


def owner():
    override = _TEST_OWNER.get()
    return OWNER if override is None else override


class Problem(Exception):
    def __init__(self, code, request_id=None):
        self.code, self.request_id = code, request_id
        super().__init__(code)


def require(condition, code='invalid_metadata'):
    if not condition:
        raise Problem(code)


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def parse(raw):
    require(len(raw) <= LIMIT)
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Problem('invalid_metadata') from None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'


def request_id(value):
    require(isinstance(value, str))
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise Problem('invalid_request_id') from None
    require(parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value,
            'invalid_request_id')
    return value


def source_sha(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) is not None, 'invalid_target_sha')
    return value


def token(value, service=False):
    pattern = '[0-9a-f]{64}' if service else '[!-~]{32,4096}'
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, 'invalid_credential')
    return value


def directory_check(fd, private=False):
    info = os.fstat(fd)
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == owner()
            and (stat.S_IMODE(info.st_mode) == 0o700 if private else not info.st_mode & 0o022),
            'directory_untrusted')


def open_directory():
    require(os.geteuid() == owner(), 'root_required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_check(fd)
        for name in ('etc', 'agent-hub', 'deploy-credentials'):
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            directory_check(fd, private=name == 'deploy-credentials')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_private(fd, name, optional=False):
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        if optional:
            return None
        raise Problem('private_file_missing') from None
    with os.fdopen(handle, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= LIMIT,
                'private_file_untrusted')
        raw = stream.read(LIMIT + 1)
    require(len(raw) <= LIMIT, 'private_file_untrusted')
    return raw


def publish(fd, name, raw, expected=None):
    """No-clobber create, or explicit replacement of the checked old output.

    The root-private lock serializes helpers; deploy-owner custody excludes
    uncoordinated operator writes during the final comparison and rename.
    """
    temporary = '.policy-service-' + uuid.uuid4().hex
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is None:
            os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=fd)
        else:
            current = read_private(fd, name)
            require(hashlib.sha256(current).hexdigest() == expected, 'output_changed')
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=fd)


@contextlib.contextmanager
def locked(fd):
    handle = os.open('model-policy-service.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=fd)
    try:
        info = os.fstat(handle)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, 'lock_untrusted')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(handle)


# Secret input/output remains in pipes. Only public URL and method enter argv.
# Parent timeout kills/reaps the child, including a trickling response body.
HTTP_PROBE = r'''
import json, sys, urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
try:
    value = json.loads(sys.stdin.buffer.read(65537))
    headers = {'Accept':'application/json'}
    if value['token'] is not None:
        headers['Authorization'] = 'Bearer ' + value['token']
    data = None if value['body'] is None else json.dumps(value['body'], separators=(',',':')).encode()
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(sys.argv[1], data=data, method=sys.argv[2], headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(128001)
        if len(raw) > 128000:
            raise ValueError()
        result = {'status':response.code, 'body':json.loads(raw)}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, separators=(',',':')).encode('utf-8'))
except Exception:
    sys.exit(1)
'''


def loopback_url(base):
    import urllib.parse
    parsed = urllib.parse.urlsplit(base)
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and not parsed.username
            and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment,
            'invalid_loopback_url')
    require(parsed.port is not None and 1 <= parsed.port <= 65535, 'invalid_loopback_url')
    return base


def http(method, path, bearer=None, body=None, base=BASE_URL):
    loopback_url(base)
    require(path in ('/health', '/api/model-policy', '/api/deployment-credentials')
            and method in ('GET', 'POST'), 'invalid_request')
    result = subprocess.run([sys.executable, '-I', '-S', '-c', HTTP_PROBE, base + path, method],
                            input=canonical({'token': bearer, 'body': body}),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, env={})
    require(result.returncode == 0 and len(result.stdout) <= 2 * HTTP_LIMIT, 'http_outcome_unknown')
    value = json.loads(result.stdout, object_pairs_hook=unique)
    require(isinstance(value, dict) and set(value) == {'status', 'body'}
            and type(value['status']) is int and 100 <= value['status'] <= 599, 'http_outcome_unknown')
    return value['status'], value['body']


def bootstrap(raw, target, now):
    value = parse(raw)
    fields = {'schema', 'intent', 'request_id', 'target_sha', 'access_token',
              'refreshed_at_ms', 'expires_at_ms', 'ttl_seconds'}
    require(isinstance(value, dict) and set(value) == fields, 'bootstrap_invalid')
    require(value['schema'] == 'hub2-model-policy-bootstrap-v1'
            and value['intent'] == 'exchange_model_policy_service_credential'
            and value['target_sha'] == target, 'bootstrap_intent_mismatch')
    request_id(value['request_id']); token(value['access_token'])
    refreshed, expires, ttl = (value[key] for key in ('refreshed_at_ms', 'expires_at_ms', 'ttl_seconds'))
    require(all(type(item) is int for item in (refreshed, expires, ttl)) and 0 < ttl <= 3600
            and 0 < refreshed <= now + 5000 and expires == refreshed + ttl * 1000
            and expires > now + 30000, 'bootstrap_expired_or_invalid')
    return value


def credential(value, wanted_id=None):
    fields = {'id', 'request_id', 'kind', 'purpose', 'label', 'scopes', 'expires_at', 'created_at', 'revoked'}
    require(isinstance(value, dict) and set(value) == fields, 'service_identity_invalid')
    identity = request_id(value['request_id'])
    require(value['id'] == 'deployment:' + identity and (wanted_id is None or identity == wanted_id)
            and value['kind'] == 'service' and value['purpose'] == 'model-policy' and value['label'] == LABEL
            and value['scopes'] == SCOPES and value['expires_at'] is None and value['revoked'] is False
            and type(value['created_at']) is int and 0 <= value['created_at'] <= 9007199254740991,
            'service_identity_invalid')
    return value


def service(raw):
    value = parse(raw)
    require(isinstance(value, dict) and set(value) == {'schema', 'target_sha', 'credential', 'access_token'}
            and value['schema'] == SERVICE_SCHEMA, 'service_marker_invalid')
    source_sha(value['target_sha']); credential(value['credential']); token(value['access_token'], service=True)
    return value


def verify_policy(request, value):
    status, body = request('GET', '/api/model-policy', value['access_token'])
    if status == 404:
        require(isinstance(body, dict) and set(body) == {'error', 'version'}
                and body['error'] == 'model_policy_unconfigured' and type(body['version']) is int
                and 0 <= body['version'] <= 9007199254740991, 'policy_read_unverified')
        return
    require(status == 200 and isinstance(body, dict) and set(body) == {'version', 'candidates'}
            and type(body['version']) is int and 1 <= body['version'] <= 9007199254740991
            and isinstance(body['candidates'], list) and len(body['candidates']) <= 32
            and all(isinstance(item, dict) for item in body['candidates']), 'policy_read_unverified')


def prepare(fd, target, *, exchange=False, post_health=False, request=http, now=None, base_url=BASE_URL):
    source_sha(target)
    clock = (lambda: int(time.time() * 1000)) if now is None else (lambda: now)
    previous = read_private(fd, OUTPUT, optional=True)
    if previous is not None:
        old = parse(previous)
        if isinstance(old, dict) and old.get('schema') == SERVICE_SCHEMA:
            value = service(previous)
            if not exchange:
                return {'status': 'service_shape_valid', 'id': value['credential']['id']}
            require(post_health, 'post_health_attestation_required')
            verify_policy(request, value)
            return {'status': 'service_ready', 'id': value['credential']['id']}
        require(isinstance(old, dict) and set(old) == {'access_token'}, 'existing_output_unrecognized')
        token(old['access_token'])  # Never use the old operator token for requests.
    pending = read_private(fd, INTENT, optional=True)
    if pending is not None:
        intent = parse(pending)
        identity = request_id(intent.get('request_id') if isinstance(intent, dict) else None)
        raise Problem('issuance_recovery_required', identity)
    raw_bootstrap = read_private(fd, BOOTSTRAP)
    value = bootstrap(raw_bootstrap, target, clock())
    if not exchange:
        return {'status': 'bootstrap_fresh_metadata', 'id': 'deployment:' + value['request_id'],
                'remaining_ms': value['expires_at_ms'] - clock()}
    require(post_health, 'post_health_attestation_required')
    status, health = request('GET', '/health')
    require(status == 200 and isinstance(health, dict) and health.get('ok') is True
            and health.get('service') == 'hub2', 'health_unverified')
    # Recheck actual remaining lifetime after health, before persisting intent.
    value = bootstrap(raw_bootstrap, target, clock())
    identity = value['request_id']
    prior = hashlib.sha256(previous).hexdigest() if previous is not None else None
    intent = {'schema': 'hub2-model-policy-service-intent-v1', 'request_id': identity,
              'target_sha': target, 'base_url': base_url, 'label': LABEL, 'purpose': 'model-policy',
              'prior_output_sha256': prior, 'phase': 'issuance_started'}
    publish(fd, INTENT, canonical(intent))
    try:
        status, response = request('POST', '/api/deployment-credentials', value['access_token'],
                                   {'request_id': identity, 'label': LABEL})
        require(status == 201 and isinstance(response, dict)
                and set(response) == {'credential', 'access_token', 'token_type'}
                and response['token_type'] == 'Bearer', 'issuance_unverified')
        metadata = credential(response['credential'], identity)
        token(response['access_token'], service=True)
        installed = {'schema': SERVICE_SCHEMA, 'target_sha': target,
                     'credential': metadata, 'access_token': response['access_token']}
        received = 'model-policy-service-' + identity + '.received.json'
        publish(fd, received, canonical(installed))
        verify_policy(request, installed)
        publish(fd, OUTPUT, canonical(installed), expected=prior)
    except Exception:
        # Intent and any received candidate remain private and immutable. Root
        # reconciles/revokes the known ID, then supplies an explicit new request.
        raise Problem('issuance_recovery_required', identity) from None
    return {'status': 'service_installed', 'id': metadata['id']}


def run(target, *, directory=None, base_url=BASE_URL, owner_uid=0,
        action='preflight', post_health=False, request=None, now=None):
    """Core adapter for owned offline/composed tests; CLI exposes no overrides.

    A supplied directory is a trusted test root, checked for symlink traversal,
    private mode and injected ownership. Production walks every fixed ancestor.
    Candidate repository identity is the protected caller's responsibility.
    """
    source_sha(target); loopback_url(base_url)
    require(type(owner_uid) is int and owner_uid >= 0 and action in ('preflight', 'exchange'))
    context = _TEST_OWNER.set(owner_uid)
    fd = None
    try:
        if directory is None:
            fd = open_directory()
        else:
            directory = Path(directory)
            require(directory.is_absolute() and directory.resolve() == directory, 'directory_untrusted')
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            directory_check(fd, private=True)
        if request is None:
            def request(method, path, bearer=None, body=None):
                return http(method, path, bearer, body, base=base_url)
        if action == 'exchange':
            with locked(fd):
                return prepare(fd, target, exchange=True, post_health=post_health,
                               request=request, now=now, base_url=base_url)
        return prepare(fd, target, request=request, now=now, base_url=base_url)
    finally:
        if fd is not None:
            os.close(fd)
        _TEST_OWNER.reset(context)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'exchange'), nargs='?', default='preflight')
    parser.add_argument('--target-sha', required=True)
    parser.add_argument('--post-health', action='store_true', help='caller verified candidate HEAD and post-start health')
    args = parser.parse_args(argv)
    try:
        result = run(args.target_sha, action=args.action, post_health=args.post_health)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        result = {'error': error.code if isinstance(error, Problem) else 'credential_preparation_failed'}
        if isinstance(error, Problem) and error.request_id:
            result['id'] = 'deployment:' + error.request_id
        print(json.dumps(result, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
