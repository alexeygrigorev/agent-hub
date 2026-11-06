"""Unit tests for the additive-only Cloudflare billing policy tool."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloudflare-billing-policy.py"
SPEC = importlib.util.spec_from_file_location("cloudflare_billing_policy", SCRIPT)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class FakeAPI:
    def __init__(self, policies=None) -> None:
        self.policies = list(policies or [])
        self.created = []
        self.updated = []
        self.deleted = []
        self.readbacks = []

    def list_policies(self):
        return [dict(item) for item in self.policies]

    def create_policy(self, body):
        self.created.append(dict(body))
        item = {
            **body,
            "id": f"id-{len(self.created)}",
            "current_state": {
                "billing_period_start": "2026-11-01T00:00:00Z",
                "billing_period_end": "2026-12-01T00:00:00Z",
            },
        }
        self.policies.append(item)
        return dict(item)

    def get_policy(self, policy_id):
        self.readbacks.append(policy_id)
        return dict(next(item for item in self.policies if item["id"] == policy_id))


def matching(product, recipient="billing@example.test", policy_id="existing"):
    return {
        "id": policy_id,
        "name": "existing policy",
        "action": "notification",
        "enabled": True,
        "product_id": product,
        "threshold_cents": 50,
        "notification_email": recipient,
        "current_state": {
            "billing_period_start": "2026-11-01T00:00:00Z",
            "billing_period_end": "2026-12-01T00:00:00Z",
        },
    }


class CloudflareBillingPolicyTests(unittest.TestCase):
    def test_plan_is_read_only_and_reuses_existing_notification_recipient(self):
        api = FakeAPI([matching("pages")])
        report = policy.audit_or_apply(api, apply=False)
        self.assertEqual([item["state"] for item in report["policies"]], ["would_create", "would_create"])
        self.assertEqual({item["recipient"] for item in report["policies"]}, {"billing@example.test"})
        self.assertTrue(all(item["recipient_source"] == "existing_notification_policy" for item in report["policies"]))
        self.assertEqual(api.created, [])
        self.assertEqual(api.updated, [])
        self.assertEqual(api.deleted, [])

    def test_apply_creates_only_missing_notification_policies_and_reads_them_back(self):
        api = FakeAPI([matching("pages")])
        report = policy.audit_or_apply(api, apply=True)
        self.assertEqual([body["product_id"] for body in api.created], ["workers", "durable_objects"])
        self.assertTrue(all(body["action"] == "notification" and body["enabled"] for body in api.created))
        self.assertEqual(api.readbacks, ["id-1", "id-2"])
        self.assertEqual([item["state"] for item in report["policies"]], ["created", "created"])
        self.assertEqual(report["policies"][0]["period"]["start"], "2026-11-01T00:00:00Z")

    def test_exact_existing_policies_are_reused_without_mutation(self):
        api = FakeAPI([matching("workers"), matching("durable_objects", policy_id="existing-2")])
        report = policy.audit_or_apply(api, apply=True)
        self.assertEqual([item["state"] for item in report["policies"]], ["already_configured", "already_configured"])
        self.assertEqual(api.created, [])
        self.assertEqual(api.readbacks, ["existing", "existing-2"])
        self.assertEqual(api.updated, [])
        self.assertEqual(api.deleted, [])

    def test_product_specific_existing_recipients_are_preserved(self):
        api = FakeAPI([
            matching("workers", "workers@example.test"),
            matching("durable_objects", "objects@example.test", "existing-2"),
        ])
        report = policy.audit_or_apply(api, apply=False)
        self.assertEqual([item["recipient"] for item in report["policies"]], ["workers@example.test", "objects@example.test"])
        self.assertEqual([item["recipient_source"] for item in report["policies"]], [
            "existing_product_notification_policy", "existing_product_notification_policy",
        ])
        self.assertEqual(api.created, [])

    def test_product_recipient_wins_over_other_configured_recipient(self):
        api = FakeAPI([
            matching("workers", "workers@example.test"),
            matching("pages", "other@example.test", "existing-2"),
        ])
        self.assertEqual(
            policy.select_recipient(api.policies, "workers"),
            ("workers@example.test", "existing_product_notification_policy"),
        )
        with self.assertRaisesRegex(policy.PolicyError, "recipients for durable_objects are ambiguous"):
            policy.audit_or_apply(FakeAPI(api.policies), apply=True)

    def test_recipient_ambiguity_fails_without_creating_policies(self):
        api = FakeAPI([matching("pages"), matching("workers", "second@example.test", "other")])
        with self.assertRaisesRegex(policy.PolicyError, "recipients for durable_objects are ambiguous"):
            policy.audit_or_apply(api, apply=True)
        self.assertEqual(api.created, [])

    def test_missing_notification_recipient_fails_closed(self):
        api = FakeAPI()
        with self.assertRaisesRegex(policy.PolicyError, "No existing billing notification recipient"):
            policy.audit_or_apply(api, apply=True)
        self.assertEqual(api.created, [])

    def test_name_conflict_fails_without_touching_any_policy(self):
        api = FakeAPI([{
            **matching("workers"),
            "name": policy.POLICY_NAMES["workers"],
            "threshold_cents": 75,
        }])
        with self.assertRaisesRegex(policy.PolicyError, "already exists with different settings"):
            policy.audit_or_apply(api, apply=True)
        self.assertEqual(api.created, [])
        self.assertEqual(api.updated, [])
        self.assertEqual(api.deleted, [])

    def test_readback_mismatch_fails_acceptance(self):
        class BadReadback(FakeAPI):
            def get_policy(self, policy_id):
                item = super().get_policy(policy_id)
                item["action"] = "usage_block"
                return item

        api = BadReadback([matching("pages")])
        with self.assertRaisesRegex(policy.PolicyError, "did not match"):
            policy.audit_or_apply(api, apply=True)
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.updated, [])
        self.assertEqual(api.deleted, [])


if __name__ == "__main__":
    unittest.main()
