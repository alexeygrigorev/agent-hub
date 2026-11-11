"""Production deployment transaction against local Git and real SQLite state.

Only the installer, service manager and health transport are fixtures. The
transaction and its protected read-only schema checker execute unmodified.
"""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
import subprocess
import sys
import time
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TRANSACTION = ROOT / 'deploy/vm/deploy-release-transaction.sh'
CHECKER = ROOT / 'scripts/schema-rollback-preflight.py'
POLICY_HELPER = ROOT / 'scripts/apply-model-policy.py'
CANONICAL_POLICY = json.loads((ROOT / 'config/model-policy.json').read_text())['candidates']
POLICY_TOKEN = 'synthetic-deploy-policy-token-' + 'x' * 40
SOURCES = {
    'CONTROL': 'control-do.ts', 'MACHINES': 'machine-do.ts',
    'PROJECTS': 'project-do.ts', 'ACCOUNTS': 'account-do.ts',
    'MAILBOXES': 'mailbox-do.ts',
}

# This installer models the schema contract, but performs no network or service
# actions. Rollback intentionally reads the restored vars when no mode is passed.
INSTALLER = '''#!/usr/bin/env python3
import json, os, pathlib, sqlite3, sys
repo = pathlib.Path.cwd()
args = sys.argv[1:]
vars_file = repo / '.dev.vars'
values = dict(line.split('=', 1) for line in vars_file.read_text().splitlines() if '=' in line)
mode = args[args.index('--schema-migrations') + 1] if '--schema-migrations' in args else values.get('HUB2_SCHEMA_MIGRATIONS', 'apply')
contract = json.loads((repo / 'fixture-contract.json').read_text())
with open(os.environ['FIXTURE_INSTALL_LOG'], 'a') as log:
    log.write(json.dumps({'mode': mode, 'args': args}) + '\\n')
if '--schema-migrations' in args:
    values['HUB2_SCHEMA_MIGRATIONS'] = mode
    vars_file.write_text(''.join(k + '=' + v + '\\n' for k, v in values.items()))
for path in pathlib.Path(os.environ['AGENT_HUB_TEST_DB_DIR']).glob('*.sqlite'):
    binding = path.name.split('-', 1)[0]
    schema = contract['schemas'][binding]
    with sqlite3.connect(path) as db:
        current = db.execute('SELECT version FROM hub2_schema WHERE singleton=1').fetchone()[0]
        if not schema['min_read'] <= current <= schema['max_read']:
            raise SystemExit('fixture installer cannot read schema')
        target = schema['bridge_write' if mode == 'defer' else 'active_write']
        db.execute('UPDATE hub2_schema SET version=? WHERE singleton=1', (max(current, target),))
if '--schema-migrations' in args and os.environ.get('FIXTURE_FAIL_INSTALL') == '1':
    raise SystemExit(23)
'''


def policy_directory(state):
    return state / ('model-policy-' + (state / 'transaction-token').read_text().strip())


