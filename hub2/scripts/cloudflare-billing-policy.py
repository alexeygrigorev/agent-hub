#!/usr/bin/env python3
"""Audit or add Hub2's notification-only Cloudflare billing backstops.

This tool never edits or deletes an existing policy. It only reuses recipients
on existing enabled billing notification policies; it never guesses from the
billing profile or creates a new recipient. Writes require --apply.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_ROOT = "https://api.cloudflare.com/client/v4"
PRODUCTS = ("workers", "durable_objects")
THRESHOLD_CENTS = 50
POLICY_NAMES = {
    "workers": "Hub2 Workers billing backstop",
    "durable_objects": "Hub2 Durable Objects billing backstop",
}
EMAIL = re.compile(r"^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$")


class PolicyError(RuntimeError):
    pass


class CloudflareAPI:
    def __init__(self, account_id: str, token: str) -> None:
        self.account_id = quote(account_id, safe="")
        self.token = token
        self.base = f"{API_ROOT}/accounts/{self.account_id}/billing"

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            raise PolicyError(f"Cloudflare billing API returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise PolicyError("Cloudflare billing API is unavailable") from None
        except (ValueError, json.JSONDecodeError):
            raise PolicyError("Cloudflare billing API returned invalid JSON") from None
        if not isinstance(result, dict) or result.get("success") is not True:
            errors = result.get("errors", []) if isinstance(result, dict) else []
            codes = [item.get("code") for item in errors if isinstance(item, dict) and isinstance(item.get("code"), int)]
            suffix = f" (Cloudflare error codes: {','.join(map(str, codes))})" if codes else ""
            raise PolicyError(f"Cloudflare billing API request failed{suffix}")
        return result

    def list_policies(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/spend-policies").get("result")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise PolicyError("Spend-policy list is missing or malformed")
        return result

    def create_policy(self, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", "/spend-policies", body).get("result")
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise PolicyError("Created spend policy has no identifier")
        return result

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        result = self.request("GET", f"/spend-policies/{quote(policy_id, safe='')}").get("result")
        if not isinstance(result, dict):
            raise PolicyError("Spend-policy read-back is missing or malformed")
        return result


def valid_email(value: Any) -> str | None:
    if isinstance(value, str) and EMAIL.fullmatch(value):
        return value.lower()
    return None


def select_recipient(policies: list[dict[str, Any]], product: str) -> tuple[str, str]:
    product_recipients = {
        email for policy in policies
        if policy.get("action") == "notification"
        and policy.get("enabled") is True
        and policy.get("product_id") == product
        if (email := valid_email(policy.get("notification_email")))
    }
    if len(product_recipients) == 1:
        return next(iter(product_recipients)), "existing_product_notification_policy"
    if len(product_recipients) > 1:
        raise PolicyError(f"Existing {product} notification recipients are ambiguous")

    configured = {
        email for policy in policies
        if policy.get("action") == "notification"
        and policy.get("enabled") is True
        if (email := valid_email(policy.get("notification_email")))
    }
    if len(configured) == 1:
        return next(iter(configured)), "existing_notification_policy"
    if not configured:
        raise PolicyError("No existing billing notification recipient is available")
    raise PolicyError(f"Existing billing notification recipients for {product} are ambiguous")


def validate_policy(policy: dict[str, Any], product: str, recipient: str) -> None:
    expected = {
        "action": "notification",
        "enabled": True,
        "product_id": product,
        "threshold_cents": THRESHOLD_CENTS,
        "notification_email": recipient,
    }
    for key, value in expected.items():
        actual = valid_email(policy.get(key)) if key == "notification_email" else policy.get(key)
        if actual != value:
            raise PolicyError(f"Cloudflare read-back did not match the requested {product} notification policy")


def audit_or_apply(api: Any, apply: bool) -> dict[str, Any]:
    policies = api.list_policies()
    recipients = {product: select_recipient(policies, product) for product in PRODUCTS}
    results: list[dict[str, Any]] = []

    for product in PRODUCTS:
        recipient, recipient_source = recipients[product]
        matches = [
            policy for policy in policies
            if policy.get("action") == "notification"
            and policy.get("enabled") is True
            and policy.get("product_id") == product
            and policy.get("threshold_cents") == THRESHOLD_CENTS
            and valid_email(policy.get("notification_email")) == recipient
        ]
        if len(matches) > 1:
            raise PolicyError(f"Multiple matching {product} policies exist; refusing to add or change any")

        if matches:
            policy = matches[0]
            state = "already_configured"
        else:
            if any(policy.get("name") == POLICY_NAMES[product] for policy in policies):
                raise PolicyError(f"A policy named {POLICY_NAMES[product]!r} already exists with different settings")
            if not apply:
                policy = {
                    "name": POLICY_NAMES[product],
                    "action": "notification",
                    "enabled": True,
                    "product_id": product,
                    "threshold_cents": THRESHOLD_CENTS,
                    "notification_email": recipient,
                }
                state = "would_create"
            else:
                policy = api.create_policy({
                    "name": POLICY_NAMES[product],
                    "action": "notification",
                    "enabled": True,
                    "product_id": product,
                    "threshold_cents": THRESHOLD_CENTS,
                    "notification_email": recipient,
                })
                if not isinstance(policy.get("id"), str):
                    raise PolicyError(f"Created {product} policy has no identifier")
                state = "created"

        if state in {"created", "already_configured"}:
            if not isinstance(policy.get("id"), str):
                raise PolicyError(f"The configured {product} policy has no identifier")
            policy = api.get_policy(policy["id"])
            validate_policy(policy, product, recipient)

        current = policy.get("current_state")
        if not isinstance(current, dict):
            current = {}
        results.append({
            "id": policy.get("id"),
            "product": product,
            "state": state,
            "action": "notification",
            "threshold_usd": THRESHOLD_CENTS / 100,
            "period": {
                "start": current.get("billing_period_start"),
                "end": current.get("billing_period_end"),
                "scope": "current Cloudflare billing period",
            },
            "current_balance_state": current.get("status", "unavailable"),
            "recipient": recipient,
            "recipient_source": recipient_source,
        })

    return {
        "scope": "account-level product billing, not Hub-attributed and not daily",
        "delivery_test": "Spend Policies API has no safe test-send operation; only configuration read-back was performed",
        "policies": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="create missing policies; never updates or deletes")
    args = parser.parse_args(argv)
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not account_id or not token:
        parser.error("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", account_id):
        parser.error("CLOUDFLARE_ACCOUNT_ID has an invalid format")

    try:
        report = audit_or_apply(CloudflareAPI(account_id, token), args.apply)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    recipient_values = {item["recipient"] for item in report["policies"]}
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Workflow logs are public for this repository. Mask existing billing
        # addresses before emitting the machine-readable verification result.
        for recipient in recipient_values:
            print(f"::add-mask::{recipient}")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
