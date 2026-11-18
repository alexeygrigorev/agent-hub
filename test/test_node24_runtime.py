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

    def run_provisioner(self, **extra):
        return subprocess.run(['/bin/bash', str(PROVISIONER)], cwd=str(self.root), env=self.env(**extra),
                              capture_output=True, text=True, timeout=30)

    def commands(self):
        return [line for line in self.log.read_text(encoding='utf-8').splitlines() if line]

    def command_prefixes(self):
        # `id` probes are excluded: this session's BASH_ENV rm-guard wrapper
        # contributes its own environment-dependent `id -u` probes around the
        # cleanup, while the guarded command sequence below stays deterministic.
        return [line.split(' ', 1)[0] for line in self.commands() if not line.startswith('id ')]

    def plain_install_calls(self):
        """apt-get install invocations that were not the bounded --download-only."""
        return [c for c in self.commands() if ' install nodejs=' in c and '--download-only' not in c]


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
            'sha256sum',      # artifact hash before installation
            'apt-get',        # the single bounded install
            'dpkg-query',     # postcheck: ownership
            'dpkg-query',     # postcheck: version
            'node',           # postcheck: PATH build toolchain major 24
        ])
        logged = '\n'.join(self.sandbox.commands())
        self.assertIn('apt-get update', logged)
        self.assertIn(f'apt-get -y --no-install-recommends install nodejs={PIN_VERSION}', logged)
        expected_line = (f'deb [arch=amd64 signed-by={self.sandbox.keyring}] '
                         f'{PIN_REPO} nodistro main')
        self.assertEqual(self.sandbox.sources.read_text(encoding='utf-8'), expected_line + '\n')
        self.assertIn(PIN_FINGERPRINT, self.sandbox.keyring.read_text(encoding='utf-8'))
        archive = self.sandbox.apt_archives / f'nodejs_{PIN_VERSION}_amd64.deb'
        self.assertTrue(archive.exists())
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

    def test_drifted_keyring_is_replaced_by_the_validated_download(self):
        self.sandbox.keyring.write_text('gpg-fixture-primary-fingerprint: DEAD\n', encoding='utf-8')
        result = self.sandbox.run_provisioner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(PIN_FINGERPRINT, self.sandbox.keyring.read_text(encoding='utf-8'))
        self.assertNotIn('DEAD', self.sandbox.keyring.read_text(encoding='utf-8'))


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

    def test_provisioner_takes_the_same_single_owner_lock_as_the_transaction(self):
        self.assertIn('.deploy-transaction.lock', PROVISIONER_SOURCE)
        self.assertIn('flock -n 9', PROVISIONER_SOURCE)
        self.assertIn('.deploy-transaction.lock', TRANSACTION_SOURCE)
        # Never nested: the transaction must not invoke the provisioner; the
        # only allowed mentions are the guard's guidance messages.
        self.assertIsNone(
            re.search(r'(?m)^.*(run_root |sudo |exec 9>>.*\|\| ).*provision-node24', TRANSACTION_SOURCE),
            'the transaction must never invoke deploy/vm/provision-node24.sh itself')
        self.assertNotRegex(TRANSACTION_SOURCE, r'(?m)^\s*(run_root\s+)?(sudo\s+)?bash\s+"\$?[A-Za-z_].*provision-node24')


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
        for later in ('mkdir -p "$TMPDIR"',
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
        self.candidate = subprocess.run(['git', '-C', str(self.work), 'rev-parse', 'HEAD'],
                                        capture_output=True, text=True, check=True).stdout.strip()

    def _prepare_repo(self):
        def git(*args):
            return subprocess.run(['git', *args], capture_output=True, text=True, check=True, timeout=30)
        git('init', '--bare', '-q', str(self.remote))
        git('init', '-q', '-b', 'main', str(self.seed))
        git('-C', str(self.seed), 'config', 'user.name', 'node24 fixture')
        git('-C', str(self.seed), 'config', 'user.email', 'fixture@example.invalid')
        (self.seed / '.dev.vars').write_text('HUB2_RELEASE_MODE=bootstrap\n', encoding='utf-8')
        (self.seed / '.gitignore').write_text('.dev.vars\n.local/\n', encoding='utf-8')
        git('-C', str(self.seed), 'add', '.')
        git('-C', str(self.seed), 'commit', '-qm', 'fixture previous release')
        git('-C', str(self.seed), 'push', '-q', str(self.remote), 'main')
        git('clone', '-q', '--branch', 'main', str(self.remote), str(self.work))
        git('-C', str(self.work), 'config', 'user.name', 'node24 fixture')
        git('-C', str(self.work), 'config', 'user.email', 'fixture@example.invalid')
        (self.work / '.dev.vars').write_text('HUB2_RELEASE_MODE=bootstrap\n', encoding='utf-8')

    def run_apply(self, runtime_fixture=True, service_version=V22):
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
        return subprocess.run(['/bin/bash', str(TRANSACTION), 'apply', self.candidate, 'bootstrap', 'hub-only'],
                              cwd=str(self.root), env=env, capture_output=True, text=True, timeout=120)

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
        # The fixture candidate has no schema preflight, so apply stops later, but
        # the runtime guard must already have passed before any mutation.
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Node24 runtime check passed', result.stdout)
        self.assertNotIn('is Node 22, not Node24', result.stderr)
        self.assertNotIn('npm', self.log.read_text(encoding='utf-8'))
        self.assertFalse(self.state.exists())

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

    def test_ci_provisions_node24_over_ssh_before_the_transaction_step(self):
        provision_idx = self.DEPLOY.index('- name: Provision the required Node24 VM runtime')
        deploy_idx = self.DEPLOY.index('- name: Deploy validated revision through the fixed VM transaction script')
        self.assertLess(provision_idx, deploy_idx)
        provision_step = self.DEPLOY[provision_idx:deploy_idx]
        self.assertIn('< deploy/vm/provision-node24.sh', provision_step)
        self.assertIn('ssh -i "$RUNNER_TEMP/agent-hub-ssh/hub_vm_key"', provision_step)
        self.assertNotIn('deploy-release-transaction.sh', provision_step)
        # No nested lock ownership: the transaction is not modified to provision.
        self.assertNotIn('Provision the required Node24', self.DEPLOY[deploy_idx:])

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
