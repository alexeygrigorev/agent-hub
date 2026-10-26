"""Configured model contract shared by admission and native startup."""
import json
from importlib import resources
from pathlib import Path


def policy_models():
    return _policy_document()['models']


def _policy_document():
    try:
        return json.loads(resources.files(__package__).joinpath('model-routing.json').read_text())
    except FileNotFoundError:
        return json.loads((Path(__file__).resolve().parents[2] / 'config/model-routing.json').read_text())


def account_aliases():
    """Canonical account label -> legacy alias labels (relabel tolerance).

    Single source of truth is config/model-routing.json `account_aliases`,
    also vendored into the host package by scripts/build-hub-cli.py. Missing
    or malformed entries fail closed to no aliases (exact match only).
    Colliding entries (one alias under two canonicals, or a canonical listed
    as another key's alias) raise: ambiguous identity must never route.
    """
    try:
        raw = _policy_document().get('account_aliases', {})
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    owners = {}
    for canonical, aliases in raw.items():
        if not isinstance(canonical, str) or not canonical:
            continue
        if not isinstance(aliases, list) or not all(isinstance(a, str) and a for a in aliases):
            continue
        if canonical in owners:
            raise ValueError('account aliases collide on ' + canonical)
        owners[canonical] = canonical
        kept = []
        for alias in aliases:
            if alias == canonical:
                continue
            if alias in owners:
                raise ValueError('account aliases collide on ' + alias)
            owners[alias] = canonical
            kept.append(alias)
        cleaned[canonical] = kept
    return cleaned


def canonical_account_label(label):
    """Map a legacy account alias to its canonical label (identity otherwise)."""
    if not isinstance(label, str):
        return label
    for canonical, aliases in account_aliases().items():
        if label == canonical or label in aliases:
            return canonical
    return label


def configured_selection(selection, engine, provider):
    if not isinstance(selection, dict):
        raise ValueError('model_unsupported: missing resolved model')
    family = {'antigravity': 'gemini', 'opencode': 'go',
              # Legacy route configs keyed by the old `codex-orin` provider
              # name still resolve to the canonical `codex` family.
              'codex-orin': 'codex'}.get(provider, provider)
    # Native ZCode uses engine=zcodex; managed ZAI uses codex/profile=zcodex.
    policy_engine = 'codex' if family == 'zai' and engine == 'zcodex' else engine
    matches = [m for m in policy_models() if m['id'] == selection.get('id') and
               m['model'] == selection.get('model') and m['engine'] == policy_engine and
               m['provider'] == family and m['effort'] == selection.get('effort')]
    if not matches:
        raise ValueError('model_unsupported: model is not configured for this route')
    return matches[0]


def replace_model_args(argv, model):
    """Replace all configured model flags before adding one authoritative flag."""
    result = []
    i = 0
    while i < len(argv):
        if argv[i] in ('--model', '-m'):
            i += 2
        elif argv[i].startswith('--model='):
            i += 1
        else:
            result.append(argv[i])
            i += 1
    return result + ['--model', model]


def native_model(command):
    if not isinstance(command, list) or any(not isinstance(x, str) for x in command):
        return None
    values = [command[i + 1] for i, x in enumerate(command[:-1]) if x in ('--model', '-m')]
    values += [x.split('=', 1)[1] for x in command if x.startswith('--model=')]
    return values[0] if values and len(set(values)) == 1 else None
