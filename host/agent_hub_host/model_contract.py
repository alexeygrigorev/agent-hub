"""Configured model contract shared by admission and native startup."""
import json
from importlib import resources
from pathlib import Path


def policy_models():
    try:
        return json.loads(resources.files(__package__).joinpath('model-routing.json').read_text())['models']
    except FileNotFoundError:
        return json.loads((Path(__file__).resolve().parents[2] / 'config/model-routing.json').read_text())['models']


def configured_selection(selection, engine, provider):
    if not isinstance(selection, dict):
        raise ValueError('model_unsupported: missing resolved model')
    family = {'antigravity': 'gemini', 'opencode': 'go', 'codex-orin': 'codex'}.get(provider, provider)
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
