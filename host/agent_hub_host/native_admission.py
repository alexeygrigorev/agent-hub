"""Fresh admission for explicitly configured native subscription accounts.

No credential import, managed install, execution or local quota probe: Hub
owns the quota verdict for every account (intake 5814e109 I4) and this module
asks it. Model selection is still validated locally against the policy.
The maintained launcher repeats resource admission before spawning.
"""
import argparse
import datetime as dt
import json
import sys
from importlib import resources
from pathlib import Path

from .model_contract import canonical_account_label, configured_selection, model_disabled_by_policy


def routing_rules():
    try:
        return json.loads(resources.files(__package__).joinpath('model-routing.json').read_text())['rules']
    except FileNotFoundError:
        return json.loads((Path(__file__).resolve().parents[2] / 'config/model-routing.json').read_text())['rules']


def hub_request(method, path, payload=None):
    from .provisioning import host_request
    return host_request(method, path, payload)


def probe(payload, service, account, engine, request=hub_request):
    now = dt.datetime.now(dt.timezone.utc)
    result = dict(launch_allowed=False, profile_supported=False, account_label=account,
                  observed_at=now.isoformat(), model_supported=False)
    if (not isinstance(payload, dict) or canonical_account_label(payload.get('account')) != canonical_account_label(account) or
            payload.get('engine') != engine or payload.get('profile') is not None or
            payload.get('mode') not in ('interactive', 'headless')):
        return result
    if any(model_disabled_by_policy(payload.get(key)) for key in ('selection', 'model', 'engine')):
        result['reason'] = 'model_disabled_by_policy'
        return result
    try:
        rules = routing_rules()
        # Grok remains disabled for execution. A fleet placement probe is read-only.
        if service in rules.get('disabled_providers', []):
            return result
        selection = payload.get('selection')
        if selection is not None:
            model = configured_selection(selection, engine, service)
            result.update(model_supported=True, model=model['model'], effort=model['effort'])
        verdict = request('POST', '/api/engines/admission', {'label': account, 'native': True})
        if not isinstance(verdict, dict) or verdict.get('account_label') != account:
            return result
        allowed = verdict.get('launch_allowed') is True
        result.update(launch_allowed=allowed, profile_supported=allowed)
        if verdict.get('reason'):
            result['reason'] = str(verdict['reason'])[:64]
        if verdict.get('quota_warning'):
            result['quota_warning'] = str(verdict['quota_warning'])[:64]
    except Exception:
        # Exceptions may contain provider or Hub response bodies. Emit fixed booleans.
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, choices=('codex', 'claude', 'zai', 'grok', 'gemini', 'go'))
    parser.add_argument('--account', required=True)
    parser.add_argument('--engine', required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.read(32769))
    except Exception:
        payload = None
    print(json.dumps(probe(payload, args.service, args.account, args.engine)))


if __name__ == '__main__':
    main()
