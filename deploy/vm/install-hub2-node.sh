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
CREDENTIAL_MONITOR_EVIDENCE_ROOT=$STATE/credential-monitor-evidence

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
  python3 - "$REPO/.dev.vars" "$RELEASE_MODE" "$SCHEMA_MIGRATIONS" "$REPO" "$CREDENTIAL_MONITOR_EVIDENCE_ROOT" <<'PY'
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ARTIFACTS = ("hub2/node/alert-evidence.ts", "hub2/node/main.ts", "hub2/src/worker.ts")

path = Path(sys.argv[1])
mode, schema_mode = sys.argv[2:4]
repo_arg = sys.argv[4] if len(sys.argv) >= 5 else ""
evidence_root_arg = sys.argv[5] if len(sys.argv) >= 6 else ""
if mode and mode not in {"bootstrap", "active"}:
    raise SystemExit("invalid release mode")
if schema_mode and schema_mode not in {"defer", "apply"}:
    raise SystemExit("invalid schema migration mode")
original_stat = os.lstat(path)
if not stat.S_ISREG(original_stat.st_mode):
    raise SystemExit("invalid .dev.vars file")
updates = {}
if mode:
    updates["HUB2_RELEASE_MODE"] = mode
if schema_mode:
    updates["HUB2_SCHEMA_MIGRATIONS"] = schema_mode


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]


def _mkdir_as_service_uid(parent_fd: int, name: str, owner_uid: int, owner_gid: int) -> None:
    old_umask = os.umask(0o077)
    switched = False
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if os.geteuid() == 0 and (owner_uid != 0 or owner_gid != 0):
            if libc.prctl(8, 1, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "prctl PR_SET_KEEPCAPS failed")
            os.setegid(owner_gid)
            os.seteuid(owner_uid)
            switched = True
            hdr = _CapHeader(0x20080522, 0)
            data = (_CapData * 2)()
            # Keep only CAP_DAC_OVERRIDE (bit 1) while creating the service-owned directory inside root-owned parent.
            data[0].effective = 1 << 1
            data[0].permitted = 1 << 1
            data[0].inheritable = 0
            if libc.capset(ctypes.byref(hdr), ctypes.byref(data)) != 0:
                raise OSError(ctypes.get_errno(), "capset CAP_DAC_OVERRIDE failed")
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    finally:
        if switched:
            try:
                os.seteuid(0)
                os.setegid(0)
            finally:
                libc.prctl(8, 0, 0, 0, 0)
        os.umask(old_umask)


