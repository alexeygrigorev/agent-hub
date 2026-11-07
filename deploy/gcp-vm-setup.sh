#!/usr/bin/env bash
# Idempotent GCP VM setup for the agent-hub port (port-gcloud, 2026-11-07).
#
# Run ON the VM (dtcdev-click project, e2-small, Ubuntu 24.04) as root, with
# the sibling files staged in the same directory as this script:
#   agent-hub-worker.service  agent-hub-cron.service  agent-hub-cron.timer
#   Caddyfile  hub2-db-backup.sh  hub2-db-restore-drill.py
#   hub-db-backup-staleness.sh
#
# Prerequisites (one-time, outside this script):
#   apt-get install -y git sqlite3 rsync curl ca-certificates caddy nodejs
#   (Node 22 via nodesource; 2G swapfile on the 2 GB e2-small)
#   git clone https://github.com/alexeygrigorev/agent-hub.git /opt/agent-hub
#   npm ci in /opt/agent-hub (TMPDIR under the repo, never /tmp)
#
# Secrets: on first run this generates /opt/agent-hub/.dev.vars (mode 600)
# with a fresh HUB2_CREDENTIAL_KEY (64 hex), HUB2_BOOTSTRAP_TOKEN and the
# HUB2_BUDGET_ENVELOPES(_PART2) pair. Secrets are generated on the VM and
# never transit git or any other machine. Existing .dev.vars is preserved.
set -euo pipefail

STAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO=/opt/agent-hub
STATE=/var/lib/agent-hub

echo "== install systemd units"
install -m 644 "$STAGING/agent-hub-worker.service" /etc/systemd/system/agent-hub-worker.service
install -m 644 "$STAGING/agent-hub-cron.service" /etc/systemd/system/agent-hub-cron.service
install -m 644 "$STAGING/agent-hub-cron.timer" /etc/systemd/system/agent-hub-cron.timer

echo "== caddy edge (HTTP until DNS cutover, then ACME on the hostname)"
install -m 644 "$STAGING/Caddyfile" /etc/caddy/Caddyfile

echo "== secrets (.dev.vars, generated on this VM, never leave it)"
if [[ ! -f "$REPO/.dev.vars" ]]; then
  CRED_KEY="$(openssl rand -hex 32)"
  BOOTSTRAP_TOKEN="$(openssl rand -base64 36 | tr -d '=+/' | head -c 43)"
  ENVELOPES="$(python3 - <<'PYEOF'
import json
units = {
    "worker_requests": 100000, "worker_cpu_ms": 5000000, "do_requests": 6700,
    "do_ws_messages": 100000, "do_gb_ms": 50000000, "background_invocations": 5000,
    "sqlite_rows_read": 50000000, "sqlite_rows_written": 5000000,
    "sqlite_byte_days": 1000000, "kv_reads": 1000, "kv_writes": 1000,
    "kv_deletes": 1000, "kv_lists": 1000, "kv_byte_days": 1000,
}
def plan(name):
    return {"units": units, "max_do_calls": 64, "max_wall_ms": 60000,
            "provenance": f"GCP VM port initial {name} envelope (2026-11-07); sized for workerd local runtime on e2-small; operator must re-approve sizing before steady-state production"}
print(json.dumps({"version": 1, "normal": plan("normal"), "safety": plan("safety")}, separators=(",", ":")))
PYEOF
)"
  HALF=$(( ${#ENVELOPES} / 2 ))
  ENV1="${ENVELOPES:0:$HALF}"
  ENV2="${ENVELOPES:$HALF}"
  umask 077
  cat > "$REPO/.dev.vars" <<EOF
HUB2_RELEASE_MODE=bootstrap
HUB2_CREDENTIAL_KEY=$CRED_KEY
HUB2_BOOTSTRAP_TOKEN=$BOOTSTRAP_TOKEN
HUB2_BUDGET_ENVELOPES='$ENV1'
HUB2_BUDGET_ENVELOPES_PART2='$ENV2'
EOF
  chown alexey:alexey "$REPO/.dev.vars"
  echo "   wrote $REPO/.dev.vars (600); HUB2_BOOTSTRAP_TOKEN is inside"
else
  echo "   keeping existing $REPO/.dev.vars"
fi

echo "== ownership + state dirs"
chown -R alexey:alexey "$REPO"
mkdir -p "$STATE/do-state" /var/backups/hub-do /var/backups/hub-do-mirror /var/lib/hub2-db-backup
chown -R alexey:alexey "$STATE"
chmod 700 /var/lib/hub2-db-backup

echo "== backup tooling"
install -m 755 "$STAGING/hub2-db-backup.sh" /usr/local/sbin/hub2-db-backup.sh
install -m 755 "$STAGING/hub2-db-restore-drill.py" /usr/local/sbin/hub2-db-restore-drill.py
install -m 755 "$STAGING/hub-db-backup-staleness.sh" /usr/local/sbin/hub-db-backup-staleness.sh
if [[ ! -f /etc/hub2-db-backup.env ]]; then
  install -m 600 "$STAGING/hub2-db-backup.env" /etc/hub2-db-backup.env
fi
if [[ ! -f /etc/hub2-db-backup-staleness.env ]]; then
  install -m 600 "$STAGING/hub2-db-backup-staleness.env" /etc/hub2-db-backup-staleness.env
fi
cat > /etc/cron.d/hub2-db-backup <<'EOF'
# Daily hub2 DO SQLite snapshot (same schedule as the hub1 job on Hetzner).
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
20 4 * * * root  /usr/local/sbin/hub2-db-backup.sh >>/var/log/hub2-db-backup.log 2>&1
EOF

echo "== enable services"
systemctl daemon-reload
systemctl enable --now caddy agent-hub-worker.service agent-hub-cron.timer
systemctl reload caddy || systemctl restart caddy
