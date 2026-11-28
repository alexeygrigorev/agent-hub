"""Pure stubbed-command tests for the Node24 runtime provisioning feature.

Covers deploy/vm/provision-node24.sh (idempotent CI-owned Node24 provisioning)
and the matching fail-closed runtime guards in deploy/vm/deploy-release-transaction.sh
and deploy/vm/install-hub2-node.sh. Every command with real-world effects is a
fake executable on a per-test PATH; no live install, service, VM, apt, network
or provider operation runs here.

Fixture protocol: stubs read state from files/env seeded by the sandbox; the
downloaded key and keyring carry `gpg-fixture-primary-fingerprint:` markers the
gpg stub turns into colons output, archives carry `sha256-fixture:` markers the
sha256sum stub reports. Production defaults of the AGENT_HUB_NODE24_TEST_* path
seams are asserted explicitly so CI keeps probing the fixed service binary.
"""
import fcntl
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVISIONER = ROOT / 'deploy/vm/provision-node24.sh'
TRANSACTION = ROOT / 'deploy/vm/deploy-release-transaction.sh'
INSTALLER = ROOT / 'deploy/vm/install-hub2-node.sh'
DEPLOY_WORKFLOW = ROOT / '.github/workflows/deploy.yml'
ROLLBACK_WORKFLOW = ROOT / '.github/workflows/rollback.yml'

PROVISIONER_SOURCE = PROVISIONER.read_text(encoding='utf-8')
TRANSACTION_SOURCE = TRANSACTION.read_text(encoding='utf-8')
INSTALLER_SOURCE = INSTALLER.read_text(encoding='utf-8')


def source_pin(name):
    match = re.search(rf"^{re.escape(name)}='([^']+)'$", PROVISIONER_SOURCE, re.M)
    assert match, f'{name} binding missing from provisioner'
    return match.group(1)


PIN_VERSION = source_pin('NODE_PACKAGE_VERSION')
PIN_FINGERPRINT = source_pin('NODESOURCE_KEY_FINGERPRINT')
PIN_REPO = source_pin('NODESOURCE_REPO')
PIN_KEY_URL = source_pin('NODESOURCE_KEY_URL')
PIN_SHA_AMD64 = source_pin('NODE_PACKAGE_SHA256_AMD64')
OLD_VERSION = '22.23.4-1nodesource1'
V22 = 'v22.23.4'
V24 = 'v' + PIN_VERSION.split('-', 1)[0]
# Fixed system PATH for sandboxes: real coreutils/git come from the standard
# directories only, so host-specific PATH wrappers (e.g. protective rm
# wrappers) cannot inject commands into the stub evidence.
SANDBOX_SYSTEM_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def extract_block(source, start_marker):
    """Slice from start_marker through the block's closing `}` line."""
    start = source.index(start_marker)
    end = source.index('\n}\n', start) + len('\n}\n')
    return source[start:end]


def extract_guard(source):
    """The complete runtime guard: major constant, probe helper and gate."""
    start = source.index('REQUIRED_NODE_MAJOR=24')
    fn_start = source.index('require_node24_runtime() {', start)
    end = source.index('\n}\n', fn_start) + len('\n}\n')
    return source[start:end]


def function_body(source, name):
    return extract_block(source, f'{name}() {{')


class Sandbox:
    """Builds a disposable stub-command environment for one script run."""

    def __init__(self, root):
        self.root = root
        self.bin = root / 'bin'
        self.bin.mkdir()
        self.repo = root / 'repo'
        (self.repo / '.local/tmp').mkdir(parents=True)
        self.keyring = root / 'keyrings/nodesource.gpg'
        self.keyring.parent.mkdir()
        self.sources = root / 'apt/sources.list.d/nodesource-node24.list'
        self.sources.parent.mkdir(parents=True)
        self.apt_archives = root / 'apt/archives'
        self.apt_archives.mkdir()
        self.os_release = root / 'os-release'
        self.service_node = root / 'service-node'
        self.log = root / 'commands.log'
        self.log.write_text('', encoding='utf-8')
        self.node_version_file = root / 'node-version'
        self.node_version_file.write_text(V22 + '\n', encoding='utf-8')
        self.dpkg_version_file = root / 'dpkg-version'
        self.dpkg_version_file.write_text(OLD_VERSION + '\n', encoding='utf-8')
        self.dpkg_owned_file = root / 'dpkg-owned'
        self.dpkg_owned_file.write_text('1\n', encoding='utf-8')
        self.state = {
            'apt_update_rc': 0,
            'apt_download_rc': 0,
            'apt_install_rc': 0,
            'curl_fail': 0,
            'arch': 'amd64',
            'os_id': 'ubuntu',
            'os_codename': 'noble',
            'key_content': f'gpg-fixture-primary-fingerprint: {PIN_FINGERPRINT}\n',
            'archive_hash': PIN_SHA_AMD64,
        }
        self.write_os_release()
        self._write_default_stubs()

    def write_os_release(self):
        self.os_release.write_text(
            f'ID={self.state["os_id"]}\nVERSION_CODENAME={self.state["os_codename"]}\n'
            f'PRETTY_NAME="fixture"\n',
            encoding='utf-8')

    def stub(self, name, text):
        path = self.bin / name
        path.write_text(f'#!/usr/bin/env bash\nset -uo pipefail\n'
                        f'printf \'%s\\n\' "{name} $*" >> "{self.log}"\n' + text,
                        encoding='utf-8')
        path.chmod(0o755)

    def _write_default_stubs(self):
        log = str(self.log)
        # The fixed service binary stub is intentionally unlogged: probe traffic
        # must not pollute the command-order evidence.
        service = self.service_node
        service.write_text(f'#!/bin/bash\ncat "{self.node_version_file}"\n')
        service.chmod(0o755)
        self.stub('id', '[[ "$1" == -u ]] && { echo 0; exit 0; }\nexit 0\n')
        self.stub('sudo', 'exec "$@"\n')
        # timeout is a pass-through wrapper and stays out of the order evidence.
        timeout = self.bin / 'timeout'
        timeout.write_text('#!/usr/bin/env bash\nshift\nexec "$@"\n')
        timeout.chmod(0o755)
        # Session-local rm wrappers call `id -u` and would pollute the stub
        # evidence; the sandbox rm execs the real binary without logging.
        rm = self.bin / 'rm'
        rm.write_text('#!/usr/bin/env bash\nexec /bin/rm "$@"\n')
        rm.chmod(0o755)
        self.stub('node', f'cat "{self.node_version_file}"\n')
        self.stub('npm', 'exit 0\n')
        self.stub('apt-get', f'''
case " $* " in
  *" update "*) exit "${{APT_UPDATE_RC:-0}}" ;;
  *" --download-only "*)
    if [[ "${{APT_DOWNLOAD_RC:-0}}" == 0 ]]; then
      printf 'sha256-fixture: %s\\n' "${{APT_ARCHIVE_HASH:-}}" \\
        > "{self.apt_archives}/nodejs_${{PIN_VERSION:-}}_${{DPKG_ARCH:-}}.deb"
      printf '%s\\n' "{PIN_VERSION}" > "{self.dpkg_version_file}.staged"
    fi
    exit "${{APT_DOWNLOAD_RC:-0}}" ;;
  *" install "*)
    if [[ "${{APT_INSTALL_RC:-0}}" == 0 ]]; then
      printf '%s\\n' "${{PIN_VERSION:-}}" > "{self.dpkg_version_file}"
      if [[ "${{APT_KEEP_OWNED:-0}}" != 1 ]]; then
        printf '1\\n' > "{self.dpkg_owned_file}"
      fi
      if [[ "${{APT_STAY_V22:-0}}" != 1 ]]; then
        printf '%s\\n' "{V24}" > "{self.node_version_file}"
      fi
    fi
    exit "${{APT_INSTALL_RC:-0}}" ;;
esac
exit 0
''')
        self.stub('apt-cache',
                  f'[[ -n "${{APT_POLICY_TEXT:-}}" ]] || exit 1\n'
                  f'printf \'%s\\n\' "${{APT_POLICY_TEXT:-}}"\n')
        self.stub('dpkg', f'[[ "$1" == --print-architecture ]] || exit 1\necho "${{DPKG_ARCH:-}}"\n')
        self.stub('dpkg-query', f'''
if [[ "$1" == "-W" ]]; then
  version="$(cat "{self.dpkg_version_file}")"
  [[ -n "$version" ]] || exit 1
  printf '%s\\n' "$version"
  exit 0
fi
if [[ "$1" == "-S" ]]; then
  owned="$(cat "{self.dpkg_owned_file}")"
  [[ "$owned" == 1 ]] || exit 1
  printf 'nodejs: %s\\n' "${{@: -1}}"
  exit 0
fi
exit 1
''')
        self.stub('gpg', f'''
mode=show
for arg in "$@"; do
  [[ "$arg" == --dearmor ]] && mode=dearmor
done
if [[ "$mode" == dearmor ]]; then cat; exit 0; fi
file="${{@: -1}}"
if grep -q "gpg-fixture-primary-fingerprint:" "$file" 2>/dev/null; then
  while read -r fpr; do
    [[ -n "$fpr" ]] || continue
    printf 'pub:-:2048:1:0000000000000000:0:::::::::::::\\nfpr:::::::::%s:\\n' "$fpr"
  done < <(grep "gpg-fixture-primary-fingerprint:" "$file" | sed 's/.*fingerprint:[[:space:]]*//')
  exit 0
fi
exit 2
''')
        self.stub('curl', f'''
if [[ "${{CURL_FAIL:-0}}" == 1 ]]; then exit 5; fi
prev=""
for arg in "$@"; do
  [[ "$prev" == "-o" ]] && printf '%s\\n' "${{CURL_KEY_CONTENT:-}}" > "$arg"
  prev="$arg"
done
exit 0
''')
        self.stub('sha256sum', f'''
file="$1"
hash="$(grep -m1 "sha256-fixture:" "$file" 2>/dev/null | sed 's/.*sha256-fixture:[[:space:]]*//')"
[[ -n "$hash" ]] || exit 3
printf '%s  %s\\n' "$hash" "$file"
count_file="{self.root}/.sha256sum-calls"
calls=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
printf '%s\\n' "$calls" > "$count_file"
# Model a cache mutation exactly after one verification passed: the next
# consumer sees the archive swapped (HASH_LATE_HASH set) or evicted (empty).
if [[ -n "${{HASH_SWAP_AFTER_CALL:-}}" && "$calls" == "${{HASH_SWAP_AFTER_CALL:-}}" ]]; then
  if [[ -n "${{HASH_LATE_HASH:-}}" ]]; then
    printf 'sha256-fixture: %s\\n' "${{HASH_LATE_HASH:-}}" > "$file"
  else
    rm -f -- "$file"
  fi
fi
''')

    def env(self, **extra):
        base = {k: v for k, v in os.environ.items()
                if not k.startswith('AGENT_HUB_NODE24_TEST_') and k != 'AGENT_HUB_DEPLOY_TEST_MODE'}
        base.update({
            'PATH': f'{self.bin}:{SANDBOX_SYSTEM_PATH}',
            'AGENT_HUB_NODE24_TEST_SERVICE_NODE': str(self.service_node),
            'AGENT_HUB_NODE24_TEST_OS_RELEASE': str(self.os_release),
            'AGENT_HUB_NODE24_TEST_KEYRING': str(self.keyring),
            'AGENT_HUB_NODE24_TEST_SOURCES_FILE': str(self.sources),
            'AGENT_HUB_NODE24_TEST_APT_CACHE': str(self.apt_archives),
            'AGENT_HUB_NODE24_TEST_REPO': str(self.repo),
            'PIN_VERSION': PIN_VERSION,
            'PIN_FINGERPRINT': PIN_FINGERPRINT,
            'V24': V24,
            'DPKG_ARCH': self.state['arch'],
            'APT_ARCHIVE_HASH': self.state['archive_hash'],
            'CURL_KEY_CONTENT': self.state['key_content'],
            'CURL_FAIL': str(self.state['curl_fail']),
            'APT_UPDATE_RC': str(self.state['apt_update_rc']),
            'APT_DOWNLOAD_RC': str(self.state['apt_download_rc']),
            'APT_INSTALL_RC': str(self.state['apt_install_rc']),
            'APT_POLICY_TEXT': self.policy_text(),
        })
        base.update(extra)
        return {k: (v if isinstance(v, str) else str(v)) for k, v in base.items()}

    def policy_text(self, candidate=PIN_VERSION, origin_line=True):
        lines = [
            'nodejs:',
            f'  Installed: {OLD_VERSION}',
            f'  Candidate: {candidate}',
            '  Version table:',
            f'     {candidate} 500',
        ]
        if origin_line:
            lines.append(f'        500 {PIN_REPO} nodistro/main amd64 Packages')
        else:
            lines.append('        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages')
        return '\n'.join(lines) + '\n'

    def run_provisioner(self, pass_fds=(), **extra):
        return subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.root), env=self.env(**extra),
                              pass_fds=pass_fds, capture_output=True, text=True, timeout=30)

    def commands(self):
        return [line for line in self.log.read_text(encoding='utf-8').splitlines() if line]

    def command_prefixes(self):
        # `id` probes are excluded: this session's BASH_ENV rm-guard wrapper
        # contributes its own environment-dependent `id -u` probes around the
        # cleanup, while the guarded command sequence below stays deterministic.
        return [line.split(' ', 1)[0] for line in self.commands() if not line.startswith('id ')]

    def plain_install_calls(self):
        """apt-get install invocations that were not the bounded --download-only."""
        return [c for c in self.commands() if ' install ' in c and '--download-only' not in c]


class ProvisionerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='node24-runtime-', dir=ROOT / '.local/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Sandbox(Path(self.temporary.name))

    def assertRefused(self, result, fragment):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(fragment, result.stderr)


class ProvisionerBindingTests(ProvisionerTestCase):
    def test_binding_is_a_complete_reviewed_node24_selection(self):
        self.assertTrue(PIN_VERSION.startswith('24.'))
        self.assertRegex(PIN_VERSION, r'^\d+\.\d+\.\d+-1nodesource1$')
        self.assertRegex(PIN_FINGERPRINT, r'^[0-9A-F]{40}$')
        self.assertRegex(PIN_SHA_AMD64, r'^[0-9a-f]{64}$')
        for arch in ('AMD64', 'ARM64'):
            self.assertRegex(source_pin(f'NODE_PACKAGE_SHA256_{arch}'), r'^[0-9a-f]{64}$')
        self.assertEqual(PIN_REPO, 'https://deb.nodesource.com/node_24.x')
        self.assertEqual(PIN_KEY_URL, 'https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key')
        self.assertEqual(source_pin('NODESOURCE_SUITE'), 'nodistro')
        self.assertEqual(source_pin('NODESOURCE_COMPONENT'), 'main')
        self.assertRegex(PROVISIONER_SOURCE, r'(?m)^REQUIRED_NODE_MAJOR=24$')

    def test_production_path_seams_default_to_the_fixed_service_locations(self):
        for seam, default in (
            ('AGENT_HUB_NODE24_TEST_SERVICE_NODE', '/usr/bin/node'),
            ('AGENT_HUB_NODE24_TEST_OS_RELEASE', '/etc/os-release'),
            ('AGENT_HUB_NODE24_TEST_KEYRING', '/usr/share/keyrings/nodesource.gpg'),
            ('AGENT_HUB_NODE24_TEST_SOURCES_FILE', '/etc/apt/sources.list.d/nodesource-node24.list'),
            ('AGENT_HUB_NODE24_TEST_APT_CACHE', '/var/cache/apt/archives'),
            ('AGENT_HUB_NODE24_TEST_REPO', '/opt/agent-hub'),
        ):
            self.assertIn(f'{seam}:-{default}', PROVISIONER_SOURCE,
                          f'{seam} production default must stay {default}')

    def test_provisioner_never_pipes_a_remote_script_or_upgrades_the_os(self):
        self.assertNotIn('setup_24.x', PROVISIONER_SOURCE)
        self.assertNotRegex(PROVISIONER_SOURCE, r'apt-get[^\n|]*\b(upgrade|dist-upgrade|full-upgrade)\b')
        self.assertNotRegex(PROVISIONER_SOURCE, r'curl[^|]*\|\s*(sudo\s+)?(ba)?sh')
        self.assertNotRegex(PROVISIONER_SOURCE, r'\bwget\b')
        # The only downloaded bytes are the approved signing key, fetched to a file.
        self.assertIn(f'-o "$PROVISION_WORK/repo.key" "$NODESOURCE_KEY_URL"', PROVISIONER_SOURCE)


