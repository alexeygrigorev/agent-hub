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
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .status_collect import MAX_HEADLINE, sanitize

MAX_OUTPUT = 8192
DEFAULT_TIMEOUT_S = 30
DEFAULT_REF_MAX_AGE_S = 3600
MAX_PROBES = 50
CF_API_BASE = 'https://api.cloudflare.com/client/v4/'
MAX_CF_BODY = 1024 * 1024
MAX_CREDENTIAL_READ = 65536

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


def _extract_deployment_shas(item: dict) -> set[str]:
    """Extract structured commit SHAs from an active deployment record.
    Unrelated notes, descriptions, or historical message text containing the SHA do NOT count."""
    shas: set[str] = set()
    if not isinstance(item, dict):
        return shas
    for key in ('sha', 'source_commit', 'commit_sha', 'head_sha', 'headSha'):
        val = item.get(key)
        if isinstance(val, str):
            parsed = _parse_sha(val)
            if parsed:
                shas.add(parsed)
    commit = item.get('commit')
    if isinstance(commit, str):
        parsed = _parse_sha(commit)
        if parsed:
            shas.add(parsed)
    elif isinstance(commit, dict):
        for key in ('id', 'sha', 'source_commit'):
            parsed = _parse_sha(str(commit.get(key) or ''))
            if parsed:
                shas.add(parsed)
    versions = item.get('versions')
    if isinstance(versions, list):
        for v in versions:
            if isinstance(v, dict):
                for key in ('source_commit', 'sha'):
                    parsed = _parse_sha(str(v.get(key) or ''))
                    if parsed:
                        shas.add(parsed)
    return shas


def build_deployment(result: dict, sha: str, environment: str = 'production') -> dict:
    """Active-deployment evidence. Deployed ONLY when an active record names
    the exact sha in structured commit identity in the target environment.
    Push/CI success never implies it. Unrelated notes or histories mentioning
    the sha do not count."""
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
        if not active:
            continue
        if environment and env and env != environment:
            continue
        extracted_shas = _extract_deployment_shas(item)
        if sha in extracted_shas:
            ref = str(item.get('id') or item.get('url') or '')[:MAX_HEADLINE]
            return {'state': 'deployed', 'deployment': sanitize(ref, 256), 'sha': sha,
                    'provenance': provenance}
    return {'state': 'not_deployed', 'reason': 'no_active_deployment_names_sha',
            'provenance': provenance}


