"""Re-pin an existing managed Claude profile to a newly verified host release.

The Hub2 updater runs this module from the checksum-verified release archive
with that release's own interpreter (``-I -S``), so the generated profile
command names the new release. It never logs in, refreshes, uploads, creates a
credential generation, runs a global sync, reads the operator CLI profile or
touches another provider. Authority is the current enrolled host only.

Output is one JSON object on stdout. Exit 0 means ``absent`` (nothing
configured), ``current`` (already pinned and ready) or ``repinned`` (published,
ready and the same generation acknowledged as ``recorded``). Anything else
exits 3 with a fixed reason and no paths, tokens or provider output.

A durable journal with the previous and new commands is written before the
profile changes, so the updater can restore the exact previous pin under the
same lock with ownership, generation and compare-and-set fences.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import tomllib

from . import credentials, private_fs, provisioning

LABEL = 'hub-claude'
PROFILE = 'hub-hub-claude'
CREDENTIAL_PATH = '.claude/.credentials.json'
LOCK_TIMEOUT_S = 30
JOURNAL_SCHEMA = 1
RELEASE = re.compile(r'[0-9a-f]{40}\Z')


class Refused(Exception):
    """A fixed, non-sensitive refusal reason."""


def _profile_table(text):
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise Refused('profile_invalid') from None
    profiles = document.get('profiles')
    if not isinstance(profiles, dict) or PROFILE not in profiles:
        return None
    table = profiles[PROFILE]
    if (not isinstance(table, dict) or set(table) != {'engine', 'command'} or table['engine'] != 'claude'
            or not isinstance(table['command'], list) or not table['command']
            or not all(isinstance(part, str) for part in table['command'])):
        raise Refused('profile_malformed')
    if text.count(provisioning.BEGIN) != 1 or text.count(provisioning.END) != 1:
        raise Refused('profile_not_managed')
    marked = text.split(provisioning.BEGIN, 1)[1].split(provisioning.END, 1)[0]
    try:
        managed = tomllib.loads(marked).get('profiles', {})
    except tomllib.TOMLDecodeError:
        raise Refused('profile_not_managed') from None
    if not isinstance(managed, dict) or managed.get(PROFILE) != table:
        raise Refused('profile_not_managed')
    return table


def _release_command(archive):
    """The exact command this release generates, refusing the unpinned form."""
    command = provisioning.module_command('execute', LABEL)
    release = archive.parent
    if command[:3] != [str(release / 'bin/python'), '-I', '-S'] or str(archive) not in command[4]:
        raise Refused('release_identity_unverified')
    return command


def _previous_release(command, expected, release):
    """Recognize only this generator's output for another managed release."""
    python = Path(command[0])
    old = python.parent.parent
    if (python.name != 'python' or python.parent.name != 'bin' or old.parent != release.parent
            or not RELEASE.fullmatch(old.name) or old == release):
        raise Refused('profile_foreign')
    if old.is_symlink() or not old.is_dir() or not (old / 'release.json').is_file() or not (old / 'hub.pyz').is_file():
        raise Refused('profile_foreign')
    if command != [part.replace(str(release), str(old)) for part in expected]:
        raise Refused('profile_foreign')
    return old


