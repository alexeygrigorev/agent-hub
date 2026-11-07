#!/usr/bin/env bash
# Idempotent VM setup for the Node-based agent hub (no Cloudflare, no GCP SDK).
# Run ON the VM as a sudo-capable user, with sibling deploy/ files present:
#   agent-hub-hub2.service  Caddyfile
# Secrets: /opt/agent-hub/.dev.vars is preserved if present; on first run it is
# created with a fresh HUB2_CREDENTIAL_KEY (64 hex) and HUB2_BOOTSTRAP_TOKEN.
# Budget envelopes are retired with the Cloudflare budget checkers.
set -euo pipefail

# The service unit and Caddyfile live in deploy/, beside vm/.
STAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO=/opt/agent-hub
STATE=/var/lib/agent-hub
DB_DIR=$STATE/hub2-db

echo "== systemd units"
install -m 644 "$STAGING/agent-hub-hub2.service" /etc/systemd/system/agent-hub-hub2.service
systemctl disable --now agent-hub-worker.service agent-hub-cron.timer agent-hub-cron.service 2>/dev/null || true
rm -f /etc/systemd/system/agent-hub-worker.service /etc/systemd/system/agent-hub-cron.service /etc/systemd/system/agent-hub-cron.timer

echo "== edge (caddy :80 -> 127.0.0.1:8787; TLS terminates upstream)"
install -m 644 "$STAGING/Caddyfile" /etc/caddy/Caddyfile

echo "== state directories"
install -d -m 750 -o alexey -g alexey "$DB_DIR" "$REPO/.local/tmp"

echo "== secrets (.dev.vars, generated on this VM, never leave it)"
if [[ ! -f "$REPO/.dev.vars" ]]; then
  CRED_KEY="$(openssl rand -hex 32)"
  BOOTSTRAP_TOKEN="$(openssl rand -base64 36 | tr -d '=+/' | head -c 43)"
  {
    echo "HUB2_RELEASE_MODE=bootstrap"
    echo "HUB2_CREDENTIAL_KEY=$CRED_KEY"
    echo "HUB2_BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN"
  } > "$REPO/.dev.vars"
  chown alexey:alexey "$REPO/.dev.vars"
  chmod 600 "$REPO/.dev.vars"
else
  echo "   keeping existing .dev.vars"
fi

echo "== daily backup + staleness cron (no MTA on this VM; alerts via ALERT_CMD)"
install -m 750 scripts/hub2-db-backup.sh /usr/local/sbin/hub2-db-backup.sh
install -m 750 scripts/hub-db-backup-staleness.sh /usr/local/sbin/hub2-db-backup-staleness.sh
install -m 600 deploy/hub2-db-backup.env /etc/hub2-db-backup.env
install -m 600 deploy/hub2-db-backup-staleness.env /etc/hub2-db-backup-staleness.env
cat > /etc/cron.d/hub2-db-backup <<'CRON'
# agent-hub daily DB snapshot (root) — 04:20 CET-ish; logs to journal
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
20 4 * * * root /usr/local/sbin/hub2-db-backup.sh >>/var/log/hub2-db-backup.log 2>&1
50 4 * * * root /usr/local/sbin/hub2-db-backup-staleness.sh >>/var/log/hub2-db-backup-staleness.log 2>&1
CRON
chmod 644 /etc/cron.d/hub2-db-backup

echo "== dns updater"
# Boot-time Route53 self-heal for the ephemeral VM IP (agent-hub-vm.dtcdev.click
# A record); the CloudFront origin resolves this name. Credentials are NOT in
# the repo: create /etc/agent-hub/dns-updater.env (0600, AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY of the scoped IAM user) on the VM separately. Without
# it the updater logs a warning and does nothing.
install -m 750 "$STAGING/hub2-dns-update.sh" /usr/local/sbin/hub2-dns-update.sh
install -m 644 "$STAGING/agent-hub-dns-update.service" /etc/systemd/system/agent-hub-dns-update.service
systemctl enable agent-hub-dns-update.service

echo "== services"
systemctl daemon-reload
systemctl enable --now agent-hub-hub2.service
systemctl reload caddy 2>/dev/null || systemctl restart caddy

sleep 3
systemctl is-active --quiet agent-hub-hub2.service
curl -fsS --max-time 10 http://127.0.0.1:8787/health
echo
echo "VM setup complete; budget cron retired with the Cloudflare checkers."
