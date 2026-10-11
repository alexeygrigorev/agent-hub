#!/usr/bin/env python3
import sys
import math
import argparse
import json

# Official Sources:
# https://developers.cloudflare.com/durable-objects/platform/pricing/
# https://developers.cloudflare.com/workers/platform/limits/

# Cloudflare Workers Free Tier Limits
LIMITS = {
    "worker_requests_per_day": 100_000,
    "do_requests_per_day": 100_000,
    "do_duration_gb_s_per_day": 13_000,
    "sqlite_rows_read_per_day": 5_000_000,
    "sqlite_rows_written_per_day": 100_000,
    "storage_gb": 5,
}

# Estimates from SPEC.md (steady state)
ESTIMATES = {
    "worker_requests_per_day": 13_500,
    "do_requests_per_day": 14_000,
    "do_duration_gb_s_per_day": 36,
    "sqlite_rows_read_per_day": 14_000 * 10,
    "sqlite_rows_written_per_day": 9_500,
    "storage_gb": 0.1,
}

ALERT_THRESHOLD = 0.70

def check_usage(estimates=None, limits=None):
    if estimates is None:
        estimates = ESTIMATES
    if limits is None:
        limits = LIMITS

    print("Agent Hub Usage Estimator")
    print("=========================")
    print("Checking estimates against Cloudflare Workers Free limits (70% alert threshold).")
    print("Applying conservative uncertainty...\n")

    alert_triggered = False

    for key, limit in limits.items():
        if key not in estimates:
            print(f"ERROR: Missing estimate for {key}")
            sys.exit(1)
        
        estimate = estimates[key]
        if not isinstance(estimate, (int, float)) or not math.isfinite(estimate) or estimate < 0:
            print(f"ERROR: Invalid estimate for {key}: {estimate}")
            sys.exit(1)

        # Apply a conservative uncertainty multiplier of 1.5 to estimates
        conservative_estimate = estimate * 1.5
        ratio = conservative_estimate / limit
        
        status = "OK"
        if ratio >= ALERT_THRESHOLD:
            status = "ALERT"
            alert_triggered = True

        print(f"Metric: {key}")
        print(f"  Limit: {limit:,.2f}")
        print(f"  Base Estimate: {estimate:,.2f}")
        print(f"  Conservative Estimate (1.5x): {conservative_estimate:,.2f}")
        print(f"  Usage Ratio: {ratio:.1%}")
        print(f"  Status: {status}\n")

    if alert_triggered:
        print("WARNING: One or more metrics exceed the 70% threshold of the Free tier limits.")
        sys.exit(1)
    else:
        print("All estimates are well within safe margins.")
        return 0

def main():
    parser = argparse.ArgumentParser(description="Check Agent Hub usage estimates against Cloudflare limits.")
    parser.add_argument("--json-estimates", type=str, help="Path to JSON file with custom estimates to override defaults.")
    args = parser.parse_args()

    estimates = ESTIMATES.copy()
    if args.json_estimates:
        try:
            with open(args.json_estimates, 'r') as f:
                custom_estimates = json.load(f)
                if not isinstance(custom_estimates, dict):
                    print("ERROR: JSON file must contain an object (dictionary).")
                    sys.exit(1)
                estimates.update(custom_estimates)
        except Exception as e:
            print(f"ERROR: Failed to load JSON estimates: {e}")
            sys.exit(1)

    sys.exit(check_usage(estimates))

if __name__ == "__main__":
    main()
