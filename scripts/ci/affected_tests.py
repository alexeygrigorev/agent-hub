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
MCP_FILES = frozenset({
    'src/mcp-tools.ts', 'src/mcp.ts', 'src/mcp-usage.ts', 'src/mcp-oauth.ts',
    'src/conversation-hub.ts', 'src/worker.ts', 'src/ui-pages.ts',
})
PARITY_FILES = frozenset({
    'host/agent_hub_host/cli.py', 'scripts/mcp-cli-inventory.py',
    'test/mcp-cli-commands.json', 'src/mcp-tools.ts',
})
PARITY_TESTS = ('test/test_mcp_parity.py', 'test/mcp.test.ts')
HOST_CLI_RE = re.compile(r'^host/agent_hub_host/[^/]+_cli\.py$')
HOST_CANARY_TESTS = frozenset({
    # Keep the recurring regressions found by Host canary hermeticity in the
    # pre-push lane for every host change. These cross-area contracts are not
    # reliably connected to host files by import-based test discovery.
    'host/tests/test_machine_binding.py',
    'host/tests/test_release_completeness.py',
    'host/tests/test_bootstrap_control.py',
    'test/test_mcp_parity.py',
})


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


def module_regex(source_path):
    """Match imports of a source module without crossing language boundaries.

    A basename-only fallback made ``src/fleet.ts`` match every Python test
    mentioning ``agent_hub_host/fleet.py``. Keep import syntax language-aware;
    explicit references to the full changed path are handled separately in
    ``select``.
    """
    path = Path(source_path)
    stem = re.escape(path.stem)
    suffix = path.suffix
    if suffix == '.py':
        return re.compile(
            rf'(agent_hub_host\.{stem}\b|from agent_hub_host import [^\n]*\b{stem}\b|'
            rf'^\s*(?:import|from) {stem}\b)', re.M)
    if suffix in {'.ts', '.js'}:
        extension = r'(?:\.ts|\.js)?'
        return re.compile(
            rf'(?:\bfrom\s*[\'"][^\'"]*(?:/|\\){stem}{extension}[\'"]|'
            rf'\bimport\s*[\'"][^\'"]*(?:/|\\){stem}{extension}[\'"])', re.M)
    return re.compile(r'(?!)')


def select(changed, root=ROOT):
    broad = [c for c in changed if c in BROAD or c.startswith(BROAD_PREFIX)]
    tests = all_tests(root)
    texts = {}
    picked = {c for c in changed if TEST_RE.match(c) and (root / c).exists()}
    for path in changed:
        if path in MCP_FILES and (root / 'test/mcp.test.ts').is_file():
            picked.add('test/mcp.test.ts')
        if path in PARITY_FILES or HOST_CLI_RE.match(path):
            picked.update(test for test in PARITY_TESTS if (root / test).is_file())
        if not path.startswith(SOURCE_DIRS) or TEST_RE.match(path) or not (root / path).exists():
            continue
        stem = Path(path).stem
        rx = module_regex(path)
        for test in tests:
            if test not in texts:
                texts[test] = (root / test).read_text(errors='replace')
            if Path(test).stem.replace('test_', '').startswith(stem) or path in texts[test] or rx.search(texts[test]):
                picked.add(test)
    if any(path.startswith('host/') for path in changed):
        picked.update(test for test in HOST_CANARY_TESTS if (root / test).is_file())
    if 'scripts/host-release-manifest.json' in changed and \
            (root / 'host/tests/test_release_completeness.py').is_file():
        picked.add('host/tests/test_release_completeness.py')
    if any(path in {'scripts/ci/affected_tests.py', 'scripts/ci/test_affected_tests.py'}
           for path in changed) and (root / 'scripts/ci/test_affected_tests.py').is_file():
        picked.add('scripts/ci/test_affected_tests.py')
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
