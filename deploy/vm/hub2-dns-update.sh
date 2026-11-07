#!/usr/bin/env bash
# Keep agent-hub-vm.dtcdev.click pointing at this VM's current external IP.
# Runs at boot (agent-hub-dns-update.service): the VM IP is ephemeral on GCP,
# and the CloudFront origin uses this DNS name, so a Route53 UPSERT after a
# stop/start restores the whole edge path. Credentials live in
# /etc/agent-hub/dns-updater.env (IAM user agent-hub-vm-dns, scoped to this
# one A record); without that file the script degrades to a no-op warning.
set -euo pipefail

ZONE_ID="${ZONE_ID:-Z05963572WVWFHDQZH5NE}"
RECORD_NAME="${RECORD_NAME:-agent-hub-vm.dtcdev.click}"
RECORD_TTL="${RECORD_TTL:-300}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

EXTERNAL_IP="$(curl -fsS --retry 5 --retry-delay 2 -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)"
CURRENT_DNS="$(dig +short "@8.8.8.8" "$RECORD_NAME" A 2>/dev/null | grep -E '^[0-9.]+$' | head -1 || true)"

if [ -z "$EXTERNAL_IP" ]; then
  echo "hub2-dns-update: metadata server returned no external IP" >&2
  exit 1
fi

if [ "$FORCE" != 1 ] && [ -n "$CURRENT_DNS" ] && [ "$CURRENT_DNS" = "$EXTERNAL_IP" ]; then
  echo "hub2-dns-update: $RECORD_NAME already -> $EXTERNAL_IP, nothing to do"
  exit 0
fi

if [ ! -f /etc/agent-hub/dns-updater.env ]; then
  echo "hub2-dns-update: WARNING no /etc/agent-hub/dns-updater.env; skipping Route53 update (IP $EXTERNAL_IP, DNS '${CURRENT_DNS:-none}')" >&2
  exit 0
fi
# shellcheck disable=SC1091
. /etc/agent-hub/dns-updater.env
: "${AWS_ACCESS_KEY_ID:?missing in /etc/agent-hub/dns-updater.env}"
: "${AWS_SECRET_ACCESS_KEY:?missing in /etc/agent-hub/dns-updater.env}"

CHANGE_XML="<ChangeResourceRecordSetsRequest xmlns=\"https://route53.amazonaws.com/doc/2013-04-01/\"><ChangeBatch><Changes><Change><Action>UPSERT</Action><ResourceRecordSet><Name>${RECORD_NAME}.</Name><Type>A</Type><TTL>${RECORD_TTL}</TTL><ResourceRecords><ResourceRecord><Value>${EXTERNAL_IP}</Value></ResourceRecord></ResourceRecords></ResourceRecordSet></Change></Changes></ChangeBatch></ChangeResourceRecordSetsRequest>"

RESPONSE="$(curl -fsS --retry 3 --retry-delay 2 -X POST \
  --aws-sigv4 "aws:amz:us-east-1:route53" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  -H "Content-Type: text/xml" \
  --data "$CHANGE_XML" \
  "https://route53.amazonaws.com/2013-04-01/hostedzone/${ZONE_ID}/rrset/")"
echo "hub2-dns-update: $RECORD_NAME -> $EXTERNAL_IP (was '${CURRENT_DNS:-unset}')"
echo "$RESPONSE" | grep -o '<Id>/Change/[^<]*</Id>' || true
