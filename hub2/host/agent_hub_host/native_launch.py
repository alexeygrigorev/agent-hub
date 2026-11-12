"""Direct native launch adapter: Hub session plans spawn through aplexer.

The Hub picks a model and host, then this adapter returns the native pane
identity immediately and preserves evidence for qualified exit and recovery. The
external Agent Quota Launcher remains untouched for its other consumers.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import stat
import sys
import time
from pathlib import Path

from .client import private_file
from . import provider_home
from . import claude_goal
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
LOCAL_ROUTES = {
    "codex": {"provider": "codex", "profile": "hub-shared-codex"},
    "go": {"provider": "go", "profile": "hub-shared-go"},
    "zai": {"provider": "zai", "profile": "hub-zai-central"},
    "claude": {"provider": "claude", "engine": "claude"},
    "antigravity": {"provider": "antigravity", "profile": "hub-hub-antigravity"},
}
HEADLESS_OUTPUT_FLAGS = {
    'claude': ('--output-format', 'stream-json', '--verbose'),
    'antigravity': ('--output-format', 'stream-json'),
}


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
                '--disable', 'shell_snapshot', '-c', 'check_for_update_on_startup=false', '--json', goal]
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


def headless_effort_argv(provider, argv, effort):
    if provider in {'codex', 'zai'}:
        value = 'xhigh' if effort == 'max' else effort
        if value not in {'low', 'medium', 'high', 'xhigh'}:
            raise ValueError('headless_effort_unsupported')
        flags = ['-c', 'model_reasoning_effort=' + json.dumps(value)]
    elif provider in {'claude', 'antigravity'} and effort in {'low', 'medium', 'high', 'max'}:
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
            result += ['--effort', effort]
        if not with_goal:
            result += ['--prompt-interactive', prompt] if provider == 'antigravity' else [prompt]
    else:
        if effort is not None:
            raise ValueError('interactive_effort_unsupported')
        if 'run' not in result or '--auto' not in result:
            raise ValueError('interactive_adapter_shape_unverified')
        result[result.index('run')] = 'mini'
        result.remove('--auto')
        result += ['--prompt', prompt]
    observed = [result[i + 1] for i, arg in enumerate(result[:-1]) if arg in {'--model', '-m'}]
    if not observed or set(observed) != {model}:
        raise ValueError('interactive_model_unverified')
    return result


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


def read_tombstone(native_id, state_dir=None):
    """Bounded explicit read of retired-sessions/<native_id>/tombstone.json.

    Optional retained output may enlarge the bounded 1 MiB envelope but is
    never returned as lifecycle evidence: it is stripped before the original
    64 KiB metadata cap applies. Symlinks, oversize envelopes/metadata and
    malformed JSON are rejected."""
    if not isinstance(native_id, str) or not UUID_RE.fullmatch(native_id.strip()):
        return None
    canonical_id = native_id.strip().lower()
    if state_dir is None:
        state_dir = os.environ.get("APLEXER_STATE_DIR")
    base = Path(state_dir).resolve() if state_dir else Path(os.path.expanduser("~/.local/state/aplexer")).resolve()
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
            self.config["routes"] = {**LOCAL_ROUTES, **{
                engine: {**LOCAL_ROUTES.get(engine, LOCAL_ROUTES.get(route.get("provider"), {})), **route}
                for engine, route in configuration.get("routes", {}).items()
            }}
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
        result = self.runner([self.native_binary, *args], input=input, text=True, capture_output=True,
                             timeout=timeout, check=False, env=env, cwd=workspace)
        if result.returncode and not allow_failure:
            raise RuntimeError("native_command_failed")
        if len(result.stdout) > output_limit:
            raise RuntimeError("native_response_too_large")
        return result

    def inventory(self):
        from .native_inventory import collect_native_inventory, CENSUS_TIMEOUT_SECONDS, MAX_CENSUS_BYTES
        return collect_native_inventory(lambda: self.native('list', '--all', '--json',
            timeout=CENSUS_TIMEOUT_SECONDS, output_limit=MAX_CENSUS_BYTES))

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
            supported = PROVIDER_CAPABILITIES.get(provider, set())
            available = bool(shutil.which(PROVIDER_BINARIES.get(provider, "")))
            if self.config.get('private_provider_homes') and provider == 'claude':
                available = provider_home.claude_install_available()
            if supported and available:
                result[engine] = set(supported)
        return result

    def managed_claude_readiness(self, release=None):
        """Exact installed release and actual profile code; no credential probe."""
        if sys.platform != 'linux':
            return None
        try:
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
            # -I -S prevents user-site startup; import only the verified bundled
            # CLI, never this daemon's duplicate package or CLI main/network.
            code = ('import sys,json; sys.path.insert(0,sys.argv[1]); '
                    'from agent_hub_host.provisioning import managed_exec_readiness; '
                    'print(json.dumps(managed_exec_readiness(sys.argv[1])))')
            result = self.runner([str(python), '-I', '-S', '-c', code, str(archive)],
                capture_output=True, text=True, check=False, timeout=3,
                env=spawn_environment(True))
            if result.returncode or len(result.stdout) > 1024:
                return None
            proof = json.loads(result.stdout)
            if proof != {'protocol': 'hub2-managed-claude-exec-v1', 'label': 'hub-claude', 'profile': 'hub-hub-claude'}:
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
        requested_mb = int(self.config.get("memory_mb", 1500))
        goal = specification["prompt"]
        fa_path = workspace / ".local" / f"first-action-{task_id}.json"
        fa_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_file(fa_path.parent)
        if provider in FIRST_ACTION_PROVIDERS:
            goal += FIRST_ACTION_INSTRUCTION.format(path=fa_path)
        if provider != 'shell':
            goal = aplexer_prompt(goal)
        local = provider == 'shell' or bool(specification.get('managed_credential_label')) or (not self.config.get("private_provider_homes", False) and not specification.get("interactive"))
        if local:
            child = self.profile_command(task_id, specification, route, goal)
        else:
            child = adapter_argv(provider, goal, specification["model"])
        objective = None
        if specification.get("interactive") and not local:
            child = interactive_argv(provider, child, goal, model=specification["model"],
                                     effort=specification.get("effort"), with_goal=specification.get("with_goal", False))
            if specification.get("with_goal"):
                objective = goal_prompt(goal, specification["workspace"], task_id, str(fa_path))
                if provider == 'claude':
                    child = claude_goal.conversation_argv(child, task_id, specification.get('operation_id'), specification.get('epoch'))
        elif not local:
            if specification.get("effort") is not None:
                child = headless_effort_argv(provider, child, specification["effort"])
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
        if specification.get("child_config_file"):
            child_env["HUB2_CONFIG"] = specification["child_config_file"]
        if objective is not None and provider == 'claude':
            # Pin the same trusted config root for the child and receipt reader,
            # including local-login hosts whose aplexer daemon has another env.
            child_env.setdefault('CLAUDE_CONFIG_DIR', os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
        tmpdir = Path(child_env["TMPDIR"])
        tmpdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        argv = [self.native_binary, "start", "--json", "--memory", f"{requested_mb}M",
                "--pids", str(self.config.get("pids", 256)), "--startup-timeout-ms", "30000",
                "--workspace", specification["workspace"], "--cwd", specification["workspace"],
                "--tag", "task-" + task_id]
        if objective is not None:
            # A literal -- command keeps its argv but does not infer an engine.
            # Codex and its zcodex fork share a transcript family; Claude has
            # its own native delivery/readiness and conversation format.
            argv += ["--engine", "claude" if provider == 'claude' else "codex"]
        if specification.get("session_id"):
            argv += ["--label", "hub.managed=true", "--label", "hub.session=" + specification["session_id"]]
        if specification.get('attempt_label'):
            argv += ['--label', 'hub.attempt=' + specification['attempt_label']]
        for key, value in child_env.items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--"] + child
        started = time.time()
        env = spawn_environment(bool(specification.get("provider_home") or specification.get("managed_credential_label")))
        env["PWD"] = str(Path(specification["workspace"]).resolve())
        # `a start` is only the daemon handshake client: the spawned session lives
        # under the aplexer supervisor, so the runner timeout bounds the handshake
        # alone. A killed handshake with a created session stays launch-uncertain
        # and is reconciled by identity, never re-spawned.
        before_dispatch = specification.get('before_native_dispatch')
        if before_dispatch:
            before_dispatch()
        try:
            result = self.runner(argv, text=True, capture_output=True,
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
        goal_registration = None
        if objective is not None:
            if start_json.get('engine') != ('claude' if provider == 'claude' else 'codex'):
                raise RuntimeError('native_goal_provider_unsupported')
            goal_registration = self.register_goal(start_json, specification, objective,
                                                   deadline=started + specification["timeout_seconds"],
                                                   claude_config_dir=child_env.get('CLAUDE_CONFIG_DIR'))
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
        with os.fdopen(fd, 'w') as stream:
            stream.write(prompt)
        profile = route.get('profile')
        from .task_output import prepare_output
        specification['_task_output'] = prepare_output(task_id, specification, profile, route.get('engine') if profile else route.get('engine', provider), prompt)
        output = specification['_task_output']
        output_args = ['--output-dir', str(Path(specification['workspace']).resolve().joinpath(*output['parts']))] if output else []
        selector = ['--profile', profile] if profile else ['--engine', route.get('engine', provider)]
        args = ['--model', model]
        args += HEADLESS_OUTPUT_FLAGS.get(provider, ())
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
            path = self.journal.directory / 'managed-claude' / (token + '.json')
            save_config(path, context)
            specification['_managed_exec_context'] = str(path)
            args.append('--hub-managed-claude-context=' + str(path))
        effort = specification.get('effort')
        if effort is not None:
            args += headless_effort_argv(provider, ['prompt'], effort)[:-1]
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
            base = Path(self.state_dir) if self.state_dir else Path(os.environ.get('APLEXER_STATE_DIR', str(Path.home()/'.local/state/aplexer')))
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