class ProvisionerFlowTests(ProvisionerTestCase):
    def test_supported_node22_to_node24_installs_in_binding_order(self):
        result = self.sandbox.run_provisioner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('runtime ready for the deploy transaction', result.stdout)
        # Refusals and binding happen before package effects; verification last.
        self.assertEqual(self.sandbox.command_prefixes(), [
            'dpkg',           # platform binding before any network or package effect
            'dpkg-query',     # current installed identity (refusal inputs)
            'curl',           # approved key download — the only fetched bytes
            'gpg',            # dearmor
            'gpg',            # fingerprint validation before any keyring write
            'gpg',            # installed keyring re-verification
            'apt-get',        # apt-get update before candidate binding
            'apt-cache',      # origin/version binding before download
            'apt-get',        # bounded download-only of the pinned artifact
            'sha256sum',      # artifact hash at download verification
            'sha256sum',      # artifact hash re-bound at consumption
            'apt-get',        # the single bounded install of the verified file
            'dpkg-query',     # postcheck: ownership
            'dpkg-query',     # postcheck: version
            'node',           # postcheck: PATH build toolchain major 24
        ])
        logged = '\n'.join(self.sandbox.commands())
        self.assertIn('apt-get update', logged)
        archive = self.sandbox.apt_archives / f'nodejs_{PIN_VERSION}_amd64.deb'
        # The install consumes the exact verified local artifact: no version
        # operand apt could re-resolve or re-download, only the verified file.
        self.assertIn(f'apt-get -y --no-install-recommends install {archive}', logged)
        for line in self.sandbox.plain_install_calls():
            self.assertIn(str(archive), line)
            self.assertNotIn('nodejs=', line)
        self.assertTrue(archive.exists())
        self.assertIn(PIN_FINGERPRINT, self.sandbox.keyring.read_text(encoding='utf-8'))
        expected_line = (f'deb [arch=amd64 signed-by={self.sandbox.keyring}] '
                         f'{PIN_REPO} nodistro main')
        self.assertEqual(self.sandbox.sources.read_text(encoding='utf-8'), expected_line + '\n')
        # The install converged the modeled runtime and package identity to 24.
        self.assertEqual(self.sandbox.node_version_file.read_text().strip(), V24)
        self.assertEqual(self.sandbox.dpkg_version_file.read_text().strip(), PIN_VERSION)

    def test_already_node24_with_matching_identity_is_a_verified_no_op(self):
        self.sandbox.node_version_file.write_text(V24 + '\n', encoding='utf-8')
        self.sandbox.dpkg_version_file.write_text(PIN_VERSION + '\n', encoding='utf-8')
        result = self.sandbox.run_provisioner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('no-op', result.stdout)
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('curl', prefixes)
        self.assertNotIn('sha256sum', prefixes)
        self.assertIn('dpkg-query', prefixes, 'the no-op must still verify package identity')
        self.assertFalse(self.sandbox.sources.exists(), 'no-op must not write an apt source')
        self.assertFalse(self.sandbox.keyring.exists(), 'no-op must not write the keyring')
        self.assertFalse((self.sandbox.repo / '.local/tmp/.deploy-transaction.lock').exists(),
                         'the read-only no-op path takes no deploy lock')

    def test_correct_existing_keyring_and_source_are_kept(self):
        self.sandbox.keyring.write_text(self.sandbox.state['key_content'], encoding='utf-8')
        self.sandbox.sources.write_text(
            f'deb [arch=amd64 signed-by={self.sandbox.keyring}] {PIN_REPO} nodistro main\n',
            encoding='utf-8')
        result = self.sandbox.run_provisioner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('already bound', result.stdout)
        self.assertEqual(self.sandbox.sources.read_text(encoding='utf-8'),
                         f'deb [arch=amd64 signed-by={self.sandbox.keyring}] {PIN_REPO} nodistro main\n')


class ProvisionerRefusalTests(ProvisionerTestCase):
    def refusals_touch_nothing(self, result):
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('apt-cache', prefixes)
        self.assertFalse(self.sandbox.sources.exists())
        self.assertFalse(self.sandbox.keyring.exists())

    def test_unsupported_os_refuses_before_any_effect(self):
        for os_id, codename in (('ubuntu', 'jammy'), ('debian', 'bullseye'), ('fedora', 'forty')):
            self.sandbox.state['os_id'] = os_id
            self.sandbox.state['os_codename'] = codename
            self.sandbox.write_os_release()
            result = self.sandbox.run_provisioner()
            self.assertRefused(result, 'unsupported OS')
            self.refusals_touch_nothing(result)
            self.assertEqual(self.sandbox.commands(), [])

    def test_unsupported_architecture_refuses_before_any_effect(self):
        self.sandbox.state['arch'] = 'armv7l'
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'unsupported package architecture')
        self.refusals_touch_nothing(result)

    def test_missing_os_identity_refuses(self):
        self.sandbox.os_release.unlink()
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'cannot read OS identity')
        self.refusals_touch_nothing(result)

    def test_key_fingerprint_mismatch_refuses_before_keyring_or_apt(self):
        self.sandbox.state['key_content'] = 'gpg-fixture-primary-fingerprint: DEADBEAFDEADBEAFDEADBEAFDEADBEAFDEADBEAF\n'
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'does not match the approved fingerprint')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('apt-cache', prefixes)
        self.assertFalse(self.sandbox.keyring.exists())
        self.assertFalse(self.sandbox.sources.exists())

    def test_corrupt_key_download_refuses(self):
        self.sandbox.state['curl_fail'] = 1
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'could not download the approved NodeSource signing key')
        self.refusals_touch_nothing(result)

    def test_foreign_or_mutated_apt_source_is_never_overwritten(self):
        self.sandbox.sources.parent.mkdir(parents=True, exist_ok=True)
        self.sandbox.sources.write_text('deb https://example.invalid/node nodistro main\n', encoding='utf-8')
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'refusing to overwrite a foreign or mutated apt source')
        self.assertEqual(self.sandbox.sources.read_text(encoding='utf-8'),
                         'deb https://example.invalid/node nodistro main\n')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)

    def test_foreign_existing_keyring_is_preserved_and_refused(self):
        # R2 negative control: foreign key material keeps its identity and
        # bytes, and no package or application effect follows the refusal.
        foreign = 'gpg-fixture-primary-fingerprint: DEAD\nplus more foreign bytes\n'
        self.sandbox.keyring.write_text(foreign, encoding='utf-8')
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'refusing to overwrite an existing keyring')
        self.assertEqual(self.sandbox.keyring.read_text(encoding='utf-8'), foreign,
                         'the existing keyring must be preserved byte for byte')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('apt-cache', prefixes)
        self.assertFalse(self.sandbox.sources.exists())
        self.assertFalse((self.sandbox.apt_archives / f'nodejs_{PIN_VERSION}_amd64.deb').exists())

    def test_symlinked_keyring_refuses_instead_of_writing_through_the_link(self):
        target = self.sandbox.root / 'keyrings/foreign-target.gpg'
        target.write_text('gpg-fixture-primary-fingerprint: DEAD\n', encoding='utf-8')
        self.sandbox.keyring.symlink_to(target)
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'refusing to overwrite an existing keyring')
        self.assertTrue(self.sandbox.keyring.is_symlink())
        self.assertEqual(target.read_text(encoding='utf-8'), 'gpg-fixture-primary-fingerprint: DEAD\n')
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_dangling_keyring_symlink_refuses(self):
        self.sandbox.keyring.symlink_to(self.sandbox.root / 'keyrings/never-created.gpg')
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'refusing to overwrite an existing keyring')
        self.assertTrue(self.sandbox.keyring.is_symlink())
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_non_regular_keyring_entry_refuses(self):
        self.sandbox.keyring.unlink(missing_ok=True)
        self.sandbox.keyring.mkdir()
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'refusing to overwrite an existing keyring')
        self.assertTrue(self.sandbox.keyring.is_dir())
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_symlinked_apt_source_refuses_instead_of_writing_through_the_link(self):
        target = self.sandbox.sources.parent / 'foreign.list'
        target.write_text('deb https://example.invalid/node nodistro main\n', encoding='utf-8')
        self.sandbox.sources.symlink_to(target)
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'symlink or non-regular entry')
        self.assertTrue(self.sandbox.sources.is_symlink())
        self.assertEqual(target.read_text(encoding='utf-8'), 'deb https://example.invalid/node nodistro main\n')
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_symlinked_apt_source_with_exact_content_refuses_and_preserves_link(self):
        # R3: Even when the symlink target contains the exact expected line,
        # the symlink must be refused before dereferencing or comparing.
        expected = (
            f'deb [arch={self.sandbox.state["arch"]} signed-by={self.sandbox.keyring}] '
            f'{PIN_REPO} nodistro main\n'
        )
        target = self.sandbox.sources.parent / 'linked-expected.list'
        target.write_text(expected, encoding='utf-8')
        self.sandbox.sources.symlink_to(target)
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'symlink or non-regular entry')
        self.assertTrue(self.sandbox.sources.is_symlink())
        self.assertEqual(target.read_text(encoding='utf-8'), expected)
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_dangling_apt_source_symlink_refuses(self):
        self.sandbox.sources.symlink_to(self.sandbox.sources.parent / 'missing-source.list')
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'symlink or non-regular entry')
        self.assertTrue(self.sandbox.sources.is_symlink())
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_non_regular_apt_source_refuses(self):
        self.sandbox.sources.mkdir()
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'symlink or non-regular entry')
        self.assertTrue(self.sandbox.sources.is_dir())
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_candidate_from_foreign_origin_refuses_before_download(self):
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(APT_POLICY_TEXT=self.sandbox.policy_text(origin_line=False)),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'not offered from the approved origin')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes[prefixes.index('apt-cache') + 1:])

    def test_candidate_version_mismatch_refuses_before_download(self):
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(APT_POLICY_TEXT=self.sandbox.policy_text(candidate='24.99.0-1nodesource1')),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'is not the selected version')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes[prefixes.index('apt-cache') + 1:])

    def test_artifact_hash_mismatch_refuses_before_install(self):
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(APT_ARCHIVE_HASH='0' * 64),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'hash mismatch')
        self.assertIn('--download-only', '\n'.join(self.sandbox.commands()))
        self.assertEqual(self.sandbox.plain_install_calls(), [])

    def test_archive_swapped_after_verification_refuses_at_consumption(self):
        # R3 negative control: the cached archive is replaced after the
        # download-time hash passed; the consumption-time re-bind must catch
        # it and no unverified selected archive may be installed.
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(HASH_SWAP_AFTER_CALL='1', HASH_LATE_HASH='f' * 64),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'changed between verification and consumption')
        self.assertEqual(len(self.sandbox.plain_install_calls()), 0)

    def test_archive_evicted_after_verification_refuses_before_install(self):
        # Synthetic cache removal between verification and consumption.
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(HASH_SWAP_AFTER_CALL='1'),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'the verified Node24 archive is missing')
        self.assertEqual(len(self.sandbox.plain_install_calls()), 0)

    def test_newer_installed_node24_refuses_an_implicit_downgrade(self):
        newer = '24.99.0-1nodesource1'
        self.sandbox.node_version_file.write_text('v24.99.0\n', encoding='utf-8')
        self.sandbox.dpkg_version_file.write_text(newer + '\n', encoding='utf-8')
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'implicit package downgrade')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('curl', prefixes)

    def test_broken_identity_of_selected_version_refuses_operator_reconciliation(self):
        # Package says the selected pin is installed, but the binary is wrong.
        self.sandbox.node_version_file.write_text(V22 + '\n', encoding='utf-8')
        self.sandbox.dpkg_version_file.write_text(PIN_VERSION + '\n', encoding='utf-8')
        result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'operator reconciliation required')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)


