#!/usr/bin/env python3
"""Setup and maintenance helper for hub.dtcdev.click infrastructure in AWS Sandbox.

Operates only in AWS Account 817685572750.
Manages:
1. Cognito User Pool client (1tplqm9l6q6545mrk0gjv6eaeh) callbacks and logouts.
2. ACM Certificate in us-east-1 for hub.dtcdev.click with Route 53 DNS validation.
3. CloudFront distribution edge proxy for hub.dtcdev.click -> agent-hub.cloudflare-ai-agent-de9ca0.workers.dev.
4. Route 53 alias records (A and AAAA) in hosted zone Z05963572WVWFHDQZH5NE.
"""
import json
import os
import sys
import time
import boto3

EXPECTED_ACCOUNT = "817685572750"
HOSTED_ZONE_ID = "Z05963572WVWFHDQZH5NE"
DOMAIN_NAME = "hub.dtcdev.click"
USER_POOL_ID = "us-east-1_H7nJu52Bs"
CLIENT_ID = "1tplqm9l6q6545mrk0gjv6eaeh"
WORKER_ORIGIN = "agent-hub.cloudflare-ai-agent-de9ca0.workers.dev"


def assert_sts():
    sts = boto3.client("sts")
    ident = sts.get_caller_identity()
    account = ident.get("Account")
    if account != EXPECTED_ACCOUNT:
        raise RuntimeError(f"ABORT: Current AWS account is {account}, expected {EXPECTED_ACCOUNT}")
    print(f"[STS] Verified Account {account} (Arn: {ident.get('Arn')})")
    return ident


def update_cognito():
    assert_sts()
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    resp = cognito.describe_user_pool_client(
        UserPoolId=USER_POOL_ID,
        ClientId=CLIENT_ID
    )
    client_info = resp["UserPoolClient"]
    current_callbacks = set(client_info.get("CallbackURLs", []))
    current_logouts = set(client_info.get("LogoutURLs", []))

    new_callbacks = sorted(list(current_callbacks | {f"https://{DOMAIN_NAME}/auth/callback"}))
    new_logouts = sorted(list(current_logouts | {f"https://{DOMAIN_NAME}/"}))

    print(f"[Cognito] Current Callbacks: {list(current_callbacks)}")
    print(f"[Cognito] New Callbacks:     {new_callbacks}")
    print(f"[Cognito] Current Logouts:   {list(current_logouts)}")
    print(f"[Cognito] New Logouts:       {new_logouts}")

    if current_callbacks == set(new_callbacks) and current_logouts == set(new_logouts):
        print("[Cognito] Callbacks and Logouts already up-to-date.")
        return client_info

    upd_resp = cognito.update_user_pool_client(
        UserPoolId=USER_POOL_ID,
        ClientId=CLIENT_ID,
        ClientName=client_info.get("ClientName", "agent-hub"),
        RefreshTokenValidity=client_info.get("RefreshTokenValidity", 30),
        AccessTokenValidity=client_info.get("AccessTokenValidity", 60),
        IdTokenValidity=client_info.get("IdTokenValidity", 60),
        TokenValidityUnits=client_info.get("TokenValidityUnits", {}),
        SupportedIdentityProviders=client_info.get("SupportedIdentityProviders", ["Google"]),
        CallbackURLs=new_callbacks,
        LogoutURLs=new_logouts,
        AllowedOAuthFlows=client_info.get("AllowedOAuthFlows", ["code"]),
        AllowedOAuthScopes=client_info.get("AllowedOAuthScopes", ["email", "openid", "profile"]),
        AllowedOAuthFlowsUserPoolClient=client_info.get("AllowedOAuthFlowsUserPoolClient", True),
        PreventUserExistenceErrors=client_info.get("PreventUserExistenceErrors", "ENABLED"),
        EnableTokenRevocation=client_info.get("EnableTokenRevocation", True)
    )
    print("[Cognito] Updated successfully!")
    return upd_resp["UserPoolClient"]


