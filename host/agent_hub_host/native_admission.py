"""Fresh admission for explicitly configured native subscription accounts.

No credential import, managed install, or execution: quse reads native auth.
The maintained launcher repeats quota and resource admission before spawning.
"""
import argparse
import datetime as dt
import json
import math
import sys
from importlib import resources
from pathlib import Path

from .fleet import quota_metadata, run_json
from .model_contract import canonical_account_label, configured_selection


def routing_rules():
    try:
        return json.loads(resources.files(__package__).joinpath('model-routing.json').read_text())['rules']
    except FileNotFoundError:
        return json.loads((Path(__file__).resolve().parents[2] / 'config/model-routing.json').read_text())['rules']


def probe(payload, service, account, engine, runner=run_json):
    now = dt.datetime.now(dt.timezone.utc)
    result = dict(launch_allowed=False, profile_supported=False, account_label=account,
                  observed_at=now.isoformat(), model_supported=False)
    if (not isinstance(payload, dict) or canonical_account_label(payload.get('account')) != canonical_account_label(account) or
            payload.get('engine') != engine or payload.get('profile') is not None or
            payload.get('mode') not in ('interactive', 'headless')):
        return result
    rules = routing_rules()
    # Grok remains disabled for execution. A fleet placement probe is read-only.
    if service in rules.get('disabled_providers', []):
        return result
    try:
        selection = payload.get('selection')
        if selection is not None:
            model = configured_selection(selection, engine, service)
            result.update(model_supported=True, model=model['model'], effort=model['effort'])
        quota = quota_metadata(runner(['quse', service, '--json']), service)
        now = dt.datetime.now(dt.timezone.utc)
        observed = dt.datetime.fromisoformat(quota['observed_at'].replace('Z', '+00:00'))
        details = quota['details']
        if (quota['status'] != 'ok' or not 0 <= (now - observed).total_seconds() <= 1200 or
                details.get('limit_reached') is not False or details.get('stale') is True or
                (service == 'grok' and details.get('has_grok_code_access') is not True)):
            return result
        reserve = rules.get('reserve_percent', {}).get(service, 0)
        known = 0
        timed = 0
        for name, window in quota['windows'].items():
            if window.get('present') is False:
                if window.get('error') == 'unavailable':
                    return result
                continue
            remaining = window.get('percent_remaining')
            if type(remaining) not in (int, float) or not math.isfinite(remaining) or not reserve < remaining <= 100 or window.get('error'):
                return result
            reset = window.get('reset_at')
            inactive_five_hour = service in ('claude', 'zai') and name in ('5h', 'five_hour') and remaining == 100 and reset is None
            if not inactive_five_hour:
                reset = dt.datetime.fromisoformat(reset.replace('Z', '+00:00'))
                if reset.tzinfo is None or reset <= now:
                    return result
                timed += 1
            known += 1
        result.update(launch_allowed=known > 0 and timed > 0, profile_supported=known > 0 and timed > 0)
    except Exception:
        # Exceptions may contain provider response bodies. Emit fixed booleans.
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, choices=('claude', 'zai', 'grok'))
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
