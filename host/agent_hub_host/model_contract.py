"""Configured model contract shared by admission and native startup."""
import calendar
import json
import math
import re
import time
from importlib import resources
from pathlib import Path


def policy_models():
    return _policy_document()['models']


def reserve_percent(service):
    """Configured admission reserve percent for a provider (rules.reserve_percent).

    Single source of truth is config/model-routing.json. Raises on missing or
    malformed entries so admission gates can fail closed instead of silently
    widening the reserve.
    """
    rules = _policy_document().get('rules') or {}
    raw = (rules.get('reserve_percent') or {}).get(service)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 100:
        raise ValueError('reserve_percent_invalid')
    return float(raw)


def _policy_document():
    try:
        return json.loads(resources.files(__package__).joinpath('model-routing.json').read_text())
    except FileNotFoundError:
        return json.loads((Path(__file__).resolve().parents[2] / 'config/model-routing.json').read_text())


def model_disabled_by_policy(selection):
    values = [selection] if isinstance(selection, str) else [selection.get('id'), selection.get('model')] if isinstance(selection, dict) else []
    patterns = _policy_document().get('rules', {}).get('disabled_model_patterns', [])
    return any(isinstance(pattern, str) and pattern and
               any(isinstance(value, str) and pattern.casefold() in value.casefold() for value in values)
               for pattern in patterns)


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


def account_plan_end(account):
    """Return the account's last routable instant (UTC epoch seconds), or None.

    Catalog dates are inclusive: YYYY-MM-DD remains eligible through that UTC
    day and expires at the following midnight.
    """
    try:
        accounts = _policy_document().get('accounts') or []
    except Exception:
        return None
    if not isinstance(accounts, list):
        return None
    label = canonical_account_label(account)
    for entry in accounts:
        if not isinstance(entry, dict) or canonical_account_label(entry.get('account')) != label:
            continue
        raw = entry.get('plan_end')
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return None
        try:
            parsed = time.strptime(raw, '%Y-%m-%d')
        except ValueError:
            return None
        return calendar.timegm(parsed) + 86400
    return None


def account_plan_expired(account, now):
    end = account_plan_end(account)
    return end is not None and now >= end


def plan_end_horizon_days():
    raw = (_policy_document().get('rules') or {}).get('plan_end_reset_horizon_days')
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw <= 0:
        raise ValueError('plan_end_reset_horizon_days_invalid')
    return float(raw)


def plan_end_burn_deadline(account, now, horizon_days):
    """Return the plan deadline when it falls within the configured horizon."""
    end = account_plan_end(account)
    if end is None or end <= now or not math.isfinite(horizon_days) or horizon_days <= 0:
        return None
    return end if end - now <= horizon_days * 86400 else None


def configured_selection(selection, engine, provider):
    if not isinstance(selection, dict):
        raise ValueError('model_unsupported: missing resolved model')
    if model_disabled_by_policy(selection):
        raise ValueError('model_disabled_by_policy')
    family = {'antigravity': 'gemini', 'opencode': 'go',
              # Legacy route configs keyed by the old `codex-orin` provider
              # name still resolve to the canonical `codex` family.
              'codex-orin': 'codex'}.get(provider, provider)
    # Native ZCode uses engine=zcodex; managed ZAI uses codex/profile=zcodex.
    # The policy lists both shapes (glm-flash: codex, glm-flash-zcodex: zcodex);
    # mapping zcodex only to codex made the native entry unmatchable, so Hub's
    # native shared-zai route was refused as admission_refused (intake 8d006dc3).
    policy_engines = {engine, 'codex'} if family == 'zai' and engine == 'zcodex' else {engine}
    matches = [m for m in policy_models() if m['id'] == selection.get('id') and
               m['model'] == selection.get('model') and m['engine'] in policy_engines and
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
