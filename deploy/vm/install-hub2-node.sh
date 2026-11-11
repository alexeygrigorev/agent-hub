#!/usr/bin/env bash
# Idempotent VM setup for the Node-based agent hub (no Cloudflare, no GCP SDK).
# Run ON the VM as a sudo-capable user, with sibling deploy/ files present:
#   agent-hub-hub2.service  hub.caddy
# Secrets: /opt/agent-hub/.dev.vars is preserved if present; on first run it is
# created with a fresh HUB2_CREDENTIAL_KEY (64 hex) and HUB2_BOOTSTRAP_TOKEN.
# Optional --release-mode updates only HUB2_RELEASE_MODE in the existing file.
# Budget envelopes are retired with the Cloudflare budget checkers.
set -euo pipefail

RELEASE_MODE=""
SCHEMA_MIGRATIONS=""
HUB_ONLY=false
KEEP_CLI_ASSETS=false
while (($#)); do
  case "$1" in
    --release-mode)
      if (($# < 2)); then
        echo "--release-mode requires bootstrap or active" >&2
        exit 2
      fi
      RELEASE_MODE="$2"
      shift 2
      ;;
    --schema-migrations)
      if (($# < 2)); then
        echo "--schema-migrations requires defer or apply" >&2
        exit 2
      fi
      SCHEMA_MIGRATIONS="$2"
      shift 2
      ;;
    --hub-only)
      HUB_ONLY=true
      shift
      ;;
    --keep-cli-assets)
      KEEP_CLI_ASSETS=true
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
if [[ -n "$RELEASE_MODE" && "$RELEASE_MODE" != bootstrap && "$RELEASE_MODE" != active ]]; then
  echo "--release-mode must be bootstrap or active" >&2
  exit 2
fi

if [[ -n "$SCHEMA_MIGRATIONS" && "$SCHEMA_MIGRATIONS" != defer && "$SCHEMA_MIGRATIONS" != apply ]]; then
  echo "--schema-migrations must be defer or apply" >&2
  exit 2
fi

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO=/opt/agent-hub
STATE=/var/lib/agent-hub
DB_DIR=$STATE/hub2-db

echo "== systemd units"
install -m 644 "$DEPLOY_DIR/agent-hub-hub2.service" /etc/systemd/system/agent-hub-hub2.service
systemctl disable --now agent-hub-worker.service agent-hub-cron.timer agent-hub-cron.service 2>/dev/null || true
rm -f /etc/systemd/system/agent-hub-worker.service /etc/systemd/system/agent-hub-cron.service /etc/systemd/system/agent-hub-cron.timer

if [[ "$HUB_ONLY" != true ]]; then
  echo "== Hub edge site (shared Caddyfile is owned by aws-infra)"
  CADDY_CONFIG=/etc/caddy/Caddyfile
  CADDY_SITE=/etc/caddy/sites/hub.caddy
  if [[ "${AGENT_HUB_DEPLOY_TEST_MODE:-0}" == 1 ]]; then
    CADDY_CONFIG="${AGENT_HUB_TEST_CADDY_CONFIG:?}"
    CADDY_SITE="${AGENT_HUB_TEST_CADDY_SITE:?}"
  fi
  if ! grep -Eq '^[[:space:]]*import[[:space:]]+/etc/caddy/sites/\*\.caddy[[:space:]]*$' "$CADDY_CONFIG"; then
    echo "shared Caddyfile must import /etc/caddy/sites/*.caddy; request aws-infra migration" >&2
    exit 1
  fi
  caddy validate --config "$DEPLOY_DIR/hub.caddy" --adapter caddyfile
  install -d -m 755 "$(dirname "$CADDY_SITE")"
  # The deploy transaction retains this site's previous contents for rollback.
  install -m 644 "$DEPLOY_DIR/hub.caddy" "$CADDY_SITE"
  caddy validate --config "$CADDY_CONFIG" --adapter caddyfile
else
  echo "== edge unchanged (Hub-only deploy)"
fi

echo "== state directories"
install -d -m 750 -o alexey -g alexey "$DB_DIR" "$REPO/.local/tmp"

if [[ "$KEEP_CLI_ASSETS" != true ]]; then
  echo "== public CLI artifacts"
  (cd "$REPO" && python3 scripts/build-hub-cli.py --out-dir hub2/public/cli)
fi

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

if [[ -n "$RELEASE_MODE" || -n "$SCHEMA_MIGRATIONS" ]]; then
  echo "== release configuration"
  python3 - "$REPO/.dev.vars" "$RELEASE_MODE" "$SCHEMA_MIGRATIONS" <<'PY'
import os
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
mode, schema_mode = sys.argv[2:4]
if mode and mode not in {"bootstrap", "active"}:
    raise SystemExit("invalid release mode")
if schema_mode and schema_mode not in {"defer", "apply"}:
    raise SystemExit("invalid schema migration mode")
updates = {}
if mode:
    updates["HUB2_RELEASE_MODE"] = mode
if schema_mode:
    updates["HUB2_SCHEMA_MIGRATIONS"] = schema_mode

original_stat = path.stat()
lines = path.read_text(encoding="utf-8").splitlines()
updated = []
inserted = set()
for line in lines:
    key, separator, _value = line.partition("=")
    key = key.strip()
    if separator and key in updates:
        if key not in inserted:
            updated.append(f"{key}={updates[key]}")
            inserted.add(key)
        continue
    updated.append(line)
for key, value in updates.items():
    if key not in inserted:
        updated.insert(0, f"{key}={value}")
contents = "\n".join(updated) + "\n"

fd, temporary_name = tempfile.mkstemp(prefix=".dev.vars.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as temporary:
        temporary.write(contents)
        temporary.flush()
        os.fsync(temporary.fileno())
    os.chown(temporary_name, original_stat.st_uid, original_stat.st_gid)
    os.chmod(temporary_name, stat.S_IMODE(original_stat.st_mode))
    os.replace(temporary_name, path)
except BaseException:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
    raise
PY
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
50 4 * * * root HUB_DB_BACKUP_ENV_FILE=/etc/hub2-db-backup-staleness.env /usr/local/sbin/hub2-db-backup-staleness.sh --no-mail >>/var/log/hub2-db-backup-staleness.log 2>&1
CRON
chmod 644 /etc/cron.d/hub2-db-backup

echo "== dns updater"
# Boot-time Route53 self-heal for the ephemeral VM IP (agent-hub-vm.dtcdev.click
# A record); the CloudFront origin resolves this name. Credentials are NOT in
# the repo: create /etc/agent-hub/dns-updater.env (0600, AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY of the scoped IAM user) on the VM separately. Without
# it the updater logs a warning and does nothing.
install -m 750 "$VM_DIR/hub2-dns-update.sh" /usr/local/sbin/hub2-dns-update.sh
install -m 644 "$VM_DIR/agent-hub-dns-update.service" /etc/systemd/system/agent-hub-dns-update.service
systemctl enable agent-hub-dns-update.service

echo "== services"
systemctl daemon-reload
systemctl enable agent-hub-hub2.service
systemctl restart agent-hub-hub2.service
if [[ "$HUB_ONLY" != true ]]; then
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

sleep 3
systemctl is-active --quiet agent-hub-hub2.service
curl -fsS --max-time 10 http://127.0.0.1:8787/health
echo
echo "VM setup complete; budget cron retired with the Cloudflare checkers."
