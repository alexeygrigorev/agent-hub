"""Subscription telemetry skips routing-disabled providers (intake 8d006dc3).

Hub maps PUT /api/fleet/subscriptions all-or-nothing (src/fleet.ts): one entry
without a fleet route for the machine returns 400
subscription_unmapped_or_invalid:unmapped for the whole batch. DESKTOP-FP4OP26
kept an operator entry for the routing-disabled Grok account, so every fleet
cycle logged fleet_telemetry_skipped and none of its 7 routed quota
observations reached Hub.
"""
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet

try:
    from host.tests.test_fleet import settings
except ImportError:
    from test_fleet import settings


def _subs():
    return [
        {'service': 'grok', 'account_label': 'shared-grok', 'engine': 'grok', 'profile': None},
        {'service': 'codex', 'account_label': 'shared-codex', 'engine': 'codex', 'profile': None},
        {'service': 'zai', 'account_label': 'shared-zai', 'engine': 'zcodex', 'profile': None},
    ]


def _run(tmp_path, disabled, capsys=None):
    probed, sent = [], []

    def collect(cfg, runner):
        probed.extend(s['account_label'] for s in cfg['subscriptions'])
        return [{'service': s['service'], 'account': s['account_label'], 'plan': None,
                 'quota': {'status': 'unknown', 'observed_at': None, 'windows': {}, 'details': {}}}
                for s in cfg['subscriptions']]

    base = settings(Path(tmp_path))
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]), \
            patch('agent_hub_host.native_admission.routing_rules', return_value={'disabled_providers': disabled}), \
            patch.object(fleet, 'collect_subscriptions', side_effect=collect):
        d = fleet.Dispatcher(base, lambda m, p, b: sent.append((m, p, b)) or {'status': 'reported'}, lambda *a: {})
        d.settings['subscriptions'] = _subs()
        d._report_subscriptions()
    return probed, sent, d


def test_disabled_provider_is_neither_probed_nor_reported(tmp_path, capsys):
    probed, sent, d = _run(tmp_path, ['grok'])
    assert probed == ['shared-codex', 'shared-zai']
    [(method, path, body)] = sent
    assert (method, path) == ('PUT', '/api/fleet/subscriptions')
    assert [s['account'] for s in body['subscriptions']] == ['shared-codex', 'shared-zai']
    assert '"fleet_subscription_skipped"' in capsys.readouterr().out
    # The dispatcher's own settings are not mutated (other paths still see the entry).
    assert [s['account_label'] for s in d.settings['subscriptions']] == ['shared-grok', 'shared-codex', 'shared-zai']


def test_reenabled_provider_is_reported_again(tmp_path, capsys):
    probed, sent, _ = _run(tmp_path, [])
    assert probed == ['shared-grok', 'shared-codex', 'shared-zai']
    assert [s['account'] for s in sent[0][2]['subscriptions']] == ['shared-grok', 'shared-codex', 'shared-zai']
    assert '"fleet_subscription_skipped"' not in capsys.readouterr().out
