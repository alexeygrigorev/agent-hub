"""Enrolled host resources and discovered git checkout observations. These tags never confer a grant."""
import re
from pathlib import Path

LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
RESOURCE = re.compile(r'(?:browser|tool|phone):[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


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
        path = Path(value).resolve(strict=True)
        if not path.is_dir() or not any(path.is_relative_to(root) for root in self.roots):
            raise ValueError('workspace_outside_allowlist')
        return path

    def clones(self):
        """Discover immediate git checkouts, including enrolled checkout roots.

        Do not traverse repository contents or widen the enrollment. Resolve
        symlinks on each observation and discard ambiguous basename labels.
        Explicit aliases retain precedence, including unavailable aliases.
        """
        candidates = {}
        for root in self.roots:
            try:
                paths = [root, *root.iterdir()]
            except OSError:
                continue
            for path in paths:
                if not LABEL.fullmatch(path.name):
                    continue
                try:
                    resolved = self.allowed_path(str(path))
                    if not (resolved / '.git').exists():
                        continue
                except (OSError, ValueError):
                    continue
                candidates.setdefault(path.name.casefold(), set()).add(resolved)
        return {label: next(iter(paths)) for label, paths in candidates.items() if len(paths) == 1}

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
        for label in self.workspaces:
            try:
                self.resolve_alias(label)
            except (OSError, ValueError):
                continue
            tags.append('workspace:' + label.casefold())
        explicit = {label.casefold() for label in self.workspaces}
        tags.extend('workspace:' + label for label in sorted(self.clones()) if label not in explicit)
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
