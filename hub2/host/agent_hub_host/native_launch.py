"""Direct native launch adapter: Hub session plans spawn through aplexer.

The Hub picks a model and host, then this adapter returns the native pane
identity immediately and preserves evidence for qualified exit and recovery. The
external Agent Quota Launcher remains untouched for its other consumers.
"""
import hashlib
import json
import math
import ntpath
import os
import re
import shutil
import subprocess
import stat
import sys
import time
import unicodedata
from pathlib import Path

from .client import private_file
from . import provider_home
from . import claude_goal
from . import resource_admission
from .provider_home import clean_provider_environment, native_environment, session_binding

FIRST_ACTION_INSTRUCTION = (
    "\n\nFirst action (required before any other work): prove this worker "
    "is live by running `aplexer whoami --json` and writing its complete "
    "stdout to {path} (overwrite that file, nothing else). "
    "Then continue the brief."
)
REQUIRED_WHOAMI_FIELDS = {"id", "tag", "workspace", "command", "phase", "schema_version", "worker_pid"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
def native_submission(command):
    """Validate the private immutable strict-SEND wire intent, without effects."""
    submission = command.get('submission')
    if (not isinstance(submission, dict) or set(submission) != {'operation_id', 'created_at_ms', 'deadline_ms', 'text', 'request_sha256'}
            or submission.get('operation_id') != command.get('id')
            or any(not isinstance(command.get(key), str) or not UUID_RE.fullmatch(command[key])
                   or command[key] != command[key].lower() for key in ('id', 'native_id'))
            or type(submission.get('created_at_ms')) is not int or not 0 < submission['created_at_ms'] <= 2**53-1
            or type(submission.get('deadline_ms')) is not int or submission['deadline_ms'] != command.get('deadline_ms')
            or not 0 < submission['deadline_ms'] <= 2**53-1):
        raise ValueError('invalid_native_send')
    text = submission.get('text')
    try:
        valid = (isinstance(text, str) and bool(text.strip()) and len(text.encode('utf8')) <= 8192
                 and not any((ord(c) < 32 or 127 <= ord(c) <= 159) and c not in '\n\r\t' for c in text))
        target = command['identity']
        digest = hashlib.sha256(json.dumps([command['native_id'], command['id'], target['tag'], target['workspace'],
            submission['created_at_ms'], submission['deadline_ms'], text], ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest()
    except (KeyError, TypeError, UnicodeError):
        raise ValueError('invalid_native_send') from None
    if not valid or submission.get('request_sha256') != digest:
        raise ValueError('invalid_native_send')
    return submission


TOMBSTONE_ENVELOPE_BYTES = 1048576
TOMBSTONE_METADATA_BYTES = 65536
# The only approved OpenCode subscription model; GLM never leaves the zai route.
OPENCODE_MODEL = "opencode-go/muse-spark-1.3-contributor"
PROVIDER_CAPABILITIES = {"shell": {"headless"}, "zai": {"headless", "interactive", "goal", "effort"},
                         "codex": {"headless", "interactive", "goal", "effort"},
                         "go": {"headless", "interactive"},
                         "claude": {"headless", "interactive", "goal", "effort"},
                         "antigravity": {"headless", "interactive", "effort"}}
PROVIDER_BINARIES = {"zai": "zcodex", "codex": "codex", "go": "opencode", "claude": "claude", "antigravity": "agy"}
FIRST_ACTION_PROVIDERS = {"zai", "go", "claude", "codex", "antigravity"}
# Central account delivery supports these providers in providers.provision().
# No local profile or account association: the picked account is validated there.
# The zcodex engine (native GLM route, t3897u) shares the zai provider wiring.
CENTRAL_ROUTES = {provider: {"provider": provider} for provider in ("codex", "claude", "go", "zai")}
CENTRAL_ROUTES["zcodex"] = {"provider": "zai"}

LOCAL_ROUTES = {
    # Local Codex runs the host's own native login through the maintained
    # quota wrapper, exactly like local interactive Codex. The legacy
    # `hub-shared-codex` profile used a Hub1 managed home whose credential no
    # Hub2 component refreshes: once its access token expired, every headless
    # start failed the wrapper's quota gate (401) with exit 75 within seconds.
    # An explicitly configured profile route keeps the profile path.
    "codex": {"provider": "codex", "engine": "codex"},
    "go": {"provider": "go", "profile": "hub-shared-go"},
    "zai": {"provider": "zai", "profile": "hub-zai-central"},
    "zcodex": {"provider": "zai", "profile": "hub-zai-central"},
    "claude": {"provider": "claude", "engine": "claude"},
    "antigravity": {"provider": "antigravity", "profile": "hub-hub-antigravity"},
}
# Keep the direct adapter and maintained local ZAI profile on the same harness.
ZAI_HARNESS_FLAGS = ('--disable', 'shell_snapshot')
HEADLESS_OUTPUT_FLAGS = {
    'claude': ('--output-format', 'stream-json', '--verbose'),
    'antigravity': ('--output-format', 'stream-json'),
}
# Hub headless Claude has no approval surface. The user-only source deliberately
# excludes project/local Claude settings that can reject the managed bypass;
# interactive sessions retain their normal workspace settings.
CLAUDE_MANAGED_HEADLESS_FLAGS = ('--setting-sources', 'user')


def native_returncode(value):
    if not isinstance(value, dict):
        return None
    code, exit_signal = value.get('code'), value.get('signal')
    if type(code) is int and -2147483648 <= code <= 2147483647 and exit_signal is None:
        return code
    if 'code' in value and code is None and type(exit_signal) is int and 1 <= exit_signal <= 127:
        return -exit_signal
    return None


# Inherited credentials that must never reach aplexer or the native SDK,
# where the ambient value would override the central private home.
INHERITED_SECRET_VARS = ('HUB2_LAUNCH_CONTEXT', 'HUB2_TOKEN', 'HUB2_SERVER', 'HUB2_CONFIG', 'HUB_TOKEN', 'HUB_JOB_TOKEN',
                         'HUB_CONFIG_FILE', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_AUTH_TOKEN', 'CODEX_BASE_URL',
                         'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ZAI_API_KEY', 'GO_API_KEY', 'AWS_ACCESS_KEY_ID',
                         'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                         'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')


CODEX_WRAPPER_PROTOCOL = 'hub2-codex-wrapper-exec-v1'
CODEX_WRAPPER_PROFILE = 'hub-shared-codex'
CODEX_WRAPPER_DEFAULT = ('git', 'cloudflare-agent-git', 'scripts', 'launch-codex.sh')


def _valid_codex_wrapper_body(text):
    """Return True only for the exact canonical maintained Codex quota wrapper template."""
    if not isinstance(text, str):
        return False
    # Reject control characters (except LF/HT) and non-ASCII whitespace/separators
    # so Python line/whitespace parsing cannot diverge from Bash.
    for ch in text:
        if ch in (' ', '\t', '\n'):
            continue
        if (ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f or ch.isspace()
                or unicodedata.category(ch) in ('Cc', 'Cf', 'Zl', 'Zp', 'Zs')):
            return False
    raw_lines = text.split('\n')
    if not raw_lines or raw_lines[0] not in ('#!/usr/bin/env bash', '#!/bin/bash'):
        return False
    stmts = []
    for line in raw_lines[1:]:
        stripped = line.strip(' \t')
        if stripped and not stripped.startswith('#'):
            stmts.append(stripped)
    if len(stmts) not in (3, 4) or stmts[0] != 'set -euo pipefail':
        return False
    if len(stmts) == 4:
        repo_match = re.fullmatch(r'repo=(/[A-Za-z0-9._/@+-]+)', stmts[1])
        if not repo_match or '..' in Path(repo_match.group(1)).parts:
            return False
        if stmts[2] != 'python3 "$repo/scripts/quota-gate.py"':
            return False
    else:
        gate_match = re.fullmatch(r'python3 (?:"(/[A-Za-z0-9._/@+-]+/quota-gate\.py)"|(/[A-Za-z0-9._/@+-]+/quota-gate\.py))', stmts[1])
        if not gate_match:
            return False
        gate_path = gate_match.group(1) or gate_match.group(2)
        if '..' in Path(gate_path).parts:
            return False
    exec_match = re.fullmatch(r'exec (/[A-Za-z0-9._/@+-]+/codex) "\$@"', stmts[-1])
    if not exec_match or '..' in Path(exec_match.group(1)).parts:
        return False
    return True


def resolve_windows_program(program, environ=None):
    """Absolute path of a bare engine name on Windows (PATH x PATHEXT).

    Windows npm installs expose only claude/claude.cmd shims; an engine spawned
    by bare name from a process that resolves .exe alone dies "program not
    found" (exit 127). Anything that is not a bare name, or is not found, is
    returned unchanged. Other platforms never rewrite."""
    if os.name != 'nt' or not isinstance(program, str) or not program or any(c in program for c in '/\\:'):
        return program
    environ = os.environ if environ is None else environ
    found = shutil.which(program, path=environ.get('PATH'))
    return found if found and ntpath.isabs(found) else program


def _is_executable_file(path):
    candidate = Path(path)
    if not candidate.is_file():
        return False
    return True if os.name == 'nt' else os.access(candidate, os.X_OK)


def resolve_engine_binary(provider, environ=None):
    """Resolve an engine binary by PATH, managed install, or known nvm path."""
    binary = PROVIDER_BINARIES.get(provider, "")
    if not binary:
        return None
    env = os.environ if environ is None else environ
    found = shutil.which(binary) if environ is None else shutil.which(binary, path=env.get('PATH'))
    if found:
        return found
    if provider != 'go':
        return None
    managed_root = Path(env.get('AGENT_HUB_MANAGED_DIR') or (Path.home() / '.config' / 'agent-hub' / 'managed-engines'))
    exe_name = binary + ('.exe' if os.name == 'nt' else '')
    if managed_root.is_dir():
        for match in sorted(managed_root.glob('shared-go-*/engine/node_modules/@opencode/cli-*/bin/' + exe_name), reverse=True):
            if _is_executable_file(match):
                return str(match)
    if os.name != 'nt':
        nvm_root = Path.home() / '.nvm' / 'versions' / 'node'
        if nvm_root.is_dir():
            for match in sorted(nvm_root.glob('*/bin/' + binary), reverse=True):
                if _is_executable_file(match):
                    return str(match)
        for standard in (Path.home() / '.local' / 'bin' / binary, Path('/usr/local/bin') / binary):
            if _is_executable_file(standard):
                return str(standard)
    return None


def windows_codex_command(argv):
    """Execute the installed npm entry with Node, without shell expansion."""
    if sys.platform != 'win32' or ntpath.basename(argv[0]).lower() not in {'codex', 'codex.cmd', 'codex.ps1', 'codex.exe'}:
        return argv
    program = argv[0] if ntpath.isabs(argv[0]) else shutil.which(argv[0])
    if not program:
        raise RuntimeError('native_codex_program_unavailable')
    if Path(program).suffix.lower() == '.exe':
        return [program, *argv[1:]]
    script = Path(program).parent / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
    node = shutil.which('node')
    if not script.is_file() or not node or not ntpath.isabs(node):
        raise RuntimeError('native_codex_program_unavailable')
    return [node, str(script), *argv[1:]]


def codex_launch_wrapper(environ=None, home=None):
    """Verified maintained Codex quota wrapper (resolved Linux path) or ValueError.

    Mirrors the host CLI's resolution: operator CODEX_LAUNCH_WRAPPER of this
    daemon, else the fixed maintained script under the passwd home. Nothing from
    an offer, bundle or worker control is consulted; a configured value never
    falls back to the default. The wrapper alone runs the fresh quota gate."""
    environ = os.environ if environ is None else environ
    if os.name == 'nt' or sys.platform != 'linux':
        raise ValueError('codex_launch_wrapper_unavailable')
    configured = environ.get('CODEX_LAUNCH_WRAPPER')
    try:
        if configured is not None:
            # Present-but-empty is an operator decision to disable Codex, not unset.
            candidate = configured
        else:
            if home is None:
                import pwd
                home = pwd.getpwuid(os.geteuid()).pw_dir
            candidate = str(Path(home).joinpath(*CODEX_WRAPPER_DEFAULT))
        if not isinstance(candidate, str) or '\0' in candidate or not os.path.isabs(candidate):
            raise ValueError
        real = os.path.realpath(candidate, strict=True)
        owners = {0, os.geteuid()}
        for item in [real] + [str(parent) for parent in Path(real).parents]:
            info = os.lstat(item)
            if info.st_uid not in owners:
                raise ValueError
            if info.st_mode & 0o002 and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX):
                raise ValueError
        info = os.lstat(real)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or not info.st_mode & 0o100
                or info.st_mode & 0o020 and info.st_gid != os.getegid() or not os.access(real, os.X_OK)):
            raise ValueError
        with open(real, 'rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError
        text = raw.decode('utf8')
        if not _valid_codex_wrapper_body(text):
            raise ValueError
        return Path(real)
    except (OSError, ValueError, UnicodeDecodeError, RuntimeError):
        raise ValueError('codex_launch_wrapper_unavailable') from None


def spawn_environment(central_home):
    """Environment for the aplexer spawn client and its inherited child env.

    The fixed credential list is always removed. With a central private home,
    every provider-prefixed variable is removed too so the SDK can only see
    the materialized home; the home's own values reach the child exclusively
    through explicit --env entries, which override nothing here."""
    env = os.environ.copy()
    for name in INHERITED_SECRET_VARS:
        env.pop(name, None)
    if central_home:
        env = clean_provider_environment(env)
    return env


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def same_workspace(left, right):
    """Compare workspace identity across equivalent OS path spellings: Windows
    can report a temporary directory through its long path and its DOS 8.3
    alias, and both must resolve to the same existing directory for the
    native identity fence."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        left_path, right_path = Path(left), Path(right)
        return left_path.is_absolute() and right_path.is_absolute() and left_path.samefile(right_path)
    except (OSError, RuntimeError, ValueError):
        return False


def native_command_model(command):
    if not isinstance(command, list) or any(not isinstance(x, str) for x in command):
        return None
    models = [command[i + 1] for i, flag in enumerate(command[:-1]) if flag in ("--model", "-m")]
    models += [x.split("=", 1)[1] for x in command if x.startswith("--model=")]
    return models[0] if models and len(set(models)) == 1 else None


def command_effort(command):
    if not isinstance(command, list) or not all(isinstance(v, str) for v in command):
        return None
    values = [command[i + 1] for i, v in enumerate(command[:-1]) if v == '--effort']
    for arg in command:
        if arg.startswith('model_reasoning_effort='):
            raw_value = arg.split('=', 1)[1]
            try:
                values.append(json.loads(raw_value))
            except ValueError:
                # Aplexer versions may normalize the JSON-quoted config value
                # into a bare enum in the returned native command.
                if raw_value not in {'low', 'medium', 'high', 'xhigh'}:
                    return None
                values.append(raw_value)
    return values[0] if values and all(isinstance(v, str) and v == values[0] for v in values) else None


def aplexer_prompt(value, *, windows=None):
    """Keep command prompts safe for Windows batch-file engine wrappers.

    cmd.exe cannot safely forward an argument containing line breaks to a
    batch file. Flatten prompt line breaks on Windows while preserving the
    original formatting on other platforms.
    """
    if not isinstance(value, str):
        raise ValueError('native_prompt_invalid')
    if windows is None:
        windows = os.name == 'nt'
    return re.sub(r'(?:\r\n|\r|\n)+', ' ', value) if windows else value


def objective_hash(value):
    if not isinstance(value, str) or len(value.encode()) > 512 * 1024:
        raise ValueError('native_goal_objective_invalid')
    return hashlib.sha256(value.replace('\r\n', '\n').replace('\r', '\n').strip().encode()).hexdigest()


def adapter_argv(provider, goal, model):
    """Engine argv per approved Hub2 route; the exact admitted model is mandatory."""
    if model and 'glm' in str(model).lower() and provider != 'zai':
        raise ValueError('glm_model_route_refused')
    if provider == 'go':
        if model is not None and str(model) != OPENCODE_MODEL:
            raise ValueError('unsupported_opencode_model')
        model = OPENCODE_MODEL
    binary = PROVIDER_BINARIES.get(provider)
    if not binary:
        raise ValueError('provider_route_unsupported')
    if provider == 'zai':
        return [binary, '--dangerously-bypass-approvals-and-sandbox', 'exec', '--model', str(model),
                *ZAI_HARNESS_FLAGS, '-c', 'check_for_update_on_startup=false', '--json', goal]
    if provider == 'codex':
        return [binary, 'exec', '--model', str(model), '--dangerously-bypass-approvals-and-sandbox', '--json', goal]
    if provider == 'antigravity':
        return [binary, '--model', str(model), '--dangerously-skip-permissions', '-p', goal]
    if provider == 'go':
        return [binary, 'run', '--standalone', '--auto', '--format', 'json', '--model', str(model), goal]
    return [binary, '--dangerously-skip-permissions', '--model', str(model), '-p', goal]


def _headless_insert_flags(provider, argv, flags):
    # Agy consumes the argument immediately after -p as its prompt. Options
    # belong before that pair. Profile callers use a single prompt placeholder.
    index = -2 if provider == 'antigravity' and len(argv) >= 2 and argv[-2] == '-p' else -1
    return [*argv[:index], *flags, *argv[index:]]


# agy: `--effort` is rejected for the Neon orchestrator (verified agy 1.5.14 on hetzner).
ANTIGRAVITY_NO_EFFORT_MODELS = frozenset({'gemini-pro-orchestrator-neon'})


def headless_effort_argv(provider, argv, effort):
    if provider in {'codex', 'zai'}:
        value = 'xhigh' if effort == 'max' else effort
        if value not in {'low', 'medium', 'high', 'xhigh'}:
            raise ValueError('headless_effort_unsupported')
        flags = ['-c', 'model_reasoning_effort=' + json.dumps(value)]
    elif provider == 'antigravity' and effort in {'low', 'medium', 'high', 'max'}:
        model = next((argv[i + 1] for i, v in enumerate(argv[:-1]) if v == '--model'), '')
        if model in ANTIGRAVITY_NO_EFFORT_MODELS:
            return list(argv)  # agy rejects --effort for these models; their policy effort is nominal
        flags = ['--effort', 'xhigh' if effort == 'max' else effort]
    elif provider == 'claude' and effort in {'low', 'medium', 'high', 'max'}:
        flags = ['--effort', effort]
    else:
        raise ValueError('headless_effort_unsupported')
    return _headless_insert_flags(provider, argv, flags)


def interactive_argv(provider, argv, prompt, *, model, effort=None, with_goal=False):
    if not isinstance(model, str) or not model.strip():
        raise ValueError('interactive_explicit_model_required')
    if provider not in {'codex', 'zai', 'claude', 'go', 'antigravity'}:
        raise ValueError('interactive_provider_unsupported')
    if with_goal and provider not in {'codex', 'zai', 'claude'}:
        raise ValueError('native_goal_provider_unsupported')
    if not argv or argv[-1] != prompt:
        raise ValueError('interactive_adapter_shape_unverified')
    result = list(argv[:-1])
    if provider in {'codex', 'zai'}:
        if 'exec' not in result or '--json' not in result:
            raise ValueError('interactive_adapter_shape_unverified')
        result.remove('exec')
        result.remove('--json')
        if effort is not None:
            mapped = 'xhigh' if effort == 'max' else effort
            if mapped not in {'low', 'medium', 'high', 'xhigh'}:
                raise ValueError('interactive_effort_unsupported')
            result += ['-c', 'model_reasoning_effort=' + json.dumps(mapped)]
        if not with_goal:
            result.append(prompt)
    elif provider in {'claude', 'antigravity'}:
        if '-p' not in result:
            raise ValueError('interactive_adapter_shape_unverified')
        result.remove('-p')
        if effort is not None:
            if effort not in {'low', 'medium', 'high', 'max'}:
                raise ValueError('interactive_effort_unsupported')
            if provider == 'antigravity' and model in ANTIGRAVITY_NO_EFFORT_MODELS:
                pass
            else:
                result += ['--effort', 'xhigh' if provider == 'antigravity' and effort == 'max' else effort]
        if not with_goal:
            result += ['--prompt-interactive', prompt] if provider == 'antigravity' else [prompt]
    else:
        if effort is not None:
            raise ValueError('interactive_effort_unsupported')
        if 'run' not in result or '--auto' not in result:
            raise ValueError('interactive_adapter_shape_unverified')
        # Mini is a terminal UI, not run's JSON event stream. Its native
        # parser rejects --format; validate and remove only our inherited pair.
        if result.count('--format') != 1:
            raise ValueError('interactive_adapter_shape_unverified')
        output = result.index('--format')
        if output + 1 >= len(result) or result[output + 1] != 'json':
            raise ValueError('interactive_adapter_shape_unverified')
        del result[output:output + 2]
        result[result.index('run')] = 'mini'
        result.remove('--auto')
        result += ['--prompt', prompt]
    observed = [result[i + 1] for i, arg in enumerate(result[:-1]) if arg in {'--model', '-m'}]
    if not observed or set(observed) != {model}:
        raise ValueError('interactive_model_unverified')
    return result


def antigravity_launch_workspace(workspace):
    """The directory an interactive agy pane actually runs in.

    aplexer starts the child with --cwd set to the launch workspace and the
    client PWD is already the resolved spelling, so the kernel/Go working
    directory is the resolved existing directory. Only that exact path is
    eligible for session trust; parents, siblings and requested aliases are
    never added."""
    if not isinstance(workspace, str) or '\0' in workspace or not Path(workspace).is_absolute():
        raise ValueError('antigravity_workspace_trust_unverified')
    try:
        resolved = Path(workspace).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError('antigravity_workspace_trust_unverified') from None
    if not resolved.is_dir() or not same_workspace(str(resolved), workspace):
        raise ValueError('antigravity_workspace_trust_unverified')
    return str(resolved)


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('antigravity_settings_unverified')
        result[key] = value
    return result


def _strict_json_constant(constant):
    raise ValueError('antigravity_settings_unverified')


def _strict_json_float(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError('antigravity_settings_unverified')
    return value


def antigravity_trust_argv(argv, workspace):
    """Session-only agy --settings granting trust to exactly one workspace.

    agy's --settings accepts an inline JSON object whose trustedWorkspaces is a
    string array. Nothing is written to agy's global settings or trust store.
    An existing inline JSON object is merged rather than shadowed by a second
    flag; any other --settings form (file path, repeated flag, conflicting
    trust list, duplicate keys or non-finite JSON values) is refused instead of
    guessed."""
    trusted = [antigravity_launch_workspace(workspace)]
    if len(argv) >= 2 and argv[-2] == '--prompt-interactive':
        options, prompt_suffix = list(argv[:-2]), list(argv[-2:])
    else:
        options, prompt_suffix = list(argv), []
    found = [i for i, arg in enumerate(options) if arg == '--settings' or arg.startswith('--settings=')]
    if len(found) > 1:
        raise ValueError('antigravity_settings_unverified')
    if found:
        index = found[0]
        if options[index] == '--settings':
            if index + 1 >= len(options):
                raise ValueError('antigravity_settings_unverified')
            raw, span = options[index + 1], 2
        else:
            raw, span = options[index].split('=', 1)[1], 1
        try:
            settings = json.loads(
                raw,
                object_pairs_hook=_strict_json_object,
                parse_constant=_strict_json_constant,
                parse_float=_strict_json_float,
            )
        except ValueError:
            settings = None
        if not isinstance(settings, dict) or settings.get('trustedWorkspaces', trusted) != trusted:
            raise ValueError('antigravity_settings_unverified')
        settings['trustedWorkspaces'] = trusted
        options[index:index + span] = ['--settings', json.dumps(settings, separators=(',', ':'), allow_nan=False)]
        return [*options, *prompt_suffix]
    settings = json.dumps({'trustedWorkspaces': trusted}, separators=(',', ':'), allow_nan=False)
    # Options stay before the interactive prompt pair agy consumes last.
    return [*options, '--settings', settings, *prompt_suffix]


_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


def detect_antigravity_first_run_block(screen_text):
    """Return True when captured Antigravity CLI screen text shows a first-run,
    onboarding, or Terms of Service prompt that blocks execution indefinitely."""
    if not isinstance(screen_text, str) or not screen_text.strip():
        return False
    plain = _ANSI_ESCAPE_RE.sub(' ', screen_text[-16384:])
    normalized = re.sub(r'\s+', ' ', plain).strip().lower()
    if not normalized:
        return False
    if 'welcome to antigravity cli' in normalized and any(
        cue in normalized for cue in (
            'choose your color scheme', 'terms of service', 'terms of use',
            'security notice', 'data use', 'onboarding',
        )
    ):
        return True
    if any(cue in normalized for cue in ('terms of service', 'terms of use', 'security notice & data use')) and any(
        prompt in normalized for prompt in (
            '[next]', '[done]', '[previous]', 'navigate', 'enter confirm', 'press enter', 'accept', 'agree', 'decline',
        )
    ):
        return True
    if 'you are currently not signed in' in normalized and any(
        cue in normalized for cue in ('authentication required', 'please visit the url to log in', 'paste the authorization code')
    ):
        return True
    return False


_CLAUDE_TRUST_YES = 'yes, i trust this folder'
_CLAUDE_TRUST_NO = 'no, exit'
# Claude's main UI after the dialog: footer hints or the busy indicator.
CLAUDE_READY_MARKERS = ('? for shortcuts', 'esc to interrupt', 'bypass permissions on', 'shift+tab to cycle')


def claude_workspace_trust_prompt(screen_text, workspace):
    """Classify a captured screen against Claude Code's folder-trust dialog.

    None: no trust dialog on screen. 'yes'/'no': the dialog is for exactly
    `workspace` (whitespace-insensitive equality of the path block between
    "Accessing workspace:" and the safety text) and the cursor is on that
    option. 'unverified': dialog markers are present but the path or cursor
    could not be verified; callers must refuse rather than guess.
    Live DESKTOP-FP4OP26 capture (intake 2699b8d7): "❯ No, exit" is listed
    first and preselected for the home directory, then "Yes, I trust this folder".
    """
    if not isinstance(screen_text, str):
        return None
    plain = _ANSI_ESCAPE_RE.sub(' ', screen_text[-16384:])
    collapsed = re.sub(r'\s+', ' ', plain.lower())
    if _CLAUDE_TRUST_YES not in collapsed and 'accessing workspace' not in collapsed:
        return None
    if not isinstance(workspace, str) or not workspace or _CLAUDE_TRUST_YES not in collapsed \
            or _CLAUDE_TRUST_NO not in collapsed:
        return 'unverified'
    block = re.search(r'accessing\s+workspace:(.*?)(?:quick\s+safety\s+check|$)', plain, re.IGNORECASE | re.DOTALL)
    if not block or re.sub(r'\s+', '', block.group(1)) != re.sub(r'\s+', '', workspace):
        return 'unverified'
    selected = []
    for line in plain.splitlines():
        if '❯' not in line:
            continue
        item = re.sub(r'^[0-9]+\.\s*', '', re.sub(r'\s+', ' ', line.replace('❯', ' ')).strip().lower())
        if item.startswith(_CLAUDE_TRUST_YES):
            selected.append('yes')
        elif item.startswith(_CLAUDE_TRUST_NO):
            selected.append('no')
    return selected[0] if len(selected) == 1 else 'unverified'


def claude_ready_screen(screen_text):
    if not isinstance(screen_text, str):
        return False
    lowered = re.sub(r'\s+', ' ', _ANSI_ESCAPE_RE.sub(' ', screen_text[-16384:]).lower())
    return any(marker in lowered for marker in CLAUDE_READY_MARKERS)


def goal_prompt(prompt, workspace, task_id, first_action_path):
    if '\r' not in prompt and '\n' not in prompt and len(('/goal ' + prompt).encode('utf-16-le')) // 2 <= 4000:
        return prompt
    root = Path(workspace).resolve()
    if any(character in str(value) for value in (root, first_action_path) for character in '\r\n'):
        raise ValueError('goal_brief_pointer_not_single_line')
    directory = root / '.local' / 'goal-briefs'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(root):
        raise ValueError('goal_brief_path_untrusted')
    path = directory / (hashlib.sha256(task_id.encode()).hexdigest() + '.md')
    data = prompt.encode()
    if len(data) > 512 * 1024:
        raise ValueError('goal_brief_too_large')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    digest = hashlib.sha256(data).hexdigest()
    result = f'First tool action: run aplexer whoami --json and write its complete JSON to {first_action_path}. Then read and verify SHA256 {digest} of {path}, and fulfill that complete objective.'
    if len(('/goal ' + result).encode('utf-16-le')) // 2 > 4000:
        raise ValueError('goal_brief_pointer_too_large')
    return result


def validate_first_action(fa_path, start_json, min_mtime):
    """Artifact identity check against the launch start record; clone-proof.

    A genuine rich native whoami taken after start must show live mutation:
    active phase and live fields (updated_at_ms/last_activity_ms) after the
    record's created_at_ms. The wrapper start JSON itself — byte-identical,
    reformatted, or augmented — is rejected as a laundered clone.
    """
    try:
        if not os.path.exists(fa_path):
            return False
        if os.path.getmtime(fa_path) < min_mtime - 5:
            return False
        with open(fa_path, 'rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            return False
        data = json.loads(raw)
        if not isinstance(data, dict) or not REQUIRED_WHOAMI_FIELDS.issubset(data):
            return False
        if not isinstance(data.get("worker_pid"), int) or isinstance(data.get("worker_pid"), bool) or data["worker_pid"] <= 0:
            return False
        if data.get("phase") not in ("running", "working", "launching"):
            return False
        if data.get("id") != start_json.get("id") or data.get("tag") != start_json.get("tag"):
            return False
        if not same_workspace(data.get("workspace"), start_json.get("workspace")):
            return False
        if "parent_session" in start_json and data.get("parent_session") != start_json.get("parent_session"):
            return False
        created = start_json.get("created_at_ms")
        stamp = data.get("updated_at_ms", data.get("last_activity_ms"))
        if type(created) is not int or type(stamp) is not int or stamp <= created:
            return False
        if set(start_json).issubset(set(data)) and all(data.get(key) == value for key, value in start_json.items()):
            return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def native_state_directory(state_dir=None):
    """Use aplexer's platform default, preserving explicit operator overrides."""
    configured = state_dir or os.environ.get('APLEXER_STATE_DIR')
    if configured:
        return Path(configured).resolve()
    if sys.platform == 'win32':
        return Path(os.environ['LOCALAPPDATA']).joinpath('aplexer', 'state').resolve()
    return Path.home().joinpath('.local', 'state', 'aplexer').resolve()


def read_tombstone(native_id, state_dir=None):
    """Bounded explicit read of retired-sessions/<native_id>/tombstone.json.

    Optional retained output may enlarge the bounded 1 MiB envelope but is
    never returned as lifecycle evidence: it is stripped before the original
    64 KiB metadata cap applies. Symlinks, oversize envelopes/metadata and
    malformed JSON are rejected."""
    if not isinstance(native_id, str) or not UUID_RE.fullmatch(native_id.strip()):
        return None
    canonical_id = native_id.strip().lower()
    base = native_state_directory(state_dir)
    tombstone_file = base / "retired-sessions" / canonical_id / "tombstone.json"
    try:
        if tombstone_file.is_symlink() or not tombstone_file.is_file():
            return None
        if tombstone_file.stat().st_size > TOMBSTONE_ENVELOPE_BYTES:
            return None
        with open(tombstone_file, 'rb') as stream:
            raw = stream.read(TOMBSTONE_ENVELOPE_BYTES + 1)
        if len(raw) > TOMBSTONE_ENVELOPE_BYTES:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        data.pop("output", None)
        if len(json.dumps(data, ensure_ascii=True).encode("utf-8")) > TOMBSTONE_METADATA_BYTES:
            return None
        return data
    except (OSError, ValueError):
        return None


def retirement_private_json(path, limit):
    """Read owner-private retirement evidence without following any path link."""
    if os.name != 'posix':
        raise ValueError('retirement evidence unsupported')
    path = Path(os.path.abspath(path))
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            if os.fstat(directory).st_uid not in (0, os.geteuid()):
                raise ValueError('foreign evidence ancestor')
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise ValueError('nonprivate evidence boundary')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            meta = os.fstat(stream.fileno())
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
                    or meta.st_nlink != 1 or meta.st_mode & 0o077 or meta.st_size > limit):
                raise ValueError('unsafe retirement file')
            raw = stream.read(limit+1)
        if len(raw) > limit:
            raise ValueError('oversized retirement evidence')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate retirement field')
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError('invalid retirement evidence')
        return value
    finally:
        os.close(directory)


def verify_tombstone(tombstone, expected_id, expected_tag, expected_workspace):
    if not isinstance(tombstone, dict):
        return False, 'tombstone is not a dictionary'
    if str(tombstone.get("id") or "").strip().lower() != expected_id.strip().lower():
        return False, 'UUID mismatch'
    if str(tombstone.get("tag") or "").strip() != str(expected_tag or "").strip():
        return False, 'tag mismatch'
    workspace = tombstone.get("workspace")
    deleted_workspace = isinstance(expected_workspace, str) and workspace == expected_workspace + " (deleted)"
    if deleted_workspace:
        # Aplexer can record the kernel's deleted-directory spelling even
        # after that path has been recreated. Only a qualified retirement
        # for the journal's exact UUID and tag can use this spelling.
        exit_info = tombstone.get("exit")
        exited_at = exit_info.get("exited_at_ms") if isinstance(exit_info, dict) else None
        if not (
            isinstance(expected_workspace, str) and Path(expected_workspace).is_absolute()
            and isinstance(tombstone.get("id"), str) and UUID_RE.fullmatch(tombstone["id"])
            and UUID_RE.fullmatch(expected_id)
            and tombstone.get("tag") == expected_tag
            and tombstone.get("cause") == "finished"
            and tombstone.get("containment_empty") is True
            and native_returncode(exit_info) is not None
            and type(exited_at) is int and exited_at > 0
        ):
            return False, 'unqualified deleted-workspace retirement'
    elif not same_workspace(workspace, expected_workspace):
        return False, 'workspace mismatch'
    return True, None


def goal_store(row, *, proc_root=None):
    """Read one exact native rollout's goal row; never newest-by-workspace."""
    candidates = set()
    history = row.get('history_path')
    if isinstance(history, str) and Path(history).is_absolute() and Path(history).parent.name == row['id']:
        try:
            with (Path(history).parent / 'transcript.json').open('rb') as stream:
                bound = json.loads(stream.read(65537))
            path = Path(bound['path'])
            if path.is_absolute():
                candidates.add(path)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    pid = row.get('workload_pid')
    if type(pid) is int and pid > 0:
        try:
            import itertools
            for fd in itertools.islice((Path(proc_root or '/proc') / str(pid) / 'fd').iterdir(), 128):
                try:
                    path = fd.resolve(strict=True)
                    if path.name.startswith('rollout-') and path.suffix == '.jsonl':
                        candidates.add(path)
                except OSError:
                    pass
        except OSError:
            pass
    matches = []
    for path in candidates:
        try:
            with path.open('rb') as stream:
                meta = json.loads(stream.readline(65537))
            payload = meta['payload']
            from datetime import datetime
            timestamp = datetime.fromisoformat(payload['timestamp'].replace('Z', '+00:00')).timestamp() * 1000
            if (meta.get('type') == 'session_meta' and payload.get('cwd') == row['workspace']
                    and abs(timestamp - row['created_at_ms']) <= 10000 and isinstance(payload.get('id'), str)
                    and len(path.parents) > 4 and path.parents[3].name == 'sessions'):
                matches.append((path, payload['id']))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if len(matches) != 1:
        return None
    path, thread = matches[0]
    import sqlite3
    stores = [(int(p.stem[6:]), p) for p in path.parents[4].glob('goals_*.sqlite') if p.stem[6:].isdigit()]
    if not stores:
        return None
    try:
        from contextlib import closing
        with closing(sqlite3.connect(max(stores)[1].as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
            db_deadline = time.monotonic() + .05
            db.set_progress_handler(lambda: int(time.monotonic() > db_deadline), 1000)
            found = db.execute('SELECT status,objective FROM thread_goals WHERE thread_id=? AND length(CAST(objective AS BLOB))<=?',
                               (thread, 512 * 1024)).fetchone()
        if found and found[0] in {'active', 'complete', 'paused', 'blocked', 'stalled', 'usage_limited', 'budget_limited'}:
            return {'status': found[0], 'thread_id': thread, 'objective_hash': objective_hash(found[1])}
    except sqlite3.Error:
        pass
    return None


class NativeLauncher:
    """Same role the Launcher adapter played, without the external runtime."""

    def __init__(self, configuration, runner=subprocess.run, aplexer="a", journal=None):
        self.config = {**configuration}
        if not self.config.get("private_provider_homes", False):
            # Local enrollment means the user's native login for Codex on every
            # platform; the legacy managed profile has a separate, stale home.
            defaults = {**LOCAL_ROUTES}
            self.config["routes"] = {**defaults, **{
                engine: {**defaults.get(engine, defaults.get(route.get("provider"), {})), **route}
                for engine, route in configuration.get("routes", {}).items()
            }}
        else:
            self.config["routes"] = ({**configuration["routes"]} if "routes" in configuration else
                                     {engine: dict(route) for engine, route in CENTRAL_ROUTES.items()})
        self.config.setdefault("routes", {})["shell"] = {"provider": "shell"}
        self.runner, self.journal = runner, journal
        self.native_binary = self.config.get("aplexer") or aplexer
        self.state_dir = self.config.get("aplexer_state_dir")

    def native(self, *args, input=None, allow_failure=False, timeout=30, workspace=None, env_overrides=None, output_limit=262144):
        env = os.environ.copy()
        for key in ('HUB2_TOKEN', 'HUB2_SERVER', 'HUB2_CONFIG', 'HUB2_LAUNCH_CONTEXT'):
            env.pop(key, None)
        if env_overrides:
            env.update(env_overrides)
        result = self.runner([self.native_binary, *args], input=input, text=True, encoding='utf-8', errors='replace', capture_output=True,
                             timeout=timeout, check=False, env=env, cwd=workspace)
        if result.returncode and not allow_failure:
            raise RuntimeError("native_command_failed")
        # Headless Windows sessions can yield None instead of captured text.
        if result.stdout is None:
            result.stdout = ""
        if result.stderr is None:
            result.stderr = ""
        if len(result.stdout) > output_limit:
            raise RuntimeError("native_response_too_large")
        return result

    def accept_claude_workspace_trust(self, native_id, workspace, *, wait_seconds=30.0, step_seconds=5.0,
                                      sleep=time.sleep, clock=time.monotonic):
        """Answer Claude's folder-trust dialog for the exact launched workspace.

        Hub placed this pane in an enrolled workspace, so trusting it is the
        operator's decision already. Unlike agy's --settings, Claude persists
        "Yes" for non-home folders in its project config (home trust is never
        persisted), which also enables that project's .claude hooks/MCP.

        One key at a time, each verified on screen before the next: Down moves
        "No, exit" to Yes only after the cursor is re-read as 'no'; Enter is sent
        only on a verified 'yes'. Success needs a positive Claude ready screen.
        A dialog that is unverified, does not react, or leaves no ready pane
        raises claude_workspace_trust_unverified (fail loudly). With neither a
        dialog nor a ready screen by the deadline the launch proceeds, logged.
        """
        import logging
        log = logging.getLogger(__name__)

        def screen():
            try:
                result = self.native('capture', native_id, '--screen', '--plain', allow_failure=True, timeout=5)
            except subprocess.TimeoutExpired:
                raise RuntimeError('claude_workspace_trust_unverified') from None
            return result.stdout if result.returncode == 0 else ''

        def key(hex_bytes):
            # `--hex` already sends the decoded bytes unframed; aplexer rejects
            # `--raw` with `--hex` (exit 2), which refused the first live start.
            try:
                result = self.native('send', native_id, '--hex', hex_bytes, allow_failure=True, timeout=5)
            except subprocess.TimeoutExpired:
                raise RuntimeError('claude_workspace_trust_unverified') from None
            if result.returncode != 0:
                raise RuntimeError('claude_workspace_trust_unverified')

        def wait_for(predicate, limit):
            while True:
                text = screen()
                if predicate(text) or clock() >= limit:
                    return text
                sleep(0.5)

        deadline = clock() + wait_seconds
        # 'unverified' frames (Claude still painting, torn redraw) keep polling;
        # only a state that is still unverified at the deadline refuses.
        text = wait_for(lambda t: claude_workspace_trust_prompt(t, workspace) in ('yes', 'no')
                        or (claude_ready_screen(t) and claude_workspace_trust_prompt(t, workspace) is None), deadline)
        state = claude_workspace_trust_prompt(text, workspace)
        if state is None:
            if not claude_ready_screen(text):
                log.warning('CLAUDE TRUST CHECK no dialog or ready screen within %ss native=%s', wait_seconds, native_id)
            return False
        if state == 'no':
            key('1b5b42')
            text = wait_for(lambda t: claude_workspace_trust_prompt(t, workspace) in ('yes', None), clock() + step_seconds)
            state = claude_workspace_trust_prompt(text, workspace)
        if state != 'yes':
            raise RuntimeError('claude_workspace_trust_unverified')
        key('0d')
        text = wait_for(lambda t: claude_workspace_trust_prompt(t, workspace) is None and claude_ready_screen(t),
                        max(deadline, clock() + step_seconds))
        if claude_workspace_trust_prompt(text, workspace) is not None or not claude_ready_screen(text):
            raise RuntimeError('claude_workspace_trust_unverified')
        log.warning('CLAUDE WORKSPACE TRUST ACCEPTED native=%s', native_id)
        return True

    def inventory(self):
        from .native_inventory import collect_native_inventory, CENSUS_TIMEOUT_SECONDS, MAX_CENSUS_BYTES
        return collect_native_inventory(lambda: self.native('list', '--all', '--json',
            timeout=CENSUS_TIMEOUT_SECONDS, output_limit=MAX_CENSUS_BYTES))

    def raw_census_response(self):
        """Bounded raw `list --all` stdout for the admission module's private parser.

        The public completeness parser and publication schema stay untouched:
        resource admission needs the workload process coordinate that the public
        projection deliberately strips, and parses it privately and strictly."""
        from .native_inventory import CENSUS_TIMEOUT_SECONDS, MAX_CENSUS_BYTES
        return self.native('list', '--all', '--json',
                           timeout=CENSUS_TIMEOUT_SECONDS, output_limit=MAX_CENSUS_BYTES).stdout

    def status(self, selector, workspace=None, *, timeout=30):
        result = self.native("status", selector, "--json", allow_failure=True, workspace=workspace, timeout=timeout)
        if result.returncode:
            return None
        try:
            row = json.loads(result.stdout)
        except ValueError:
            return None
        return row if isinstance(row, dict) else None

    def strict_native_send(self, command, row):
        submission = native_submission(command)
        expected, native_id = command['identity'], command['native_id']
        if (row.get('workspace') != expected['workspace'] or type(row.get('created_at_ms')) is not int
                or row.get('created_at_ms') != submission['created_at_ms']
                or row.get('phase', row.get('state')) != 'running'):
            raise RuntimeError('native_target_identity_mismatch')
        # A newly collected complete census, not a cached heartbeat, requalifies
        # the exact incarnation. Strict native RPC repeats its own worker fence.
        observation = self.inventory()
        if not observation.fresh():
            raise RuntimeError('native_census_stale')
        matches = [item for item in observation.rows if item.get('id') == native_id]
        if (len(matches) != 1 or any(matches[0].get(key) != value for key, value in expected.items())
                or type(matches[0].get('created_at_ms')) is not int or matches[0].get('created_at_ms') != submission['created_at_ms']
                or matches[0].get('state') != 'running' or matches[0].get('phase') != 'running'
                or matches[0].get('worker_alive') is not True):
            raise RuntimeError('native_target_identity_mismatch')
        if self.journal is None:
            raise RuntimeError('native_target_ownership_unavailable')
        if self.journal.db.execute('SELECT 1 FROM launches WHERE native_id=?', (native_id,)).fetchone():
            raise RuntimeError('native_target_managed')
        remaining = (command['deadline_ms'] - time.time()*1000)/1000
        if remaining < 12:
            raise RuntimeError('native_command_deadline')
        try:
            response = self.native('send', native_id, '--strict', '--enter', '--expect-tag', expected['tag'],
                '--expect-workspace', expected['workspace'], '--expect-created-at-ms', str(submission['created_at_ms']),
                '--idempotency-key', command['id'],
                '--deadline-ms', str(command['deadline_ms']), '--stdin', '--json', input=submission['text'],
                allow_failure=True, timeout=min(remaining, 13), output_limit=16384)
        except subprocess.TimeoutExpired:
            return {'error': 'native_send_uncertain', 'consumed': None}
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate_receipt_field')
                result[key] = value
            return result
        try:
            receipt = json.loads(response.stdout, object_pairs_hook=unique)
            if (not isinstance(receipt, dict) or type(receipt.get('version')) is not int or receipt['version'] != 1
                    or receipt.get('session_id') != native_id or receipt.get('operation_id') != command['id']
                    or receipt.get('request_sha256') != submission['request_sha256']
                    or type(receipt.get('deadline_ms')) is not int or receipt['deadline_ms'] != command['deadline_ms']
                    or 'consumed' not in receipt or receipt['consumed'] is not None
                    or not isinstance(receipt.get('reason'), str) or not re.fullmatch('[a-z_]{1,80}', receipt['reason'])):
                raise ValueError('unverified_receipt')
            verified = (response.returncode == 0 and receipt.get('status') == 'submitted_verified'
                and all(receipt.get(key) is True for key in ('mutation', 'landed', 'submitted'))
                and receipt['reason'] == 'submission_observed')
            refused = (response.returncode == 4 and receipt.get('status') == 'refused'
                and all(receipt.get(key) is False for key in ('mutation', 'landed', 'submitted')))
            uncertain = (response.returncode == 4 and receipt.get('status') == 'uncertain'
                and all(receipt.get(key) is None or type(receipt[key]) is bool for key in ('mutation', 'landed', 'submitted')))
            if not (verified or refused or uncertain):
                raise ValueError('unverified_receipt')
            proof = {key: receipt.get(key) for key in ('version', 'session_id', 'operation_id', 'request_sha256',
                'deadline_ms', 'status', 'mutation', 'landed', 'submitted', 'consumed', 'reason')}
            if receipt.get('replayed') is True:
                proof['replayed'] = True
            return {'submission': proof}
        except (ValueError, TypeError):
            return {'error': 'native_send_receipt_unverified', 'consumed': None}

    def native_control(self, command):
        """Exact UUID only. Missing registry rows never prove native exit."""
        kind, native_id, expected = command['kind'], command['native_id'], command['identity']
        deadline = command['deadline_ms']
        def remaining():
            seconds = (deadline - time.time() * 1000) / 1000
            if seconds <= 0:
                raise RuntimeError('native_command_deadline')
            return min(seconds, 10)
        def proof(row, source):
            if (row and row.get('id') == native_id and row.get('tag') == expected['tag']
                    and row.get('phase', row.get('state')) == 'exited'
                    and row.get('containment_empty') is True and native_returncode(row.get('exit')) is not None
                    and type(row['exit'].get('exited_at_ms')) is int and row['exit']['exited_at_ms'] > 0
                    and row['exit']['exited_at_ms'] <= time.time()*1000+30000):
                return {'stopped': True, 'evidence': {'source': source, 'native_id': native_id,
                    'tag': expected['tag'], 'workspace': expected['workspace'], 'phase': 'exited',
                    'containment_empty': True, 'exit': {k: row['exit'][k] for k in ('code', 'signal', 'exited_at_ms') if k in row['exit']},
                    'observed_at_ms': round(time.time()*1000)}}
            return None
        remaining()
        if kind == 'stop':
            retired = self.retired(native_id, expected)
            if retired:
                return proof(retired, 'aplexer_retirement') or {'stopped': False, 'error': 'stop_unconfirmed'}
        # A missing/deleted workspace permits only positive tombstone proof.
        if not Path(expected['workspace']).is_dir():
            raise RuntimeError('native_target_workspace_unavailable')
        def status():
            result = self.native('status', native_id, '--json', allow_failure=True, timeout=remaining())
            if result.returncode:
                return None
            try:
                row = json.loads(result.stdout)
            except ValueError:
                return None
            if (not isinstance(row, dict) or row.get('id') != native_id or row.get('tag') != expected['tag']
                    or not same_workspace(row.get('workspace'), expected['workspace'])):
                raise RuntimeError('native_target_identity_mismatch')
            return row
        row = status()
        if row is None:
            raise RuntimeError('native_target_unavailable')
        if kind == 'stop':
            result = proof(row, 'aplexer_status')
            if result:
                return result
            self.native('kill', native_id, timeout=remaining())
            retired = self.retired(native_id, expected)
            if retired:
                return proof(retired, 'aplexer_retirement') or {'stopped': False, 'error': 'stop_unconfirmed'}
            result = proof(status(), 'aplexer_status')
            return result or {'stopped': False, 'error': 'stop_unconfirmed'}
        if kind == 'send':
            return self.strict_native_send(command, row)
        if kind != 'capture':
            raise RuntimeError('invalid_native_command')
        args = ('--screen', '--plain') if command.get('mode') == 'screen' else ('--bytes', '4096')
        raw = self.native('capture', native_id, *args, timeout=remaining()).stdout
        # Operator-only bounded output. Strip terminal controls, not just ANSI colour.
        raw = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', raw)
        clean = ''.join(c for c in raw if c in '\n\t' or ord(c) >= 32 and not 127 <= ord(c) <= 159)
        return {'output': clean.encode('utf8')[:4096].decode('utf8', errors='ignore')}

    def capabilities(self):
        result = {"shell": {"headless"}}
        for engine, route in self.config.get("routes", {}).items():
            provider = route.get("provider") if isinstance(route, dict) else None
            supported = set(PROVIDER_CAPABILITIES.get(provider, set()))
            binary_path = resolve_engine_binary(provider)
            available = bool(binary_path)
            if self.config.get('private_provider_homes') and provider == 'claude':
                available = provider_home.claude_install_available()
            if provider == 'codex':
                # Advertise Codex only when the maintained quota wrapper (and,
                # for a profile route, the maintained profile name) is usable.
                available = self._codex_advertisable(route)
                if not available and self._codex_withdrawn_reason == 'profile_release_mismatch':
                    supported.discard('headless')
                    available = bool(supported)
            elif provider == 'go' and not self.config.get('private_provider_homes', False) and route.get('profile') == 'hub-shared-go':
                if self._go_advertisable(route):
                    if not binary_path:
                        supported.discard('interactive')
                    available = bool(supported)
                else:
                    supported.discard('headless')
                    available = bool(supported) and bool(binary_path)
            if supported and available:
                result[engine] = set(supported)
        return result

    def _codex_advertisable(self, route):
        """Check shared wrapper and local headless profile readiness.

        launch() needs the wrapper and, for the local profile path, the
        release-pinned profile proof. Checking only the wrapper advertised codex
        on a host whose profile pointed at a stale release, so the picker kept
        routing headless work there (intake f88ae417). Profile failure removes
        only headless support; interactive work uses the direct wrapper path.
        Report a fixed diagnostic once per change of reason."""
        reason = None
        if os.name == 'nt':
            # Windows hosts have no maintained quota wrapper or release-pinned
            # profile: the installed Codex CLI with its own login is the route.
            return bool(shutil.which(PROVIDER_BINARIES['codex']))
        try:
            wrapper = str(codex_launch_wrapper())
        except ValueError:
            reason = 'wrapper'
        else:
            if route.get('profile') not in (None, CODEX_WRAPPER_PROFILE):
                reason = 'profile_name'
            elif (not self.config.get('private_provider_homes', False)
                  and route.get('profile') == CODEX_WRAPPER_PROFILE):
                now = time.monotonic()
                cached = getattr(self, '_codex_ready_cache', None)
                if cached is None or now - cached[0] > 30 or cached[1] != wrapper:
                    cached = (now, wrapper, bool(self.codex_exec_readiness(route, wrapper)))
                    self._codex_ready_cache = cached
                if not cached[2]:
                    reason = 'profile_release_mismatch'
        if getattr(self, '_codex_withdrawn_reason', None) != reason:
            self._codex_withdrawn_reason = reason
            if reason:
                affected = 'headless capability withdrawn; interactive remains available' if reason == 'profile_release_mismatch' else 'all codex capabilities withdrawn'
                print('ALERT hub2-host: codex_launch_wrapper_unavailable (%s): %s' % (reason, affected),
                      file=sys.stderr, flush=True)
            else:
                print('RECOVERED hub2-host: codex advertised again', file=sys.stderr, flush=True)
        return reason is None

    def go_profile_readiness(self, route):
        """True when the local hub-shared-go profile and its launcher python/archive exist."""
        if os.name == 'nt' or not isinstance(route, dict) or route.get('profile') != 'hub-shared-go':
            return True
        if self.runner is not subprocess.run and 'APLEXER_CONFIG' not in os.environ:
            return True
        config_path = Path(os.environ.get('APLEXER_CONFIG') or (Path.home() / '.config' / 'aplexer' / 'config.toml'))
        if not config_path.is_file():
            return 'APLEXER_CONFIG' not in os.environ
        try:
            import tomllib
            parsed = tomllib.loads(config_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        table = (parsed.get('profiles') or {}).get('hub-shared-go')
        command = table.get('command') if isinstance(table, dict) else None
        if not isinstance(command, list) or not command or not isinstance(command[0], str):
            return False
        entry = Path(command[0])
        if not _is_executable_file(entry):
            return False
        if entry.suffix == '.sh':
            import shlex
            try:
                lines = entry.read_text(encoding='utf-8').splitlines()
                parts = shlex.split(lines[1]) if len(lines) >= 2 else []
            except (OSError, ValueError):
                return False
            if len(parts) < 4 or parts[0] != 'exec' or not _is_executable_file(parts[1]):
                return False
            if parts[2] == '-c':
                for match in re.finditer(r"sys\.path\.insert\(0,\s*'([^']+)'\)", parts[3]):
                    if match.group(1).endswith('.pyz') and not Path(match.group(1)).is_file():
                        return False
        return True

    def _go_advertisable(self, route):
        now = time.monotonic()
        cached = getattr(self, '_go_ready_cache', None)
        if cached is None or now - cached[0] > 30:
            cached = (now, bool(self.go_profile_readiness(route)))
            self._go_ready_cache = cached
        ready = cached[1]
        if getattr(self, '_go_withdrawn', False) is not (not ready):
            self._go_withdrawn = not ready
            if not ready:
                print('ALERT hub2-host: engine_binary_unavailable (hub-shared-go): headless capability withdrawn',
                      file=sys.stderr, flush=True)
            else:
                print('RECOVERED hub2-host: go advertised again', file=sys.stderr, flush=True)
        return ready

    def windows_credential_artifacts(self):
        """A held Windows artifact owner, not consumer enrollment/readiness.

        Deliberately separate from the Linux managed/readiness contract. No
        normal delivery/ACK path opts Windows in during the artifact tranche.
        """
        if sys.platform != 'win32':
            return None
        from ._release import HOST_VERSION, PROTOCOL_VERSION
        from .windows_release import WindowsRelease
        return WindowsRelease.open(HOST_VERSION, PROTOCOL_VERSION)

    def _verified_release(self, release=None):
        """The exact installed release archive; no profile, credential or network probe.

        Returns (archive, python, sha256, read) or None. ``read`` re-reads a
        bounded owner-only file of the release without following links."""
        if sys.platform != 'linux':
            return None
        if release is None:
            from ._release import HOST_VERSION
            base = Path.home() / '.local/share/agent-hub2'
            release = base / 'releases' / HOST_VERSION
            if (base / 'current').resolve(strict=True) != release:
                return None
        release = Path(release)
        info = release.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            return None
        def read(path, limit):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                meta = os.fstat(stream.fileno())
                if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1
                        or meta.st_mode & 0o022 or meta.st_size > limit):
                    raise ValueError('managed_exec_unavailable')
                raw = stream.read(limit+1)
                if len(raw) > limit:
                    raise ValueError('managed_exec_unavailable')
                return raw
        receipt = json.loads(read(release / 'release.json', 65536))
        if receipt.get('host_version') != release.name or not re.fullmatch('[0-9a-f]{40}', release.name):
            return None
        record = receipt['artifacts']['hub.pyz']
        archive = release / 'hub.pyz'
        raw = read(archive, 16777216)
        digest = hashlib.sha256(raw).hexdigest()
        if type(record['size']) is not int or record['size'] != len(raw) or record['sha256'] != digest:
            return None
        python = release / 'bin/python'
        if not python.is_file():
            return None
        return archive, python, digest, read

    def verified_release_archive(self, release=None):
        """Checksum-verified installed release for the default-Claude consumer.

        Unlike managed readiness this needs no managed profile: a host that only
        opted its default Claude login in has none. Same trust boundary."""
        try:
            found = self._verified_release(release)
        except (OSError, ValueError, TypeError, KeyError, ImportError):
            return None
        if found is None:
            return None
        archive, _, digest, read = found
        try:
            if hashlib.sha256(read(archive, 16777216)).hexdigest() != digest:
                return None
        except (OSError, ValueError):
            return None
        return {'archive': str(archive), 'sha256': digest}

    def managed_claude_readiness(self, release=None):
        """Exact installed release and actual profile code; no credential probe."""
        return self._release_proof('managed_exec_readiness', {'protocol': 'hub2-managed-claude-exec-v1',
                                   'label': 'hub-claude', 'profile': 'hub-hub-claude'}, release, 'managed_exec_unavailable')

    def codex_exec_readiness(self, route, wrapper, release=None):
        """Maintained Codex profile executes the wrapper-aware pinned archive, or None."""
        if sys.platform != 'linux' or not isinstance(route, dict) or route.get('profile') != CODEX_WRAPPER_PROFILE:
            return None
        proof = self._release_proof('codex_exec_readiness', None, release, 'codex_launch_wrapper_unavailable',
                                    {'CODEX_LAUNCH_WRAPPER': wrapper})
        if not proof or proof.get('wrapper') != wrapper or proof.get('profile') != CODEX_WRAPPER_PROFILE:
            return None
        return proof

    def _release_proof(self, function, expected, release, error, extra_env=None):
        try:
            found = self._verified_release(release)
            if found is None:
                return None
            archive, python, digest, read = found
            # -I -S prevents user-site startup; import only the verified bundled
            # CLI, never this daemon's duplicate package or CLI main/network.
            code = ('import sys,json; sys.path.insert(0,sys.argv[1]); '
                    'from agent_hub_host.provisioning import ' + function + '; '
                    'print(json.dumps(' + function + '(sys.argv[1])))')
            result = self.runner([str(python), '-I', '-S', '-c', code, str(archive)],
                capture_output=True, text=True, check=False, timeout=3,
                env=dict(spawn_environment(True), **(extra_env or {})))
            if result.returncode or len(result.stdout) > 1024:
                return None
            proof = json.loads(result.stdout)
            if expected is None:
                if (not isinstance(proof, dict) or proof.get('protocol') != CODEX_WRAPPER_PROTOCOL
                        or proof.get('label') != 'shared-codex' or not isinstance(proof.get('wrapper'), str)):
                    return None
            elif proof != expected:
                return None
            # Repeat the archive check after the helper; immutable owner release
            # storage is the existing installer trust boundary.
            if hashlib.sha256(read(archive, 16777216)).hexdigest() != digest:
                return None
            return dict(proof, archive=str(archive), sha256=digest)
        except (OSError, ValueError, TypeError, KeyError, ImportError, subprocess.SubprocessError):
            return None

    def launch(self, task_id, specification):
        route = self.config.get("routes", {}).get(specification["engine"])
        if not isinstance(route, dict) or route.get("provider") not in PROVIDER_CAPABILITIES:
            raise RuntimeError("native_launch_route_missing")
        provider = route["provider"]
        codex_wrapper = None
        if provider == 'codex' and sys.platform != 'win32':
            # Resolve and verify before anything is written or spawned. The
            # operator-owned path is the only source; no raw SDK fallback.
            try:
                codex_wrapper = str(codex_launch_wrapper())
            except ValueError:
                raise RuntimeError('codex_launch_wrapper_unavailable') from None
        if specification.get('managed_credential_label') is not None:
            if (specification['managed_credential_label'] != 'hub-claude' or specification['engine'] != 'claude'
                    or provider != 'claude' or specification.get('model') != 'opus'
                    or specification.get('effort') != 'medium' or specification.get('interactive')
                    or specification.get('with_goal') or specification.get('account_id') != 'claude'
                    or not self.managed_claude_readiness()):
                raise RuntimeError('managed_exec_unavailable')
            route = {'provider': 'claude', 'profile': 'hub-hub-claude'}
        # Mode/effort/goal capability gating already happened in the Bridge's
        # validate_launch against the same capability table.
        workspace = Path(specification["workspace"])
        # One validated cap snapshot feeds both the argv below and the admission
        # transaction; malformed or oversized values refuse instead of clamping.
        caps = resource_admission.validate_worker_caps(self.config.get("memory_mb"), self.config.get("pids"))
        specification["admission_caps"] = caps
        goal = specification["prompt"]
        fa_path = workspace / ".local" / f"first-action-{task_id}.json"
        fa_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_file(fa_path.parent)
        if provider in FIRST_ACTION_PROVIDERS:
            goal += FIRST_ACTION_INSTRUCTION.format(path=fa_path)
        if provider != 'shell':
            goal = aplexer_prompt(goal)
        # Native-login Codex (no profile) always takes the direct adapter path:
        # on Linux the verified wrapper replaces the SDK executable below.
        native_codex = provider == 'codex' and not route.get('profile')
        local = provider == 'shell' or bool(specification.get('managed_credential_label')) or (not self.config.get("private_provider_homes", False) and not specification.get("interactive") and not native_codex)
        if local:
            if codex_wrapper and not self.codex_exec_readiness(route, codex_wrapper):
                raise RuntimeError('codex_launch_wrapper_unavailable')
            if provider == 'go' and not self.go_profile_readiness(route):
                raise RuntimeError('engine_binary_unavailable')
            child = self.profile_command(task_id, specification, route, goal)
        else:
            child = adapter_argv(provider, goal, specification["model"])
            if codex_wrapper:
                # Only the SDK executable changes; model, effort and prompt argv stay.
                child[0] = codex_wrapper
            elif self.runner is subprocess.run:
                resolved_bin = resolve_engine_binary(provider)
                if not resolved_bin:
                    raise RuntimeError('engine_binary_unavailable')
                child[0] = resolved_bin
            elif provider == 'go':
                resolved_bin = resolve_engine_binary(provider)
                if resolved_bin:
                    child[0] = resolved_bin
        objective = None
        if specification.get("interactive") and not local:
            child = interactive_argv(provider, child, goal, model=specification["model"],
                                     effort=specification.get("effort"), with_goal=specification.get("with_goal", False))
            if provider == 'antigravity':
                # The interactive pane otherwise stops at agy's workspace-trust
                # prompt. Trust only the exact directory this process runs in.
                child = antigravity_trust_argv(child, specification["workspace"])
            if specification.get("with_goal"):
                objective = goal_prompt(goal, specification["workspace"], task_id, str(fa_path))
                if provider == 'claude':
                    child = claude_goal.conversation_argv(child, task_id, specification.get('operation_id'), specification.get('epoch'))
        elif not local:
            if specification.get("effort") is not None:
                child = headless_effort_argv(provider, child, specification["effort"])
            if provider == 'claude' and not specification.get('managed_credential_label'):
                child = _headless_insert_flags(provider, child, CLAUDE_MANAGED_HEADLESS_FLAGS)
            child = _headless_insert_flags(provider, child, HEADLESS_OUTPUT_FLAGS.get(provider, ()))
        runtime_deadline = specification.get("runtime_deadline")
        if runtime_deadline is not None and (type(runtime_deadline) is not int or
                not time.time() * 1000 < runtime_deadline <= (time.time() + specification["timeout_seconds"]) * 1000 + 5000):
            raise RuntimeError("native_runtime_deadline_unverified")
        child_env = {"TMPDIR": str(workspace / ".local" / "tmp" / task_id),
                     "APLEXER_RETAIN_RETIRED_OUTPUT": "1"}
        if specification.get("provider_home"):
            # Recheck only the current session binding. Quota and credential
            # health choose/fallback routes; they do not block the start.
            data = session_binding(specification["provider_home"], specification["client"],
                                   specification["session_id"], specification["operation_id"],
                                   specification["epoch"])
            runtime_deadline = data["runtime_deadline"]
            if provider == 'claude':
                binary = data.get('engine_binary_path')
                if not isinstance(binary, str) or not Path(binary).is_absolute():
                    raise RuntimeError('native_managed_engine_unverified')
                child[0] = binary
            # Ambient credentials were already sanitized in spawn_environment
            # (client env) BEFORE the trusted home is added; the child dict is
            # built fresh and must keep the trusted CODEX_HOME/CLAUDE_CONFIG_DIR/
            # OPENCODE_DB/ZCODE_HOME paths from native_environment intact.
            child_env.update(native_environment(specification["provider_home"]))
            child_env["TMPDIR"] = str(workspace / ".local" / "tmp" / task_id)
            child_env["APLEXER_RETAIN_RETIRED_OUTPUT"] = "1"
        if codex_wrapper and local:
            # Central-home sanitization removes CODEX_*; carry only the verified
            # path so the gated profile execution resolves the same wrapper.
            child_env["CODEX_LAUNCH_WRAPPER"] = codex_wrapper
        if provider == "zai":
            # Fixed per-attempt metadata diagnostics only. Trusted child values
            # and a managed profile's existing explicit env retain precedence.
            child_env.setdefault("RUST_LOG", "error,opentelemetry_sdk=off,opentelemetry_otlp=off,codex_tool_lifecycle=info")
        if specification.get("child_config_file"):
            child_env["HUB2_CONFIG"] = specification["child_config_file"]
        claude_config_dir = child_env.get('CLAUDE_CONFIG_DIR')
        if objective is not None and provider == 'claude':
            # An explicit default directory changes Claude's global onboarding
            # path. Preserve native absence while pinning its transcript root.
            if 'CLAUDE_CONFIG_DIR' not in child_env and os.environ.get('CLAUDE_CONFIG_DIR'):
                child_env['CLAUDE_CONFIG_DIR'] = os.environ['CLAUDE_CONFIG_DIR']
            claude_config_dir = child_env.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))
            claude_goal.approve_external_includes(child_env.get('CLAUDE_CONFIG_DIR'), Path(specification['workspace']).resolve())
        tmpdir = Path(child_env["TMPDIR"])
        tmpdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if child:
            child = [resolve_windows_program(child[0]), *child[1:]]
            if provider == 'codex':
                child = windows_codex_command(child)
        argv = [self.native_binary, "start", "--json", "--memory", f"{caps['memory_mb']}M",
                "--pids", str(caps['pids']), "--startup-timeout-ms", "30000",
                "--workspace", specification["workspace"], "--cwd", specification["workspace"],
                "--tag", "task-" + task_id]
        if specification.get("interactive") and not local:
            # Literal argv does not infer native engine metadata. Every real
            # interactive TUI needs its family for input framing/readiness;
            # /goal registration remains conditional on objective below.
            native_engine = {"codex": "codex", "zai": "codex", "claude": "claude",
                             "go": "opencode", "antigravity": "antigravity"}[provider]
            argv += ["--engine", native_engine]
        if specification.get("session_id"):
            argv += ["--label", "hub.managed=true", "--label", "hub.session=" + specification["session_id"]]
        if specification.get('attempt_label'):
            argv += ['--label', 'hub.attempt=' + specification['attempt_label']]
        for key, value in child_env.items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--"] + child
        started = time.time()
        env = spawn_environment(bool(specification.get("provider_home") or specification.get("managed_credential_label")))
        if objective is not None and provider == 'claude' and 'CLAUDE_CONFIG_DIR' not in child_env:
            env.pop('CLAUDE_CONFIG_DIR', None)  # Empty inherited values stay absent too.
        env["PWD"] = str(Path(specification["workspace"]).resolve())
        # Private exact-attempt diagnostics are opt-in; no public launch field.
        from .zai_diagnostic_selector import prepare as prepare_zai_diagnostic
        diagnostic = prepare_zai_diagnostic(self, specification, route, child, child_env, env, local=local)
        if diagnostic is not None:
            argv, child = diagnostic.transport(argv, child)
        # `a start` is only the daemon handshake client: the spawned session lives
        # under the aplexer supervisor, so the runner timeout bounds the handshake
        # alone. A killed handshake with a created session stays launch-uncertain
        # and is reconciled by identity, never re-spawned.
        before_dispatch = specification.get('before_native_dispatch')
        if diagnostic is not None:
            diagnostic.before_dispatch()
        # The admission callback commits marker 1 with its reservation and
        # returns the short original-monotonic permit; the check below is
        # read-only and never renews the sample clock or resets the marker.
        permit = before_dispatch() if before_dispatch else None
        try:
            # The admission's one original deadline may already be consumed by
            # the committed admission or by verify_permit's own final state/
            # STOP journal reads, which re-check expiry immediately before the
            # runner. Time-only: the callback legitimately changed
            # dispatch_started to 1.
            if diagnostic is not None:
                diagnostic.check()
            resource_admission.verify_permit(permit, journal=self.journal)
            result = self.runner(argv, text=True, encoding='utf-8', errors='replace', capture_output=True,
                                 timeout=35, check=False,
                                 env=env, cwd=specification["workspace"])
        except subprocess.TimeoutExpired:
            raise RuntimeError("native_start_uncertain") from None
        except OSError:
            raise RuntimeError("native_start_refused") from None
        if result.returncode != 0 or len(result.stdout or '') > 262144:
            raise RuntimeError("native_start_refused")
        try:
            start_json = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("native_start_ambiguous") from None
        if not isinstance(start_json, dict) or not isinstance(start_json.get("id"), str) \
                or not UUID_RE.fullmatch(start_json["id"]):
            raise RuntimeError("native_start_identity_invalid")
        if start_json.get("tag") != "task-" + task_id or not same_workspace(start_json.get("workspace"), specification["workspace"]):
            raise RuntimeError("native_start_identity_invalid")
        identity_observed = specification.get('native_identity_observed')
        if identity_observed:
            identity_observed(start_json['id'])
        if not local and native_command_model(start_json.get("command")) != specification["model"]:
            raise RuntimeError("native_model_mismatch")
        if not local and specification.get("effort") is not None and command_effort(start_json.get("command")) != (
                'xhigh' if specification["effort"] == 'max' and provider in {'codex', 'zai'} else specification["effort"]):
            raise RuntimeError("native_effort_mismatch")
        if local and start_json.get("command") != child:
            raise RuntimeError("native_profile_command_mismatch")
        if specification.get("interactive") and start_json.get("command") != child:
            raise RuntimeError("native_interactive_command_mismatch")
        if specification.get("interactive") and provider == 'claude':
            # Before any goal or prompt delivery: Enter on the default "No, exit"
            # would end the pane (home workspace, intake 2699b8d7).
            self.accept_claude_workspace_trust(start_json["id"], str(specification["workspace"]))
        goal_registration = None
        if objective is not None:
            if start_json.get('engine') != ('claude' if provider == 'claude' else 'codex'):
                raise RuntimeError('native_goal_provider_unsupported')
            goal_registration = self.register_goal(start_json, specification, objective,
                                                   deadline=started + specification["timeout_seconds"],
                                                   claude_config_dir=claude_config_dir)
        # Pane creation is the start receipt. First-action proof is observed later;
        # a slow model must not hold the native id behind the server deadline.
        receipt = {"session_id": start_json["id"],
                   "first_action_verified": validate_first_action(str(fa_path), start_json, started)}
        receipt.update({"model": specification["model"], "model_verified": True,
                        "mode": "interactive" if specification.get("interactive") else "headless"})
        if specification.get("interactive"):
            receipt["interactive_verified"] = True
        if specification.get("effort") is not None:
            receipt["effort"] = specification["effort"]
            receipt["effort_verified"] = True
        if goal_registration:
            receipt["goal_registration"] = goal_registration
        self.record_evidence(task_id, specification, receipt, start_json)
        return receipt

    def profile_command(self, task_id, specification, route, prompt):
        """Use the same local profile primitive as model-roulette, without shell parsing."""
        provider = route["provider"]
        if provider == 'shell':
            # Explicit operator-only jobs execute command text without a model.
            # The aplexer containment and accepted runtime deadline still apply.
            if specification.get('interactive') or specification.get('with_goal') or specification.get('effort') is not None:
                raise RuntimeError('launcher_capability_unavailable')
            return ([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/s', '/c', prompt] if os.name == 'nt'
                    else ['/bin/sh', '-c', prompt])
        model = specification["model"]
        if model and 'glm' in model.lower() and provider != 'zai':
            raise ValueError('glm_model_route_refused')
        if provider == 'go' and model != OPENCODE_MODEL:
            raise ValueError('unsupported_opencode_model')
        directory = Path(specification["workspace"]) / '.local' / 'hub2-prompts'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(Path(specification['workspace']).resolve()):
            raise RuntimeError('prompt_path_untrusted')
        path = directory / (task_id + '.txt')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(prompt)
        profile = route.get('profile')
        from .task_output import prepare_output
        specification['_task_output'] = prepare_output(task_id, specification, profile, route.get('engine') if profile else route.get('engine', provider), prompt)
        output = specification['_task_output']
        output_args = ['--output-dir', str(Path(specification['workspace']).resolve().joinpath(*output['parts']))] if output else []
        selector = ['--profile', profile] if profile else ['--engine', route.get('engine', provider)]
        args = ['--model', model]
        if provider == 'zai':
            args += ZAI_HARNESS_FLAGS
        args += HEADLESS_OUTPUT_FLAGS.get(provider, ())
        if provider == 'claude' and not specification.get('interactive') and not specification.get('managed_credential_label'):
            args += CLAUDE_MANAGED_HEADLESS_FLAGS
        if provider == 'go':
            args += ['--format', 'json']
        if specification.get('managed_credential_label'):
            if self.journal is None:
                raise RuntimeError('managed_exec_unavailable')
            from .client import save_config
            ready = self.managed_claude_readiness()
            if not ready:
                raise RuntimeError('managed_exec_unavailable')
            context = {key: specification[key] for key in ('engine', 'model', 'effort', 'session_id',
                       'operation_id', 'epoch', 'workspace', 'runtime_deadline', 'request_digest', 'account_id', 'machine_id')}
            context.update(protocol='hub2-managed-claude-exec-v1', label='hub-claude',
                           profile='hub-hub-claude', task_id=task_id, server=specification['client'].base_url,
                           exec_archive_sha256=ready['sha256'])
            token = hashlib.sha256(canonical(context).encode()).hexdigest()
            context_path = self.journal.directory / 'managed-claude' / (token + '.json')
            save_config(context_path, context)
            specification['_managed_exec_context'] = str(context_path)
            args.append('--hub-managed-claude-context=' + str(context_path))
        effort = specification.get('effort')
        if effort is not None:
            args += headless_effort_argv(provider, ['--model', model, 'prompt'], effort)[2:-1]
        return [self.native_binary, 'task', 'run', *selector, '--cwd', specification['workspace'],
                '--prompt-file', str(path), '--timeout-secs', str(specification['timeout_seconds']),
                '--no-notify', *output_args, *['--engine-arg=' + arg for arg in args]]

    def wait_first_action(self, fa_path, start_json, started, specification, poll=None, sleep=None):
        poll = poll or (lambda selector: self.status(selector, workspace=specification["workspace"]))
        sleep = sleep or time.sleep
        deadline = started + specification["timeout_seconds"]
        while time.time() < deadline:
            if validate_first_action(fa_path, start_json, started):
                return {"session_id": start_json["id"], "first_action_verified": True}
            row = poll(start_json["id"])
            if row is not None and str(row.get("phase", row.get("state", ""))) == "dead":
                break
            sleep(min(2.0, max(0.0, deadline - time.time())))
        raise RuntimeError("native_first_action_unverified")

    def register_goal(self, start_json, specification, prompt, deadline, claude_config_dir=None):
        """One guarded /goal send plus the exact engine conversation's proof."""
        engine = start_json.get("engine")
        if engine not in {"codex", "zcodex", "claude"} or '\r' in prompt or '\n' in prompt:
            raise RuntimeError("native_goal_provider_unsupported")
        expected_thread = claude_goal.conversation_id(start_json.get('command')) if engine == 'claude' else None
        if engine == 'claude' and not expected_thread:
            raise RuntimeError('native_goal_identity_unverified')
        read_goal = (lambda row: claude_goal.goal_store(row, claude_config_dir, expected_thread, objective_hash)) if engine == 'claude' else goal_store
        expected = objective_hash(prompt)
        deadline = min(deadline, time.time() + 60)
        row = self.status(start_json["id"], workspace=specification["workspace"])
        if (not row or row.get("id") != start_json["id"] or row.get("tag") != start_json.get("tag")
                or not same_workspace(row.get("workspace"), start_json.get("workspace")) or row.get("engine") != engine
                or row.get("created_at_ms") != start_json.get("created_at_ms") or row.get("phase") != "running"):
            raise RuntimeError("native_goal_identity_unverified")
        if engine == 'claude' and row.get('command') != start_json.get('command'):
            raise RuntimeError('native_goal_identity_unverified')

        def matching(proof):
            if proof and proof.get("objective_hash") != expected:
                raise RuntimeError("native_goal_objective_conflict")
            return proof

        proof = matching(read_goal(row))
        if proof:
            return proof
        result = self.native("send", start_json["id"], "/goal " + prompt, "--enter", "--json", "--wait-ack", "0",
                             "--resends", "0", "--ready-timeout",
                             str(min(25, max(1, int(deadline - time.time()) - 1))),
                             timeout=30, workspace=specification["workspace"])
        receipt = json.loads(result.stdout)
        delivery = receipt.get("delivery", {})
        if (receipt.get("id") != start_json["id"] or delivery.get("engine") != engine
                or any(delivery.get(key) is not True for key in ('ready', 'landed', 'submitted'))
                or delivery.get("status") not in {'submitted', 'delivered_working'}
                or type(delivery.get("sends")) is not int or delivery["sends"] != 1
                or type(delivery.get("enter_presses")) is not int or not 1 <= delivery["enter_presses"] <= 3):
            raise RuntimeError("native_goal_delivery_unverified")
        while time.time() < deadline:
            proof = matching(read_goal(row))
            if proof:
                return proof
            time.sleep(0.5)
        raise RuntimeError("native_goal_registration_unverified")

    def record_evidence(self, task_id, specification, receipt, start_json):
        try:
            path = Path(specification["workspace"]) / ".local" / f"launch-{task_id}.json"
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_file(path.parent)
            evidence = {"task_id": task_id, "session_id": receipt["session_id"], "tag": "task-" + task_id,
                        "workspace": specification["workspace"], "created_at_ms": start_json.get("created_at_ms"),
                        "engine": start_json.get("engine"), "native_started_at": time.time(),
                        "first_action_validated_at": time.time() if receipt.get("first_action_verified") else None,
                        "interactive": bool(specification.get("interactive")),
                        "effort": specification.get("effort"), "with_goal": bool(specification.get("with_goal")),
                        "goal_registration": receipt.get("goal_registration"),
                        "task_output": specification.get("_task_output"),
                        "managed_exec_context": specification.get("_managed_exec_context")}
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                stream.write(json.dumps(evidence))
                stream.flush()
                os.fsync(stream.fileno())
            private_file(path)
        except OSError:
            import logging
            logging.getLogger(__name__).exception('Native launch evidence write failed task=%s', task_id)

    def retired(self, native_id, expected):
        tombstone = read_tombstone(native_id, state_dir=self.state_dir)
        if tombstone is None:
            return None
        valid, _ = verify_tombstone(tombstone, native_id, expected.get("tag"), expected.get("workspace"))
        if not valid or tombstone.get('containment_empty') is not True:
            return None
        code = native_returncode(tombstone.get('exit'))
        if code is None:
            return None
        return {'id': native_id, 'tag': tombstone['tag'], 'workspace': expected['workspace'],
                'phase': 'exited', 'containment_empty': True, 'exit': tombstone['exit'],
                'deadline_ms': tombstone.get('deadline_ms'), 'deadline_exceeded': tombstone.get('deadline_exceeded'),
                'retirement': 'verified_finished'}

    def retired_origin(self, native_id, expected):
        """Automatic reconcile only: legacy host launch evidence, not live identity.

        The caller must qualify the unique current journal attempt. Legacy launch
        manifests are host-persisted evidence, not immutable/cryptographic proof;
        they lack direct attempt fields. Any stored attempt fields must match too.
        """
        try:
            session = expected['session_id']
            workspace = expected['workspace']
            if (not isinstance(session, str) or not UUID_RE.fullmatch(session)
                    or not isinstance(native_id, str) or not UUID_RE.fullmatch(native_id)
                    or expected.get('native_id') != native_id or expected.get('tag') != 'task-hub2-'+session
                    or not isinstance(workspace, str) or not Path(workspace).is_absolute()
                    or type(expected.get('epoch')) is not int or expected['epoch'] < 1
                    or not isinstance(expected.get('operation_id'), str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,120}', expected['operation_id'])
                    or not re.fullmatch('[0-9a-f]{64}', expected.get('digest', ''))):
                return None
            manifest = retirement_private_json(Path(workspace)/'.local'/('launch-hub2-'+session+'.json'), 65536)
            base = native_state_directory(self.state_dir)
            tombstone = retirement_private_json(base/'retired-sessions'/native_id.lower()/'tombstone.json', TOMBSTONE_ENVELOPE_BYTES)
            tombstone.pop('output', None)
            if len(json.dumps(tombstone, ensure_ascii=True).encode()) > TOMBSTONE_METADATA_BYTES:
                return None
            if (manifest.get('task_id') != 'hub2-'+session or manifest.get('session_id') != native_id
                    or manifest.get('tag') != expected['tag'] or manifest.get('workspace') != workspace
                    or tombstone.get('id') != native_id or tombstone.get('tag') != expected['tag']
                    or tombstone.get('workspace') == workspace
                    or not isinstance(tombstone.get('workspace'), str) or not Path(tombstone['workspace']).is_absolute()
                    or len(tombstone['workspace'].encode('utf8')) > 4096
                    or tombstone.get('cause') != 'finished' or tombstone.get('containment_empty') is not True
                    or native_returncode(tombstone.get('exit')) is None):
                return None
            birth = manifest.get('created_at_ms')
            exited = tombstone['exit'].get('exited_at_ms')
            finished = tombstone.get('finished_at_ms')
            started = manifest.get('native_started_at')
            if (any(type(value) is not int for value in (birth, exited, finished))
                    or not 0 < birth <= exited <= finished <= int(time.time()*1000)+5000
                    or tombstone.get('created_at_ms') != birth or type(tombstone.get('created_at_ms')) is not int
                    or type(started) not in (int, float) or not birth <= started*1000 <= exited):
                return None
            for field in ('operation_id', 'epoch', 'digest'):
                if field in manifest and (type(manifest[field]) is not type(expected[field]) or manifest[field] != expected[field]):
                    return None
            proof = {'source': 'host_launch_manifest', 'created_at_ms': birth,
                     'origin_workspace': workspace, 'observed_workspace': tombstone['workspace'],
                     'operation_id': expected['operation_id'], 'epoch': expected['epoch'], 'journal_digest': expected['digest']}
            return {'id': native_id, 'tag': expected['tag'], 'workspace': workspace, 'phase': 'exited',
                    'containment_empty': True, 'exit': tombstone['exit'], 'retirement': 'verified_origin',
                    'retirement_origin': proof, 'deadline_ms': tombstone.get('deadline_ms'),
                    'deadline_exceeded': tombstone.get('deadline_exceeded')}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return None

    def recover(self, task_id, workspace, requirements=None):
        """Identity from our persisted evidence plus the live native row.

        Required modes must be proven by the launch evidence, mirroring the
        receipt verification a fresh launch performs."""
        path = Path(workspace) / '.local' / f'launch-{task_id}.json'
        try:
            with path.open('rb') as stream:
                raw = stream.read(262145)
        except OSError:
            raise RuntimeError("native_launch_recovery_unverified") from None
        if len(raw) > 262144:
            raise RuntimeError("native_recovery_evidence_too_large")
        try:
            record = json.loads(raw)
        except ValueError:
            raise RuntimeError("native_launch_recovery_unverified") from None
        native_id = record.get('session_id')
        if not isinstance(native_id, str) or not (record.get('native_started_at') or record.get('first_action_validated_at')) \
                or record.get('tag') != 'task-' + task_id or not same_workspace(record.get('workspace'), workspace):
            raise RuntimeError("native_launch_recovery_unverified")
        if requirements:
            if requirements.get('interactive') and record.get('interactive') is not True:
                raise RuntimeError("native_recovery_mode_unverified")
            if requirements.get('effort') is not None and record.get('effort') != requirements['effort']:
                raise RuntimeError("native_recovery_mode_unverified")
            if requirements.get('with_goal') and not isinstance(record.get('goal_registration'), dict):
                raise RuntimeError("native_recovery_mode_unverified")
        row = self.status(native_id, workspace=workspace)
        if row is None:
            row = self.status(f"{workspace}:task-{task_id}", workspace=workspace)
        if not row or (row.get("id") != native_id and not (same_workspace(row.get("workspace"), workspace)
                                                           and row.get("tag") == "task-" + task_id)):
            raise RuntimeError("native_launch_recovery_unverified")
        return native_id

    def aborted_identity(self, task_id, workspace, attempt):
        for selector in (f"{workspace}:task-{task_id}", "task-" + task_id):
            row = self.status(selector, workspace=workspace)
            labels = row.get('labels') if row else None
            if row and same_workspace(row.get("workspace"), workspace) and row.get("tag") == "task-" + task_id \
                    and isinstance(row.get("id"), str) and UUID_RE.fullmatch(row["id"]) \
                    and isinstance(labels,dict) and labels.get('hub.attempt')==attempt:
                return row["id"]
        return None

    def finalize(self, native_id, expected):
        """Qualified retirement: verified tombstone first, else the live native row.

        The live status only counts when the caller-visible row already shows a
        clean containment, which Bridge.reconcile checks before calling this."""
        tombstone = read_tombstone(native_id, state_dir=self.state_dir)
        if tombstone is not None:
            valid, _ = verify_tombstone(tombstone, native_id, expected.get("tag"), expected.get("workspace"))
            if valid:
                return 'retired-finished'
        row = self.status(native_id, workspace=expected.get("workspace"))
        if row:
            phase = str(row.get("phase", row.get("state", "")))
            if phase == 'retired-finished' or (phase == 'exited' and row.get("containment_empty") is True):
                return 'retired-finished'
            if phase == 'dead':
                return 'dead'
        raise RuntimeError("native_retirement_unverified")
