#!/usr/bin/env python3
"""Read-only rollback compatibility check for a reviewed candidate and live SQLite schemas."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote

SOURCES = {
    'CONTROL': 'hub2/src/control-do.ts', 'MACHINES': 'hub2/src/machine-do.ts',
    'PROJECTS': 'hub2/src/project-do.ts', 'ACCOUNTS': 'hub2/src/account-do.ts',
    'MAILBOXES': 'hub2/src/mailbox-do.ts',
}
MAX_DATABASES = 5000


class Incompatible(RuntimeError):
    pass


def git_file(repo, revision, path, optional=False):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise Incompatible('full_commit_sha_required')
    done = subprocess.run(['git', '-C', str(repo), 'show', revision + ':' + path],
                          capture_output=True, timeout=10)
    if done.returncode:
        if optional:
            return None
        raise Incompatible('schema_source_missing:' + path)
    if len(done.stdout) > 1_048_576:
        raise Incompatible('schema_source_too_large')
    return done.stdout.decode('utf8')


def split_top_level(expr):
    items, cur, depth, quote_char, escape = [], [], 0, None, False
    for ch in expr:
        if quote_char:
            cur.append(ch)
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == quote_char:
                quote_char = None
            continue
        if ch in ("'", '"', '`'):
            quote_char = ch
            cur.append(ch)
        elif ch in ('(', '[', '{'):
            depth += 1
            cur.append(ch)
        elif ch in (')', ']', '}'):
            depth -= 1
            if depth < 0:
                raise ValueError('unbalanced_delimiters')
            cur.append(ch)
        elif ch == ',' and depth == 0:
            item = ''.join(cur).strip()
            if not item:
                raise ValueError('empty_argument')
            items.append(item)
            cur = []
        else:
            cur.append(ch)
    if quote_char or depth != 0:
        raise ValueError('unterminated_expression')
    tail = ''.join(cur).strip()
    if tail:
        items.append(tail)
    return items


def legacy_schema_bounds(source, binding):
    matches = list(re.finditer(r'initializeSchema\s*\(', source))
    if len(matches) != 1:
        raise Incompatible('legacy_schema_source_unrecognized:' + binding)
    start = matches[0].end()
    depth, quote_char, escape, pos = 1, None, False, start
    while pos < len(source) and depth > 0:
        ch = source[pos]
        if quote_char:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == quote_char:
                quote_char = None
        elif ch in ("'", '"', '`'):
            quote_char = ch
        elif ch in ('(', '[', '{'):
            depth += 1
        elif ch in (')', ']', '}'):
            depth -= 1
        pos += 1
    if depth != 0:
        raise Incompatible('legacy_schema_source_unrecognized:' + binding)
    try:
        args = split_top_level(source[start:pos - 1])
    except ValueError:
        raise Incompatible('legacy_schema_source_unrecognized:' + binding) from None
    if len(args) < 4 or args[0] not in ('ctx', 'this.ctx') or args[1] != 'this.db' or not re.fullmatch(r'\d+', args[2]):
        raise Incompatible('legacy_schema_source_unrecognized:' + binding)
    target = int(args[2])
    if not 1 <= target <= 100:
        raise Incompatible('legacy_schema_source_unrecognized:' + binding)
    if len(args) < 5:
        return target, target
    migrations_expr = args[4].strip()
    if not (migrations_expr.startswith('[') and migrations_expr.endswith(']')):
        raise Incompatible('legacy_schema_source_unrecognized:' + binding)
    inner = migrations_expr[1:-1].strip()
    if not inner:
        return target, target
    try:
        steps = split_top_level(inner)
    except ValueError:
        raise Incompatible('legacy_schema_source_unrecognized:' + binding) from None
    if target < 2 or len(steps) != target - 1 or any(step in ('[]', 'null', 'undefined') for step in steps):
        raise Incompatible('legacy_schema_source_unrecognized:' + binding)
    return 1, target


def manifest(repo, revision):
    raw = git_file(repo, revision, 'deploy/schema-compatibility.json', optional=True)
    if raw is None:
        schemas = {}
        needs_migration_runner = False
        for binding, path in SOURCES.items():
            source = git_file(repo, revision, path)
            min_read, target = legacy_schema_bounds(source, binding)
            if min_read < target:
                needs_migration_runner = True
            schemas[binding] = {'min_read': min_read, 'max_read': target, 'bridge_write': target, 'active_write': target}
        if needs_migration_runner:
            db_source = git_file(repo, revision, 'hub2/src/db.ts', optional=True)
            if db_source is None or not re.search(
                r'while\s*\(\s*applied\s*<\s*version\s*\).*?migrations\s*\[\s*applied\s*-\s*1\s*\]',
                db_source,
                re.DOTALL,
            ):
                raise Incompatible('legacy_schema_migration_runner_unrecognized')
        return {'format': 1, 'bridge': False, 'schemas': schemas}
    try:
        value = json.loads(raw)
        if set(value) != {'format', 'bridge', 'schemas'} or value['format'] != 1 or value['bridge'] is not True:
            raise ValueError()
        if set(value['schemas']) != set(SOURCES):
            raise ValueError()
        for binding, schema in value['schemas'].items():
            if set(schema) != {'min_read', 'max_read', 'bridge_write', 'active_write'}:
                raise ValueError()
            if any(type(v) is not int or not 1 <= v <= 100 for v in schema.values()):
                raise ValueError()
            if not schema['min_read'] <= schema['bridge_write'] <= schema['active_write'] == schema['max_read']:
                raise ValueError()
            source = git_file(repo, revision, SOURCES[binding])
            targets = re.findall(r'initializeSchema\(\s*(?:ctx|this\.ctx)\s*,\s*this\.db\s*,\s*(\d+)\s*,', source)
            if targets != [str(schema['max_read'])]:
                raise Incompatible('manifest_source_mismatch:' + binding)
            if schema['bridge_write'] != schema['active_write']:
                contract = r'schemaMigrationCeiling\(\s*env\.HUB2_SCHEMA_MIGRATIONS\s*,\s*' + str(schema['bridge_write']) + r'\s*,\s*' + str(schema['active_write']) + r'\s*\)'
                if not re.search(contract, source):
                    raise Incompatible('bridge_source_contract_missing:' + binding)
        return value
    except (ValueError, TypeError, KeyError):
        raise Incompatible('invalid_schema_compatibility_manifest') from None


def live_schemas(directory):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise Incompatible('database_directory_missing')
    rows = []
    started = time.monotonic()
    for path in root.iterdir():
        if not path.name.endswith('.sqlite'):
            continue
        if len(rows) >= MAX_DATABASES or time.monotonic() - started > 30:
            raise Incompatible('database_census_limit')
        if path.is_symlink() or not path.is_file():
            raise Incompatible('database_path_untrusted')
        binding = path.name.split('-', 1)[0]
        if binding not in SOURCES:
            raise Incompatible('unknown_database_namespace')
        try:
            with closing(sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True, timeout=5)) as db:
                db.execute('PRAGMA query_only=ON')
                identity_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub2_identity'").fetchone()
                if identity_exists:
                    identity = db.execute('SELECT binding FROM hub2_identity WHERE singleton=1').fetchone()
                    if identity is None or identity[0] != binding:
                        raise Incompatible('database_namespace_mismatch')
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub2_schema'").fetchone()
                record = db.execute('SELECT version FROM hub2_schema WHERE singleton=1').fetchone() if exists else None
                if record is None:
                    raise Incompatible('database_schema_record_missing')
                version = record[0] if record else 0
                if type(version) is not int or version < 1:
                    raise Incompatible('database_schema_version_invalid')
                rows.append((binding, version))
        except sqlite3.Error:
            raise Incompatible('database_schema_read_failed') from None
    return rows


def choose_mode(candidate, previous, live):
    for binding, version in live:
        if not version:
            continue
        for label, value in [('candidate', candidate), ('rollback', previous)]:
            schema = value['schemas'][binding]
            if not schema['min_read'] <= version <= schema['max_read']:
                raise Incompatible(label + '_cannot_read_live_schema:' + binding)
    for mode, field in [('apply', 'active_write'), ('defer', 'bridge_write')]:
        if mode == 'defer' and not candidate['bridge']:
            continue
        if all(previous['schemas'][binding]['min_read'] <= schema[field] <= previous['schemas'][binding]['max_read']
               for binding, schema in candidate['schemas'].items()):
            return mode
    raise Incompatible('rollback_cannot_read_candidate_schemas')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--previous', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--print-mode', action='store_true')
    args = parser.parse_args()
    try:
        previous, candidate = manifest(args.repo, args.previous), manifest(args.repo, args.candidate)
        live = live_schemas(args.data_dir)
        mode = choose_mode(candidate, previous, live)
        if args.print_mode:
            print(mode)
        else:
            print(json.dumps({'schema_migrations': mode, 'bridge_required': mode == 'defer',
                              'databases_checked': len(live),
                              'live_max_versions': {binding: max([v for k, v in live if k == binding], default=0) for binding in SOURCES}}))
    except (Incompatible, OSError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, Incompatible) else 'schema_preflight_unavailable'
        print('Schema rollback preflight refused: ' + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
