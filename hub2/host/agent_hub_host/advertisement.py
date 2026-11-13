"""Enrolled host resources and discovered git checkout observations. These tags never confer a grant."""
import re
from pathlib import Path
import stat

from .repository_identity import (MAX_CHECKOUT_CANDIDATES, MAX_ROOT_ENTRIES,
                                  QUALIFIED_WORKSPACE, canonical_repo_basename,
                                  read_git_origin, workspace_token_from_origin)

LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
RESOURCE = re.compile(r'(?:browser|tool|phone):[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
MAX_WORKSPACE_TAGS = 2048
INVENTORY_OVERFLOW_TAG = 'workspace:inventory-overflow'


class Advertisement:
    def __init__(self, roots, capabilities=None, workspaces=None):
        self.roots = [Path(root).resolve(strict=True) for root in roots]
        capabilities = [] if capabilities is None else capabilities
        workspaces = {} if workspaces is None else workspaces
        if (not isinstance(capabilities, list) or len(capabilities) > 32
                or any(not isinstance(tag, str) or not RESOURCE.fullmatch(tag) for tag in capabilities)):
            raise ValueError('invalid_resource_capabilities')
        if (not isinstance(workspaces, dict) or len(workspaces) > 32
                or any(not isinstance(label, str) or not LABEL.fullmatch(label) for label in workspaces)
                or len({label.casefold() for label in workspaces}) != len(workspaces)):
            raise ValueError('invalid_workspace_aliases')
        if any(not isinstance(path, str) or not Path(path).is_absolute()
               for path in workspaces.values()):
            raise ValueError('invalid_workspace_alias_path')
        # Alias identity is retained without probing the filesystem: a missing
        # or deleted directory stays enrolled, is omitted from heartbeat tags,
        # and refuses only launches that request it.
        self.capabilities = sorted(set(capabilities))
        self.workspaces = dict(workspaces)
        if len(self.capabilities) + len(self.workspaces) > 31:
            raise ValueError('advertisement_capacity_exceeded')

    def allowed_path(self, value):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('invalid_workspace_alias_path')
        try:
            path = Path(value).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError('workspace_unavailable') from None
        if not path.is_dir() or not any(path.is_relative_to(root) for root in self.roots):
            raise ValueError('workspace_outside_allowlist')
        return path

    def _observe(self):
        """Observe bounded immediate checkout metadata; never walk repo contents."""
        candidates = {}
        origins = {}
        scanned_entries = 0
        checkout_paths = set()
        overflow = False
        for root in self.roots:
            paths = [root]
            try:
                iterator = root.iterdir()
                for path in iterator:
                    scanned_entries += 1
                    if scanned_entries > MAX_ROOT_ENTRIES:
                        overflow = True
                        break
                    paths.append(path)
            except OSError:
                continue
            if overflow:
                break
            for path in paths:
                try:
                    resolved = self.allowed_path(str(path))
                    if not resolved.is_dir():
                        continue
                    marker = resolved / '.git'
                    marker_mode = marker.lstat().st_mode
                    if not (stat.S_ISDIR(marker_mode) or stat.S_ISREG(marker_mode)):
                        continue
                except (OSError, RuntimeError, ValueError):
                    continue
                if resolved in checkout_paths:
                    continue
                checkout_paths.add(resolved)
                if len(checkout_paths) > MAX_CHECKOUT_CANDIDATES:
                    overflow = True
                    break
                if LABEL.fullmatch(path.name):
                    candidates.setdefault(path.name.casefold(), set()).add(resolved)
                origin = read_git_origin(resolved, self.roots)
                if origin:
                    token = workspace_token_from_origin(origin)
                    origins.setdefault(token, {}).setdefault(origin, set()).add(resolved)
            if overflow:
                break
        clones = {label: next(iter(paths)) for label, paths in candidates.items() if len(paths) == 1}
        return clones, origins, overflow

    def clones(self):
        """Discover immediate labels; duplicate basenames remain ambiguous."""
        clones, _origins, overflow = self._observe()
        return {} if overflow else clones

    @staticmethod
    def _select_repository_path(origin, paths):
        basename = canonical_repo_basename(origin)
        ordered = sorted(paths, key=lambda path: (path.name.casefold() != (basename or '').casefold(), str(path)))
        return ordered[0] if ordered else None

    def resolve_repository(self, token):
        """Resolve a qualified token from a fresh full-origin observation.

        Tokens are reserved: a missing, stale, or digest-ambiguous token never
        falls through to a matching directory name or a root-relative path.
        """
        if not isinstance(token, str) or not QUALIFIED_WORKSPACE.fullmatch(token):
            raise ValueError('workspace_identity_unavailable')
        _clones, origins, overflow = self._observe()
        if overflow:
            raise ValueError('workspace_identity_unavailable')
        by_origin = origins.get(token, {})
        if len(by_origin) != 1:
            raise ValueError('workspace_identity_unavailable')
        origin, paths = next(iter(by_origin.items()))
        workspace = self._select_repository_path(origin, paths)
        if workspace is None:
            raise ValueError('workspace_identity_unavailable')
        # Re-resolve and re-read origin at launch time after deterministic choice;
        # tags are only observations and are never trusted as launch authority.
        try:
            current = self.allowed_path(str(workspace))
        except (OSError, ValueError):
            raise ValueError('workspace_identity_unavailable') from None
        if read_git_origin(current, self.roots) != origin or workspace_token_from_origin(origin) != token:
            raise ValueError('workspace_identity_unavailable')
        return current

    def resolve_alias(self, label):
        for name, path in self.workspaces.items():
            if name.casefold() == label.casefold():
                # Recheck symlinks and existence at every launch/heartbeat.
                # An enrolled but unavailable alias refuses only the launch
                # that requests it; basename lookup never applies to it.
                try:
                    return self.allowed_path(path)
                except OSError:
                    raise ValueError('workspace_unavailable') from None
        return self.clones().get(label.casefold())

    def tags(self):
        tags = list(self.capabilities)
        clones, origins, overflow = self._observe()
        explicit = {label.casefold() for label in self.workspaces}
        explicit_tags = []
        for label in self.workspaces:
            try:
                self.allowed_path(self.workspaces[label])
            except (OSError, RuntimeError, ValueError):
                continue
            explicit_tags.append('workspace:' + label.casefold())
        clone_tags = {'workspace:' + label for label in clones if label not in explicit}
        workspace_tags = set(explicit_tags) | clone_tags
        # A short digest is only an index. If two full origins share it, omit
        # the token rather than advertising an ambiguous target.
        qualified_tags = {'workspace:' + token for token, by_origin in origins.items() if len(by_origin) == 1}
        ambiguous_digest = any(len(by_origin) != 1 for by_origin in origins.values())
        incomplete = overflow or ambiguous_digest or len(workspace_tags | qualified_tags) > MAX_WORKSPACE_TAGS
        if not incomplete:
            tags.extend(sorted(workspace_tags | qualified_tags))
            return tags

        # Bare labels stay available for explicit workspace requests. Qualified
        # identities share the existing 2048 workspace-tag budget, so reserve
        # one marker slot and spend remaining capacity on legacy labels first.
        # If the inventory or tag budget is incomplete, omitted repo tokens are
        # not inferred from basenames; repo-only routing fails closed for them.
        available = max(0, MAX_WORKSPACE_TAGS - 1)
        selected = set(sorted(explicit_tags)[:available])
        remaining = available - len(selected)
        selected.update(sorted(clone_tags)[:remaining])
        remaining = available - len(selected)
        if not overflow:
            for tag in sorted(qualified_tags):
                if remaining <= 0:
                    break
                if tag not in selected:
                    selected.add(tag)
                    remaining -= 1
        if MAX_WORKSPACE_TAGS > 0:
            selected.add(INVENTORY_OVERFLOW_TAG)
        tags.extend(sorted(selected))
        return tags


def configure(config, capabilities, workspace_args):
    """Replace observations in an existing enrollment; never widen roots."""
    if not config.get('machine_id') or not config.get('roots'):
        raise ValueError('host_enrollment_required')
    workspaces = {}
    for item in workspace_args:
        label, separator, path = item.partition('=')
        if not separator or label in workspaces:
            raise ValueError('invalid_workspace_alias')
        workspaces[label] = path
    observed = Advertisement(config['roots'], capabilities, workspaces)
    return {**config, 'capabilities': observed.capabilities,
            'workspaces': {label: str(observed.allowed_path(path)) for label, path in workspaces.items()}}
