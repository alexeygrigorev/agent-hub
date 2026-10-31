#!/usr/bin/env python3
"""Select the tests affected by a change (intake cb5c362e).

Pre-push verification runs only these; the full suite runs once after the push
in the deploy CI job. Prints JSON: vitest files, pytest files, flags. Anything
that can change test behaviour globally (dependencies, runner config, the
admission gate) sets "broad": true and the caller falls back to the full runner.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BROAD = ('package.json', 'package-lock.json', 'vitest.config.ts', 'wrangler.jsonc', 'pyproject.toml', 'uv.lock',
         'host/tests/conftest.py', 'test/heavy-slot.global-setup.mjs')
BROAD_PREFIX = ('tsconfig', 'scripts/heavy-', 'scripts/capped-', 'scripts/test-full-local', 'scripts/local-dependencies')
SOURCE_DIRS = ('src/', 'public/', 'host/', 'scripts/', 'test/', 'migrations/')
TEST_ROOTS = ('test', 'host/tests')
TEST_RE = re.compile(r'(^test/.+\.test\.ts$|^test/test_.+\.py$|^host/tests/test_.+\.py$)')


def git(*args, cwd=ROOT):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def changed_files(base='origin/main', cwd=ROOT):
    fork = git('merge-base', 'HEAD', base, cwd=cwd).strip()
    names = set(git('diff', '--name-only', '--no-renames', fork, cwd=cwd).splitlines())
    names |= {line[3:].split(' -> ')[-1] for line in git('status', '--porcelain', cwd=cwd).splitlines()}
    return sorted(names)


def all_tests(root=ROOT):
    found = []
    for base in TEST_ROOTS:
        for path in (root / base).rglob('*'):
            rel = path.relative_to(root).as_posix()
            if path.is_file() and TEST_RE.match(rel):
                found.append(rel)
    return sorted(found)


def module_regex(stem):
    s = re.escape(stem)
    return re.compile(rf'(agent_hub_host\.{s}\b|from agent_hub_host import [^\n]*\b{s}\b|^\s*(?:import|from) {s}\b|{s}\.(?:py|ts|js|html)\b)', re.M)


def select(changed, root=ROOT):
    broad = [c for c in changed if c in BROAD or c.startswith(BROAD_PREFIX)]
    tests = all_tests(root)
    texts = {}
    picked = {c for c in changed if TEST_RE.match(c) and (root / c).exists()}
    for path in changed:
        if not path.startswith(SOURCE_DIRS) or TEST_RE.match(path) or not (root / path).exists():
            continue
        stem = Path(path).stem
        rx = module_regex(stem)
        for test in tests:
            if test not in texts:
                texts[test] = (root / test).read_text(errors='replace')
            if Path(test).stem.replace('test_', '').startswith(stem) or path in texts[test] or rx.search(texts[test]):
                picked.add(test)
    ts = sorted(p for p in picked if p.endswith('.ts'))
    py = sorted(p for p in picked if p.endswith('.py'))
    return {
        'changed': changed,
        'broad': broad,
        'vitest': ts,
        'pytest': py,
        'typecheck': any(c.endswith('.ts') or c.startswith('tsconfig') for c in changed),
        'workflows': any(c.startswith(('.github/workflows/', 'scripts/ci/')) for c in changed),
        'build': any(c.startswith(('src/', 'wrangler')) for c in changed),
    }


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else 'origin/main'
    json.dump(select(changed_files(base)), sys.stdout, indent=1)
    print()


if __name__ == '__main__':
    main()
