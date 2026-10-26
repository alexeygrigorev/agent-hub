import io
import json
import unittest
import urllib.error
from agent_hub_host import session_control


class AdmissionDiagnosticsTests(unittest.TestCase):
    def test_concurrency_hold_explains_healthy_quota_without_echoing_route_data(self):
        payload = {"error": "no_eligible_route", "rejected": [
            {"reason": "account_concurrency", "account": "private-account",
             "concurrency": {"active": 8, "limit": 8}, "quota_status": "ok"}]}
        error = urllib.error.HTTPError("https://hub.test/api/sessions", 409, "hold", {},
                                       io.BytesIO(json.dumps(payload).encode()))
        message = str(session_control.http_error(error))
        self.assertIn("account_concurrency", message)
        self.assertIn("configured account concurrency limit reached; quota is healthy", message)
        self.assertNotIn("private-account", message)
        self.assertNotIn("account_capacity", message)