def _local_receipt():
    """The installed target row, receipt and same-generation credential state."""
    try:
        _, rows = provisioning.scoped_state_snapshot(provisioning.root() / 'state.json')
    except (OSError, ValueError):
        raise Refused('state_invalid') from None
    matches = [row for row in rows if row['label'] == LABEL]
    if len(matches) != 1:
        raise Refused('state_ambiguous')
    row = matches[0]
    if set(row) != {'label', 'revision', 'provider'} or row['provider'] != 'claude' or not provisioning.REV.fullmatch(row['revision']):
        raise Refused('state_invalid')
    dest = provisioning.root() / (LABEL + '-' + row['revision'])
    if dest.is_symlink() or not dest.is_dir():
        raise Refused('receipt_invalid')
    try:
        receipt = provisioning.load_bundle(dest)
        provisioning.validate(receipt)
    except (OSError, ValueError, TypeError):
        raise Refused('receipt_invalid') from None
    if receipt.get('label') != LABEL or receipt.get('provider') != 'claude' or receipt.get('revision') != row['revision']:
        raise Refused('receipt_invalid')
    try:
        state = json.loads(private_fs.read_bytes(dest / credentials.STATE_FILE, 65536))
    except (OSError, ValueError):
        raise Refused('generation_state_invalid') from None
    if (not isinstance(state, dict) or set(state) != {'generation', 'digests'}
            or type(state['generation']) is not int or not 1 <= state['generation'] <= 2**53 - 1
            or not isinstance(state['digests'], dict) or set(state['digests']) != {CREDENTIAL_PATH}
            or not isinstance(state['digests'][CREDENTIAL_PATH], str)
            or not re.fullmatch('[0-9a-f]{64}', state['digests'][CREDENTIAL_PATH])):
        raise Refused('generation_state_invalid')
    _local_credential_proof(dest / 'home' / CREDENTIAL_PATH, state['digests'][CREDENTIAL_PATH])
    return row['revision'], receipt, state['generation']


def _local_credential_proof(target, digest):
    """The materialized bytes are exactly the recorded generation and unexpired.

    Read in memory only (no-follow, owner, bounded); never logged or sent. This
    is a local observation, not a canonical expiry claim.
    """
    try:
        with private_fs.parent(target) as anchor:
            fd = private_fs._open(anchor, target, os.O_RDONLY, private=False)
            with os.fdopen(fd, 'rb') as stream:
                if os.fstat(stream.fileno()).st_uid != os.getuid():
                    raise ValueError()
                data = stream.read(credentials.MAX_FILE * 2 + 1)
    except (OSError, ValueError):
        raise Refused('local_credential_unverified') from None
    if len(data) > credentials.MAX_FILE * 2 or hashlib.sha256(data).hexdigest() != digest:
        raise Refused('local_credential_unverified')
    try:
        expiry = json.loads(data)['claudeAiOauth']['expiresAt']
    except (ValueError, KeyError, TypeError):
        expiry = None
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not time.time() * 1000 < expiry < 1e15:
        raise Refused('local_credential_expired')


def _bind(base):
    """Current enrolled v2 host only: never an explicit or legacy profile."""
    from . import native_import_host
    if os.environ.get('HUB_CONFIG_FILE') or os.environ.get('HUB2_CONFIG'):
        raise Refused('enrollment_unverified')
    enrollment = Path.home() / '.config/agent-hub/v2.json'
    if enrollment.is_symlink() or not enrollment.is_file():
        raise Refused('enrollment_unverified')
    try:
        _, request = native_import_host.bind_managed_claude_repin(base)
    except (OSError, ValueError, TypeError):
        raise Refused('enrollment_unverified') from None
    return request


def _fenced_generation(request, generation, revision):
    """Hub still has exactly this generation/revision, with no rejection, lease or
    unresolved refresh. An observation, not a transaction with local files."""
    try:
        observed = request('GET', '/api/engines/hub-claude/generation?generation=%d&revision=%s' % (generation, revision))
    except Exception:
        raise Refused('generation_unverified') from None
    if observed != {'label': LABEL, 'generation': generation, 'revision': revision, 'status': 'current'}:
        raise Refused('generation_unverified')


def _acknowledge(request, generation, revision):
    try:
        report = request('POST', '/api/engines/sync-report', {'state': 'synced', 'items': [{
            'label': LABEL, 'revision': revision, 'state': 'installed', 'credential_generation': generation}]})
    except Exception:
        report = None
    if not isinstance(report, dict) or report.get('status') != 'recorded':
        raise Refused('generation_ack_unverified')


