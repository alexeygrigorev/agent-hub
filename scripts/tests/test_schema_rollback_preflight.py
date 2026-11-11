"""Read-only schema preflight against disposable Git commits and SQLite files."""
from contextlib import closing
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('schema_preflight', ROOT/'scripts/schema-rollback-preflight.py')
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class SchemaRollbackPreflightTest(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.local/tmp';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'repo';self.repo.mkdir();self.db=self.root/'db';self.db.mkdir()
        self.git('init','-q');self.git('config','user.name','Test');self.git('config','user.email','test@example.invalid')
        self.latest=json.loads((ROOT/'deploy/schema-compatibility.json').read_text())
        self.old=copy.deepcopy(self.latest);self.old['bridge']=False
        for schema in self.old['schemas'].values():
            schema['max_read']=schema['active_write']=schema['bridge_write']
            schema['min_read']=schema['max_read']
        self.previous=self.commit(self.old,manifest=False)
        self.candidate=self.commit(self.latest)

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.repo),*args],capture_output=True,text=True,check=True).stdout.strip()

    def commit(self,value,manifest=True):
        for binding,path in preflight.SOURCES.items():
            p=self.repo/path;p.parent.mkdir(parents=True,exist_ok=True)
            row=value['schemas'][binding]
            p.write_text('initializeSchema(ctx, this.db, '+str(row['max_read'])+', SCHEMA);\n'+
                         ('schemaMigrationCeiling(env.HUB2_SCHEMA_MIGRATIONS,'+str(row['bridge_write'])+','+str(row['max_read'])+');\n' if row['bridge_write']!=row['max_read'] else ''))
        p=self.repo/'deploy/schema-compatibility.json';p.parent.mkdir(parents=True,exist_ok=True)
        if manifest:p.write_text(json.dumps(value))
        elif p.exists():p.unlink()
        self.git('add','.');self.git('commit','-qm','fixture')
        return self.git('rev-parse','HEAD')

    def sqlite(self,binding,version,identity=None):
        path=self.db/(binding+'-synthetic-1234567890.sqlite')
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE hub2_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL)')
            db.execute('INSERT INTO hub2_schema VALUES(1,?)',(version,))
            db.execute('CREATE TABLE retained(id TEXT PRIMARY KEY,value TEXT)');db.execute("INSERT INTO retained VALUES('user-write','must survive')")
            if identity:
                db.execute('CREATE TABLE hub2_identity(singleton INTEGER PRIMARY KEY,binding TEXT,name TEXT)')
                db.execute('INSERT INTO hub2_identity VALUES(1,?,?)',(identity,'synthetic'))
            db.commit()
        return path

    def test_old_reader_requires_bridge_then_verified_bridge_can_roll_back_migration(self):
        self.sqlite('CONTROL',4);self.sqlite('MACHINES',8)
        old=preflight.manifest(self.repo,self.previous);new=preflight.manifest(self.repo,self.candidate)
        self.assertEqual(preflight.choose_mode(new,old,preflight.live_schemas(self.db)),'defer')
        self.assertEqual(preflight.choose_mode(new,new,preflight.live_schemas(self.db)),'apply')
        self.assertEqual(preflight.choose_mode(new,new,[('CONTROL',5),('MACHINES',7)]),'apply')

    def test_already_migrated_live_state_cannot_use_old_rollback_reader(self):
        self.sqlite('CONTROL',5)
        with self.assertRaisesRegex(preflight.Incompatible,'rollback_cannot_read_live_schema'):
            preflight.choose_mode(preflight.manifest(self.repo,self.candidate),preflight.manifest(self.repo,self.previous),preflight.live_schemas(self.db))

    def test_census_reads_actual_hub2_schema_and_never_changes_user_rows(self):
        path=self.sqlite('MACHINES',7,identity='MACHINES');before=path.read_bytes()
        self.assertEqual(preflight.live_schemas(self.db),[('MACHINES',7)])
        self.assertEqual(path.read_bytes(),before)
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT value FROM retained').fetchone()[0],'must survive')

    def test_unknown_future_version_and_namespace_are_rejected(self):
        self.sqlite('CONTROL',999)
        with self.assertRaisesRegex(preflight.Incompatible,'candidate_cannot_read_live_schema'):
            preflight.choose_mode(self.latest,self.latest,preflight.live_schemas(self.db))
        self.sqlite('UNKNOWN',1)
        with self.assertRaisesRegex(preflight.Incompatible,'unknown_database_namespace'):preflight.live_schemas(self.db)

    def test_symlink_and_identity_mismatch_are_rejected(self):
        path=self.sqlite('CONTROL',4,identity='MACHINES')
        with self.assertRaisesRegex(preflight.Incompatible,'database_namespace_mismatch'):preflight.live_schemas(self.db)
        path.unlink();path.symlink_to(self.root/'private.sqlite')
        with self.assertRaisesRegex(preflight.Incompatible,'database_path_untrusted'):preflight.live_schemas(self.db)

    def test_stale_manifest_cannot_overstate_reader_support(self):
        source=self.repo/preflight.SOURCES['CONTROL'];source.write_text('initializeSchema(ctx,this.db,4,SCHEMA);')
        self.git('add','.');self.git('commit','-qm','stale contract');revision=self.git('rev-parse','HEAD')
        with self.assertRaisesRegex(preflight.Incompatible,'manifest_source_mismatch'):preflight.manifest(self.repo,revision)

    def test_cli_emits_only_mode_and_leaves_every_database_byte_unchanged(self):
        path=self.sqlite('CONTROL',4);before=path.read_bytes()
        done=subprocess.run(['python3',str(ROOT/'scripts/schema-rollback-preflight.py'),'--repo',str(self.repo),'--previous',self.previous,'--candidate',self.candidate,'--data-dir',str(self.db),'--print-mode'],capture_output=True,text=True)
        self.assertEqual((done.returncode,done.stdout),(0,'defer\n'),done.stderr)
        self.assertEqual(path.read_bytes(),before)

    def test_legacy_manifestless_release_with_migrations_accepts_mixed_live_versions_and_rejects_invalid_bounds(self):
        db_ts = self.repo / 'hub2/src/db.ts'
        db_ts.parent.mkdir(parents=True, exist_ok=True)
        db_ts.write_text(
            'export function initializeSchema(state, db, version, statements, migrations = []) {\n'
            '  while (applied < version) {\n'
            '    const migration = migrations[applied - 1];\n'
            '  }\n'
            '}\n'
        )
        legacy_sources = {
            'CONTROL': 'initializeSchema(ctx, this.db, 4, SCHEMA, [M1, M2, M3], afterInit);\n',
            'MACHINES': 'initializeSchema(ctx, this.db, 6, LATEST_SCHEMA, [[S0], M2, M3, M4, M5], () => this.scheduleNext(), 6);\n',
            'PROJECTS': 'initializeSchema(ctx, this.db, 4, SCHEMA, [], afterInit, 4);\n',
            'ACCOUNTS': 'initializeSchema(ctx, this.db, 2, SCHEMA, [DROP_SESSION_CAPACITY], undefined, 2);\n',
            'MAILBOXES': 'initializeSchema(ctx, this.db, 1, SCHEMA, [], () => this.schedule());\n',
        }
        for binding, src in legacy_sources.items():
            (self.repo / preflight.SOURCES[binding]).write_text(src)
        manifest_path = self.repo / 'deploy/schema-compatibility.json'
        if manifest_path.exists():
            manifest_path.unlink()
        self.git('add', '-A')
        self.git('commit', '-qm', 'legacy manifestless with migrations')
        legacy_rev = self.git('rev-parse', 'HEAD')

        old = preflight.manifest(self.repo, legacy_rev)
        new = preflight.manifest(self.repo, self.candidate)
        self.assertEqual(old['schemas']['ACCOUNTS']['min_read'], 1)
        self.assertEqual(old['schemas']['MACHINES']['min_read'], 1)
        self.assertEqual(old['schemas']['CONTROL']['min_read'], 1)
        self.assertEqual(old['schemas']['PROJECTS']['min_read'], 4)

        for name, (binding, ver) in {
            'ACCOUNTS-a.sqlite': ('ACCOUNTS', 1),
            'ACCOUNTS-b.sqlite': ('ACCOUNTS', 2),
            'MACHINES-a.sqlite': ('MACHINES', 5),
            'MACHINES-b.sqlite': ('MACHINES', 6),
            'CONTROL-a.sqlite': ('CONTROL', 4),
            'PROJECTS-a.sqlite': ('PROJECTS', 4),
        }.items():
            with closing(sqlite3.connect(self.db / name)) as db:
                db.execute('CREATE TABLE hub2_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL)')
                db.execute('INSERT INTO hub2_schema VALUES(1,?)', (ver,))
                db.commit()

        live = preflight.live_schemas(self.db)
        # Native commands bridge writes8: a legacy max6/7 rollback reader is insufficient.
        with self.assertRaisesRegex(preflight.Incompatible, 'rollback_cannot_read_candidate_schemas'):
            preflight.choose_mode(new, old, live)
        prior8 = copy.deepcopy(old)
        prior8['schemas']['MACHINES'].update(max_read=8, bridge_write=8, active_write=8)
        self.assertEqual(preflight.choose_mode(new, prior8, live), 'defer')

        with self.assertRaisesRegex(preflight.Incompatible, 'candidate_cannot_read_live_schema:ACCOUNTS'):
            preflight.choose_mode(new, old, [*live, ('ACCOUNTS', 3)])
        with self.assertRaisesRegex(preflight.Incompatible, 'rollback_cannot_read_live_schema:MACHINES'):
            preflight.choose_mode(new, old, [*live, ('MACHINES', 7)])
        with self.assertRaisesRegex(preflight.Incompatible, 'candidate_cannot_read_live_schema:PROJECTS'):
            preflight.choose_mode(new, old, [*live, ('PROJECTS', 3)])

        (self.repo / preflight.SOURCES['MACHINES']).write_text(
            'initializeSchema(ctx, this.db, 6, LATEST_SCHEMA, [M1, M2]);\n'
        )
        self.git('add', '-A')
        self.git('commit', '-qm', 'incomplete legacy migrations')
        bad_mig_rev = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(preflight.Incompatible, 'legacy_schema_source_unrecognized:MACHINES'):
            preflight.manifest(self.repo, bad_mig_rev)

        (self.repo / preflight.SOURCES['MACHINES']).write_text(legacy_sources['MACHINES'])
        db_ts.write_text('export function initializeSchema() {}\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'missing db migration loop')
        bad_db_rev = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(preflight.Incompatible, 'legacy_schema_migration_runner_unrecognized'):
            preflight.manifest(self.repo, bad_db_rev)
