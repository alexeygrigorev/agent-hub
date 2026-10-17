"""M14c probes: explicit-argv Git ancestry / CI / active-deployment evidence.

Read-only adapters over maintained native CLIs. Every probe is an explicit
argv array (no shell, no interpolation); only allowlisted tools and
read-only subcommands run, working directories must sit under
operator-allowlisted repo roots, output is bounded and token-redacted, and
unavailable probes stay unknown with an actionable reason. Tokens are never
sent to Hub: captured output is redacted and argv secrets are masked in
provenance.

Evidence maps 1:1 onto the Hub observation kinds (probe_git/probe_ci/
probe_deploy). Provenance (tool, argv, exact sha, ref freshness) is always
attached. Deploy state is NEVER inferred from push or CI success: deployed
requires an explicit active-deployment record naming the exact sha.
Stale origin/main refs fail closed to unknown ancestry.

No external calls happen in tests: the runner is injectable and the only
live subprocesses in tests run against disposable synthetic fixture repos
inside the owned private TMPDIR.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .status_collect import MAX_HEADLINE, sanitize

MAX_OUTPUT = 8192
DEFAULT_TIMEOUT_S = 30
DEFAULT_REF_MAX_AGE_S = 3600
MAX_PROBES = 50

# Explicit trusted tools + read-only subcommands. Anything else is refused
# (unknown with reason), never executed.
ALLOWLIST = {
    'git': {'rev-parse', 'merge-base', 'status', 'log', 'show-ref', 'branch', 'ls-remote'},
    'gh': {'api', 'run'},
    'wrangler': {'deployments'},
}
REFUSED_GIT_REMOTES = {'fetch', 'push', 'pull', 'clone'}
_GH_WRITE_TOKENS = {'post', 'patch', 'put', 'delete'}
_GH_RUN_READONLY = {'list', 'view'}
_LS_REMOTE_REF_RE = None  # compiled lazily to keep import light

KINDS = ('git_ancestry', 'ci', 'deployment')


def masked_argv(argv: list) -> list:
    """Provenance-safe argv: token-shaped elements are masked, never stored raw."""
    out = []
    for element in argv:
        text = element if isinstance(element, str) else str(element)
        out.append('[masked]' if sanitize(text, 8) != text[:8] and _looks_secret(text) else text)
    return out


def _looks_secret(text: str) -> bool:
    redacted = sanitize(text, len(text))
    return redacted != text


def check_argv(argv: Any) -> tuple[list | None, str | None]:
    """Validate an explicit argv. Returns (clean_argv, None) or (None, reason)."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        return None, 'probe_argv_must_be_nonempty_string_list'
    tool = os.path.basename(argv[0])
    if tool not in ALLOWLIST:
        return None, 'probe_tool_not_allowlisted'
    if len(argv) < 2:
        return None, 'probe_subcommand_missing'
    if tool == 'git' and argv[1] in REFUSED_GIT_REMOTES:
        return None, 'probe_git_remote_refused'
    if argv[1] not in ALLOWLIST[tool]:
        return None, 'probe_subcommand_not_allowlisted'
    if tool == 'git' and argv[1] == 'ls-remote':
        # Read-only fresh-remote read with exact args only: no fetch mutations,
        # no extra flags, no other remotes/refs. Shape:
        #   git ls-remote origin refs/heads/<branch>
        if len(argv) != 4 or argv[2] != 'origin':
            return None, 'probe_git_ls_remote_exact_args_only'
        import re as _re
        if not _re.fullmatch(r'refs/heads/[A-Za-z0-9._-]+', argv[3]):
            return None, 'probe_git_ls_remote_exact_args_only'
        return argv, None
    if tool == 'gh' and argv[1] == 'api':
        # gh api defaults to GET; any write method/body flag is refused so the
        # adapter stays truly read-only.
        lowered = [a.lower() for a in argv[2:]]
        if any(t in _GH_WRITE_TOKENS for t in lowered):
            return None, 'probe_gh_write_refused'
        for i, token in enumerate(argv[2:]):
            low = token.lower()
            if low == '-x':
                return None, 'probe_gh_write_refused'
            if low.startswith('--method'):
                value = ''
                if '=' in token:
                    value = token.split('=', 1)[1]
                elif i + 1 < len(argv[2:]):
                    value = argv[2:][i + 1]
                if value.lower() not in ('', 'get'):
                    return None, 'probe_gh_write_refused'
            if low == '--input':
                return None, 'probe_gh_write_refused'
        return argv, None
    if tool == 'gh' and argv[1] == 'run':
        # Only read-only run inspection; cancel/rerun are mutations.
        if len(argv) < 3 or argv[2] not in _GH_RUN_READONLY:
            return None, 'probe_gh_run_write_refused'
        return argv, None
    if tool == 'wrangler' and argv[1] == 'deployments':
        if len(argv) < 3 or argv[2] != 'list':
            return None, 'probe_subcommand_not_allowlisted'
        return argv, None
    return argv, None


