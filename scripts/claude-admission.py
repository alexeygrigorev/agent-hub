#!/usr/bin/env python3
"""Admission adapter for Claude Code engine and models."""
import datetime as dt
import json
import subprocess
import sys

r = json.load(sys.stdin)
ok = False
try:
    p = subprocess.run(['quse', 'claude', '--json'], capture_output=True, text=True, timeout=25)
    if p.returncode == 0:
        data = json.loads(p.stdout)
        q = data.get('claude', {})
        windows = q.get('windows', {})
        known = [v for v in windows.values() if isinstance(v, dict) and v.get('percent_remaining') is not None]
        ok = (
            q.get('status') == 'ok'
            and not q.get('error')
            and q.get('details', {}).get('limit_reached') is False
            and bool(known)
            and all(isinstance(v.get('percent_remaining'), (int, float)) and v['percent_remaining'] > 0 for v in known)
        )
except Exception:
    ok = False

selection = r.get('selection')
model = 'opus'
effort = 'high'
if selection is not None and isinstance(selection, dict):
    req_model = selection.get('model')
    if req_model in ('opus', 'sonnet', 'haiku', 'claude-opus', 'claude-sonnet', 'claude-haiku'):
        model = req_model
        effort = selection.get('effort')
    else:
        ok = False

result = {
    'launch_allowed': bool(ok),
    'profile_supported': bool(ok),
    'account_label': 'shared-claude',
    'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    'model_supported': bool(ok),
    'model': model,
    'effort': effort,
}
print(json.dumps(result))