def ensure_acm_certificate():
    assert_sts()
    acm = boto3.client("acm", region_name="us-east-1")
    r53 = boto3.client("route53")

    # Check existing certs
    paginator = acm.get_paginator("list_certificates")
    cert_arn = None
    for page in paginator.paginate(CertificateStatuses=["PENDING_VALIDATION", "ISSUED"]):
        for summary in page.get("CertificateSummaryList", []):
            if summary.get("DomainName") == DOMAIN_NAME:
                cert_arn = summary["CertificateArn"]
                print(f"[ACM] Found existing certificate {cert_arn} ({summary.get('Status')})")
                break
        if cert_arn:
            break

    if not cert_arn:
        print(f"[ACM] Requesting new certificate for {DOMAIN_NAME} in us-east-1...")
        resp = acm.request_certificate(
            DomainName=DOMAIN_NAME,
            ValidationMethod="DNS",
            IdempotencyToken="hubdtcdevclick",
            Tags=[
                {"Key": "Project", "Value": "agent-hub"},
                {"Key": "Environment", "Value": "sandbox"},
                {"Key": "ManagedBy", "Value": "agent-hub-domain-worker"}
            ]
        )
        cert_arn = resp["CertificateArn"]
        print(f"[ACM] Requested certificate ARN: {cert_arn}")

    # Wait for DNS validation records
    print("[ACM] Waiting for DNS validation options...")
    validation_records = []
    for _ in range(30):
        desc = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"]
        opts = desc.get("DomainValidationOptions", [])
        if opts and "ResourceRecord" in opts[0]:
            validation_records = [o["ResourceRecord"] for o in opts if "ResourceRecord" in o]
            break
        time.sleep(2)

    if not validation_records:
        raise RuntimeError("[ACM] Failed to obtain DNS validation record for certificate")

    # Upsert Route 53 DNS validation record
    for rec in validation_records:
        print(f"[Route53] Upserting validation record: {rec['Name']} {rec['Type']} -> {rec['Value']}")
        r53.change_resource_record_sets(
            HostedZoneId=HOSTED_ZONE_ID,
            ChangeBatch={
                "Comment": f"ACM DNS validation for {DOMAIN_NAME}",
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": rec["Name"],
                            "Type": rec["Type"],
                            "TTL": 300,
                            "ResourceRecords": [{"Value": rec["Value"]}]
                        }
                    }
                ]
            }
        )

    # Wait for certificate to be ISSUED
    print("[ACM] Waiting for certificate validation (Status=ISSUED)...")
    for _ in range(60):
        desc = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"]
        status = desc.get("Status")
        print(f"[ACM] Certificate status: {status}")
        if status == "ISSUED":
            print(f"[ACM] Certificate {cert_arn} is ISSUED and ready!")
            return cert_arn
        elif status == "FAILED":
            raise RuntimeError(f"[ACM] Certificate validation failed: {desc.get('FailureReason')}")
        time.sleep(5)

    raise RuntimeError("[ACM] Timed out waiting for certificate to be ISSUED")


