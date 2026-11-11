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
import sqlite3
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TRANSACTION = ROOT / 'deploy/vm/deploy-release-transaction.sh'
CHECKER = ROOT / 'scripts/schema-rollback-preflight.py'
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

    def transaction(self, operation, *, success=True, **env):
        args = ['bash', str(TRANSACTION), operation]
        if operation == 'apply':
            args += [self.candidate, 'active', 'hub-only']
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


if __name__ == '__main__':
    unittest.main()
