"""Deliver one approved hub-claude generation into the maintained managed home.

The Hub2 daemon runs this module from the checksum-verified release archive
with that release's own interpreter (``-I -S``) and passes the passively
observed generation on stdin. This helper has no network or enrollment
authority: it never logs in, refreshes, leases, uploads, acknowledges, runs a
global sync, installs a package, reads the operator profile or touches another
account, label or profile. The daemon acknowledges only after this reports the
exact generation the consumer now holds.

Under the provisioning lock every managed writer and exec reader holds, it
checks the canonical state row and receipt and that the managed aplexer
profile is exactly the command this verified release generates (interpreter,
archive and managed root, via ``managed_exec_readiness``). It then publishes
the receipt copy first and only afterwards the home file and digest state
(``credentials.materialize_current``: file first, state last; older or
equal-but-changed generations are refused before anything is written). The
receipt is therefore never older than the home: a failure or kill between the
two steps leaves the offline fallback (``refresh_launch_credential`` ->
``reconcile`` of the receipt) moving forward, never reverting a delivered
generation.

Output is one JSON object on stdout. Exit 0 means ``absent`` (no managed
hub-claude consumer here, nothing written), ``in_step`` or ``materialized``.
Anything else exits 3 with a fixed reason; never paths, tokens or exceptions.
"""
import json
import re
import sys

from . import credentials, managed_claude_repin, private_fs, provisioning

LABEL = 'hub-claude'
CREDENTIAL_PATH = '.claude/.credentials.json'
LOCK_TIMEOUT_S = 30
MAX_INPUT = 32768
REVISION = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')


class Refused(Exception):
    """A fixed, non-sensitive refusal reason."""


def _read_request(stream):
    raw = stream.read(MAX_INPUT + 1)
    if isinstance(raw, str):
        raw = raw.encode('utf8')
    if not raw or len(raw) > MAX_INPUT:
        raise Refused('invalid_request')
    try:
        request = json.loads(raw)
    except ValueError:
        raise Refused('invalid_request') from None
    if (not isinstance(request, dict) or set(request) != {'label', 'generation', 'revision', 'files'}
            or request['label'] != LABEL
            or type(request['generation']) is not int or not 1 <= request['generation'] <= 2**53 - 1
            or not isinstance(request['revision'], str) or not REVISION.fullmatch(request['revision'])
            or not isinstance(request['files'], dict) or set(request['files']) != {CREDENTIAL_PATH}
            or not isinstance(request['files'][CREDENTIAL_PATH], str) or not request['files'][CREDENTIAL_PATH]
            or len(request['files'][CREDENTIAL_PATH].encode('utf8')) > credentials.MAX_FILE):
        raise Refused('invalid_request')
    return request


def _profile_present(archive):
    """The managed profile exists and is exactly this release's generated
    command bound to this managed root. Caller holds the provisioning lock."""
    try:
        text = private_fs.read_bytes(provisioning.profile_path(), 262144).decode()
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise Refused('profile_unreadable') from None
    try:
        if managed_claude_repin._profile_table(text) is None:
            return False
    except managed_claude_repin.Refused as refused:
        raise Refused(str(refused)) from None
    try:
        provisioning.managed_exec_readiness(archive)
    except (OSError, ValueError, TypeError, KeyError):
        raise Refused('profile_unverified') from None
    return True


def _receipt(revision):
    """The canonical state row and receipt, or None when nothing is installed."""
    try:
        _, rows = provisioning.scoped_state_snapshot(provisioning.root() / 'state.json')
    except (OSError, ValueError):
        raise Refused('state_invalid') from None
    matches = [row for row in rows if row['label'] == LABEL]
    if not matches:
        return None
    row = matches[0]
    if (set(row) != {'label', 'revision', 'provider'} or row['provider'] != 'claude'
            or not isinstance(row['revision'], str) or not provisioning.REV.fullmatch(row['revision'])):
        raise Refused('state_invalid')
    if row['revision'] != revision:
        # A new engine revision needs the maintained install path; never here.
        raise Refused('receipt_revision_mismatch')
    dest = provisioning.root() / (LABEL + '-' + row['revision'])
    if dest.is_symlink() or not dest.is_dir():
        raise Refused('receipt_invalid')
    try:
        receipt = provisioning.load_bundle(dest)
        provisioning.validate(receipt)
    except (OSError, ValueError, TypeError, KeyError):
        raise Refused('receipt_invalid') from None
    if (not isinstance(receipt, dict) or receipt.get('label') != LABEL or receipt.get('provider') != 'claude'
            or receipt.get('revision') != revision or receipt.get('credential_paths') != [CREDENTIAL_PATH]):
        raise Refused('receipt_invalid')
    return dest, receipt


def deliver(archive, request):
    with provisioning.lock(timeout=LOCK_TIMEOUT_S, bounded=True):
        found = _receipt(request['revision'])
        if found is None or not _profile_present(archive):
            return {'status': 'absent'}
        dest, receipt = found
        generation = request['generation']
        # A fault after the receipt step can leave the receipt ahead of the
        # digest state; never move the receipt backwards or swap material
        # within one generation.
        held = receipt.get('credential_generation')
        if held is not None and (type(held) is not int or held > generation or (
                held == generation and (receipt.get('files') or {}).get(CREDENTIAL_PATH)
                != request['files'][CREDENTIAL_PATH])):
            raise Refused('generation_refused')
        files = dict(receipt.get('files') or {})
        files[CREDENTIAL_PATH] = request['files'][CREDENTIAL_PATH]
        bundle = dict(receipt, credential_generation=generation, files=files)
        home = dest / 'home'
        try:
            provisioning.validate(bundle)
            # Refuse older or equal-but-changed generations before any write.
            credentials.materialize_current(bundle, home, dest, generation, check_only=True)
        except (OSError, ValueError, TypeError, KeyError):
            raise Refused('generation_refused') from None
        try:
            # Receipt first: it is never older than the home it backs.
            if provisioning.load_bundle(dest) != bundle:
                provisioning.save_bundle(dest, bundle)
        except (OSError, ValueError, TypeError):
            raise Refused('receipt_publication_failed') from None
        try:
            outcome = credentials.materialize_current(bundle, home, dest, generation)
        except (OSError, ValueError, TypeError, KeyError):
            raise Refused('generation_refused') from None
        if (not isinstance(outcome, dict) or outcome.get('generation') != generation
                or outcome.get('status') not in ('in_step', 'materialized')):
            raise Refused('generation_refused')
        return {'status': outcome['status'], 'generation': generation, 'revision': request['revision']}


def main(argv, stdin=None):
    """argv: [<verified hub.pyz this helper runs from>, 'materialize']."""
    try:
        argv = list(argv)
        if len(argv) != 2 or argv[1] != 'materialize' or not isinstance(argv[0], str) or not argv[0].startswith('/'):
            raise Refused('invalid_arguments')
        stream = stdin if stdin is not None else sys.stdin.buffer
        result, code = deliver(argv[0], _read_request(stream)), 0
    except Refused as refused:
        result, code = {'status': 'refused', 'reason': str(refused)}, 3
    except TimeoutError:
        result, code = {'status': 'refused', 'reason': 'lock_timeout'}, 3
    except Exception:
        # Never stringify credential material, paths or provider output.
        result, code = {'status': 'refused', 'reason': 'delivery_failed'}, 3
    print(json.dumps(result, sort_keys=True))
    return code