def realpath_under(path: str, roots: list) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        try:
            anchor = os.path.realpath(root)
        except OSError:
            continue
        if real == anchor or real.startswith(anchor + os.sep):
            return True
    return False


def _drain_bounded(pipe, limit: int) -> tuple[bytes, bool]:
    """Read a PIPE to EOF, keeping at most limit+1 bytes in flight.

    Returns (data_capped, overflow). Overflow means the child wrote more than
    the bound; the caller kills the child and truncates. Chunked reads keep
    memory bounded while draining (no unbounded capture before truncate).
    """
    chunks: list = []
    total = 0
    try:
        while True:
            data = pipe.read(4096)
            if not data:
                break
            total += len(data)
            if total > limit:
                # Keep only up to the bound; record overflow and stop keeping.
                keep = limit - (total - len(data))
                if keep > 0:
                    chunks.append(data[:keep])
                return b''.join(chunks), True
            chunks.append(data)
    except (OSError, ValueError):
        pass
    return b''.join(chunks), False


def run_argv(argv: list, cwd: str, timeout_s: int,
             runner: Callable | None = None) -> dict:
    """Run one explicit argv (no shell). Returns {exit, stdout, stderr} bounded+redacted.

    Subprocess output is bounded IN FLIGHT (chunked drain, at most
    MAX_OUTPUT bytes kept per stream); overflow kills the child. Timeout is
    enforced (sane clamp applied by the caller, 1..120s).
    """
    if runner is not None:
        result = runner(argv, cwd, timeout_s)
        return {'exit': int(result.get('exit', 99)),
                'stdout': sanitize(result.get('stdout', ''), MAX_OUTPUT),
                'stderr': sanitize(result.get('stderr', ''), MAX_OUTPUT)}
    import threading

    try:
        timeout_f = float(timeout_s)
    except (TypeError, ValueError):
        timeout_f = float(DEFAULT_TIMEOUT_S)
    timeout_f = max(1.0, min(timeout_f, 120.0))
    try:
        proc = subprocess.Popen(argv, cwd=cwd, shell=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, env=_scrubbed_env())
    except FileNotFoundError:
        return {'exit': 127, 'stdout': '', 'stderr': 'tool_not_installed'}
    except OSError as exc:
        return {'exit': 126, 'stdout': '', 'stderr': sanitize(str(exc), 200)}
    out: dict = {}
    def _read(key: str, pipe) -> None:
        out[key] = _drain_bounded(pipe, MAX_OUTPUT)
    threads = [threading.Thread(target=_read, args=('stdout', proc.stdout), daemon=True),
               threading.Thread(target=_read, args=('stderr', proc.stderr), daemon=True)]
    for thread in threads:
        thread.start()
    try:
        proc.wait(timeout=timeout_f)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for thread in threads:
            thread.join(timeout=2)
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except OSError:
            pass
        return {'exit': 124, 'stdout': '', 'stderr': 'probe_timeout'}
    for thread in threads:
        thread.join(timeout=5)
    try:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    except OSError:
        pass
    (stdout_bytes, stdout_overflow), (stderr_bytes, stderr_overflow) = (
        out.get('stdout', (b'', False)), out.get('stderr', (b'', False)))
    if stdout_overflow or stderr_overflow:
        return {'exit': proc.returncode if proc.returncode is not None else 99,
                'stdout': sanitize(stdout_bytes.decode('utf-8', errors='replace'), MAX_OUTPUT),
                'stderr': 'probe_output_truncated'}
    return {'exit': proc.returncode if proc.returncode is not None else 99,
            'stdout': sanitize(stdout_bytes.decode('utf-8', errors='replace'), MAX_OUTPUT),
            'stderr': sanitize(stderr_bytes.decode('utf-8', errors='replace'), MAX_OUTPUT)}