def _write_journal(path, record):
    """Durably publish complete journal bytes exclusively (no secrets inside).

    The updater root need not be a private directory, so this does not use
    private_fs; a fixed-name exclusive link never replaces an existing entry.
    """
    path = Path(path)
    if path.is_symlink() or path.exists():
        raise Refused('journal_exists')
    data = json.dumps(record, sort_keys=True).encode()
    pending = path.with_name(path.name + '.%d.pending' % os.getpid())
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(pending, path)
        except FileExistsError:
            raise Refused('journal_exists') from None
    finally:
        pending.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def repin(archive, base, journal):
    # module_command derives the release pin from the running archive; keep
    # it bound for generation, publication and readiness alike.
    previous = sys.argv[0]
    try:
        sys.argv[0] = str(Path(archive).resolve(strict=True)) if Path(archive).is_absolute() else ''
        return _repin(archive, base, journal)
    finally:
        sys.argv[0] = previous


def _repin(archive, base, journal):
    archive = Path(archive)
    if not archive.is_absolute() or archive.name != 'hub.pyz' or not Path(journal).is_absolute():
        raise Refused('invalid_arguments')
    archive = archive.resolve(strict=True)
    release = archive.parent
    if not RELEASE.fullmatch(release.name) or not (release / 'release.json').is_file():
        raise Refused('release_identity_unverified')
    path = provisioning.profile_path()
    try:
        text = private_fs.read_bytes(path, 262144).decode()
    except FileNotFoundError:
        return {'status': 'absent'}
    except (OSError, ValueError):
        raise Refused('profile_unreadable') from None
    if _profile_table(text) is None:
        return {'status': 'absent'}
    expected = _release_command(archive)
    with provisioning.lock(timeout=LOCK_TIMEOUT_S, bounded=True):
        # Re-read under the same lock every managed writer holds.
        try:
            text = private_fs.read_bytes(path, 262144).decode()
        except (OSError, ValueError):
            raise Refused('profile_unreadable') from None
        table = _profile_table(text)
        if table is None:
            return {'status': 'absent'}
        revision, receipt, generation = _local_receipt()
        if table['command'] == expected:
            try:
                provisioning.managed_exec_readiness(archive)
            except (OSError, ValueError):
                raise Refused('readiness_unverified') from None
            return {'status': 'current', 'generation': generation, 'revision': revision}
        old = _previous_release(table['command'], expected, release)
        request = _bind(base)
        # Before any write: fenced Hub observation, then the strict ACK CAS.
        _fenced_generation(request, generation, revision)
        _acknowledge(request, generation, revision)
        _write_journal(journal, {
            'schema': JOURNAL_SCHEMA, 'release': release.name, 'previous_release': old.name,
            'profile_path': str(Path(path).absolute()), 'managed_root': str(provisioning.root()),
            'previous_command': table['command'], 'command': expected,
            'generation': generation, 'revision': revision, 'verified': False})
        try:
            provisioning.scoped_profile(receipt, expected_text=text)
        except (OSError, ValueError):
            raise Refused('profile_publication_failed') from None
        try:
            provisioning.managed_exec_readiness(archive)
        except (OSError, ValueError):
            raise Refused('readiness_unverified') from None
        # After publication: re-observe, then acknowledge the same generation.
        _local_receipt()
        _fenced_generation(request, generation, revision)
        _acknowledge(request, generation, revision)
    return {'status': 'repinned', 'generation': generation, 'revision': revision,
            'previous_release': old.name, 'release': release.name}


def main(argv):
    try:
        if len(argv) != 4 or argv[1] != 'repin':
            raise Refused('invalid_arguments')
        archive, _, base, journal = argv
        result = repin(archive, base, journal)
        code = 0
    except Refused as refused:
        result, code = {'status': 'refused', 'reason': str(refused)}, 3
    except Exception:
        # Never stringify provider output, paths or secret-bearing exceptions.
        result, code = {'status': 'refused', 'reason': 'repin_failed'}, 3
    print(json.dumps(result, sort_keys=True))
    return code
