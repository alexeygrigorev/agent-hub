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
# Human metadata that the legacy seed/refresh scan must never send (docs/REPOS.md).
HUMAN = ('label', 'purpose', 'stack', 'key_folders', 'urls', 'aliases', 'allowed_machines')
CHECKOUT = ('workspace_root', 'checkout_status', 'branch', 'dirty_counts', 'disk_free_bytes', 'origin_url', 'cloned_on_demand')
# Hub2 PATCH /api/repos/{owner}/{name} fields. The legacy Hub1 human fields
# (label, stack, key_folders, urls, canonical, allowed_machines, portable) are
# not Hub2 fields and are refused by name so they can never be silently
# ignored by the server.
HUB2_FIELDS = ('canonical_url', 'purpose', 'status', 'duplicate_of', 'domains', 'aliases', 'metadata')
CREATE_REQUIRED = ('canonical_url',)
SCAN = ('dir', 'origin', 'org', 'default_branch', 'status', 'canonical', 'last_commit', 'workflows', 'github_url', 'flags')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
OWNER_NAME = re.compile(r'[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,100}\Z')
DOMAIN = re.compile(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z')
ALIAS = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?\Z')
LIST_PAGE_DEFAULT = 50
LIST_PAGE_MAX = 100
LIST_PAGE_BOUND = 1000


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
            _post_batch(path, batch)
            batch = []
        batch.append(repo)
    if batch: _post_batch(path, batch)


def _post_batch(path, batch):
    try:
        intake.request('POST', path, {'repos': batch})
    except intake.IntakeHTTPError as exc:
        if exc.status == 404 and exc.code == 'not_found':
            raise intake.IntakeInputError(
                'repos route not found on this Hub: Hub2 has no POST ' + path
                + ' route (docs/REPOS.md); register entries instead with: hub repos add <owner/name> --file <json>') from None
        raise


def validate_repo_name(name):
    if not isinstance(name, str) or not OWNER_NAME.fullmatch(name):
        raise intake.IntakeInputError('Invalid repository name: use owner/name with 1-80 and 1-100 characters of letters, digits, . _ -')
    return name


def validate_repo_fields(payload, name=None, creating=False):
    """Client-side mirror of the Hub2 PATCH validator, with loud field errors."""
    if not isinstance(payload, dict):
        raise intake.IntakeInputError('Repository fields must be one JSON object')
    unknown = sorted(set(payload) - set(HUB2_FIELDS))
    if unknown:
        raise intake.IntakeInputError('Unknown repository fields: ' + ', '.join(unknown)
                                      + '; Hub2 accepts: ' + ', '.join(HUB2_FIELDS)
                                      + '; legacy Hub1 fields (label, stack, key_folders, urls, canonical, allowed_machines) are not Hub2 fields')
    if creating:
        missing = [field for field in CREATE_REQUIRED if field not in payload]
        if missing:
            raise intake.IntakeInputError('Creating a registry entry requires: ' + ', '.join(missing)
                                          + '; for example: {"canonical_url":"https://github.com/owner/name"}')
    url = payload.get('canonical_url')
    if url is not None and (not isinstance(url, str) or not url.startswith('https://') or len(url) > 2048):
        raise intake.IntakeInputError('canonical_url must be an https:// URL of at most 2048 characters')
    purpose = payload.get('purpose')
    if purpose is not None and (not isinstance(purpose, str) or len(purpose) > 2000):
        raise intake.IntakeInputError('purpose must be a string of at most 2000 characters')
    status = payload.get('status')
    if status is not None and status not in ('active', 'archived', 'duplicate'):
        raise intake.IntakeInputError('status must be one of: active, archived, duplicate')
    duplicate_of = payload.get('duplicate_of')
    if status == 'duplicate' and (not isinstance(duplicate_of, str) or not duplicate_of):
        raise intake.IntakeInputError('status "duplicate" requires duplicate_of: the canonical entry name')
    if duplicate_of is not None:
        if status != 'duplicate':
            raise intake.IntakeInputError('duplicate_of requires status "duplicate"')
        if duplicate_of == name:
            raise intake.IntakeInputError('duplicate_of must differ from the entry name')
    domains = payload.get('domains')
    if domains is not None and (not isinstance(domains, list) or len(domains) > 32
                                or any(not isinstance(value, str) or not DOMAIN.fullmatch(value) for value in domains)):
        raise intake.IntakeInputError('domains must be a list of at most 32 valid hostnames')
    aliases = payload.get('aliases')
    if aliases is not None and (not isinstance(aliases, list) or len(aliases) > 64
                                or any(not isinstance(value, str) or len(value) > 180 or not ALIAS.fullmatch(value) for value in aliases)):
        raise intake.IntakeInputError('aliases must be a list of at most 64 names (letters, digits, . _ - with an optional /group)')
    metadata = payload.get('metadata')
    if metadata is not None and (not isinstance(metadata, dict)
                                 or len(json.dumps(metadata, ensure_ascii=False).encode()) > 4096):
        raise intake.IntakeInputError('metadata must be a JSON object of at most 4096 UTF-8 bytes')
    return payload


def list_repos(query, follow_all=True):
    """One page, or every page of the keyset listing (bounded walk with guards)."""
    params = dict(query)
    if follow_all:
        params.setdefault('limit', LIST_PAGE_DEFAULT)
    items, seen, cursors, after = [], set(), set(), None
    for _ in range(LIST_PAGE_BOUND):
        page_query = dict(params)
        if after:
            page_query['after'] = after
        page = intake.request('GET', BASE + ('?' + urlencode(page_query) if page_query else ''))
        if not isinstance(page, dict) or not isinstance(page.get('items'), list):
            raise intake.IntakeInputError('Unexpected repos list response; expected {"items": [...], "next": cursor}')
        for row in page['items']:
            name = row.get('canonical_name') if isinstance(row, dict) else None
            if not isinstance(name, str) or not name:
                raise intake.IntakeInputError('Unexpected repos list item: canonical_name missing')
            if name in seen:
                raise intake.IntakeInputError('Duplicate repos list item; retry the command')
            seen.add(name)
            items.append(row)
        after = page.get('next')
        if not follow_all or not after:
            return {'items': items, 'next': after, 'total': len(items)}
        if after in cursors:
            raise intake.IntakeInputError('Invalid repos list cursor; retry the command')
        cursors.add(after)
    raise intake.IntakeInputError('Repos list page bound exceeded')


def add_commands(sub):
    commands = sub.add_parser('repos', help='Private server-side repository registry').add_subparsers(dest='repos_cmd', required=True)
    listing = commands.add_parser('list', help='List registry entries; follows all pages unless --limit is given')
    listing.add_argument('--org')
    listing.add_argument('--host')
    listing.add_argument('--missing-on')
    listing.add_argument('--status', choices=('active', 'archived', 'duplicate'))
    listing.add_argument('--flagged', action='store_true')
    listing.add_argument('--limit', type=int, help=f'One page of this size (1-{LIST_PAGE_MAX}); returns the next cursor instead of walking')
    listing.add_argument('--after', help='Keyset cursor from the previous page\'s next')
    show = commands.add_parser('show')
    show.add_argument('name')
    add = commands.add_parser('add', help='Create (or update) one registry entry in one command; JSON object file with canonical_url')
    add.add_argument('name', help='owner/name as on GitHub')
    add.add_argument('--file', required=True, help='JSON object: canonical_url (required), purpose, status, duplicate_of, domains, aliases, metadata')
    edit = commands.add_parser('edit', help='Operator-only repository metadata for an existing entry; JSON object file')
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
        if getattr(args, 'after', None):
            query['after'] = args.after
        if getattr(args, 'limit', None) is not None:
            if not 1 <= args.limit <= LIST_PAGE_MAX:
                raise intake.IntakeInputError(f'--limit must be between 1 and {LIST_PAGE_MAX}')
            query['limit'] = args.limit
            return list_repos(query, follow_all=False)
        return list_repos(query)
    if args.repos_cmd == 'show':
        try:
            return intake.request('GET', BASE + '/' + quote(args.name, safe=''))
        except intake.IntakeHTTPError as exc:
            if exc.status == 404 and exc.code == 'repo_not_found':
                raise intake.IntakeInputError(f'repo_not_found: {args.name} is not in the registry; create it with: hub repos add <owner/name> --file <json>') from None
            raise
    if args.repos_cmd == 'add':
        payload = validate_repo_fields(json.loads(intake.read_file(args.file)), name=args.name, creating=True)
        validate_repo_name(args.name)
        return intake.request('PATCH', BASE + '/' + quote(args.name, safe=''), payload)
    if args.repos_cmd == 'edit':
        payload = validate_repo_fields(json.loads(intake.read_file(args.file)), name=args.name)
        validate_repo_name(args.name)
        try:
            intake.request('GET', BASE + '/' + quote(args.name, safe=''))
        except intake.IntakeHTTPError as exc:
            if exc.status == 404 and exc.code == 'repo_not_found':
                raise intake.IntakeInputError(f'repo_missing: {args.name} is not registered; create it in one command with: hub repos add {args.name} --file <json> (requires "canonical_url")') from None
            raise
        return intake.request('PATCH', BASE + '/' + quote(args.name, safe=''), payload)
    if args.repos_cmd == 'seed':
        roots = scan_roots(args.root)
        if len(roots) != 1: raise ValueError('seed_requires_one_root')
        repos = seed_map(intake.read_file(args.file, limit=131072), roots[0], args.machine)
        post_batches(BASE + '/seed', repos)
        return dict(seeded=len(repos))
    roots = scan_roots(args.root)
    page = intake.request('GET', BASE + '?limit=1')
    if isinstance(page, dict) and isinstance(page.get('items'), list) and 'repos' not in page:
        raise intake.IntakeInputError('repos refresh is disabled on Hub2: the Hub2 API has no POST /api/repos/refresh route (docs/REPOS.md); '
                                      'register or update entries with: hub repos add <owner/name> --file <json>')
    existing = page.get('repos') if isinstance(page, dict) else None
    if not isinstance(existing, list):
        raise intake.IntakeInputError('Unexpected registry response for repos refresh; expected the legacy {"repos": [...]} shape')
    repos = scan_all(roots, args.machine, existing)
    post_batches(BASE + '/refresh', repos)
    return dict(at=dt.datetime.now(dt.timezone.utc).isoformat(), scanned=len(repos),
                flagged=[dict(name=r['name'], flags=r['flags']) for r in repos if r['flags']])
