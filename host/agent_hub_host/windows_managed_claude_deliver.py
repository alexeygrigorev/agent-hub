"""Windows managed hub-claude consumer only; no enrollment/default/native API.

Executed only by the resident worker through a held same-release Windows lease.
Reuses the maintained canonical receipt/profile and antirollback materializer.
The logical consumer identity is not a physical enrollment UUID or hostile-UID CAS.
"""
import hashlib
import json
import ntpath
import re
import sys

from . import credentials, managed_claude_deliver as managed, provisioning

ABI = 'hub2-windows-managed-claude-consumer-v1'


def _set(archive, revision, present):
    command = None
    if present:
        previous = sys.argv[0]
        try:
            sys.argv[0] = archive
            command = provisioning.module_command('execute', managed.LABEL)
        finally:
            sys.argv[0] = previous
    public = {'abi': ABI, 'root': str(provisioning.root()), 'label': managed.LABEL,
              'revision': revision if present else None, 'command': command}
    return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()[:32]


def deliver(archive, request, check=False):
    # _managed locks, checks exact installed receipt/profile, and preserves the
    # receipt-first/home-then-state publication and equal-generation safeguards.
    result = managed._managed(archive, request, check)
    # Reacquire the SAME canonical reconciler lock. A changed consumer in the
    # intervening gap cannot receive a held result for the earlier consumer.
    with provisioning.lock(timeout=managed.LOCK_TIMEOUT_S, bounded=True):
        found = managed._receipt(request['revision'])
        present = found is not None and managed._profile_present(archive)
        if result['status'] == 'absent':
            if present:
                raise managed.Refused('consumer_set_changed')
            return {'status': 'absent', 'consumer_set': _set(archive, request['revision'], False)}
        if not present:
            raise managed.Refused('consumer_set_changed')
        dest, receipt = found
        held = (receipt.get('credential_generation') == request['generation']
                and (receipt.get('files') or {}).get(managed.CREDENTIAL_PATH)
                == request['files'][managed.CREDENTIAL_PATH])
        current = credentials.materialize_current(
            receipt if held else dict(receipt, credential_generation=request['generation'],
                files=dict(receipt.get('files') or {}, **request['files'])),
            dest / 'home', dest, request['generation'], check_only=True)
        if result['status'] in ('in_step', 'materialized') and (
                not held or current.get('status') != 'in_step'):
            raise managed.Refused('consumer_set_changed')
        result = dict(result)
        result['consumer_set'] = _set(archive, request['revision'], True)
        return result


def _archive_argument(value):
    if not isinstance(value, str) or len(value) > 32767 or '\x00' in value:
        return False
    drive, tail = ntpath.splitdrive(value)
    return (bool(re.fullmatch(r'[A-Za-z]:', drive)) and tail.startswith('\\')
            and ':' not in tail and not any(part in ('.', '..') for part in tail.split('\\'))
            and ntpath.basename(value).lower() == 'hub.pyz')


def main(argv, stdin=None):
    try:
        argv = list(argv)
        if (sys.platform != 'win32' or len(argv) != 2 or not _archive_argument(argv[0])
                or argv[1] not in ('check', 'materialize')):
            raise managed.Refused('invalid_arguments')
        stream = stdin if stdin is not None else sys.stdin.buffer
        result = deliver(argv[0], managed._read_request(stream), argv[1] == 'check')
        code = 0
    except managed.Refused as error:
        reason = str(error)
        allowed = {'invalid_arguments', 'invalid_request', 'profile_unreadable',
            'profile_unverified', 'state_invalid', 'receipt_revision_mismatch',
            'receipt_invalid', 'generation_refused', 'receipt_publication_failed',
            'consumer_set_changed'}
        result, code = {'status': 'refused', 'reason': reason if reason in allowed else 'delivery_failed'}, 3
    except TimeoutError:
        result, code = {'status': 'refused', 'reason': 'lock_timeout'}, 3
    except Exception:
        result, code = {'status': 'refused', 'reason': 'delivery_failed'}, 3
    print(json.dumps(result, sort_keys=True))
    return code
