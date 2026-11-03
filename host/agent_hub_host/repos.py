"""Private repository discovery. Uses git/gh argv only; never reads gh credentials."""
import datetime as dt
import json
import os
import shutil
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, urlencode
from . import intake

BASE = '/api/repos'
HUMAN = ('label', 'purpose', 'stack', 'key_folders', 'urls', 'aliases', 'allowed_machines')
CHECKOUT = ('workspace_root', 'checkout_status', 'branch', 'dirty_counts', 'disk_free_bytes', 'origin_url', 'cloned_on_demand')
EDITABLE = HUMAN + ('status', 'canonical', 'portable')  # portable: intake 178ec5bb
SCAN = ('dir', 'origin', 'org', 'default_branch', 'status', 'canonical', 'last_commit', 'workflows', 'github_url', 'flags')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def command(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('repo_command_failed')
    return result.stdout.strip()


def origin_name(value):
    match = re.fullmatch(r'(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', value)
    return match[1] if match else ''


def scan_single(path, machine, *, default_branch='main', cloned_on_demand=False, runner=command):
    """Report a completed checkout without scanning or changing sibling repos."""
    path = Path(path)
    if path.is_symlink() or path.resolve(strict=True) != path or not (path/'.git').is_dir() or (path/'.git').is_symlink():
        raise ValueError('repo_checkout_missing')
    origin = origin_name(runner(['git', '-C', str(path), 'remote', 'get-url', 'origin']))
    if not origin:
        raise ValueError('repo_origin_missing')
    row = dict(name=path.name, machine=machine, dir=str(path), workspace_root=str(path.parent),
               checkout_status='present', origin=origin, origin_url=origin, default_branch=default_branch,
               flags=[], cloned_on_demand=bool(cloned_on_demand), disk_free_bytes=shutil.disk_usage(path).free)
    try:
        commit = runner(['git', '-C', str(path), 'log', '-1', '--format=%H%n%cI']).splitlines()
        if len(commit) == 2:
            row['last_commit'] = {'sha':commit[0], 'date':commit[1]}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass  # Empty repositories have no HEAD; origin and checkout still exist.
    return row


def seed_map(text, root, machine):
    """Parse the 2026-10-12 map without interpreting its text as commands.

    Purpose / stack is a combined human cell: preserve it verbatim in both
    fields. Backticks and annotations in all human cells remain unchanged.
    """
    root = Path(root).expanduser().resolve()
    repos = {}
    headers = None
    org = ''
    section = ''
    duplicates = {}

    def add(name, **values):
        if not NAME.fullmatch(name):
            return
        repos.setdefault(name, dict(name=name, label=name, machine=machine, dir=str(root / name),
                                    origin='', org=org, purpose='', stack='', key_folders='', workflows='',
                                    urls='', default_branch='main', status='active', aliases=[], flags=[]))
        repos[name].update(values)

    for line in text.splitlines():
        if line.startswith('## '):
            section = line[3:]
            headers = None
        if line.startswith('### '):
            heading_org = re.search(r'org `([A-Za-z0-9_.-]+)`', line)
            org = heading_org[1] if heading_org else 'alexeygrigorev'
        if line.startswith('|'):
            cells = [c.strip() for c in line.strip('|').split('|')]
            if cells[0] in ('Dir', 'Group'):
                headers = cells
                continue
            if not headers or cells[0].startswith('---'):
                continue
            if headers[0] == 'Dir':
                data = dict(zip(headers, cells))
                name = data['Dir'].strip('`')
                origin = re.match(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)', data.get('Origin', ''))
                purpose = data.get('Purpose / stack', data.get('Purpose', ''))
                add(name, origin=origin[1] if origin else '', org=origin[1].split('/')[0] if origin else org,
                    purpose=purpose, stack=purpose if 'Purpose / stack' in data else '',
                    key_folders=data.get('Key folders', ''), workflows=data.get('Workflows (deploy)', data.get('Workflows', '')),
                    urls=data.get('URL', ''), default_branch='master' if '(master)' in data.get('Origin', '') else 'main')
            elif section.startswith('Other repos') and cells[0] != 'Not git / scratch':
                org = 'DataTalksClub' if cells[0].startswith('DTC') else 'alexeygrigorev'
                for match in re.finditer(r'`([^`]+)`(?:\s*\(([^)]+)\))?', cells[1]):
                    name, annotation = match.groups()
                    for item in ([name.replace('(-code)', ''), name.replace('(-code)', '-code')] if '(-code)' in name else [name]):
                        add(item)
                        if annotation:
                            remote = re.match(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', annotation)
                            if remote: repos[item].update(origin=remote[0], org=remote[0].split('/')[0])
                            elif 'no origin' in annotation: repos[item]['origin'] = ''
                            elif annotation.startswith('= website'): duplicates[item] = 'dtc-website'
                            elif re.fullmatch(r'[A-Za-z0-9_.-]+', annotation): repos[item]['org'] = annotation
        if line.startswith('Duplicates of the same origin:'):
            # Explicit relationships in the source, including the shared (= ...) pair.
            for name in ('ai-shipping-labs-workshops-content', 'ai-shipping-labs-workshops-raw'):
                add(name)
                duplicates[name] = 'workshops-content'
            add('aisl-content-1714')
            duplicates['aisl-content-1714'] = 'ai-shipping-labs-content'
            related = line.split('Related:', 1)[-1]
            for name in re.findall(r'`([^`]+)`', related):
                add(name)
        if section == 'Aliases' and line.startswith('- '):
            # A line may contain several mappings, including parenthetical
            # content aliases and semicolon-separated project aliases.
            mappings = re.sub(r'\([^()]*\)', lambda m: m[0] if '->' in m[0] else '', line)
            for mapping in re.finditer(r'([^;()]*?)\s*->\s*`([^`]+)`', mappings):
                source, target = mapping.groups()
                if target not in repos:
                    continue
                aliases = re.findall(r'"([^"]+)"', source)
                if not aliases:
                    alias = source.strip().removeprefix('- ').strip()
                    aliases = [alias] if alias else []
                repos[target]['aliases'].extend(a for a in aliases if a not in repos[target]['aliases'])
    masters = re.search(r'`master`: ([^)]+)', text)
    if masters:
        for name in masters[1].split(', '):
            if name.strip() in repos: repos[name.strip()]['default_branch'] = 'master'
    for name, target in duplicates.items():
        if name in repos:
            repos[name].update(status='duplicate', canonical=target, origin=repos[target]['origin'], org=repos[target]['org'])
    for repo in repos.values():
        path = Path(repo['dir'])
        if (path / '.git').is_dir() and not path.is_symlink():
            try:
                remote = origin_name(command(['git', '-C', str(path), 'remote', 'get-url', 'origin']))
                if remote: repo.update(origin=remote, origin_url=remote, org=remote.split('/')[0])
            except (ValueError, OSError, subprocess.TimeoutExpired):
                pass
    return list(repos.values())


def scan(root, machine, existing, runner=command):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir(): raise ValueError('invalid_repo_root')
    known = {}
    for r in existing:
        if 'checkouts' in r:
            checkout = next((c for c in r['checkouts'] if c.get('host_id', c.get('machine_id')) == machine), None)
            if not checkout: continue
            known[r['name']] = dict(r, machine=machine, dir=checkout.get('path', checkout.get('local_path')), flags=checkout.get('flags', []))
        elif r.get('machine') == machine:
            known[r['name']] = r
    discovered = []
    present = set()
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_dir() or not (path / '.git').is_dir() or (path / '.git').is_symlink() or not NAME.fullmatch(path.name):
            continue
        old = known.get(path.name)
        flags = list(old.get('flags', [])) if old else ['NEW']
        flags = [f for f in flags if f not in ('MISSING', 'WORKTREE', 'GH_ERROR', 'GIT_ERROR')]
        repo = dict(name=path.name, machine=machine, dir=str(path), workspace_root=str(root), checkout_status='present', disk_free_bytes=shutil.disk_usage(path).free, status=old.get('status', 'active') if old else 'active', flags=flags)
        try:
            remote = origin_name(runner(['git', '-C', str(path), 'remote', 'get-url', 'origin']))
        except (ValueError, OSError, subprocess.TimeoutExpired):
            remote = old.get('origin', '') if old else ''
        repo.update(origin=remote, origin_url=remote, org=remote.split('/')[0] if remote else (old.get('org', '') if old else ''))
        # A missing old directory plus the same origin at a new directory is a move.
        if not old and remote:
            moved = [r for r in known.values() if r.get('origin') == remote and not (Path(r['dir']) / '.git').is_dir() and r['status'] != 'duplicate']
            if len(moved) == 1:
                old = moved[0]
                repo.update(name=old['name'], status=old['status'], flags=list(dict.fromkeys([f for f in old.get('flags', []) if f not in ('MISSING', 'WORKTREE', 'GH_ERROR', 'GIT_ERROR')] + ['MOVED'])))
        if old and old['dir'] != str(path) and 'MOVED' not in repo['flags']: repo['flags'].append('MOVED')
        try:
            commit = runner(['git', '-C', str(path), 'log', '-1', '--format=%H%n%cI']).splitlines()
            repo['last_commit'] = dict(sha=commit[0], date=commit[1])
        except (ValueError, OSError, IndexError, subprocess.TimeoutExpired):
            repo['flags'].append('GIT_ERROR')
        repo['workflows'] = ', '.join(p.name for p in sorted((path / '.github' / 'workflows').glob('*')) if p.is_file() and p.suffix in ('.yml', '.yaml'))
        try:
            repo['branch'] = runner(['git', '-C', str(path), 'branch', '--show-current'])
            lines = runner(['git', '-C', str(path), 'status', '--porcelain=v1', '--untracked-files=normal']).splitlines()
            repo['dirty_counts'] = dict(staged=sum(len(x) > 1 and x[0] not in (' ', '?') for x in lines),
                                        unstaged=sum(len(x) > 1 and x[1] not in (' ', '?') for x in lines),
                                        untracked=sum(x.startswith('??') for x in lines))
        except (ValueError, OSError, subprocess.TimeoutExpired):
            repo['checkout_status'] = 'error'
            if 'GIT_ERROR' not in repo['flags']: repo['flags'].append('GIT_ERROR')
        if remote:
            try:
                view = json.loads(runner(['gh', 'repo', 'view', remote, '--json', 'defaultBranchRef,isArchived,url']))
                repo.update(default_branch=(view.get('defaultBranchRef') or {}).get('name') or '',
                            github_url=view['url'], status='duplicate' if old and old['status'] == 'duplicate' else 'archived' if view['isArchived'] else 'active')
            except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
                repo['flags'].append('GH_ERROR')
        if repo['status'] == 'duplicate': repo['canonical'] = old['canonical']
        present.add(repo['name'])
        discovered.append(repo)
    for name, old in known.items():
        if name in present or not old.get('dir') or Path(old['dir']).parent != root: continue
        flags = list(dict.fromkeys(old.get('flags', []) + ['MISSING']))
        if (Path(old['dir']) / '.git').is_file(): flags.append('WORKTREE')
        repo = {k: old[k] for k in ('name', 'machine', *SCAN) if k in old}
        repo['flags'] = list(dict.fromkeys(flags))
        repo['workspace_root'] = str(root)
        repo['checkout_status'] = 'worktree_only' if 'WORKTREE' in flags else 'missing'
        discovered.append(repo)
    return discovered


def scan_roots(explicit=None, settings_path=None):
    if explicit:
        values = explicit if isinstance(explicit, list) else [explicit]
    else:
        path = Path(settings_path) if settings_path else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'agent-hub' / 'subscriptions.json'
        try:
            settings = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict): raise ValueError('invalid_repo_roots')
        repos_config = settings.get('repos', {})
        if not isinstance(repos_config, dict): raise ValueError('invalid_repo_roots')
        if 'scan_roots' in repos_config:
            values = repos_config['scan_roots']
        else:
            values = settings.get('workspace_roots', [])
            if isinstance(values, list): values = [v for v in values if isinstance(v, str) and Path(v).name not in ('worktrees', 'tmp')]
    if not isinstance(values, list) or any(not isinstance(v, str) or not v or not Path(v).expanduser().is_absolute() for v in values):
        raise ValueError('invalid_repo_roots')
    roots = list(dict.fromkeys(str(Path(v).expanduser().resolve(strict=True)) for v in values))
    if not roots: raise ValueError('no_configured_repo_roots')
    if any(not Path(v).is_dir() for v in roots): raise ValueError('invalid_repo_roots')
    return roots


def scan_all(roots, machine, existing, runner=command):
    rows = {}
    for root in roots:
        for repo in scan(root, machine, existing, runner):
            key = repo['name'].lower()
            previous = rows.get(key)
            if not previous or repo['checkout_status'] not in ('missing', 'worktree_only'):
                if previous and previous['checkout_status'] == 'present' and repo['checkout_status'] == 'present' and previous['dir'] != repo['dir']:
                    raise ValueError('multiple_repo_checkouts_on_host')
                rows[key] = repo
    return list(rows.values())


def post_batches(path, repos):
    # Stay below the API body cap even for the longer verbatim seed cells.
    batch = []
    for repo in repos:
        if batch and (len(batch) == 25 or len(json.dumps({'repos': batch + [repo]}, ensure_ascii=False).encode()) > 30000):
            intake.request('POST', path, {'repos': batch})
            batch = []
        batch.append(repo)
    if batch: intake.request('POST', path, {'repos': batch})


def add_commands(sub):
    commands = sub.add_parser('repos', help='Private server-side repository registry').add_subparsers(dest='repos_cmd', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--org')
    listing.add_argument('--host')
    listing.add_argument('--missing-on')
    listing.add_argument('--status', choices=('active', 'archived', 'duplicate'))
    listing.add_argument('--flagged', action='store_true')
    show = commands.add_parser('show')
    show.add_argument('name')
    edit = commands.add_parser('edit', help='Operator-only repository metadata; JSON object file')
    edit.add_argument('name')
    edit.add_argument('--file', required=True)
    for action in ('seed', 'refresh'):
        cmd = commands.add_parser(action)
        cmd.add_argument('--root', action='append')
        cmd.add_argument('--machine', required=True)
        if action == 'seed': cmd.add_argument('--file', required=True)


def execute(args):
    if args.repos_cmd == 'list':
        query = {k: getattr(args, k) for k in ('org', 'status', 'host', 'missing_on') if getattr(args, k, None)}
        if args.flagged: query['flagged'] = 'true'
        return intake.request('GET', BASE + ('?' + urlencode(query) if query else ''))
    if args.repos_cmd == 'show': return intake.request('GET', BASE + '/' + quote(args.name, safe=''))
    if args.repos_cmd == 'edit':
        payload = json.loads(intake.read_file(args.file))
        if not isinstance(payload, dict) or set(payload) - set(EDITABLE): raise ValueError('invalid_repo_fields')
        allowed = payload.get('allowed_machines')
        if allowed is not None and (not isinstance(allowed, list) or len(allowed) > 256 or
                any(not isinstance(host, str) or not NAME.fullmatch(host) for host in allowed) or
                len(set(allowed)) != len(allowed)):
            raise ValueError('invalid_repo_fields')
        return intake.request('PATCH', BASE + '/' + quote(args.name, safe=''), payload)
    if args.repos_cmd == 'seed':
        roots = scan_roots(args.root)
        if len(roots) != 1: raise ValueError('seed_requires_one_root')
        repos = seed_map(intake.read_file(args.file, limit=131072), roots[0], args.machine)
        post_batches(BASE + '/seed', repos)
        return dict(seeded=len(repos))
    roots = scan_roots(args.root)
    existing = intake.request('GET', BASE)['repos']
    repos = scan_all(roots, args.machine, existing)
    post_batches(BASE + '/refresh', repos)
    return dict(at=dt.datetime.now(dt.timezone.utc).isoformat(), scanned=len(repos),
                flagged=[dict(name=r['name'], flags=r['flags']) for r in repos if r['flags']])
