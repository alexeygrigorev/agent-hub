#!/usr/bin/env bash
# Idempotent VM setup for the Node-based agent hub (no Cloudflare, no GCP SDK).
# Run ON the VM as a sudo-capable user, with sibling deploy/ files present:
#   agent-hub-hub2.service  Caddyfile
# Secrets: /opt/agent-hub/.dev.vars is preserved if present; on first run it is
# created with a fresh HUB2_CREDENTIAL_KEY (64 hex) and HUB2_BOOTSTRAP_TOKEN.
# Budget envelopes are retired with the Cloudflare budget checkers.
set -euo pipefail

STAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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

echo "== services"
systemctl daemon-reload
systemctl enable --now agent-hub-hub2.service
systemctl reload caddy 2>/dev/null || systemctl restart caddy

sleep 3
systemctl is-active --quiet agent-hub-hub2.service
curl -fsS --max-time 10 http://127.0.0.1:8787/health
echo
echo "VM setup complete; budget cron retired with the Cloudflare checkers."