class ProvisionerFailureReceiptTests(ProvisionerTestCase):
    def test_index_refresh_failure_reports_before_any_package_effect(self):
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(APT_UPDATE_RC=1),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'apt-get update failed')
        prefixes = self.sandbox.command_prefixes()
        self.assertEqual(prefixes.count('apt-get'), 1)

    def test_download_failure_never_installs(self):
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(APT_DOWNLOAD_RC=1),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'no package was installed')
        self.assertEqual(self.sandbox.plain_install_calls(), [])

    def test_install_failure_is_a_single_honest_attempt(self):
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(APT_INSTALL_RC=1),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'package manager owns recovery')
        self.assertEqual(len(self.sandbox.plain_install_calls()), 1)

    def test_postinstall_probe_failure_reports_broken_runtime_without_retry(self):
        # Model a broken install: apt exits 0 but the service binary never
        # converges to Node24. Postcheck must refuse and never re-install.
        result = subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.sandbox.root),
                                env=self.sandbox.env(APT_STAY_V22='1'),
                                capture_output=True, text=True, timeout=30)
        self.assertRefused(result, 'post-install check failed')
        self.assertIn('is Node 22, not Node24', result.stderr)
        self.assertEqual(len(self.sandbox.plain_install_calls()), 1)

    def test_postinstall_ownership_failure_refuses(self):
        # Model an install that leaves the service binary unowned by the package.
        self.sandbox.dpkg_owned_file.write_text('0\n', encoding='utf-8')
        result = self.sandbox.run_provisioner(APT_KEEP_OWNED='1')
        self.assertRefused(result, 'not owned by the nodejs package')
        self.assertEqual(len(self.sandbox.plain_install_calls()), 1)

    def test_provisioning_refuses_while_a_deploy_transaction_lock_is_held(self):
        lock_path = self.sandbox.repo / '.local/tmp/.deploy-transaction.lock'
        with open(lock_path, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.sandbox.run_provisioner()
        self.assertRefused(result, 'another Hub2 deploy transaction operation is running')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('curl', prefixes)
        # Releasing the lock lets the same mutation path proceed.
        result = self.sandbox.run_provisioner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inherited_custody_under_a_real_lock_owner_provisions(self):
        # R1: The deploy transaction passes its actual held open-file-description
        # on .deploy-transaction.lock to the child provisioner; the child
        # proves same-OFD custody and parent lock continuity persists after exit.
        lock_path = self.sandbox.repo / '.local/tmp/.deploy-transaction.lock'
        with open(lock_path, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.sandbox.run_provisioner(
                pass_fds=(lock.fileno(),),
                AGENT_HUB_NODE24_LOCK_INHERITED=str(lock_path),
                AGENT_HUB_NODE24_LOCK_FD=str(lock.fileno()),
            )
            # Parent lock continuity after child exits: an independent OFD must still fail.
            probe = os.open(lock_path, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('runtime ready for the deploy transaction', result.stdout)
        self.assertIn('apt-get', self.sandbox.command_prefixes())

    def test_inherited_custody_without_a_real_lock_owner_refuses(self):
        # An environment flag alone proves nothing: with no owner holding the
        # lock and no inherited fd, the run must refuse before any effect.
        lock_path = self.sandbox.repo / '.local/tmp/.deploy-transaction.lock'
        lock_path.write_text('', encoding='utf-8')
        result = self.sandbox.run_provisioner(
            AGENT_HUB_NODE24_LOCK_INHERITED=str(lock_path),
            AGENT_HUB_NODE24_LOCK_FD='9',
        )
        self.assertRefused(result, 'deploy-lock custody was not inherited')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('curl', prefixes)
        self.assertFalse(self.sandbox.sources.exists())

    def test_inherited_custody_flag_only_while_foreign_owner_holds_lock_refuses(self):
        # R1 negative: a foreign process holds the lock, and a caller sets the
        # inherited env flag without passing the owner's open-file-description.
        lock_path = self.sandbox.repo / '.local/tmp/.deploy-transaction.lock'
        with open(lock_path, 'w') as foreign_lock:
            fcntl.flock(foreign_lock, fcntl.LOCK_EX)
            result = self.sandbox.run_provisioner(
                AGENT_HUB_NODE24_LOCK_INHERITED=str(lock_path),
                AGENT_HUB_NODE24_LOCK_FD='9',
            )
        self.assertRefused(result, 'deploy-lock custody was not inherited')
        prefixes = self.sandbox.command_prefixes()
        self.assertNotIn('apt-get', prefixes)
        self.assertNotIn('curl', prefixes)
        self.assertFalse(self.sandbox.sources.exists())

    def test_inherited_custody_with_different_ofd_while_foreign_owner_holds_lock_refuses(self):
        # R1 negative: even when the passed fd points to the right lock inode, if it is a
        # different open-file-description from the foreign lock holder, it refuses.
        lock_path = self.sandbox.repo / '.local/tmp/.deploy-transaction.lock'
        with open(lock_path, 'w') as foreign_lock:
            fcntl.flock(foreign_lock, fcntl.LOCK_EX)
            other_ofd = os.open(lock_path, os.O_RDONLY)
            try:
                result = self.sandbox.run_provisioner(
                    pass_fds=(other_ofd,),
                    AGENT_HUB_NODE24_LOCK_INHERITED=str(lock_path),
                    AGENT_HUB_NODE24_LOCK_FD=str(other_ofd),
                )
            finally:
                os.close(other_ofd)
        self.assertRefused(result, 'deploy-lock custody was not inherited')
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())
        self.assertNotIn('curl', self.sandbox.command_prefixes())

    def test_inherited_custody_with_unlocked_fd_and_no_owner_refuses(self):
        # R1 negative: an open fd on the lock inode without an active lock holder refuses.
        lock_path = self.sandbox.repo / '.local/tmp/.deploy-transaction.lock'
        lock_path.write_text('', encoding='utf-8')
        unlocked_fd = os.open(lock_path, os.O_RDONLY)
        try:
            result = self.sandbox.run_provisioner(
                pass_fds=(unlocked_fd,),
                AGENT_HUB_NODE24_LOCK_INHERITED=str(lock_path),
                AGENT_HUB_NODE24_LOCK_FD=str(unlocked_fd),
            )
        finally:
            os.close(unlocked_fd)
        self.assertRefused(result, 'deploy-lock custody was not inherited')
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_foreign_inherited_lock_path_refuses(self):
        other = self.sandbox.root / 'elsewhere.lock'
        with open(other, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.sandbox.run_provisioner(
                AGENT_HUB_NODE24_LOCK_INHERITED=str(other),
                AGENT_HUB_NODE24_LOCK_FD='9',
            )
        self.assertRefused(result, 'foreign lock custody path')
        self.assertNotIn('apt-get', self.sandbox.command_prefixes())

    def test_provisioner_takes_the_same_single_owner_lock_as_the_transaction(self):
        # R1: ONE existing deploy-lock owner spans package provisioning and the
        # application transaction. The transaction derives the staged
        # provisioner path from the validated deploy SHA and invokes it inside
        # its held fd9 lock; the provisioner never opens a nested flock in
        # that mode and never trusts a skip-lock flag — custody is proven by
        # binding the inherited fd to the fixed lock inode and verifying same-OFD
        # ownership while an independent acquisition fails.
        self.assertIn('flock -n 9', PROVISIONER_SOURCE)
        self.assertIn('.deploy-transaction.lock', PROVISIONER_SOURCE)
        self.assertIn('.deploy-transaction.lock', TRANSACTION_SOURCE)
        self.assertIn('AGENT_HUB_NODE24_LOCK_INHERITED', PROVISIONER_SOURCE)
        self.assertIn('AGENT_HUB_NODE24_LOCK_FD', PROVISIONER_SOURCE)
        self.assertIn('AGENT_HUB_NODE24_LOCK_FD=9', TRANSACTION_SOURCE)
        self.assertIn('deploy-lock custody was not inherited', PROVISIONER_SOURCE)
        self.assertIn('provision_node24_runtime', TRANSACTION_SOURCE)
        self.assertIn('ci-node24-provision-$deploy_sha/provision-node24.sh', TRANSACTION_SOURCE)
        # No skip-lock escape hatch exists anywhere in the feature.
        for source in (PROVISIONER_SOURCE, TRANSACTION_SOURCE, INSTALLER_SOURCE):
            self.assertNotRegex(source, r'SKIP_LOCK|SKIPLOCK|LOCK_SKIP|NO_LOCK')
        # The invocation happens inside apply, before the candidate checkout,
        # npm dependency mutation, transaction state and unit writes.
        apply_body = function_body(TRANSACTION_SOURCE, 'apply_release')
        invoke_idx = apply_body.index('provision_node24_runtime "$deploy_sha"')
        for later in ('git -C "$REPO" checkout --detach --force "$deploy_sha"',
                      'install_release_dependencies',
                      'install-hub2-node.sh" --release-mode',
                      'install -d -o root -g root -m 700 "$TRANSACTION_DIR"'):
            self.assertLess(invoke_idx, apply_body.index(later),
                            f'provisioning must precede: {later}')
        # The provisioner's inherited mode proves genuine inherited-OFD custody
        # bound to the fixed lock inode instead of trusting an env string or
        # mere contention: an independent probe must fail with EAGAIN/EWOULDBLOCK
        # AND non-blocking LOCK_EX on the inherited fd must succeed.
        custody = function_body(PROVISIONER_SOURCE, 'main')
        self.assertIn('fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)', custody)
        self.assertIn('fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)', custody)
        self.assertLess(custody.index('deploy-lock custody was not inherited'),
                        custody.index('PROVISION_WORK="$(mktemp'))


class GuardContractTests(unittest.TestCase):
    def test_transaction_and_installer_guards_are_identical(self):
        self.assertEqual(extract_guard(TRANSACTION_SOURCE), extract_guard(INSTALLER_SOURCE),
                         'installer and transaction runtime guards must stay byte-identical')

    def test_guards_probe_the_fixed_service_binary_not_only_path_node(self):
        for source in (TRANSACTION_SOURCE, INSTALLER_SOURCE):
            self.assertIn('${AGENT_HUB_NODE24_TEST_SERVICE_NODE:-/usr/bin/node}', source)
            self.assertIn('require_node24_runtime', source)

    def test_transaction_guard_runs_before_any_mutation(self):
        guard_idx = TRANSACTION_SOURCE.index(
            'if [[ "${AGENT_HUB_DEPLOY_TEST_MODE:-0}" == 1 && -z "${AGENT_HUB_NODE24_TEST_SERVICE_NODE:-}" ]]; then')
        for later in ('install -d -o root -g root -m 700 "$TRANSACTION_DIR"',
                      'install_release_dependencies\n  if [[ "$cli_mode" == matched-cli ]]',
                      'git -C "$REPO" checkout --detach --force "$deploy_sha"',
                      'install-hub2-node.sh" --release-mode'):
            self.assertLess(guard_idx, TRANSACTION_SOURCE.index(later),
                            f'runtime guard must precede: {later}')

    def test_transaction_guard_skips_in_test_mode_without_fixture(self):
        self.assertIn('Node24 runtime guard skipped: test mode without a AGENT_HUB_NODE24_TEST_SERVICE_NODE fixture.',
                      TRANSACTION_SOURCE)

    def test_installer_guard_runs_before_the_first_write(self):
        call_idx = INSTALLER_SOURCE.index('\n# Direct-call guard:')
        call_idx = INSTALLER_SOURCE.index('require_node24_runtime\n', call_idx)
        self.assertEqual(INSTALLER_SOURCE.count('\nrequire_node24_runtime\n'), 1)
        for later in ('install -m 644 "$DEPLOY_DIR/agent-hub-hub2.service"',
                      'install -d -m 750 -o alexey -g alexey "$DB_DIR"',
                      'echo "== secrets (.dev.vars'):
            self.assertLess(call_idx, INSTALLER_SOURCE.index(later),
                            f'installer runtime guard must precede: {later}')


class GuardBehaviorTests(unittest.TestCase):
    """Executes the extracted guard block with stub binaries."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='node24-guard-', dir=ROOT / '.local/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.guard = extract_guard(TRANSACTION_SOURCE)

    def run_guard(self, service_version=None, path_version=None, service_missing=False,
                  npm_present=True, extra_env=None):
        log = self.root / 'commands.log'
        log.write_text('', encoding='utf-8')
        node_stub = self.bin / 'node'
        if path_version is None:
            node_stub.unlink(missing_ok=True)
        else:
            node_stub.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "node $*" >> "{log}"\n'
                                 f'printf \'{path_version}\\n\'\n', encoding='utf-8')
            node_stub.chmod(0o755)
        npm_stub = self.bin / 'npm'
        if npm_present:
            npm_stub.write_text('#!/bin/bash\nexit 0\n')
            npm_stub.chmod(0o755)
        else:
            npm_stub.unlink(missing_ok=True)
        service_node = self.root / 'service-node'
        if service_missing:
            service_node.unlink(missing_ok=True)
        else:
            version = service_version if service_version is not None else V24
            if version == 'BROKEN':
                service_node.write_text('#!/bin/bash\nexit 3\n')
            else:
                service_node.write_text(f'#!/bin/bash\nprintf \'{version}\\n\'\n')
            service_node.chmod(0o755)
        harness = (
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            'fail() { printf \'ERROR: %s\\n\' "$*" >&2; exit 1; }\n'
            + self.guard + '\nrequire_node24_runtime\n'
        )
        script = self.root / 'guard.sh'
        script.write_text(harness, encoding='utf-8')
        script.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_HUB_NODE24_TEST_')}
        # Only the stub bin is on PATH: the guard must find exactly the
        # fixture-provided node/npm and nothing from the host.
        env['PATH'] = str(self.bin)
        if not service_missing:
            env['AGENT_HUB_NODE24_TEST_SERVICE_NODE'] = str(service_node)
        env.update(extra_env or {})
        return subprocess.run(['/bin/bash', str(script)], cwd=str(self.root), env=env,
                              capture_output=True, text=True, timeout=15)

    def test_matching_node24_service_and_build_pass(self):
        result = self.run_guard(service_version=V24, path_version=V24)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Node24 runtime check passed', result.stdout)
        self.assertIn(V24, result.stdout)

    def test_node22_service_refuses_with_provisioning_guidance(self):
        result = self.run_guard(service_version=V22, path_version=V24)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('is Node 22, not Node24', result.stderr)
        self.assertIn('deploy/vm/provision-node24.sh', result.stderr)

    def test_missing_service_binary_refuses(self):
        result = self.run_guard(service_missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('is missing; provision Node24 first', result.stderr)

    def test_failing_service_probe_refuses(self):
        result = self.run_guard(service_version='BROKEN', path_version=V24)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runtime probe failed', result.stderr)

    def test_wrong_build_toolchain_major_refuses(self):
        result = self.run_guard(service_version=V24, path_version=V22)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('would run on Node 22', result.stderr)

    def test_missing_path_node_refuses(self):
        result = self.run_guard(service_version=V24, path_version=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no node on PATH', result.stderr)

    def test_missing_npm_refuses(self):
        result = self.run_guard(service_version=V24, path_version=V24, npm_present=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('npm is required', result.stderr)


class TransactionOrderingTests(unittest.TestCase):
    """Runs the real transaction script in test mode with a runtime fixture."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='node24-tx-', dir=ROOT / '.local/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.work = self.root / 'work'
        self.state = self.root / 'state'
        self.log = self.root / 'commands.log'
        self.log.write_text('', encoding='utf-8')
        for name, text in (
            ('sudo', 'exec "$@"\n'),
            ('id', '[[ "$1" == -u ]] && { echo 0; exit 0; }\nexit 0\n'),
            ('npm', f'printf \'%s\\n\' "npm $*" >> "{self.log}"\nexit 0\n'),
            ('systemctl', 'exit 0\n'),
            ('caddy', 'exit 0\n'),
            ('curl', 'exit 0\n'),
        ):
            path = self.bin / name
            path.write_text(f'#!/usr/bin/env bash\nset -uo pipefail\n'
                            f'printf \'%s\\n\' "{name} $*" >> "{self.log}"\n' + text,
                            encoding='utf-8')
            path.chmod(0o755)
        node = self.bin / 'node'
        node.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "node $*" >> "{self.log}"\n'
                        f'printf \'{V24}\\n\'\n')
        node.chmod(0o755)
        self._prepare_repo()
        self.previous_sha = subprocess.run(['git', '-C', str(self.work), 'rev-parse', 'HEAD'],
                                           capture_output=True, text=True, check=True).stdout.strip()
        self.candidate = self.previous_sha

    def _prepare_repo(self):
        def git(*args):
            return subprocess.run(['git', *args], capture_output=True, text=True, check=True, timeout=30)
        git('init', '--bare', '-q', str(self.remote))
        git('init', '-q', '-b', 'main', str(self.seed))
        git('-C', str(self.seed), 'config', 'user.name', 'node24 fixture')
        git('-C', str(self.seed), 'config', 'user.email', 'fixture@example.invalid')
        (self.seed / '.dev.vars').write_text('HUB2_RELEASE_MODE=bootstrap\n', encoding='utf-8')
        (self.seed / '.gitignore').write_text('.dev.vars\n.local/\n', encoding='utf-8')
        preflight = self.seed / 'scripts/schema-rollback-preflight.py'
        preflight.parent.mkdir(parents=True, exist_ok=True)
        preflight.write_text('import sys\nprint("apply")\n', encoding='utf-8')
        git('-C', str(self.seed), 'add', '.')
        git('-C', str(self.seed), 'commit', '-qm', 'fixture previous release')
        git('-C', str(self.seed), 'push', '-q', str(self.remote), 'main')
        git('clone', '-q', '--branch', 'main', str(self.remote), str(self.work))
        git('-C', str(self.work), 'config', 'user.name', 'node24 fixture')
        git('-C', str(self.work), 'config', 'user.email', 'fixture@example.invalid')
        (self.work / '.dev.vars').write_text('HUB2_RELEASE_MODE=bootstrap\n', encoding='utf-8')

    def run_apply(self, runtime_fixture=True, service_version=V22, provision=False,
                  qualified_previous_sha=None, caddy_mode='hub-only', extra_env=None):
        service_node = self.root / 'service-node'
        if runtime_fixture:
            service_node.write_text(f'#!/usr/bin/env bash\nprintf \'{service_version}\\n\'\n')
            service_node.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('AGENT_HUB_NODE24_TEST_') and k != 'AGENT_HUB_DEPLOY_TEST_MODE'}
        env.update({
            'PATH': f'{self.bin}:{SANDBOX_SYSTEM_PATH}',
            'AGENT_HUB_DEPLOY_TEST_MODE': '1',
            'AGENT_HUB_TEST_REPO': str(self.work),
            'AGENT_HUB_TEST_TRANSACTION_DIR': str(self.state),
            'AGENT_HUB_TEST_CADDY_CONFIG': str(self.root / 'Caddyfile'),
            'AGENT_HUB_TEST_CADDY_SITE': str(self.root / 'sites/hub.caddy'),
        })
        if runtime_fixture:
            env['AGENT_HUB_NODE24_TEST_SERVICE_NODE'] = str(service_node)
        if extra_env:
            env.update(extra_env)
        args = ['apply', self.candidate, 'bootstrap', caddy_mode, 'legacy']
        if provision:
            args.append('provision-node24')
            args.append(qualified_previous_sha if qualified_previous_sha is not None else self.previous_sha)
        return subprocess.run(['/bin/bash', str(TRANSACTION), *args],
                              cwd=str(self.root), env=env, capture_output=True, text=True, timeout=120)

    def stage_provisioner_fixture(self, body):
        """Commits candidate provision-node24.sh bytes and stages them at the SHA-derived path."""
        script_bytes = f'#!/usr/bin/env bash\n{body}\n'
        provisioner_in_repo = self.seed / 'deploy/vm/provision-node24.sh'
        provisioner_in_repo.parent.mkdir(parents=True, exist_ok=True)
        provisioner_in_repo.write_text(script_bytes, encoding='utf-8')
        provisioner_in_repo.chmod(0o755)
        subprocess.run(['git', '-C', str(self.seed), 'add', 'deploy/vm/provision-node24.sh'],
                       capture_output=True, text=True, check=True, timeout=30)
        subprocess.run(['git', '-C', str(self.seed), 'commit', '-qm', 'candidate provisioner fixture'],
                       capture_output=True, text=True, check=True, timeout=30)
        subprocess.run(['git', '-C', str(self.seed), 'push', '-q', str(self.remote), 'main'],
                       capture_output=True, text=True, check=True, timeout=30)
        self.candidate = subprocess.run(['git', '-C', str(self.seed), 'rev-parse', 'HEAD'],
                                        capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        staged = self.work / '.local/tmp' / f'ci-node24-provision-{self.candidate}' / 'provision-node24.sh'
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(script_bytes, encoding='utf-8')
        staged.chmod(0o750)
        return staged

    def test_staged_provisioner_runs_inside_the_deploy_lock_before_app_effects(self):
        # R1 interleaving control, exercised against the real transaction:
        # while the transaction invokes the staged provisioner, a competing
        # non-blocking acquisition of the same deploy lock must fail, and the
        # invocation must precede any npm or transaction-state effect.
        lock_path = self.work / '.local/tmp/.deploy-transaction.lock'
        self.stage_provisioner_fixture(
            f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\n'
            f'if flock -n "{lock_path}" true; then\n'
            f'  printf \'%s\\n\' "competing-lock-acquired" >> "{self.log}"\n'
            f'fi\nexit 0\n')
        result = self.run_apply(runtime_fixture=False, provision=True)
        self.assertIn('running the staged candidate provisioner', result.stdout)
        log = self.log.read_text(encoding='utf-8')
        self.assertIn('provisioner invoked', log)
        self.assertNotIn('competing-lock-acquired', log,
                         'a competing lock acquisition must fail while the transaction owns custody')
        if 'npm ' in log:
            self.assertLess(log.index('provisioner invoked'), log.index('npm '))

    def test_failing_staged_provisioner_is_an_honest_incident_before_any_effect(self):
        self.stage_provisioner_fixture('echo "modeled apt failure" >&2\nexit 3\n')
        result = self.run_apply(runtime_fixture=False, provision=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Node24 runtime provisioning failed inside the deploy transaction', result.stderr)
        self.assertIn('no automatic OS rollback exists', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_missing_staged_provisioner_refuses_before_any_effect(self):
        result = self.run_apply(runtime_fixture=False, provision=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing or not a regular file', result.stderr)
        self.assertNotIn('npm', self.log.read_text(encoding='utf-8'))
        self.assertFalse(self.state.exists())

    def test_symlinked_staged_provisioner_refuses(self):
        target = self.work / '.local/tmp' / 'evil-provisioner.sh'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
        staged = self.work / '.local/tmp' / f'ci-node24-provision-{self.candidate}' / 'provision-node24.sh'
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.symlink_to(target)
        result = self.run_apply(runtime_fixture=False, provision=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing or not a regular file', result.stderr)
        self.assertNotIn('npm', self.log.read_text(encoding='utf-8'))
        self.assertFalse(self.state.exists())

    def test_group_writable_staged_provisioner_refuses(self):
        self.stage_provisioner_fixture('exit 0\n').chmod(0o770)
        result = self.run_apply(runtime_fixture=False, provision=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('group/other writable', result.stderr)
        self.assertFalse(self.state.exists())

    def test_altered_staged_provisioner_with_valid_owner_and_mode_refuses(self):
        # R2: Same-owner, mode-valid, syntactically valid staged script whose
        # bytes differ from deploy_sha:deploy/vm/provision-node24.sh must refuse
        # before execution and before any package/app effect.
        staged = self.stage_provisioner_fixture('printf \'%s\\n\' "expected candidate" >> "{}"\nexit 0\n'.format(self.log))
        staged.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "altered script executed" >> "{self.log}"\nexit 0\n', encoding='utf-8')
        staged.chmod(0o750)
        result = self.run_apply(runtime_fixture=False, provision=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match candidate', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('altered script executed', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_previous_sha_mismatch_under_lock_refuses_before_provisioning_or_app_effects(self):
        # R4: If VM HEAD changed since the unlocked CI current-sha read, apply
        # must refuse under the held deploy lock before provisioning or app effects.
        self.stage_provisioner_fixture(f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\nexit 0\n')
        wrong_sha = '0' * 40
        result = self.run_apply(runtime_fixture=False, provision=True, qualified_previous_sha=wrong_sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match the Node24-qualified previous release', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('provisioner invoked', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_previous_sha_none_when_checkout_exists_refuses_before_provisioning(self):
        # R4: Passing 'none' when the VM checkout actually has a HEAD revision
        # must refuse before provisioning or app effects.
        self.stage_provisioner_fixture(f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\nexit 0\n')
        result = self.run_apply(runtime_fixture=False, provision=True, qualified_previous_sha='none')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match the Node24-qualified previous release', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('provisioner invoked', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_invalid_shared_caddy_import_refuses_before_provisioning_or_app_effects(self):
        # R1: Read-only Caddy preconditions must refuse before provision_node24_runtime
        # runs, so an invalid shared Caddyfile never mutates OS packages.
        caddy_site = self.seed / 'deploy/hub.caddy'
        caddy_site.parent.mkdir(parents=True, exist_ok=True)
        caddy_site.write_text('hub.example.invalid {\n  reverse_proxy 127.0.0.1:8787\n}\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.seed), 'add', 'deploy/hub.caddy'],
                       capture_output=True, text=True, check=True, timeout=30)
        subprocess.run(['git', '-C', str(self.seed), 'commit', '-qm', 'add candidate caddy'],
                       capture_output=True, text=True, check=True, timeout=30)
        subprocess.run(['git', '-C', str(self.seed), 'push', '-q', str(self.remote), 'main'],
                       capture_output=True, text=True, check=True, timeout=30)
        self.stage_provisioner_fixture(f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\nexit 0\n')
        (self.root / 'Caddyfile').write_text(':80 {\n  respond "missing import"\n}\n', encoding='utf-8')
        result = self.run_apply(runtime_fixture=False, provision=True, caddy_mode='full')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('shared Caddyfile must import', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('provisioner invoked', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_missing_candidate_caddy_refuses_before_provisioning_or_app_effects(self):
        # R1: Missing candidate deploy/hub.caddy in full mode refuses before provisioning.
        self.stage_provisioner_fixture(f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\nexit 0\n')
        result = self.run_apply(runtime_fixture=False, provision=True, caddy_mode='full')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requested revision has no deploy/hub.caddy', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('provisioner invoked', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_schema_rollback_refusal_has_no_provisioning_or_app_effects(self):
        # R1: Unrollbackable candidate schema preflight refuses before provisioning.
        (self.seed / 'scripts/schema-rollback-preflight.py').write_text(
            'import sys\nprint("schema unsafe", file=sys.stderr)\nsys.exit(1)\n',
            encoding='utf-8',
        )
        subprocess.run(['git', '-C', str(self.seed), 'add', 'scripts/schema-rollback-preflight.py'],
                       capture_output=True, text=True, check=True, timeout=30)
        subprocess.run(['git', '-C', str(self.seed), 'commit', '-qm', 'failing schema preflight'],
                       capture_output=True, text=True, check=True, timeout=30)
        subprocess.run(['git', '-C', str(self.seed), 'push', '-q', str(self.remote), 'main'],
                       capture_output=True, text=True, check=True, timeout=30)
        self.stage_provisioner_fixture(f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\nexit 0\n')
        result = self.run_apply(runtime_fixture=False, provision=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('candidate cannot be safely rolled back', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('provisioner invoked', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_short_write_zero_progress_on_verified_copy_refuses_before_execution(self):
        # R2: A zero-progress os.write on the private verified execution copy
        # must refuse without executing any script or mutating app state.
        self.stage_provisioner_fixture(f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\nexit 0\n')
        py_hook_dir = self.root / 'pyhook-zero'
        py_hook_dir.mkdir()
        (py_hook_dir / 'sitecustomize.py').write_text(
            'import os\n'
            '_orig_open = os.open\n'
            '_orig_write = os.write\n'
            '_target_fds = set()\n'
            'def _hook_open(path, flags, mode=0o777, *args, **kwargs):\n'
            '    fd = _orig_open(path, flags, mode, *args, **kwargs)\n'
            '    if "ci-node24-verified." in str(path) and (flags & os.O_CREAT):\n'
            '        _target_fds.add(fd)\n'
            '    return fd\n'
            'def _hook_write(fd, data):\n'
            '    if fd in _target_fds:\n'
            '        return 0\n'
            '    return _orig_write(fd, data)\n'
            'os.open = _hook_open\n'
            'os.write = _hook_write\n',
            encoding='utf-8',
        )
        result = self.run_apply(runtime_fixture=False, provision=True,
                                extra_env={'PYTHONPATH': str(py_hook_dir)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('short or zero-progress write', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('provisioner invoked', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def test_truncated_verified_copy_readback_mismatch_refuses_before_execution(self):
        # R2: Even if a partial write leaves a syntactically valid bash prefix
        # without raising, independent readback length/digest verification must
        # refuse before bash -n or execution.
        self.stage_provisioner_fixture(f'printf \'%s\\n\' "provisioner invoked" >> "{self.log}"\nexit 0\n')
        py_hook_dir = self.root / 'pyhook-trunc'
        py_hook_dir.mkdir()
        (py_hook_dir / 'sitecustomize.py').write_text(
            'import os\n'
            '_orig_open = os.open\n'
            '_orig_write = os.write\n'
            '_target_fds = set()\n'
            'def _hook_open(path, flags, mode=0o777, *args, **kwargs):\n'
            '    fd = _orig_open(path, flags, mode, *args, **kwargs)\n'
            '    if "ci-node24-verified." in str(path) and (flags & os.O_CREAT):\n'
            '        _target_fds.add(fd)\n'
            '    return fd\n'
            'def _hook_write(fd, data):\n'
            '    if fd in _target_fds:\n'
            '        prefix = b"#!/usr/bin/env bash\\n"\n'
            '        _orig_write(fd, prefix)\n'
            '        return len(data)\n'
            '    return _orig_write(fd, data)\n'
            'os.open = _hook_open\n'
            'os.write = _hook_write\n',
            encoding='utf-8',
        )
        result = self.run_apply(runtime_fixture=False, provision=True,
                                extra_env={'PYTHONPATH': str(py_hook_dir)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('readback length or digest mismatch', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('provisioner invoked', log)
        self.assertNotIn('npm', log)
        self.assertFalse(self.state.exists())

    def run_current_sha(self, repo):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('AGENT_HUB_TEST_') and k != 'AGENT_HUB_DEPLOY_TEST_MODE'}
        env.update({
            'AGENT_HUB_DEPLOY_TEST_MODE': '1',
            'AGENT_HUB_TEST_REPO': str(repo),
            'AGENT_HUB_TEST_TRANSACTION_DIR': str(self.state),
            'AGENT_HUB_TEST_CADDY_CONFIG': str(self.root / 'Caddyfile'),
            'AGENT_HUB_TEST_CADDY_SITE': str(self.root / 'sites/hub.caddy'),
        })
        return subprocess.run(['/bin/bash', str(TRANSACTION), 'current-sha'],
                              cwd=str(self.root), env=env, capture_output=True, text=True, timeout=30)

    def test_current_sha_reports_the_deployed_identity_without_the_deploy_lock(self):
        result = self.run_current_sha(self.work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), self.candidate)
        # Read-only under contention: a held deploy lock does not block it.
        lock_path = self.work / '.local/tmp/.deploy-transaction.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_current_sha(self.work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), self.candidate)

    def test_current_sha_reports_none_without_a_deployed_checkout(self):
        empty = self.root / 'no-repo'
        empty.mkdir()
        result = self.run_current_sha(empty)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'none')

    def test_node22_fixture_refuses_before_npm_or_transaction_state(self):
        result = self.run_apply(runtime_fixture=True, service_version=V22)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('is Node 22, not Node24', result.stderr)
        self.assertIn('deploy/vm/provision-node24.sh', result.stderr)
        log = self.log.read_text(encoding='utf-8')
        self.assertNotIn('npm', log, 'npm must not run before the runtime guard')
        self.assertNotIn('node ', log, 'no build probe may run before the runtime guard')
        self.assertFalse(self.state.exists(), 'no transaction state may be created before the runtime guard')

    def test_node24_fixture_passes_the_guard_before_later_stages(self):
        result = self.run_apply(runtime_fixture=True, service_version=V24)
        self.assertIn('Node24 runtime check passed', result.stdout)
        self.assertNotIn('is Node 22, not Node24', result.stderr)
        self.assertTrue(self.state.exists())

    def test_test_mode_without_fixture_keeps_the_guard_out_of_existing_fixtures(self):
        result = self.run_apply(runtime_fixture=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('not Node24', result.stderr)
        self.assertIn('Node24 runtime guard skipped: test mode', result.stdout)
        self.assertNotIn('node ', self.log.read_text(encoding='utf-8'))


class RollbackCompatibilityTests(unittest.TestCase):
    def test_rollback_stays_code_and_config_only(self):
        body = function_body(TRANSACTION_SOURCE, 'restore_previous_release')
        self.assertIn('install-hub2-node.sh" --hub-only', body)
        self.assertIn('install_release_dependencies', body)
        for forbidden in ('provision-node24', 'apt-get', 'dpkg', 'flock -n 9'):
            self.assertNotIn(forbidden, body, f'rollback must not touch the OS runtime: {forbidden}')
        self.assertIn('application rollback never restores or downgrades /usr/bin/node', body)

    def test_rollback_rebuild_keeps_the_ambient_node_runtime_contract(self):
        self.assertIn('TMPDIR="$TMPDIR" npm --prefix "$REPO" ci --omit=dev', TRANSACTION_SOURCE)


class WorkflowIntegrationTests(unittest.TestCase):
    DEPLOY = DEPLOY_WORKFLOW.read_text(encoding='utf-8')
    ROLLBACK = ROLLBACK_WORKFLOW.read_text(encoding='utf-8')

    def deploy_job(self):
        start = self.DEPLOY.index('\n  deploy:')
        end = self.DEPLOY.index('\n  rollback_pending:', start)
        return self.DEPLOY[start:end]

    def test_ci_stages_the_provisioner_and_provisions_inside_the_transaction_step(self):
        # R1: no separate provisioning SSH stage whose lock ownership would
        # end before the app transaction starts. The deploy step stages the
        # exact candidate provisioner bytes to a SHA-bound path over normal CI
        # SSH, then ONE transaction invocation applies them under a single
        # deploy-lock owner.
        deploy_idx = self.DEPLOY.index('- name: Deploy validated revision through the fixed VM transaction script')
        step = self.DEPLOY[deploy_idx:self.DEPLOY.index('- name: Read Hub API health', deploy_idx)]
        self.assertNotIn('- name: Provision the required Node24 VM runtime', self.DEPLOY)
        self.assertIn('tar -C deploy/vm -cf - provision-node24.sh | ssh', step)
        self.assertIn('ci-node24-provision-', step)
        staging_idx = step.index('tar -C deploy/vm -cf - provision-node24.sh')
        invoke_idx = step.index('bash -s -- apply "$DEPLOY_SHA" "$RELEASE_MODE" "$caddy_mode"')
        self.assertLess(staging_idx, invoke_idx, 'staging must precede the transaction invocation')
        self.assertIn('provision-node24 "$PREVIOUS_SHA" \\\n            < deploy/vm/deploy-release-transaction.sh', step)
        # The provisioner bytes are never executed directly over SSH: the
        # transaction is the only runner of the staged script on the VM.
        self.assertNotIn('< deploy/vm/provision-node24.sh', self.DEPLOY)
        self.assertNotIn('bash -s -- < deploy/vm/provision-node24.sh', step)

    def test_single_deploy_lock_owner_spans_provisioning_and_application(self):
        # The transaction invocation carries the literal token and the
        # transaction derives the staged path from the validated deploy SHA.
        self.assertIn('needs: [test, hub2, distribution]', self.DEPLOY,
                      'the deploy gate must stay exactly on the release contract')
        self.assertNotIn('previous-release-compatibility]', self.DEPLOY)
        self.assertIn('provision_node24_runtime "$deploy_sha"', TRANSACTION_SOURCE)
        self.assertIn('ci-node24-provision-$deploy_sha/provision-node24.sh', TRANSACTION_SOURCE)

    def test_forward_previous_release_compatibility_proves_the_deployed_source(self):
        # Qualification control, inside the gated deploy job: the normal
        # forward workflow rebuilds and starts the exact release the VM
        # reports as deployed, on the same Node24 the rollback path would
        # keep — never a fake OS rollback.
        job = self.deploy_job()
        self.assertIn("grep -Eq '^v24\\.'", job)
        self.assertIn('node-version: 24', job)
        self.assertIn('fetch-depth: 0', job)
        self.assertIn('bash -s -- current-sha < deploy/vm/deploy-release-transaction.sh', job)
        self.assertIn('git checkout --detach --force "$PREVIOUS_SHA"', job)
        self.assertIn('npm ci', job)
        self.assertIn('npm run build', job)
        self.assertIn('No deployed release on the VM yet', job)
        # The compatibility checkout must restore the candidate bytes before
        # any later stage transports provisioner or release artifacts.
        self.assertIn('git checkout --detach --force "$DEPLOY_SHA"', job)
        # Rollout is gated on the proof: the compat steps run before the
        # deploy step, ahead of even the Python host tests wait.
        rebuild_idx = job.index('- name: Rebuild and start the exact deployed previous release on Node24')
        wait_idx = job.index('- name: Require green Python host tests for this main commit')
        deploy_step_idx = job.index('- name: Deploy validated revision through the fixed VM transaction script')
        self.assertLess(rebuild_idx, wait_idx)
        self.assertLess(rebuild_idx, deploy_step_idx)
        # Exactly one deploy-key install stays in the workflow (deploy job):
        # the release contract pins the private-key handling count.
        self.assertEqual(self.DEPLOY.count('printf \'%s\\n\' "$VM_DEPLOY_KEY"'), 2)
        self.assertEqual(self.DEPLOY.count("ssh-keygen -y -P '' -f"), 2)

    def test_distribution_and_rollback_lanes_agree_on_node24(self):
        for workflow, name in ((self.DEPLOY, 'deploy.yml'), (self.ROLLBACK, 'rollback.yml')):
            self.assertNotIn('node-version: 22', workflow, f'{name} must not validate on Node22')
            self.assertIn('node-version: 24', workflow, f'{name} must validate on Node24')
            self.assertIn("grep -Eq '^v24\\.'", workflow, f'{name} must assert the Node24 major explicitly')

    def test_required_checks_cover_the_provisioner_and_its_tests(self):
        self.assertIn('bash -n deploy/vm/provision-node24.sh', self.DEPLOY)
        self.assertIn('python3 test/test_node24_runtime.py', self.DEPLOY)


if __name__ == '__main__':
    unittest.main()