def _publish_directory(parent_fd: int, staging: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ValueError("renameat2 unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(staging), parent_fd, os.fsencode(target), 1) != 0:
        raise OSError(ctypes.get_errno(), "exclusive evidence root publication refused")


def ensure_private_evidence_root(target_root: Path, owner_uid: int, owner_gid: int) -> None:
    raw = os.fspath(target_root)
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw or "\x00" in raw or len(target_root.parts) < 2:
        raise ValueError("invalid evidence root path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target_root.parent.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            if info.st_uid not in (0, owner_uid) or (info.st_mode & 0o002):
                raise ValueError("untrusted evidence root ancestor")
        leaf = target_root.name
        expected_identity = None
        try:
            linfo = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            scan_fd = os.dup(fd)
            try:
                with os.scandir(scan_fd) as entries:
                    existing_inodes = {entry.inode() for entry in entries}
            finally:
                os.close(scan_fd)
            # Sleep across a kernel timestamp tick so any pre-existing directory renamed into
            # staging during a raced mkdir has st_mtime_ns < st_ctime_ns and is rejected.
            time.sleep(0.01)
            staging = ".stage-root-" + uuid.uuid4().hex
            staging_fd = None
            try:
                _mkdir_as_service_uid(fd, staging, owner_uid, owner_gid)
                slinfo = os.stat(staging, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(slinfo.st_mode):
                    raise ValueError("staging evidence root must be a real directory")
                staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                sinfo = os.fstat(staging_fd)
                if (
                    not stat.S_ISDIR(sinfo.st_mode)
                    or (slinfo.st_dev, slinfo.st_ino) != (sinfo.st_dev, sinfo.st_ino)
                    or sinfo.st_ino in existing_inodes
                    or sinfo.st_uid != owner_uid
                    or sinfo.st_gid != owner_gid
                    or stat.S_IMODE(sinfo.st_mode) != 0o700
                    or sinfo.st_nlink != 2
                    or sinfo.st_mtime_ns != sinfo.st_ctime_ns
                    or os.listdir(staging_fd)
                ):
                    raise ValueError("staging evidence root failed provenance verification")
                os.fsync(staging_fd)
                _publish_directory(fd, staging, leaf)
                os.fsync(fd)
                expected_identity = (sinfo.st_dev, sinfo.st_ino)
            finally:
                if staging_fd is not None:
                    os.close(staging_fd)
            linfo = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISDIR(linfo.st_mode):
            raise ValueError("evidence root must be a 0700 directory")
        ev_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            finfo = os.fstat(ev_fd)
            if not stat.S_ISDIR(finfo.st_mode) or (linfo.st_dev, linfo.st_ino) != (finfo.st_dev, finfo.st_ino):
                raise ValueError("evidence root identity mismatch")
            if expected_identity is not None and (finfo.st_dev, finfo.st_ino) != expected_identity:
                raise ValueError("evidence root replacement detected")
            if finfo.st_uid != owner_uid or finfo.st_gid != owner_gid or stat.S_IMODE(finfo.st_mode) != 0o700:
                raise ValueError("evidence root must be a 0700 directory owned by service user")
        finally:
            os.close(ev_fd)
    finally:
        os.close(fd)


if mode == "active" and (repo_arg or evidence_root_arg):
    if not repo_arg or not evidence_root_arg:
        raise SystemExit("credential_monitor_staging_failed")
    try:
        repo = Path(repo_arg)
        evidence_root = Path(evidence_root_arg)
        owner_uid, owner_gid = original_stat.st_uid, original_stat.st_gid
        ensure_private_evidence_root(evidence_root, owner_uid, owner_gid)
        git_env = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
        head_proc = subprocess.run(
            ["/usr/bin/git", "--no-optional-locks", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            env=git_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if head_proc.returncode != 0 or len(head_proc.stdout) > 64:
            raise ValueError("unable to resolve checkout HEAD")
        expected_commit = head_proc.stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
            raise ValueError("invalid checkout HEAD")
        hashes = {}
        for rel in ARTIFACTS:
            artifact_path = repo / rel
            st = os.lstat(artifact_path)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or (st.st_mode & 0o022):
                raise ValueError(f"untrusted authority artifact: {rel}")
            hashes[rel] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        destination = evidence_root / f"{expected_commit}-{uuid.uuid4().hex}"

        def _drop_privileges():
            if os.geteuid() == 0 and (owner_uid != 0 or owner_gid != 0):
                os.setgroups([owner_gid])
                os.setresgid(owner_gid, owner_gid, owner_gid)
                os.setresuid(owner_uid, owner_uid, owner_uid)

        stage_cmd = [
            sys.executable,
            "-I",
            "-S",
            str(repo / "scripts/stage-credential-monitor.py"),
            "--checkout",
            str(repo),
            "--destination",
            str(destination),
            "--expected-commit",
            expected_commit,
            "--observer-sha256",
            hashes["hub2/node/alert-evidence.ts"],
            "--main-sha256",
            hashes["hub2/node/main.ts"],
            "--worker-sha256",
            hashes["hub2/src/worker.ts"],
        ]
        stage_proc = subprocess.run(
            stage_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            preexec_fn=_drop_privileges if os.geteuid() == 0 and (owner_uid != 0 or owner_gid != 0) else None,
        )
        if stage_proc.returncode != 0:
            raise ValueError("stage-credential-monitor refused")
        receipt = json.loads(stage_proc.stdout.decode("utf-8"))
        if (
            not isinstance(receipt, dict)
            or receipt.get("kind") != "credential_monitor_staged_boundary_v1"
            or receipt.get("destination") != str(destination)
            or receipt.get("release_sha") != expected_commit
            or receipt.get("activated") is not False
            or not (destination / "enrollment.json").is_file()
            or not (destination / "journal.json").is_file()
        ):
            raise ValueError("invalid staged boundary receipt")
        updates["HUB2_ALERT_EVIDENCE_DIR"] = str(destination)
    except Exception as exc:
        raise SystemExit(f"credential_monitor_staging_failed: {exc}") from None

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
        if key == "HUB2_ALERT_EVIDENCE_DIR":
            updated.append(f"{key}={value}")
        else:
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

wait_for_hub2_readiness() {
  local attempt=0 curl_rc=0 health_body="" max_wait=15 start_seconds deadline remaining max_time
  if [[ "${AGENT_HUB_DEPLOY_TEST_MODE:-0}" == 1 && "${AGENT_HUB_TEST_READINESS_DEADLINE_S:-}" =~ ^[1-9][0-9]*$ ]]; then
    max_wait="$AGENT_HUB_TEST_READINESS_DEADLINE_S"
  fi
  start_seconds=$SECONDS
  deadline=$((start_seconds + max_wait))
  for ((attempt = 1; attempt <= 15; attempt++)); do
    if ((attempt > 1 && SECONDS >= deadline)); then
      attempt=$((attempt - 1))
      break
    fi
    if ! systemctl is-active --quiet agent-hub-hub2.service; then
      printf 'ERROR: agent-hub-hub2.service entered an inactive/failed state on readiness attempt %s/15.\n' "$attempt" >&2
      return 1
    fi
    remaining=$((deadline - SECONDS))
    if ((remaining < 1)); then
      remaining=1
    fi
    if ((remaining < 3)); then
      max_time=$remaining
    else
      max_time=3
    fi
    if health_body="$(curl -fsS --max-time "$max_time" http://127.0.0.1:8787/health 2>&1)"; then
      printf '%s\n' "$health_body"
      return 0
    else
      curl_rc=$?
    fi
    if ((attempt >= 15 || SECONDS >= deadline)); then
      break
    fi
    sleep 1
  done
  if ! systemctl is-active --quiet agent-hub-hub2.service; then
    printf 'ERROR: agent-hub-hub2.service entered an inactive/failed state after %s readiness attempts.\n' "$((attempt > 15 ? 15 : attempt))" >&2
    return 1
  fi
  printf 'ERROR: Hub2 local health check http://127.0.0.1:8787/health did not become ready after %s attempts / %ss (last curl exit=%s): %s\n' "$((attempt > 15 ? 15 : attempt))" "$((SECONDS - start_seconds))" "$curl_rc" "$health_body" >&2
  return 1
}

wait_for_hub2_readiness
echo
echo "VM setup complete; budget cron retired with the Cloudflare checkers."