def _scrubbed_env() -> dict:
    """Child env without secret-bearing variables (tools use config files, not env)."""
    keep = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if 'TOKEN' in upper or 'SECRET' in upper or 'KEY' in upper or 'PASSWORD' in upper:
            continue
        keep[key] = value
    return keep


def _ls_remote_ref(ref: str) -> str | None:
    """Map a local tracking ref to its ls-remote ref. None when unmappable."""
    if not isinstance(ref, str) or not ref:
        return None
    if ref.startswith('refs/heads/'):
        return ref
    if ref.startswith('origin/'):
        branch = ref[len('origin/'):]
    elif '/' not in ref:
        branch = ref
    else:
        return None
    import re as _re
    if not _re.fullmatch(r'[A-Za-z0-9._-]+', branch):
        return None
    return 'refs/heads/' + branch


def _parse_sha(text: str) -> str | None:
    token = (text or '').strip().split()[0] if (text or '').strip() else ''
    if len(token) == 40 and all(c in '0123456789abcdefABCDEF' for c in token):
        return token.lower()
    return None


def build_git_ancestry(repo: str, sha: str, ref: str, ref_max_age_s: int,
                       runner: Callable | None = None,
                       timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
    """Local ancestry facts with FRESH remote verification (no commit-time proxy).

    merged_in_main ONLY on exit-0 is-ancestor AND an exact SHA match between
    the local ``ref`` and a fresh read-only ``git ls-remote origin
    refs/heads/main``. A perfectly current remote with an old commit stays
    fresh (SHA equality is freshness); a stale local ref with a recent commit
    never counts. Network/parse failures fail closed to unknown.
    ``ref_max_age_s`` is retained for ABI compatibility and ignored.
    """
    _ = ref_max_age_s
    provenance = {'tool': 'git', 'repo': repo, 'sha': sha, 'ref': ref}
    head = run_argv(['git', 'rev-parse', 'HEAD'], repo, timeout_s, runner)
    if head['exit'] != 0:
        return {'sha': sha, 'ancestry': 'unknown', 'reason': 'head_unreadable:' + head['stderr'][:120],
                'provenance': provenance}
    local = run_argv(['git', 'rev-parse', ref], repo, timeout_s, runner)
    if local['exit'] != 0:
        return {'sha': sha, 'ancestry': 'unknown', 'reason': 'ref_unreadable:' + local['stderr'][:120],
                'provenance': provenance}
    local_sha = _parse_sha(local['stdout'])
    if not local_sha:
        return {'sha': sha, 'ancestry': 'unknown', 'reason': 'ref_unparseable',
                'provenance': provenance}
    ls_ref = _ls_remote_ref(ref)
    if not ls_ref:
        return {'sha': sha, 'ancestry': 'unknown', 'reason': 'ref_unmappable_for_ls_remote',
                'provenance': {**provenance, 'origin_main_sha': local_sha}}
    remote = run_argv(['git', 'ls-remote', 'origin', ls_ref], repo, timeout_s, runner)
    if remote['exit'] != 0:
        return {'sha': sha, 'ancestry': 'unknown', 'reason': 'network_unknown_fails_closed',
                'provenance': {**provenance, 'origin_main_sha': local_sha}}
    remote_sha = _parse_sha(remote['stdout'])
    if not remote_sha:
        return {'sha': sha, 'ancestry': 'unknown', 'reason': 'network_unknown_fails_closed',
                'provenance': {**provenance, 'origin_main_sha': local_sha}}
    fresh = (local_sha == remote_sha)
    provenance = {**provenance, 'origin_main_sha': local_sha,
                  'remote_sha': remote_sha, 'ref_fresh': fresh}
    if not fresh:
        return {'sha': sha, 'ancestry': 'unknown', 'reason': 'stale_ref_fails_closed',
                'provenance': provenance}
    ancestor = run_argv(['git', 'merge-base', '--is-ancestor', sha, ref], repo, timeout_s, runner)
    if ancestor['exit'] == 0:
        return {'sha': sha, 'ancestry': 'merged_in_main', 'ref': ref, 'provenance': provenance}
    if ancestor['exit'] == 1:
        return {'sha': sha, 'ancestry': 'not_merged', 'ref': ref, 'provenance': provenance}
    return {'sha': sha, 'ancestry': 'unknown',
            'reason': 'ancestry_check_failed:' + ancestor['stderr'][:120], 'provenance': provenance}


def build_ci(result: dict, sha: str) -> dict:
    """CI evidence for the EXACT sha. A run naming another sha (or none) is
    unknown, never borrowed as this commit's CI state."""
    provenance = {'tool': result.get('tool', 'gh'), 'sha': sha}
    if result.get('exit') != 0:
        return {'state': 'unknown', 'reason': 'ci_query_failed', 'provenance': provenance}
    try:
        runs = json.loads(result.get('stdout') or 'null')
    except ValueError:
        return {'state': 'unknown', 'reason': 'ci_output_unparseable', 'provenance': provenance}
    items = runs if isinstance(runs, list) else (runs.get('workflow_runs') or runs.get('runs') or [])
    for run in items if isinstance(items, list) else []:
        if not isinstance(run, dict):
            continue
        if run.get('head_sha') != sha and run.get('headSha') != sha:
            continue
        conclusion = str(run.get('conclusion') or run.get('status') or '').lower()
        ref = str(run.get('id') or run.get('url') or run.get('name') or '')[:MAX_HEADLINE]
        if conclusion in ('success', 'completed-success', 'passed', 'passing'):
            return {'state': 'passing', 'run': sanitize(ref, 256), 'provenance': provenance}
        if conclusion in ('failure', 'failed', 'failing', 'timed_out', 'cancelled'):
            return {'state': 'failing', 'run': sanitize(ref, 256), 'provenance': provenance}
    return {'state': 'unknown', 'reason': 'no_completed_run_for_sha', 'provenance': provenance}


def build_deployment(result: dict, sha: str, environment: str = 'production') -> dict:
    """Active-deployment evidence. Deployed ONLY when an active record names
    the exact sha in the target environment. Push/CI success never implies it."""
    provenance = {'tool': result.get('tool', 'cf'), 'sha': sha, 'environment': environment}
    if result.get('exit') != 0:
        return {'state': 'unknown', 'reason': 'deployment_query_failed', 'provenance': provenance}
    try:
        payload = json.loads(result.get('stdout') or 'null')
    except ValueError:
        return {'state': 'unknown', 'reason': 'deployment_output_unparseable', 'provenance': provenance}
    items = payload if isinstance(payload, list) else (payload.get('deployments') or payload.get('result') or [])
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        env = str(item.get('environment') or item.get('env') or '')
        active = str(item.get('state') or item.get('status') or '').lower() in (
            'active', 'success', 'deployed', 'current', 'idle')
        names_sha = sha in json.dumps(item)
        if active and (not environment or env == environment or not env) and names_sha:
            ref = str(item.get('id') or item.get('url') or '')[:MAX_HEADLINE]
            return {'state': 'deployed', 'deployment': sanitize(ref, 256), 'sha': sha,
                    'provenance': provenance}
    return {'state': 'not_deployed', 'reason': 'no_active_deployment_names_sha',
            'provenance': provenance}


def collect_probes(spec: dict, runner: Callable | None = None) -> dict:
    """Run operator-configured probes. Returns {'observations', 'skipped'} where
    observations map onto probe_git/probe_ci/probe_deploy payloads."""
    observations: list = []
    skipped: list = []
    if not isinstance(spec, dict):
        return {'observations': [], 'skipped': [{'reason': 'invalid_spec'}]}
    machine_id = spec.get('machine_id')
    observed_at = spec.get('observed_at', '')
    repo_roots = [str(r) for r in (spec.get('repo_roots') or [])]
    probes = spec.get('probes') or []
    if not machine_id or not observed_at or not isinstance(probes, list):
        return {'observations': [], 'skipped': [{'reason': 'invalid_spec'}]}
    for probe in probes[:MAX_PROBES]:
        if not isinstance(probe, dict):
            skipped.append({'reason': 'invalid_probe_entry'})
            continue
        pid = str(probe.get('id') or 'unnamed')
        kind = probe.get('kind')
        cwd = probe.get('cwd')
        native_id = probe.get('for_native_id')
        if kind not in KINDS or not cwd or not native_id:
            skipped.append({'id': pid, 'reason': 'invalid_probe_entry'})
            continue
        if not realpath_under(str(cwd), repo_roots):
            skipped.append({'id': pid, 'reason': 'cwd_outside_allowlist'})
            continue
        argv, reason = check_argv(probe.get('argv'))
        if argv is None:
            skipped.append({'id': pid, 'reason': reason})
            continue
        timeout_s = probe.get('timeout_s', DEFAULT_TIMEOUT_S)
        try:
            timeout_s = int(timeout_s)
        except (TypeError, ValueError):
            timeout_s = DEFAULT_TIMEOUT_S
        timeout_s = max(1, min(timeout_s, 120))
        if kind == 'git_ancestry':
            sha = str(probe.get('sha') or '')
            if len(sha) != 40 or not all(c in '0123456789abcdefABCDEF' for c in sha):
                skipped.append({'id': pid, 'reason': 'sha_required'})
                continue
            evidence = build_git_ancestry(str(cwd), sha.lower(),
                                          str(probe.get('ref') or 'origin/main'),
                                          int(probe.get('ref_max_age_s', DEFAULT_REF_MAX_AGE_S)),
                                          runner, timeout_s)
            payload = {'sha': evidence['sha'], 'ancestry': evidence['ancestry']}
            if evidence.get('ref'):
                payload['ref'] = evidence['ref'][:128]
            observations.append({'machine_id': machine_id, 'native_id': native_id, 'kind': 'probe_git',
                                 'observed_at': observed_at, 'payload': payload})
        elif kind == 'ci':
            sha = str(probe.get('sha') or '')
            if len(sha) != 40 or not all(c in '0123456789abcdefABCDEF' for c in sha):
                skipped.append({'id': pid, 'reason': 'sha_required'})
                continue
            result = run_argv(argv, str(cwd), timeout_s, runner)
            result['tool'] = os.path.basename(argv[0])
            evidence = build_ci(result, sha.lower())
            payload = {'state': evidence['state'], 'sha': sha.lower()}
            if evidence.get('run'):
                payload['run'] = evidence['run']
            observations.append({'machine_id': machine_id, 'native_id': native_id, 'kind': 'probe_ci',
                                 'observed_at': observed_at, 'payload': payload})
        else:
            sha = str(probe.get('sha') or '')
            if len(sha) != 40 or not all(c in '0123456789abcdefABCDEF' for c in sha):
                skipped.append({'id': pid, 'reason': 'sha_required'})
                continue
            result = run_argv(argv, str(cwd), timeout_s, runner)
            result['tool'] = os.path.basename(argv[0])
            evidence = build_deployment(result, sha.lower(), str(probe.get('environment') or 'production'))
            payload = {'state': evidence['state'], 'sha': sha.lower()}
            if evidence.get('deployment'):
                payload['deployment'] = evidence['deployment']
            observations.append({'machine_id': machine_id, 'native_id': native_id, 'kind': 'probe_deploy',
                                 'observed_at': observed_at, 'payload': payload})
    return {'observations': observations, 'skipped': skipped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='status_probe', description='M14c explicit-argv Git/CI/deploy probes')
    parser.add_argument('--once', action='store_true', required=True, help='Run a single probe round')
    parser.add_argument('--probes', default=None, help='Probe spec JSON path (or - for stdin)')
    parser.add_argument('--submit', action='store_true', help='POST evidence observations to the Hub')
    return parser


def main(argv: list | None = None, runner: Callable | None = None) -> dict:
    args = build_parser().parse_args(argv)
    if args.probes in (None, ''):
        print('status_probe: --probes is required', file=sys.stderr)
        raise SystemExit(2)
    try:
        if args.probes == '-':
            spec = json.load(sys.stdin)
        else:
            with open(args.probes, 'r', encoding='utf-8') as handle:
                spec = json.load(handle)
    except (OSError, ValueError) as exc:
        print('status_probe: cannot read probe spec: %s' % exc, file=sys.stderr)
        raise SystemExit(2)
    if not spec.get('observed_at'):
        from datetime import datetime, timezone
        spec['observed_at'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    result = collect_probes(spec, runner)
    if args.submit:
        from . import api, auth, config
        from .status_collect import submit
        cfg = config.load_config()
        base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
        api.check_url(base)
        token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN') or auth.get_access_token()
        result['submit'] = submit(result['observations'], base, token)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write('\n')
    return result


if __name__ == '__main__':
    main()