class PolicyAPI:
    """Offline protocol peer; requests still traverse the actual protected helper."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.current = {'version': 5, 'candidates': copy.deepcopy(CANONICAL_POLICY)}
        self.calls = []
        self.unsupported = self.drop_apply = self.drop_rollback = self.invalid_prior = self.compete_apply = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.handle_policy('GET')

            def do_POST(self):
                self.handle_policy('POST')

            def do_PUT(self):
                self.handle_policy('PUT')

            def handle_policy(self, method):
                if self.path != '/api/model-policy' or self.headers.get('Authorization') != 'Bearer ' + POLICY_TOKEN:
                    self.reply(403, {'error': 'forbidden'})
                    return
                body = None if method == 'GET' else json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.calls.append((method, copy.deepcopy(body), fixture.git(fixture.repo, 'rev-parse', 'HEAD')))
                if method == 'GET':
                    if outer.current['candidates']:
                        self.reply(200, outer.current)
                    else:
                        self.reply(404, {'error': 'model_policy_unconfigured', 'version': outer.current['version']})
                    return
                if method == 'POST' and outer.unsupported:
                    self.reply(403, {'error': 'forbidden'})
                    return
                if body['expected_version'] != outer.current['version']:
                    self.reply(409, {'error': 'model_policy_version_conflict'})
                    return
                action = body.get('action')
                marker = {'protocol': 'model-policy-recovery-v1', 'action': action} if method == 'POST' else {}
                if method == 'POST' and outer.invalid_prior:
                    self.reply(409, {'error': 'model_policy_prior_unrestorable'})
                    return
                if action == 'validate_recovery':
                    assert fixture.health_log.exists(), 'prepare must follow candidate health'
                    self.reply(200, dict(marker, **outer.current, changed=outer.current['candidates'] != body['candidates']))
                    return
                receipt = json.loads((policy_directory(fixture.state) / 'receipt.json').read_text())
                assert receipt['phase'] == ('applying' if method == 'POST' else 'rolling_back')
                assert (fixture.state / 'policy-apply-started').exists(), 'intent must precede mutation'
                assert not (fixture.state / 'applied').exists() if method == 'POST' else True
                if method == 'POST' and outer.compete_apply:
                    outer.compete_apply = False
                    outer.current = {'version': outer.current['version'] + 1, 'candidates': body['candidates']}
                    self.reply(409, {'error': 'model_policy_version_conflict'})
                    return
                changed = outer.current['candidates'] != body['candidates']
                outer.current = {'version': outer.current['version'] + int(changed), 'candidates': body['candidates']}
                if method == 'POST' and outer.drop_apply or method == 'PUT' and outer.drop_rollback:
                    outer.drop_apply = outer.drop_rollback = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(200, dict(marker, **outer.current, unchanged=not changed))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def writes(self):
        return [call for call in self.calls if call[0] == 'PUT' or call[0] == 'POST' and call[1]['action'] == 'apply_recoverable']


class SchemaReleaseTransactionTest(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='schema-transaction-', dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.seed = self.root / 'seed'
        self.repo = self.root / 'checkout'
        self.remote = self.root / 'origin.git'
        self.db_dir = self.root / 'databases'
        self.state = self.root / 'transaction'
        self.bin = self.root / 'bin'
        self.db_dir.mkdir()
        self.bin.mkdir()
        self.install_log = self.root / 'install.jsonl'
        self.health_log = self.root / 'health.log'
        self.policy = PolicyAPI(self)
        self.addCleanup(self.policy.close)
        credential_dir = self.root / 'credentials'
        credential_dir.mkdir(mode=0o700)
        self.credential = credential_dir / 'admin.json'
        self.credential.write_text(json.dumps({'access_token': POLICY_TOKEN}))
        self.credential.chmod(0o600)
        self.latest = json.loads((ROOT / 'deploy/schema-compatibility.json').read_text())
        self.old = copy.deepcopy(self.latest)
        self.old['bridge'] = False
        for schema in self.old['schemas'].values():
            schema['min_read'] = schema['max_read'] = schema['active_write'] = schema['bridge_write']

        self.run_cmd('git', 'init', '--bare', '-q', str(self.remote))
        self.run_cmd('git', 'init', '-q', '-b', 'main', str(self.seed))
        self.git(self.seed, 'config', 'user.name', 'Schema transaction fixture')
        self.git(self.seed, 'config', 'user.email', 'fixture@example.invalid')
        self.previous = self.commit_release(self.old, bridge=False)
        self.candidate = self.commit_release(self.latest, bridge=True)
        self.git(self.seed, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.seed, 'push', '-q', 'origin', 'main')
        self.run_cmd('git', 'clone', '-q', '--branch', 'main', str(self.remote), str(self.repo))
        self.git(self.repo, 'checkout', '-q', '--detach', self.previous)
        self.vars = 'HUB2_SCHEMA_MIGRATIONS=apply\nHUB2_CREDENTIAL_KEY=fixture-retained-key\n'
        (self.repo / '.dev.vars').write_text(self.vars)
        for binding, schema in self.old['schemas'].items():
            with closing(sqlite3.connect(self.database(binding))) as db:
                db.executescript('CREATE TABLE hub2_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL);'
                                 'CREATE TABLE hub2_identity(singleton INTEGER PRIMARY KEY,binding TEXT,name TEXT);'
                                 'CREATE TABLE retained(id TEXT PRIMARY KEY,value TEXT NOT NULL);')
                db.execute('INSERT INTO hub2_schema VALUES(1,?)', (schema['max_read'],))
                db.execute('INSERT INTO hub2_identity VALUES(1,?,?)', (binding, 'fixture'))
                db.execute('INSERT INTO retained VALUES(?,?)', ('before-deploy', binding + '-original'))
                db.commit()
        self.write_executable('sudo', '#!/bin/sh\nexec "$@"\n')
        # Real install semantics and permissions; omit ownership changes only.
        real_install = shutil.which('install')
        self.write_executable('install', '#!/usr/bin/env python3\nimport os,sys\na=sys.argv[1:]; out=[]; i=0\n'
                              'while i<len(a):\n'
                              ' if a[i] in ("-o","-g"): i+=2\n'
                              ' else: out.append(a[i]); i+=1\n'
                              'os.execv(' + repr(real_install) + ',[' + repr(real_install) + ']+out)\n')
        self.write_executable('npm', '#!/bin/sh\nexit 0\n')
        self.write_executable('systemctl', '#!/bin/sh\nexit 0\n')
        self.write_executable('caddy', '#!/bin/sh\nexit 0\n')
        self.write_executable('curl', '''#!/usr/bin/env python3
import os, pathlib, sqlite3
with open(os.environ['FIXTURE_HEALTH_LOG'], 'a') as log: log.write('healthy\\n')
if os.environ.get('FIXTURE_CORRUPT_AT_HEALTH') == '1':
    path = pathlib.Path(os.environ['AGENT_HUB_TEST_DB_DIR']) / 'CONTROL-fixture.sqlite'
    with sqlite3.connect(path) as db: db.execute('DELETE FROM hub2_schema')
print('{"ok":true}')
''')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        AGENT_HUB_DEPLOY_TEST_MODE='1', AGENT_HUB_TEST_REPO=str(self.repo),
                        AGENT_HUB_TEST_TRANSACTION_DIR=str(self.state),
                        AGENT_HUB_TEST_CADDY_CONFIG=str(self.root / 'Caddyfile'),
                        AGENT_HUB_TEST_DB_DIR=str(self.db_dir),
                        AGENT_HUB_TEST_POLICY_URL=self.policy.url,
                        AGENT_HUB_TEST_POLICY_CREDENTIAL=str(self.credential),
                        HUB2_TOKEN='ambient-token-must-never-be-used',
                        FIXTURE_INSTALL_LOG=str(self.install_log), FIXTURE_HEALTH_LOG=str(self.health_log))

    def run_cmd(self, *args):
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout.strip()

    def git(self, repo, *args):
        return self.run_cmd('git', '-C', str(repo), *args)

    def commit_release(self, contract, *, bridge):
        for binding, filename in SOURCES.items():
            path = self.seed / 'hub2/src' / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            schema = contract['schemas'][binding]
            source = f"initializeSchema(ctx, this.db, {schema['max_read']}, SCHEMA);\n"
            if bridge and schema['bridge_write'] != schema['active_write']:
                source += f"schemaMigrationCeiling(env.HUB2_SCHEMA_MIGRATIONS,{schema['bridge_write']},{schema['active_write']});\n"
            path.write_text(source)
        deploy = self.seed / 'deploy'
        (deploy / 'vm').mkdir(parents=True, exist_ok=True)
        (deploy / 'vm/install-hub2-node.sh').write_text('#!/bin/sh\nexec python3 fixture-installer.py "$@"\n')
        (self.seed / 'fixture-installer.py').write_text(INSTALLER)
        (self.seed / 'fixture-contract.json').write_text(json.dumps(contract))
        (self.seed / '.gitignore').write_text('.dev.vars\n.local/\n')
        if bridge:
            (deploy / 'schema-compatibility.json').write_text(json.dumps(contract))
            scripts = self.seed / 'scripts'
            scripts.mkdir(exist_ok=True)
            shutil.copyfile(CHECKER, scripts / CHECKER.name)
            shutil.copyfile(POLICY_HELPER, scripts / POLICY_HELPER.name)
            config = self.seed / 'config'
            config.mkdir(exist_ok=True)
            (config / 'model-policy.json').write_text(json.dumps({'candidates': CANONICAL_POLICY}))
        self.git(self.seed, 'add', '.')
        self.git(self.seed, 'commit', '-qm', 'compatible bridge' if bridge else 'previous legacy reader')
        return self.git(self.seed, 'rev-parse', 'HEAD')

    def write_executable(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def database(self, binding):
        return self.db_dir / (binding + '-fixture.sqlite')

    def versions(self):
        result = {}
        for binding in SOURCES:
            with closing(sqlite3.connect(self.database(binding))) as db:
                result[binding] = db.execute('SELECT version FROM hub2_schema WHERE singleton=1').fetchone()[0]
        return result

    def rows(self):
        result = {}
        for binding in SOURCES:
            with closing(sqlite3.connect(self.database(binding))) as db:
                result[binding] = db.execute('SELECT * FROM retained ORDER BY id').fetchall()
        return result

    def transaction(self, operation, *, success=True, release_mode='active', **env):
        args = ['bash', str(TRANSACTION), operation]
        if operation == 'apply':
            args += [self.candidate, release_mode, 'hub-only']
        result = subprocess.run(args, cwd=self.root, env=dict(self.env, **env),
                                capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_bridge_apply_rollback_and_reapply_preserve_new_user_writes(self):
        original = self.rows()
        self.transaction('apply')
        self.assertEqual(self.versions(), {k: v['bridge_write'] for k, v in self.latest['schemas'].items()})
        self.assertEqual((self.state / 'schema-mode').read_text().strip(), 'defer')
        self.assertEqual((self.state / 'schema-preflight.py').read_bytes(), CHECKER.read_bytes())
        self.assertEqual((self.state / 'schema-preflight.py').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.rows(), original)
        self.transaction('commit')
        self.assertFalse(self.state.exists())

        self.transaction('apply')
        self.assertEqual((self.state / 'schema-mode').read_text().strip(), 'apply')
        migrated = {k: v['active_write'] for k, v in self.latest['schemas'].items()}
        self.assertEqual(self.versions(), migrated)
        for binding in SOURCES:
            with closing(sqlite3.connect(self.database(binding))) as db:
                db.execute('INSERT INTO retained VALUES(?,?)', ('after-migration', binding + '-concurrent-write'))
                db.commit()
        retained = self.rows()
        self.transaction('rollback')
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)
        self.assertIn('HUB2_SCHEMA_MIGRATIONS=defer', (self.repo / '.dev.vars').read_text())
        self.assertEqual(self.versions(), migrated)
        self.assertEqual(self.rows(), retained)
        self.transaction('apply')
        self.assertEqual(self.versions(), migrated)
        self.assertEqual(self.rows(), retained)
        self.transaction('commit')
        modes = [json.loads(line)['mode'] for line in self.install_log.read_text().splitlines()]
        self.assertEqual(modes, ['defer', 'apply', 'defer', 'apply'])

    def test_bridge_installer_failure_returns_old_code_without_upgrading_state(self):
        versions, rows = self.versions(), self.rows()
        result = self.transaction('apply', success=False, FIXTURE_FAIL_INSTALL='1')
        self.assertIn('Automatic rollback succeeded', result.stderr)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)
        self.assertEqual((self.repo / '.dev.vars').read_text(), self.vars)
        self.assertEqual(self.versions(), versions)
        self.assertEqual(self.rows(), rows)
        self.assertFalse(self.state.exists())

    def test_already_upgraded_database_refuses_old_reader_before_checkout(self):
        with closing(sqlite3.connect(self.database('CONTROL'))) as db:
            db.execute('UPDATE hub2_schema SET version=5'); db.commit()
        before = {p.name: p.read_bytes() for p in self.db_dir.iterdir()}
        result = self.transaction('apply', success=False)
        self.assertIn('rollback_cannot_read_live_schema:CONTROL', result.stderr)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.install_log.exists())
        self.assertFalse(self.health_log.exists())
        self.assertEqual({p.name: p.read_bytes() for p in self.db_dir.iterdir()}, before)

    def test_missing_schema_metadata_refuses_before_health_or_checkout(self):
        with closing(sqlite3.connect(self.database('MACHINES'))) as db:
            db.execute('DELETE FROM hub2_schema'); db.commit()
        result = self.transaction('apply', success=False)
        self.assertIn('database_schema_record_missing', result.stderr)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)
        self.assertFalse(self.health_log.exists())
        self.assertFalse(self.state.exists())

    def test_successful_health_cannot_hide_rejected_post_install_metadata(self):
        retained = self.rows()
        result = self.transaction('apply', success=False, FIXTURE_CORRUPT_AT_HEALTH='1')
        self.assertTrue(self.health_log.exists(), 'exercise a successful health response first')
        self.assertIn('database_schema_record_missing', result.stderr)
        self.assertIn('automatic rollback failed', result.stderr)
        self.assertFalse((self.state / 'applied').exists())
        self.assertTrue((self.state / 'prepared').exists(), 'retain protected recovery evidence')
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)
        self.assertEqual(self.rows(), retained)

    def test_rollback_rechecks_live_schema_before_switching_to_previous_reader(self):
        self.transaction('apply')
        with closing(sqlite3.connect(self.database('MACHINES'))) as db:
            db.execute('UPDATE hub2_schema SET version=999'); db.commit()
        installs = self.install_log.read_text()
        health = self.health_log.read_text()
        result = self.transaction('rollback', success=False)
        self.assertIn('cannot_read_live_schema:MACHINES', result.stderr)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)
        self.assertEqual(self.install_log.read_text(), installs)
        self.assertEqual(self.health_log.read_text(), health)
        self.assertTrue((self.state / 'prepared').exists())


    def receipt(self):
        return policy_directory(self.state) / 'receipt.json'

    def changed_prior(self):
        prior = copy.deepcopy(CANONICAL_POLICY)
        prior[0]['weight'] = 37
        self.policy.current = {'version': 5, 'candidates': prior}
        return copy.deepcopy(self.policy.current)

    def test_active_deploy_noop_policy_is_protected_and_commit_only_reads(self):
        self.transaction('apply')
        receipt = json.loads(self.receipt().read_text())
        self.assertEqual(receipt['phase'], 'applied')
        self.assertFalse(receipt['changed'])
        self.assertEqual(receipt['source_sha'], self.candidate)
        self.assertEqual(receipt['url'], self.policy.url)
        self.assertEqual(self.policy.current['version'], 5)
        self.assertEqual((policy_directory(self.state) / 'apply-model-policy.py').read_bytes(), POLICY_HELPER.read_bytes())
        self.assertEqual(self.receipt().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.receipt().parent.stat().st_mode & 0o777, 0o700)
        writes = self.policy.writes()
        self.transaction('commit')
        self.assertEqual(self.policy.writes(), writes, 'finalizing never acquires write ownership')
        self.assertFalse(self.state.exists())

    def test_changed_policy_smoke_failure_restores_before_checkout_and_preserves_rows(self):
        before = self.changed_prior()
        self.transaction('apply')
        self.assertEqual(self.policy.current, {'version': 6, 'candidates': CANONICAL_POLICY})
        self.assertEqual(json.loads(self.receipt().read_text())['before'], before)
        with closing(sqlite3.connect(self.database('PROJECTS'))) as db:
            db.execute('INSERT INTO retained VALUES(?,?)', ('while-candidate-active', 'keep-user-write'))
            db.commit()
        rows = self.rows()
        self.transaction('rollback')  # same operation used after a failed public smoke
        self.assertEqual(self.policy.current, {'version': 7, 'candidates': before['candidates']})
        self.assertEqual(self.policy.writes()[-1][0], 'PUT')
        self.assertEqual(self.policy.writes()[-1][2], self.candidate, 'policy restored while compatible candidate still runs')
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)
        self.assertFalse((self.repo / 'scripts/apply-model-policy.py').exists(), 'rollback uses retained candidate helper')
        self.assertEqual(self.rows(), rows)
        self.transaction('rollback')  # idempotent completed rollback
        self.assertEqual(len(self.policy.writes()), 2)

    def test_initial_unconfigured_restore_keeps_monotonic_tombstone_and_reapply(self):
        self.policy.current = {'version': 0, 'candidates': []}
        self.transaction('apply')
        self.assertEqual(self.policy.current['version'], 1)
        self.transaction('rollback')
        self.assertEqual(self.policy.current, {'version': 2, 'candidates': []})
        self.assertTrue(self.policy.writes()[-1][1]['restore_unconfigured'])
        self.assertEqual(self.policy.writes()[-1][1]['expected_version'], 1)
        self.transaction('apply')
        self.assertEqual(self.policy.current, {'version': 3, 'candidates': CANONICAL_POLICY})
        self.transaction('commit')

    def test_operator_write_even_identical_policy_blocks_commit_and_rollback(self):
        self.changed_prior()
        self.transaction('apply')
        self.policy.current['version'] += 1  # operator wrote even the same desired data
        writes = copy.deepcopy(self.policy.writes())
        self.transaction('commit', success=False)
        result = self.transaction('rollback', success=False)
        self.assertIn('policy rollback unconfirmed', result.stderr)
        self.assertEqual(self.policy.writes(), writes)
        self.assertEqual(self.policy.current['version'], 7)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)
        self.assertTrue(self.receipt().exists())

    def test_lost_apply_response_retains_candidate_and_uncertain_receipt(self):
        self.changed_prior()
        self.policy.drop_apply = True
        result = self.transaction('apply', success=False)
        self.assertIn('automatic rollback failed', result.stderr)
        self.assertEqual(self.policy.current['version'], 6)
        self.assertEqual(json.loads(self.receipt().read_text())['phase'], 'uncertain')
        self.assertFalse((self.state / 'applied').exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)
        writes = copy.deepcopy(self.policy.writes())
        self.transaction('rollback', success=False)
        self.transaction('commit', success=False)
        self.assertEqual(self.policy.writes(), writes)
        self.assertTrue(self.state.exists())

    def test_identical_competing_write_after_prepare_cannot_grant_rollback_ownership(self):
        self.changed_prior()
        self.policy.compete_apply = True
        result = self.transaction('apply', success=False)
        self.assertIn('automatic rollback failed', result.stderr)
        self.assertEqual(self.policy.current, {'version': 6, 'candidates': CANONICAL_POLICY})
        self.assertEqual(json.loads(self.receipt().read_text())['phase'], 'uncertain')
        writes = copy.deepcopy(self.policy.writes())
        self.transaction('rollback', success=False)
        self.assertEqual(self.policy.writes(), writes)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)

    def test_lost_rollback_response_reconciles_without_second_restore(self):
        before = self.changed_prior()
        self.transaction('apply')
        self.policy.drop_rollback = True
        self.transaction('rollback')
        self.transaction('rollback')
        self.assertEqual(self.policy.current, {'version': 7, 'candidates': before['candidates']})
        self.assertEqual(len(self.policy.writes()), 2)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)

    def test_missing_or_corrupt_receipt_never_authorizes_checkout_or_commit(self):
        self.changed_prior()
        self.transaction('apply')
        valid = self.receipt().read_bytes()
        writes = copy.deepcopy(self.policy.writes())
        for replacement in (None, b'not-json', b'{}'):
            with self.subTest(replacement=replacement):
                if replacement is None:
                    self.receipt().unlink()
                else:
                    self.receipt().write_bytes(replacement)
                    self.receipt().chmod(0o600)
                self.transaction('rollback', success=False)
                self.transaction('commit', success=False)
                self.assertEqual(self.policy.writes(), writes)
                self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)
                self.assertTrue(self.state.exists())
                self.receipt().write_bytes(valid)
                self.receipt().chmod(0o600)
        self.transaction('rollback')

    def test_commit_with_prepared_receipt_never_applies_it(self):
        self.transaction('apply')
        data = json.loads(self.receipt().read_text())
        data.update(phase='prepared', applied=None)
        self.receipt().write_text(json.dumps(data))
        writes = copy.deepcopy(self.policy.writes())
        self.transaction('commit', success=False)
        self.assertEqual(self.policy.writes(), writes)
        self.assertTrue(self.state.exists())

    def test_unsupported_protocol_prepare_rolls_back_code_without_policy_mutation(self):
        self.policy.unsupported = True
        result = self.transaction('apply', success=False)
        self.assertIn('Automatic rollback succeeded', result.stderr)
        self.assertEqual(self.policy.writes(), [])
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)
        self.assertFalse(self.state.exists())

    def test_invalid_prior_prepare_rolls_back_code_without_policy_mutation(self):
        self.policy.current = {'version': 4, 'candidates': [dict(CANONICAL_POLICY[0], model='gpt-6-astra', capabilities=[])]}
        before = copy.deepcopy(self.policy.current)
        self.policy.invalid_prior = True
        result = self.transaction('apply', success=False)
        self.assertIn('Automatic rollback succeeded', result.stderr)
        self.assertEqual(self.policy.writes(), [])
        self.assertEqual(self.policy.current, before)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)

    def test_explicit_private_admin_credential_required_before_checkout(self):
        for kind in ('missing', 'public', 'symlink'):
            with self.subTest(kind=kind):
                self.credential.unlink(missing_ok=True)
                if kind == 'symlink':
                    target = self.credential.with_name('other.json')
                    target.write_text(json.dumps({'access_token': POLICY_TOKEN}))
                    target.chmod(0o600)
                    self.credential.symlink_to(target)
                elif kind == 'public':
                    self.credential.write_text(json.dumps({'access_token': POLICY_TOKEN}))
                    self.credential.chmod(0o644)
                result = self.transaction('apply', success=False)
                self.assertIn('policy_admin_credential_unavailable_or_untrusted', result.stderr)
                self.assertNotIn(POLICY_TOKEN, result.stdout + result.stderr)
                self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.previous)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.install_log.exists())
        self.assertEqual(self.policy.calls, [])

    def test_same_sha_redeploy_rejects_stale_transaction_token(self):
        self.changed_prior()
        self.transaction('apply')
        token_a = (self.state / 'transaction-token').read_text().strip()
        self.transaction('commit')
        self.transaction('apply')
        token_b = (self.state / 'transaction-token').read_text().strip()
        self.assertNotEqual(token_a, token_b)
        writes = copy.deepcopy(self.policy.writes())
        directory = policy_directory(self.state)
        result = subprocess.run([sys.executable, str(directory / 'apply-model-policy.py'), 'rollback',
                                 '--url', self.policy.url, '--source-sha', self.candidate,
                                 '--receipt', str(directory / 'receipt.json'), '--transaction-token', token_a,
                                 '--token-file', str(self.credential)],
                                env={k: v for k, v in self.env.items() if k != 'HUB2_TOKEN'},
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('recovery_transaction_mismatch', result.stdout)
        self.assertEqual(self.policy.writes(), writes)
        self.assertTrue(self.receipt().exists())
        self.transaction('rollback')

    def test_delayed_privileged_rollback_after_parent_death_cannot_undo_commit(self):
        self.changed_prior()
        self.transaction('apply')
        directory = policy_directory(self.state)
        ready, release, outcome = [self.root / name for name in ('child-ready', 'child-release', 'child-outcome')]
        # Model sudo closing the inherited transaction descriptor. The actual
        # protected helper survives its killed shell and starts after commit.
        self.write_executable('python3', '#!' + sys.executable + '\n' + r'''import os, pathlib, subprocess, sys, time
args = sys.argv[1:]
if args and args[0].endswith('/apply-model-policy.py') and len(args)>1 and args[1]=='rollback' and os.environ.get('FIXTURE_PAUSE_ROLLBACK')=='1':
    try: os.close(9)
    except OSError: pass
    pathlib.Path(os.environ['FIXTURE_CHILD_READY']).touch()
    end=time.monotonic()+10
    while not pathlib.Path(os.environ['FIXTURE_CHILD_RELEASE']).exists():
        if time.monotonic()>end: raise SystemExit(90)
        time.sleep(.01)
    result=subprocess.run([sys.executable,*args])
    pathlib.Path(os.environ['FIXTURE_CHILD_OUTCOME']).write_text(str(result.returncode))
    raise SystemExit(result.returncode)
if args and args[0]=='-' and os.environ.get('FIXTURE_FAIL_RETIRE')=='1' and len(args)==3:
    raise SystemExit(31)
os.execv(sys.executable,[sys.executable,*args])
''')
        env = dict(self.env, FIXTURE_PAUSE_ROLLBACK='1', FIXTURE_CHILD_READY=str(ready),
                   FIXTURE_CHILD_RELEASE=str(release), FIXTURE_CHILD_OUTCOME=str(outcome))
        process = subprocess.Popen(['bash', str(TRANSACTION), 'rollback'], cwd=self.root, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                self.assertIsNone(process.poll())
                time.sleep(.01)
            self.assertTrue(ready.exists())
            process.kill()
            process.wait(timeout=3)
            # Fail cleanup after sealing the receipt, leaving the old helper
            # reachable. Its late rollback must observe the durable fence.
            self.transaction('commit', success=False, FIXTURE_FAIL_RETIRE='1')
            self.assertEqual(json.loads(self.receipt().read_text())['phase'], 'committed')
            writes = copy.deepcopy(self.policy.writes())
            release.touch()
            deadline = time.monotonic() + 10
            while not outcome.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(outcome.exists())
            self.assertNotEqual(outcome.read_text(), '0')
            self.assertEqual(self.policy.writes(), writes)
            self.assertEqual(self.policy.current['version'], 6)
            self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), self.candidate)
            self.transaction('commit')  # forward-only finalization retry
            self.assertFalse(self.state.exists())
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            # Child has a bounded deadline; wait before cleaning its fixture.
            deadline = time.monotonic() + 12
            while ready.exists() and not outcome.exists() and time.monotonic() < deadline:
                time.sleep(.01)

    def test_bootstrap_deploy_never_requires_or_applies_canonical_policy(self):
        self.credential.unlink()
        self.transaction('apply', release_mode='bootstrap')
        self.assertFalse((self.state / 'policy-required').exists())
        self.assertEqual(self.policy.calls, [])
        self.transaction('commit')
        self.assertEqual(self.policy.calls, [])


if __name__ == '__main__':
    unittest.main()