def load_cloudflare_credentials(path: str | Path | None) -> tuple[dict | None, str | None]:
    """Read CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN from a private regular
    current-user-owned 0600 file. Symlinks and broad permissions are rejected.
    Returns (creds_dict, None) or (None, failure_reason).
    """
    if not path or not isinstance(path, (str, Path)):
        return None, 'credentials_path_missing'
    str_path = str(path)
    try:
        if os.path.islink(str_path):
            return None, 'credentials_file_symlink_refused'
        st = os.lstat(str_path)
    except FileNotFoundError:
        return None, 'credentials_file_not_found'
    except PermissionError:
        return None, 'credentials_file_unreadable'
    except OSError:
        return None, 'credentials_file_error'

    if not stat.S_ISREG(st.st_mode):
        return None, 'credentials_file_not_regular'
    if st.st_uid != os.getuid():
        return None, 'credentials_file_wrong_owner'
    if (st.st_mode & 0o777) != 0o600:
        return None, 'credentials_file_broad_permissions'

    try:
        with open(str_path, 'r', encoding='utf-8') as handle:
            raw_data = handle.read(MAX_CREDENTIAL_READ + 1)
        if len(raw_data) > MAX_CREDENTIAL_READ:
            return None, 'credentials_file_too_large'
    except OSError:
        return None, 'credentials_file_unreadable'

    cfg: dict[str, str] = {}
    for line in raw_data.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, v = line.split('=', 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in ('CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_API_TOKEN'):
                cfg[k] = v

    account_id = cfg.get('CLOUDFLARE_ACCOUNT_ID', '').strip()
    api_token = cfg.get('CLOUDFLARE_API_TOKEN', '').strip()
    if not account_id:
        return None, 'credentials_missing_account_id'
    if not api_token:
        return None, 'credentials_missing_api_token'
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', account_id):
        return None, 'credentials_invalid_account_id'
    if any(c in api_token for c in '\r\n\x00') or len(api_token) < 8:
        return None, 'credentials_invalid_api_token'

    return {'account_id': account_id, 'api_token': api_token}, None


class DisallowRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so Authorization header is never forwarded."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect_refused', headers, fp)

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect_refused', headers, fp)

    def http_error_303(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect_refused', headers, fp)

    def http_error_307(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect_refused', headers, fp)

    def http_error_308(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect_refused', headers, fp)


def cf_http_get(url: str, token: str, timeout_s: float,
                http_transport: Callable | None = None) -> tuple[dict | None, str | None]:
    """Execute GET to fixed Cloudflare API endpoint with no redirects and bounded response.
    Never exposes token or sensitive details in error strings.
    Returns (parsed_json, None) or (None, failure_reason).
    """
    if not url.startswith(CF_API_BASE):
        return None, 'invalid_cf_api_url'
    headers = {
        'Authorization': 'Bearer ' + token,
        'User-Agent': 'agent-hub-host/1',
        'Accept': 'application/json',
    }
    timeout_s = max(1.0, min(float(timeout_s), 120.0))

    if http_transport is not None:
        try:
            resp = http_transport(url, headers, timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return None, 'redirect_refused'
            return None, f'cf_http_{exc.code}'
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(getattr(exc, 'reason', None), TimeoutError) or isinstance(exc, TimeoutError):
                return None, 'cf_timeout'
            return None, 'cf_network_error'
        except Exception as exc:
            if 'redirect' in type(exc).__name__.lower() or 'redirect' in str(exc).lower():
                return None, 'redirect_refused'
            return None, 'cf_request_failed'

        if isinstance(resp, dict):
            status = resp.get('status', 200)
            if status in (301, 302, 303, 307, 308):
                return None, 'redirect_refused'
            if status == 401:
                return None, 'cf_http_401'
            if status == 429:
                return None, 'cf_http_429'
            if status != 200:
                return None, f'cf_http_{status}'
            if 'json' in resp and isinstance(resp['json'], dict):
                payload = resp['json']
            else:
                body = resp.get('body', '')
                if isinstance(body, bytes):
                    body = body.decode('utf-8', errors='replace')
                if len(body) > MAX_CF_BODY:
                    return None, 'response_bounds_exceeded'
                try:
                    payload = json.loads(body)
                except ValueError:
                    return None, 'cf_response_unparseable'
        else:
            return None, 'cf_response_unparseable'
    else:
        opener = urllib.request.build_opener(DisallowRedirectHandler)
        req = urllib.request.Request(url, headers=headers, method='GET')
        try:
            with opener.open(req, timeout=timeout_s) as res:
                code = res.getcode()
                if code != 200:
                    return None, f'cf_http_{code}'
                raw = res.read(MAX_CF_BODY + 1)
                if len(raw) > MAX_CF_BODY:
                    return None, 'response_bounds_exceeded'
                try:
                    payload = json.loads(raw.decode('utf-8', errors='replace'))
                except ValueError:
                    return None, 'cf_response_unparseable'
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return None, 'redirect_refused'
            return None, f'cf_http_{exc.code}'
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(getattr(exc, 'reason', None), TimeoutError) or isinstance(exc, TimeoutError):
                return None, 'cf_timeout'
            return None, 'cf_network_error'
        except OSError:
            return None, 'cf_network_error'

    if not isinstance(payload, dict):
        return None, 'cf_response_unparseable'
    if payload.get('success') is not True:
        return None, 'cf_api_error'
    return payload, None


def fetch_cloudflare_deployment_snapshot(
    account_id: str,
    script_name: str,
    api_token: str,
    timeout_s: float = 15.0,
    http_transport: Callable | None = None
) -> dict:
    """Fetch active deployment and active version annotations from Cloudflare API.
    Returns:
      {'success': True, 'deployment_id': str, 'versions': list[dict]}
      or
      {'success': False, 'reason': str}
    """
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', account_id):
        return {'success': False, 'reason': 'invalid_account_id'}
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', script_name):
        return {'success': False, 'reason': 'invalid_script_name'}

    deployments_url = f"{CF_API_BASE}accounts/{account_id}/workers/scripts/{script_name}/deployments"
    data, err = cf_http_get(deployments_url, api_token, timeout_s, http_transport)
    if err is not None:
        return {'success': False, 'reason': err}

    res = data.get('result')
    deployments = res if isinstance(res, list) else (res.get('deployments') if isinstance(res, dict) else [])
    if not deployments or not isinstance(deployments, list):
        return {'success': False, 'reason': 'no_deployments_found'}

    latest = deployments[0]
    if not isinstance(latest, dict):
        return {'success': False, 'reason': 'invalid_deployment_record'}
    deployment_id = str(latest.get('id') or 'unknown')[:64]

    raw_versions = latest.get('versions')
    if not raw_versions or not isinstance(raw_versions, list):
        return {'success': False, 'reason': 'deployment_no_versions'}

    parsed_versions = []
    total_percentage = 0.0
    for v in raw_versions:
        if not isinstance(v, dict):
            return {'success': False, 'reason': 'invalid_version_entry'}
        vid = v.get('version_id')
        if not vid or not re.fullmatch(r'[0-9a-fA-F-]{36}', str(vid)):
            return {'success': False, 'reason': 'invalid_version_id'}
        pct = v.get('percentage')
        if pct is None and len(raw_versions) == 1:
            pct = 100
        try:
            pct_num = float(pct)
        except (TypeError, ValueError):
            return {'success': False, 'reason': 'invalid_traffic_percentage'}
        if pct_num < 0.0 or pct_num > 100.0:
            return {'success': False, 'reason': 'invalid_traffic_percentage'}
        total_percentage += pct_num
        parsed_versions.append({'version_id': str(vid).lower(), 'percentage': pct_num})

    if abs(total_percentage - 100.0) > 0.01:
        return {'success': False, 'reason': 'invalid_traffic_percentage'}

    # Fetch annotations for versions with positive traffic (percentage > 0)
    enriched_versions = []
    for v in parsed_versions:
        vid = v['version_id']
        pct = v['percentage']
        if pct > 0:
            version_url = f"{CF_API_BASE}accounts/{account_id}/workers/scripts/{script_name}/versions/{vid}"
            v_data, v_err = cf_http_get(version_url, api_token, timeout_s, http_transport)
            if v_err is not None:
                return {'success': False, 'reason': v_err}
            v_res = v_data.get('result') or {}
            ann = v_res.get('annotations') or {}
            message = str(ann.get('workers/message') or '')
            m = re.search(r'source_commit=([0-9a-fA-F]{40})', message)
            source_commit = m.group(1).lower() if m else None
            if not source_commit:
                raw_sc = ann.get('source_commit') or (v_res.get('metadata') or {}).get('source_commit')
                if isinstance(raw_sc, str):
                    sc_parsed = _parse_sha(raw_sc)
                    if sc_parsed:
                        source_commit = sc_parsed
            enriched_versions.append({
                'version_id': vid,
                'percentage': pct,
                'source_commit': source_commit,
                'created_on': (v_res.get('metadata') or {}).get('created_on')
            })
        else:
            enriched_versions.append({
                'version_id': vid,
                'percentage': pct,
                'source_commit': None,
                'created_on': None
            })

    return {
        'success': True,
        'deployment_id': deployment_id,
        'versions': enriched_versions
    }


def evaluate_cloudflare_deployment(
    snapshot: dict,
    sha: str,
    environment: str = 'production',
    script_name: str = 'agent-hub'
) -> dict:
    """Evaluate requested SHA against Cloudflare deployment snapshot.
    Returns:
      {'state': 'deployed' | 'not_deployed' | 'unknown', 'sha': sha,
       'deployment': compact_ref, 'version': ..., 'traffic': ..., 'reason': ..., 'provenance': ...}
    """
    provenance = {
        'tool': 'cloudflare_worker',
        'sha': sha,
        'environment': environment,
        'script_name': script_name
    }
    if not snapshot.get('success'):
        reason = snapshot.get('reason') or 'cf_snapshot_failed'
        return {'state': 'unknown', 'sha': sha, 'reason': reason, 'provenance': provenance}

    deployment_id = snapshot.get('deployment_id', 'unknown')
    versions = snapshot.get('versions') or []
    active = [v for v in versions if v.get('percentage', 0) > 0]
    if not active:
        return {'state': 'unknown', 'sha': sha, 'reason': 'no_active_traffic', 'provenance': provenance}

    # Require explicit source_commit annotation for versions with positive traffic
    for v in active:
        if not v.get('source_commit'):
            return {'state': 'unknown', 'sha': sha, 'reason': 'missing_source_commit_annotation',
                    'provenance': provenance}

    active_shas = {v['source_commit'] for v in active}
    primary_version = active[0]['version_id']
    primary_pct = int(active[0]['percentage'])

    # Only full active traffic naming requested SHA is deployed
    if active_shas == {sha}:
        summary = f"{deployment_id} (version {primary_version}, {primary_pct}% traffic)"
        return {
            'state': 'deployed',
            'sha': sha,
            'deployment': sanitize(summary, 256),
            'version': primary_version,
            'traffic': primary_pct,
            'provenance': provenance
        }

    # Entirely known other active source -> not_deployed
    if sha not in active_shas:
        summary = f"{deployment_id} (version {primary_version}, {primary_pct}% traffic)"
        return {
            'state': 'not_deployed',
            'sha': sha,
            'deployment': sanitize(summary, 256),
            'version': primary_version,
            'traffic': primary_pct,
            'reason': 'different_active_commit',
            'provenance': provenance
        }

    # Mixed/partial traffic (some versions have sha, some have other shas)
    summary = f"{deployment_id} (mixed traffic)"
    return {
        'state': 'unknown',
        'sha': sha,
        'deployment': sanitize(summary, 256),
        'reason': 'mixed_traffic',
        'provenance': provenance
    }


def make_cloudflare_probe(
    probe_id: str,
    for_native_id: str,
    sha: str,
    credentials_file: str,
    script_name: str = 'agent-hub',
    environment: str = 'production',
    cwd: str | None = None,
    timeout_s: int = 15,
) -> dict:
    """Read-only helper to construct a valid Cloudflare Worker deployment probe entry."""
    entry = {
        'id': probe_id,
        'kind': 'deployment',
        'adapter': 'cloudflare_worker',
        'for_native_id': for_native_id,
        'sha': sha,
        'credentials_file': credentials_file,
        'script_name': script_name,
        'environment': environment,
        'timeout_s': timeout_s,
    }
    if cwd is not None:
        entry['cwd'] = cwd
    return entry


def collect_probes(spec: dict, runner: Callable | None = None,
                   http_transport: Callable | None = None) -> dict:
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
    if http_transport is None and 'http_transport' in spec:
        http_transport = spec['http_transport']

    cf_snapshots: dict[tuple[str, str, str], dict] = {}

    for probe in probes[:MAX_PROBES]:
        if not isinstance(probe, dict):
            skipped.append({'reason': 'invalid_probe_entry'})
            continue
        pid = str(probe.get('id') or 'unnamed')
        kind = probe.get('kind')
        adapter = probe.get('adapter')
        cwd = probe.get('cwd')
        native_id = probe.get('for_native_id')
        if kind not in KINDS or not native_id:
            skipped.append({'id': pid, 'reason': 'invalid_probe_entry'})
            continue

        timeout_s = probe.get('timeout_s', DEFAULT_TIMEOUT_S)
        try:
            timeout_s = int(timeout_s)
        except (TypeError, ValueError):
            timeout_s = DEFAULT_TIMEOUT_S
        timeout_s = max(1, min(timeout_s, 120))

        if kind == 'deployment' and adapter == 'cloudflare_worker':
            sha = str(probe.get('sha') or '')
            if len(sha) != 40 or not all(c in '0123456789abcdefABCDEF' for c in sha):
                skipped.append({'id': pid, 'reason': 'sha_required'})
                continue
            if cwd is not None and not realpath_under(str(cwd), repo_roots):
                skipped.append({'id': pid, 'reason': 'cwd_outside_allowlist'})
                continue
            cred_path = (probe.get('credentials_file') or probe.get('credentials_path')
                         or spec.get('credentials_file') or spec.get('cloudflare_credentials_file'))
            if not cred_path:
                skipped.append({'id': pid, 'reason': 'credentials_path_missing'})
                continue

            creds, cred_err = load_cloudflare_credentials(cred_path)
            env = str(probe.get('environment') or 'production')
            script_name = str(probe.get('script_name') or 'agent-hub')

            if creds is None:
                # Network/credential failures remain unknown, not not_deployed
                payload = {'state': 'unknown', 'sha': sha.lower()}
                observations.append({
                    'machine_id': machine_id,
                    'native_id': native_id,
                    'kind': 'probe_deploy',
                    'observed_at': observed_at,
                    'payload': payload
                })
                continue

            account_id = creds['account_id']
            cache_key = (account_id, script_name, str(cred_path))
            if cache_key not in cf_snapshots:
                cf_snapshots[cache_key] = fetch_cloudflare_deployment_snapshot(
                    account_id=account_id,
                    script_name=script_name,
                    api_token=creds['api_token'],
                    timeout_s=float(timeout_s),
                    http_transport=http_transport
                )
            snapshot = cf_snapshots[cache_key]

            evidence = evaluate_cloudflare_deployment(
                snapshot=snapshot,
                sha=sha.lower(),
                environment=env,
                script_name=script_name
            )
            payload = {'state': evidence['state'], 'sha': sha.lower()}
            if evidence.get('deployment'):
                payload['deployment'] = evidence['deployment']
            observations.append({
                'machine_id': machine_id,
                'native_id': native_id,
                'kind': 'probe_deploy',
                'observed_at': observed_at,
                'payload': payload
            })
            continue

        # Standard argv-based probes (git, gh, wrangler)
        if not cwd:
            skipped.append({'id': pid, 'reason': 'invalid_probe_entry'})
            continue
        if not realpath_under(str(cwd), repo_roots):
            skipped.append({'id': pid, 'reason': 'cwd_outside_allowlist'})
            continue
        argv, reason = check_argv(probe.get('argv'))
        if argv is None:
            skipped.append({'id': pid, 'reason': reason})
            continue

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


def main(argv: list | None = None, runner: Callable | None = None,
         http_transport: Callable | None = None) -> dict:
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
    result = collect_probes(spec, runner, http_transport)
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