def ensure_cloudfront_distribution(cert_arn: str):
    assert_sts()
    cf = boto3.client("cloudfront")

    # Check existing distribution by alias
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for dist in page.get("DistributionList", {}).get("Items", []):
            aliases = dist.get("Aliases", {}).get("Items", [])
            if DOMAIN_NAME in aliases:
                dist_id = dist["Id"]
                dist_domain = dist["DomainName"]
                print(f"[CloudFront] Found existing distribution {dist_id} ({dist_domain}) for {DOMAIN_NAME}")
                return dist_id, dist_domain

    print(f"[CloudFront] Creating CloudFront distribution for {DOMAIN_NAME} -> {WORKER_ORIGIN}...")
    caller_ref = f"agent-hub-domain-{int(time.time())}"
    config = {
        "CallerReference": caller_ref,
        "Aliases": {
            "Quantity": 1,
            "Items": [DOMAIN_NAME]
        },
        "DefaultRootObject": "",
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": "agent-hub-worker-backend",
                    "DomainName": WORKER_ORIGIN,
                    "OriginPath": "",
                    "CustomHeaders": {
                        "Quantity": 1,
                        "Items": [
                            {
                                "HeaderName": "X-Forwarded-Host",
                                "HeaderValue": DOMAIN_NAME
                            }
                        ]
                    },
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        "OriginProtocolPolicy": "https-only",
                        "OriginSslProtocols": {
                            "Quantity": 1,
                            "Items": ["TLSv1.2"]
                        },
                        "OriginReadTimeout": 30,
                        "OriginKeepaliveTimeout": 5
                    },
                    "ConnectionAttempts": 3,
                    "ConnectionTimeout": 10,
                    "OriginShield": {"Enabled": False}
                }
            ]
        },
        "OriginGroups": {"Quantity": 0},
        "DefaultCacheBehavior": {
            "TargetOriginId": "agent-hub-worker-backend",
            "TrustedSigners": {"Enabled": False, "Quantity": 0},
            "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 7,
                "Items": ["HEAD", "DELETE", "POST", "GET", "OPTIONS", "PUT", "PATCH"],
                "CachedMethods": {
                    "Quantity": 3,
                    "Items": ["HEAD", "GET", "OPTIONS"]
                }
            },
            "SmoothStreaming": False,
            "Compress": True,
            "LambdaFunctionAssociations": {"Quantity": 0},
            "FunctionAssociations": {"Quantity": 0},
            "FieldLevelEncryptionId": "",
            "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",       # CachingDisabled
            "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac", # AllViewer
            "GrpcConfig": {"Enabled": False}
        },
        "CacheBehaviors": {"Quantity": 0},
        "CustomErrorResponses": {"Quantity": 0},
        "Comment": "Agent Hub edge proxy for hub.dtcdev.click",
        "Logging": {
            "Enabled": False,
            "IncludeCookies": False,
            "Bucket": "",
            "Prefix": ""
        },
        "PriceClass": "PriceClass_100",
        "Enabled": True,
        "ViewerCertificate": {
            "CloudFrontDefaultCertificate": False,
            "ACMCertificateArn": cert_arn,
            "SSLSupportMethod": "sni-only",
            "MinimumProtocolVersion": "TLSv1.2_2021",
            "Certificate": cert_arn,
            "CertificateSource": "acm"
        },
        "Restrictions": {
            "GeoRestriction": {
                "RestrictionType": "none",
                "Quantity": 0
            }
        },
        "WebACLId": "",
        "HttpVersion": "http2and3",
        "IsIPV6Enabled": True
    }

    resp = cf.create_distribution(DistributionConfig=config)
    dist = resp["Distribution"]
    dist_id = dist["Id"]
    dist_domain = dist["DomainName"]
    print(f"[CloudFront] Created distribution {dist_id} ({dist_domain}) for {DOMAIN_NAME}")
    return dist_id, dist_domain


def ensure_route53_dns(dist_domain: str):
    assert_sts()
    r53 = boto3.client("route53")
    cf_hosted_zone = "Z2FDTNDATAQYW2"  # Standard CloudFront alias hosted zone ID

    # Create A and AAAA alias records
    for rtype in ["A", "AAAA"]:
        print(f"[Route53] Upserting {rtype} alias record: {DOMAIN_NAME} -> {dist_domain}")
        r53.change_resource_record_sets(
            HostedZoneId=HOSTED_ZONE_ID,
            ChangeBatch={
                "Comment": f"Alias record for {DOMAIN_NAME} to CloudFront",
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": DOMAIN_NAME,
                            "Type": rtype,
                            "AliasTarget": {
                                "HostedZoneId": cf_hosted_zone,
                                "DNSName": dist_domain,
                                "EvaluateTargetHealth": False
                            }
                        }
                    }
                ]
            }
        )
    print(f"[Route53] Alias records created for {DOMAIN_NAME}!")


if __name__ == "__main__":
    assert_sts()
    update_cognito()
    cert_arn = ensure_acm_certificate()
    dist_id, dist_domain = ensure_cloudfront_distribution(cert_arn)
    ensure_route53_dns(dist_domain)
    print(f"COMPLETE: {DOMAIN_NAME} -> CloudFront {dist_id} ({dist_domain}) -> {WORKER_ORIGIN}")
