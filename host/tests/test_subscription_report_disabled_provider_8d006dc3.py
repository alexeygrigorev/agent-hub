"""No host-side subscription or quota report is sent to the Hub."""

from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet

try:
    from host.tests.test_fleet import settings
except ImportError:
    from test_fleet import settings


def test_subscription_report_path_is_disabled(tmp_path):
    calls = []
    dispatcher = fleet.Dispatcher(
        settings(Path(tmp_path)),
        lambda method, path, body=None: calls.append((method, path, body)),
        lambda *args, **kwargs: {},
    )
    with patch.object(fleet, 'collect_subscriptions', side_effect=AssertionError('host quota probe invoked')):
        assert dispatcher._report_subscriptions() == {'status': 'disabled', 'source': 'hub_server'}
    assert calls == []
