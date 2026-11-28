"""Codex executes only through the maintained quota wrapper (synthetic end to end).

The wrapper under test has the exact shape of the maintained public script
(`set -euo pipefail`, `python3 <repo>/scripts/quota-gate.py`, `exec <codex> "$@"`)
but points at a synthetic gate and a synthetic Codex that only record what they
saw. No provider, credential, quota service or live profile is involved.
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agent_hub_host import provisioning as p

HOST = str(Path(__file__).resolve().parents[1])
LABEL = 'my-codex'
REVISION = '11111111-1111-4111-8111-111111111111'

GATE = '''import os, sys
from pathlib import Path
repo = Path(__file__).resolve().parents[1]
with open(repo / 'gate.log', 'a') as log:
    log.write('gate\\n')
mode = (repo / 'quota.txt').read_text().strip()
if mode == 'timeout':
    sys.exit(124)
try:
    remaining = float(mode)
except ValueError:
    sys.exit(2)
sys.exit(0 if remaining > 15 else 1)
'''

CODEX = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
record = {'argv': sys.argv[1:], 'cwd': os.getcwd(),
          'env': {k: os.environ.get(k) for k in ('HOME', 'CODEX_HOME', 'OPENAI_API_KEY')}}
with open(Path(__file__).resolve().parent / 'codex-run.json', 'a') as out:
    out.write(json.dumps(record) + '\\n')
print('{"type":"synthetic-output"}')
'''

DRIVER = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_hub_host import provisioning as p
managed = Path(sys.argv[2])
p.root = lambda: managed
p.hub_launch_gate = lambda *a, **k: (True, 'ok')
sys.argv[:] = ['execute'] + sys.argv[3:]
p.execute()
'''


def wrapper_text(repo, codex):
    return ('#!/usr/bin/env bash\nset -euo pipefail\nrepo=%s\n'
            'python3 "$repo/scripts/quota-gate.py"\nexec %s "$@"\n') % (repo, codex)


class World:
    def __init__(self, tmp_path):
        os.chmod(tmp_path, 0o700)
        self.base = tmp_path
        self.repo = tmp_path / 'repo'
        (self.repo / 'scripts').mkdir(parents=True, mode=0o700)
        (self.repo / 'scripts/quota-gate.py').write_text(GATE)
        self.codex = self.repo / 'codex'
        self.codex.write_text(CODEX)
        self.codex.chmod(0o700)
        self.wrapper = self.repo / 'scripts/launch-codex.sh'
        self.wrapper.write_text(wrapper_text(self.repo, self.codex))
        self.wrapper.chmod(0o700)
        self.managed = tmp_path / 'managed'
        self.managed.mkdir(mode=0o700)
        self.home = tmp_path / 'home'
        self.home.mkdir(mode=0o700)
        bundle = dict(label=LABEL, provider='codex', version='0.163.0', revision=REVISION,
                      files={'.codex/auth.json': '{"token":"synthetic-private-marker"}'}, env={}, args=[])
        self.dest = self.managed / (LABEL + '-' + REVISION)
        self.dest.mkdir(mode=0o700)
        p.write_json(self.dest / 'bundle.json', bundle)
        p.write_json(self.managed / 'state.json', [{'label': LABEL, 'revision': REVISION, 'provider': 'codex'}])
        self.quota('80')

    def quota(self, value):
        (self.repo / 'quota.txt').write_text(value)

    @property
    def gate_calls(self):
        log = self.repo / 'gate.log'
        return len(log.read_text().split()) if log.exists() else 0

    @property
    def runs(self):
        out = self.repo / 'codex-run.json'
        return [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []

    def execute(self, *argv, wrapper='default', label=None):
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.home), 'AGENT_HUB_MANAGED_DIR': str(self.managed),
               'OPENAI_API_KEY': 'ambient-secret', 'CODEX_HOME': '/ambient/codex'}
        value = str(self.wrapper) if wrapper == 'default' else wrapper
        if value is not None:
            env['CODEX_LAUNCH_WRAPPER'] = value
        work = self.base / 'work'
        work.mkdir(exist_ok=True)
        return subprocess.run([sys.executable, '-c', DRIVER, HOST, str(self.managed), label or LABEL, *argv],
                              env=env, cwd=work, capture_output=True, text=True, timeout=60)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


HEADLESS = ['exec', '--model', 'gpt-6.1-sol', '-c', 'model_reasoning_effort="high"', 'synthetic prompt']


def test_headless_execution_goes_through_gate_once_with_exact_argv_and_account_env(world):
    result = world.execute(*HEADLESS)
    assert result.returncode == 0, result.stderr
    assert world.gate_calls == 1
    [run] = world.runs
    # Wrapper adds nothing: the SDK argv (model, effort, prompt) is unchanged and
    # the permission bypass is placed before the subcommand as before.
    assert run['argv'][0] == '--dangerously-bypass-approvals-and-sandbox'
    assert run['argv'][1:] == HEADLESS
    assert run['env']['HOME'] == str(world.dest / 'home')
    assert run['env']['CODEX_HOME'] == str(world.dest / 'home/.codex')
    assert run['env']['OPENAI_API_KEY'] != 'ambient-secret'
    assert Path(run['cwd']).resolve() == (world.base / 'work').resolve()
    assert '{"type":"synthetic-output"}' in result.stdout


@pytest.mark.parametrize('quota', ['6', '15', '15.0', 'malformed', 'timeout', ''])
def test_quota_refusal_never_starts_sdk(world, quota):
    world.quota(quota)
    result = world.execute(*HEADLESS)
    assert result.returncode != 0
    assert world.gate_calls == 1
    assert world.runs == []


def test_wrapper_exit_75_never_starts_sdk(world):
    (world.repo / 'scripts/quota-gate.py').write_text('import sys\nsys.exit(75)\n')
    result = world.execute(*HEADLESS)
    assert result.returncode == 75
    assert world.runs == []


def test_retained_headless_output_path_receives_wrapper_argv(tmp_path, monkeypatch):
    from agent_hub_host import headless_output
    world = World(tmp_path)
    monkeypatch.setattr(p, 'root', lambda: world.managed)
    monkeypatch.setattr(p, 'hub_launch_gate', lambda *a: (True, 'ok'))
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', str(world.wrapper))
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(world.managed))
    seen = {}
    monkeypatch.setattr(headless_output, 'identity', lambda env, engine: {'native_id': 'x'})
    monkeypatch.setattr(headless_output, 'run', lambda argv, env, engine: seen.update(argv=argv, engine=engine) or 0)
    def finish(code):
        seen['exit'] = code
        raise SystemExit(code)
    monkeypatch.setattr(headless_output, 'exit_like_child', finish)
    monkeypatch.setattr(sys, 'argv', ['execute', LABEL, *HEADLESS])
    with pytest.raises(SystemExit):
        p.execute()
    assert seen['argv'][0] == str(world.wrapper.resolve()) and seen['argv'][1] == '--dangerously-bypass-approvals-and-sandbox'
    assert seen['argv'][2:] == HEADLESS and seen['engine'] == 'codex' and seen['exit'] == 0
    assert world.runs == [] and world.gate_calls == 0, 'only the actual wrapper runs the gate'


def test_unsafe_missing_or_incompatible_wrapper_refuses_before_sdk(world, tmp_path):
    cases = {}
    cases['missing'] = str(world.base / 'absent.sh')
    cases['relative'] = 'launch-codex.sh'
    cases['empty'] = ''
    cases['directory'] = str(world.repo / 'scripts')
    writable = world.repo / 'scripts/writable.sh'
    writable.write_text(world.wrapper.read_text())
    writable.chmod(0o707)
    cases['world-writable'] = str(writable)
    for name, body in {'raw-exec': '#!/usr/bin/env bash\nset -euo pipefail\nexec %s "$@"\n' % world.codex,
                       'no-fail-closed': '#!/usr/bin/env bash\npython3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'gate-only-in-comment': '#!/usr/bin/env bash\nset -euo pipefail\nexec %s "$@"\n# quota-gate.py\n' % world.codex,
                       'extra-args': wrapper_text(world.repo, world.codex).replace('"$@"', '--x "$@"'),
                       'shebang-semicolon': '#!/usr/bin/env bash; set -euo pipefail; echo quota-gate.py; exec %s "$@"\n' % world.codex,
                       'echo-gate': '#!/usr/bin/env bash\nset -euo pipefail\necho quota-gate.py\nexec %s "$@"\n' % world.codex,
                       'conditional-gate': '#!/usr/bin/env bash\nset -euo pipefail\nif false; then python3 %s/scripts/quota-gate.py; fi\nexec %s "$@"\n' % (world.repo, world.codex),
                       'ignored-failure': '#!/usr/bin/env bash\nset -euo pipefail\npython3 %s/scripts/quota-gate.py || true\nexec %s "$@"\n' % (world.repo, world.codex),
                       'shadowed-python': '#!/usr/bin/env bash\nset -euo pipefail\npython3() { :; }\nrepo=%s\npython3 "$repo/scripts/quota-gate.py"\nexec %s "$@"\n' % (world.repo, world.codex),
                       'reordered-statements': '#!/usr/bin/env bash\nrepo=%s\npython3 "$repo/scripts/quota-gate.py"\nset -euo pipefail\nexec %s "$@"\n' % (world.repo, world.codex),
                       'repo-traversal': '#!/usr/bin/env bash\nset -euo pipefail\nrepo=%s/../repo\npython3 "$repo/scripts/quota-gate.py"\nexec %s "$@"\n' % (world.repo, world.codex),
                       'comment-vt-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\vpython3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'comment-ff-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\fpython3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'comment-nel-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\x85python3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'comment-ls-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\u2028python3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'comment-ps-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\u2029python3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'ws-nbsp-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n\u00a0python3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'ctrl-cr': '#!/usr/bin/env bash\nset -euo pipefail\r\npython3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'ctrl-fs': '#!/usr/bin/env bash\nset -euo pipefail\n\x1cpython3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'ctrl-del': '#!/usr/bin/env bash\nset -euo pipefail\n# \x7f\npython3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex),
                       'ctrl-bom': '#!/usr/bin/env bash\nset -euo pipefail\n\ufeffpython3 %s/scripts/quota-gate.py\nexec %s "$@"\n' % (world.repo, world.codex)}.items():
        script = world.repo / ('scripts/%s.sh' % name)
        script.write_text(body)
        script.chmod(0o700)
        cases[name] = str(script)
    writable_dir = world.base / 'open'
    writable_dir.mkdir()
    nested = writable_dir / 'launch-codex.sh'
    nested.write_text(world.wrapper.read_text())
    nested.chmod(0o700)
    writable_dir.chmod(0o777)
    cases['world-writable-parent'] = str(nested)
    link = world.base / 'link.sh'
    link.symlink_to(writable)
    cases['symlink-to-unsafe'] = str(link)
    for name, value in cases.items():
        result = world.execute(*HEADLESS, wrapper=value)
        assert result.returncode != 0, name
        assert 'codex_launch_wrapper_unavailable' in result.stderr, name
        assert world.runs == [] and world.gate_calls == 0, name


def test_bundle_cannot_choose_wrapper_and_default_is_operator_home_only(world, monkeypatch):
    bundle = dict(label=LABEL, provider='codex', version='0.163.0', revision=REVISION, files={},
                  env={'CODEX_LAUNCH_WRAPPER': '/evil'}, args=['--x'])
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', str(world.wrapper))
    assert p.engine_argv(bundle, world.dest) == [str(world.wrapper.resolve())]
    # Unset: the fixed maintained location under the given operator home.
    home = world.base / 'operator'
    target = home.joinpath(*p.CODEX_WRAPPER_DEFAULT)
    target.parent.mkdir(parents=True, mode=0o700)
    assert target.parent.parent.parent.is_dir()
    with pytest.raises(ValueError, match='codex_launch_wrapper_unavailable'):
        p.codex_launch_wrapper(environ={}, home=home)
    target.write_text(wrapper_text(world.repo, world.codex))
    target.chmod(0o700)
    assert p.codex_launch_wrapper(environ={}, home=home) == target.resolve()
    # A configured but unusable value never falls back to the default.
    with pytest.raises(ValueError, match='codex_launch_wrapper_unavailable'):
        p.codex_launch_wrapper(environ={'CODEX_LAUNCH_WRAPPER': str(home / 'nope')}, home=home)
    with pytest.raises(ValueError, match='codex_launch_wrapper_unavailable'):
        p.codex_launch_wrapper(environ={'CODEX_LAUNCH_WRAPPER': ''}, home=home)


def test_canonical_wrapper_templates_with_shell_whitespace(world):
    templates = {
        'repo-template': (
            '#!/usr/bin/env bash\n'
            '  # maintained comment\n'
            '\tset -euo pipefail\n'
            f'repo={world.repo}\n'
            'python3 "$repo/scripts/quota-gate.py"\n'
            f'exec {world.codex} "$@"\n'
        ),
        'direct-template': (
            '#!/bin/bash\n'
            'set -euo pipefail\n'
            f'\tpython3 "{world.repo}/scripts/quota-gate.py"\n'
            f'exec {world.codex} "$@"\n'
        ),
    }
    for name, body in templates.items():
        script = world.repo / ('scripts/%s.sh' % name)
        script.write_text(body)
        script.chmod(0o700)
        assert p.codex_launch_wrapper(environ={'CODEX_LAUNCH_WRAPPER': str(script)}) == script.resolve(), name


def test_other_providers_are_not_wrapped(world, monkeypatch):
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', '/nonexistent')
    gemini = dict(label='g', provider='gemini', version='1', revision=REVISION, files={}, env={}, args=[])
    argv = p.engine_argv(gemini, world.dest)
    assert argv == [str(world.dest / 'engine/node_modules/.bin/gemini')]


def test_windows_codex_fails_closed_without_guessing_bash(world, monkeypatch):
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', str(world.wrapper))
    codex = dict(label=LABEL, provider='codex', version='0.163.0', revision=REVISION, files={}, env={}, args=[])
    with pytest.raises(ValueError, match='codex_launch_wrapper_unavailable'):
        p.engine_argv(codex, world.dest, is_windows=True)
    monkeypatch.setattr(sys, 'platform', 'win32')
    with pytest.raises(ValueError, match='codex_launch_wrapper_unavailable'):
        p.codex_launch_wrapper()


def test_wrong_account_label_runs_nothing(world):
    result = world.execute(*HEADLESS, label='some-other-account')
    assert result.returncode != 0 and 'Managed profile removed' in result.stderr
    assert world.runs == [] and world.gate_calls == 0


def profile_toml(path, command, engine='codex', extra=''):
    block = '[profiles.hub-shared-codex]\nengine = %s\ncommand = %s\n%s' % (json.dumps(engine), json.dumps(command), extra)
    path.write_text('version = 1\n\n' + block)
    path.chmod(0o600)


def test_exec_readiness_requires_maintained_profile_for_this_archive(world, monkeypatch):
    archive = world.base / 'hub.pyz'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('agent_hub_host/__init__.py', '')
    config = world.base / 'aplexer.toml'
    monkeypatch.setenv('APLEXER_CONFIG', str(config))
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', str(world.wrapper))
    monkeypatch.setattr(sys, 'argv', [str(archive)])
    good = p.module_command('execute', 'shared-codex')
    profile_toml(config, good)
    proof = p.codex_exec_readiness(archive)
    assert proof == {'protocol': p.CODEX_WRAPPER_PROTOCOL, 'label': 'shared-codex',
                     'profile': 'hub-shared-codex', 'wrapper': str(world.wrapper.resolve())}
    other = world.base / 'other.pyz'
    other.write_bytes(archive.read_bytes())
    bad = {
        'custom exec': ['/bin/sh', '-c', 'exec codex "$@"', 'shared-codex'],
        'other archive': [good[0], '-c', good[2].replace(str(archive), str(other)), 'shared-codex'],
        'other label': good[:-1] + ['someone-else'],
        'ungated handler': [good[0], '-c', good[2].replace('execute()', 'noop()'), 'shared-codex'],
        'unverified interpreter': ['/tmp/evil-python', *good[1:]],
        'extra flag': [good[0], '-u', *good[1:]],
        'old archive ahead': [good[0], '-c', good[2].replace('import os;', f'sys.path.insert(0, {repr(str(other))}); import os;'), 'shared-codex'],
        'additional statement': [good[0], '-c', good[2].replace('execute()', 'os.system("true"); execute()'), 'shared-codex'],
        'wrong managed dir': [good[0], '-c', good[2].replace(repr(str(p.root())), repr(str(world.base / 'other-managed'))), 'shared-codex'],
    }
    for name, command in bad.items():
        profile_toml(config, command)
        with pytest.raises(ValueError, match='codex_launch_wrapper_unavailable'):
            p.codex_exec_readiness(archive)
    profile_toml(config, good, engine='other')
    with pytest.raises(ValueError):
        p.codex_exec_readiness(archive)
    profile_toml(config, good, extra='args = ["--oss"]\n')
    with pytest.raises(ValueError):
        p.codex_exec_readiness(archive)
    profile_toml(config, good)
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', str(world.base / 'absent'))
    with pytest.raises(ValueError):
        p.codex_exec_readiness(archive)
