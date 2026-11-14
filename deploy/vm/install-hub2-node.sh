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
import json
import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


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
original_text = path.read_bytes().decode("utf-8")
updates = {}
if mode:
    updates["HUB2_RELEASE_MODE"] = mode
if schema_mode:
    updates["HUB2_SCHEMA_MIGRATIONS"] = schema_mode


def _publish_directory(src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ValueError("renameat2 unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(src_dir_fd, os.fsencode(src_name), dst_dir_fd, os.fsencode(dst_name), 1) != 0:
        raise OSError(ctypes.get_errno(), "exclusive evidence root publication refused")


# Public source mode repair is separate from evidence/history. Only selected
# Git bytes and trusted fixed source inodes may lose group/other write bits.
NODE_SOURCES = ('hub2/node/alert-evidence.ts', 'hub2/node/main.ts', 'hub2/src/worker.ts',
                'hub2/src/model-picker.ts', 'hub2/src/routing-pool.ts')
NODE_ROLES = ('credential_monitor', 'picker_excluded', 'picker_quota_stale')


class SourceModeRefused(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('selected_source_unverified')


def source_check(condition, reason):
    if not condition:
        raise SourceModeRefused(reason)


def source_fields(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def source_parent(path, owner):
    source_check(path.is_absolute() and os.path.normpath(path) == str(path), 'source_directory_untrusted')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = nxt
            info = os.fstat(fd)
            source_check(info.st_uid in (0, owner) and not info.st_mode & 0o002, 'source_directory_untrusted')
        return fd
    except BaseException:
        os.close(fd); raise


def source_read(fd, owner):
    info = os.fstat(fd)
    source_check(stat.S_ISREG(info.st_mode), 'source_file_not_regular')
    source_check(info.st_uid in (0, owner), 'source_file_foreign_owner')
    source_check(info.st_nlink == 1, 'source_file_linked')
    source_check(info.st_size <= 16 * 1024 * 1024, 'source_file_oversized')
    os.lseek(fd, 0, os.SEEK_SET)
    data = b''
    while len(data) <= info.st_size:
        chunk = os.read(fd, min(65536, info.st_size + 1 - len(data)))
        if not chunk: break
        data += chunk
    source_check(len(data) == info.st_size and source_fields(os.fstat(fd)) == source_fields(info), 'source_file_changed')
    return info, hashlib.sha256(data).hexdigest()


def source_git(repo, commit, relative, env, child_options):
    command = ['/usr/bin/git', '--no-optional-locks', '-C', str(repo), 'cat-file']
    size = subprocess.run(command + ['-s', commit + ':' + relative], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=5, check=False, **child_options)
    source_check(size.returncode == 0 and len(size.stdout) <= 32 and size.stdout.strip().isdigit(), 'source_git_unverified')
    count = int(size.stdout); source_check(count <= 16 * 1024 * 1024, 'source_file_oversized')
    result = subprocess.run(command + ['blob', commit + ':' + relative], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=5, check=False, **child_options)
    source_check(result.returncode == 0 and len(result.stdout) == count, 'source_git_unverified')
    return hashlib.sha256(result.stdout).hexdigest()


def source_head(repo, commit, env, child_options):
    result = subprocess.run(['/usr/bin/git', '--no-optional-locks', '-C', str(repo), 'rev-parse', '--verify', 'HEAD'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False, **child_options)
    source_check(result.returncode == 0 and result.stdout == (commit + '\n').encode(), 'source_git_unverified')


def source_recheck(handle, owner):
    parent, fd, name, expected_info, expected_hash = handle
    info, digest = source_read(fd, owner)
    source_check(source_fields(info) == source_fields(expected_info)
                 and source_fields(os.stat(name, dir_fd=parent, follow_symlinks=False)) == source_fields(info), 'source_file_changed')
    source_check(digest == expected_hash, 'source_hash_mismatch')
    source_check(not info.st_mode & 0o022, 'source_file_unsafe_mode')
    os.fsync(fd)  # Includes a retry after prior uncertain mode durability.


def source_close(handles):
    failures = {}
    for relative, (parent, fd, *_) in handles.items():
        for descriptor in (fd, parent):
            try: os.close(descriptor)
            except OSError:  # Never retry a possibly consumed FD.
                for role in (NODE_ROLES if NODE_SOURCES.index(relative) < 3 else NODE_ROLES[1:]):
                    failures.setdefault(role, 'source_file_unavailable')
    return failures


def harden_node_sources(repo, commit, owner, env, child_options):
    source_head(repo, commit, env, child_options)
    handles, failures = {}, {}
    for index, relative in enumerate(NODE_SOURCES):
        parent = fd = None
        try:
            expected_hash = source_git(repo, commit, relative, env, child_options)
            path = repo / relative
            parent = source_parent(path.parent, owner)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info, digest = source_read(fd, owner)
            source_check(digest == expected_hash, 'source_hash_mismatch')
            source_check(source_fields(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) == source_fields(info), 'source_file_changed')
            desired = stat.S_IMODE(info.st_mode) & ~0o022
            if stat.S_IMODE(info.st_mode) != desired:
                os.fchmod(fd, desired)  # ONLY already-opened verified public source inode.
            os.fsync(fd)
            after = os.fstat(fd)
            source_check((after.st_dev, after.st_ino, after.st_uid, after.st_gid, after.st_nlink, after.st_size, after.st_mtime_ns) ==
                         (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns)
                         and stat.S_IMODE(after.st_mode) == desired, 'source_file_changed')
            handle = (parent, fd, path.name, after, expected_hash)
            source_recheck(handle, owner)
            handles[relative] = handle; parent = fd = None
        except Exception as error:
            reason = error.reason if isinstance(error, SourceModeRefused) else 'source_file_unavailable'
            for role in (NODE_ROLES if index < 3 else NODE_ROLES[1:]):
                failures.setdefault(role, reason)
        finally:
            for descriptor in (fd, parent):
                if descriptor is not None:
                    try: os.close(descriptor)
                    except OSError:
                        for role in (NODE_ROLES if index < 3 else NODE_ROLES[1:]):
                            failures.setdefault(role, 'source_file_unavailable')
    try:
        source_head(repo, commit, env, child_options)
    except BaseException:
        source_close(handles); raise
    return handles, failures


def recheck_node_sources(handles, failures, repo, commit, owner, env, child_options):
    source_head(repo, commit, env, child_options)
    for relative, handle in handles.items():
        try:
            source_recheck(handle, owner)
        except Exception as error:
            reason = error.reason if isinstance(error, SourceModeRefused) else 'source_file_unavailable'
            for role in (NODE_ROLES if NODE_SOURCES.index(relative) < 3 else NODE_ROLES[1:]):
                failures.setdefault(role, reason)
    source_head(repo, commit, env, child_options)
    return failures



def ensure_private_evidence_root(target_root: Path, owner_uid: int, owner_gid: int) -> None:
    raw = os.fspath(target_root)
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw or "\x00" in raw or len(target_root.parts) < 2:
        raise ValueError("invalid evidence root path")
    caller_uid = os.geteuid()
    caller_gid = os.getegid()
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
            box = ".stage-box-" + uuid.uuid4().hex
            child_name = "evidence-root"
            box_fd = None
            child_fd = None
            box_identity = None
            try:
                old_umask = os.umask(0o077)
                try:
                    os.mkdir(box, 0o700, dir_fd=fd)
                finally:
                    os.umask(old_umask)
                blinfo = os.stat(box, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(blinfo.st_mode):
                    raise ValueError("staging container must be a real directory")
                box_fd = os.open(box, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                binfo = os.fstat(box_fd)
                if (
                    not stat.S_ISDIR(binfo.st_mode)
                    or (blinfo.st_dev, blinfo.st_ino) != (binfo.st_dev, binfo.st_ino)
                    or binfo.st_uid != caller_uid
                    or binfo.st_gid != caller_gid
                    or stat.S_IMODE(binfo.st_mode) != 0o700
                    or binfo.st_nlink != 2
                    or os.listdir(box_fd)
                ):
                    raise ValueError("staging container failed verification")
                box_identity = (binfo.st_dev, binfo.st_ino)
                old_umask = os.umask(0o077)
                try:
                    os.mkdir(child_name, 0o700, dir_fd=box_fd)
                finally:
                    os.umask(old_umask)
                clinfo = os.stat(child_name, dir_fd=box_fd, follow_symlinks=False)
                if not stat.S_ISDIR(clinfo.st_mode):
                    raise ValueError("staged evidence root must be a real directory")
                child_fd = os.open(child_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=box_fd)
                cinfo = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(cinfo.st_mode)
                    or (clinfo.st_dev, clinfo.st_ino) != (cinfo.st_dev, cinfo.st_ino)
                    or cinfo.st_uid != caller_uid
                    or cinfo.st_gid != caller_gid
                    or stat.S_IMODE(cinfo.st_mode) != 0o700
                    or cinfo.st_nlink != 2
                    or os.listdir(child_fd)
                    or os.listdir(box_fd) != [child_name]
                    or os.fstat(box_fd).st_nlink != 3
                ):
                    raise ValueError("staged evidence root failed verification")
                if (cinfo.st_uid, cinfo.st_gid) != (owner_uid, owner_gid):
                    os.fchown(child_fd, owner_uid, owner_gid)
                    cinfo = os.fstat(child_fd)
                cur_box = os.stat(box, dir_fd=fd, follow_symlinks=False)
                if (
                    (cur_box.st_dev, cur_box.st_ino) != box_identity
                    or cinfo.st_uid != owner_uid
                    or cinfo.st_gid != owner_gid
                    or stat.S_IMODE(cinfo.st_mode) != 0o700
                    or cinfo.st_nlink != 2
                    or os.listdir(child_fd)
                ):
                    raise ValueError("staged evidence root ownership verification failed")
                os.fsync(child_fd)
                _publish_directory(box_fd, child_name, fd, leaf)
                os.fsync(fd)
                expected_identity = (cinfo.st_dev, cinfo.st_ino)
            finally:
                # Retain the 0700 caller-owned .stage-box-<uuid> container as a deliberate
                # artifact rather than performing name-based directory removal under parent.
                if child_fd is not None:
                    os.close(child_fd)
                if box_fd is not None:
                    os.close(box_fd)
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


# Match Node parseDevVars: outer JS trim, paired quotes, last assignment wins.
OBSERVER_KEY = "HUB2_ALERT_EVIDENCE_DIR"
OBSERVER_KEYS = (OBSERVER_KEY, "HUB2_PICKER_EXCLUDED_EVIDENCE_DIR", "HUB2_PICKER_QUOTA_STALE_EVIDENCE_DIR")
JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def file_observer_value(text, target=OBSERVER_KEY):
    selected = ""
    for raw in text.split("\n"):
        line = raw.strip(JS_SPACE)
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip(JS_SPACE) != target:
            continue
        value = value.strip(JS_SPACE)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        selected = value
    return selected


def service_environment():
    # Never inspect sudo/caller environment as service authority. Refresh the
    # installed unit/drop-ins first; no running process is restarted here.
    def systemctl(*args):
        result = subprocess.run(["/usr/bin/systemctl", *args], env={"PATH": os.defpath, "LC_ALL": "C"},
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("observer_service_environment_unverified")
        return result.stdout.decode("utf-8")
    systemctl("daemon-reload")
    names = ("Environment", "EnvironmentFiles", "PassEnvironment", "UnsetEnvironment", "NeedDaemonReload")
    raw = systemctl("show", "agent-hub-hub2.service", *("--property=" + name for name in names))
    props, file_rows = {}, []
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in names:
            raise ValueError("observer_service_environment_unverified")
        if key == "EnvironmentFiles":
            file_rows.append(value)
        elif key in props:
            raise ValueError("observer_service_environment_unverified")
        else:
            props[key] = value
    # systemd255 omits this empty array and emits one row per populated item.
    # Scalar properties must still be present exactly once.
    if set(props) != set(names) - {"EnvironmentFiles"} or props["NeedDaemonReload"] != "no":
        raise ValueError("observer_service_environment_unverified")
    for name in ("PassEnvironment", "UnsetEnvironment"):
        if any(word.split("=", 1)[0] in OBSERVER_KEYS for word in shlex.split(props[name])):
            raise ValueError("observer_service_override_unsupported")
    entries = []
    for row in file_rows:
        files = row.strip()
        parsed = re.findall(r"([^\s]+) \(ignore_errors=(yes|no)\)", files)
        if (not files and len(file_rows) != 1
                or " ".join(f"{name} (ignore_errors={optional})" for name, optional in parsed) != files):
            raise ValueError("observer_service_environment_unverified")
        entries.extend(parsed)
    for name, optional in entries:
        if not os.path.isabs(name) or any(char in name for char in "\\*?%["):
            raise ValueError("observer_service_environment_unverified")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if optional == "yes":
                continue
            raise
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("observer_service_environment_unverified")
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("observer_service_environment_unverified")
        # Do not implement systemd's multiline value language. An assignment
        # affecting this target needs explicit owner resolution, not OFF.
        logical = content.decode("utf-8").replace("\\\n", "").replace("\\\r\n", "")
        if re.search(r"(?m)^[ \t\r\v\f]*(?:" + "|".join(OBSERVER_KEYS) + r")[ \t\r\v\f]*=", logical):
            raise ValueError("observer_service_override_unsupported")
    assignments = {}
    for word in shlex.split(props["Environment"]):
        key, separator, value = word.partition("=")
        if separator and key in OBSERVER_KEYS:
            assignments[key] = value
    if any(key not in assignments for key in OBSERVER_KEYS):
        manager = systemctl("show-environment")
        if any(line.split("=", 1)[0] in OBSERVER_KEYS for line in manager.splitlines()):
            raise ValueError("observer_service_override_unsupported")
    return assignments


# Observer preparation is optional to primary service availability. A refusal
# never authorizes a zero, deletes prior evidence or fabricates activation.
if mode == "active" and repo_arg and evidence_root_arg:
    source_handles = {}
    try:
        overrides = service_environment()
        repo, evidence_root = Path(repo_arg), Path(evidence_root_arg)
        owner_uid, owner_gid = original_stat.st_uid, original_stat.st_gid
        ensure_private_evidence_root(evidence_root, owner_uid, owner_gid)
        def _drop_privileges():
            if os.geteuid() == 0 and (owner_uid != 0 or owner_gid != 0):
                os.setgroups([owner_gid])
                os.setresgid(owner_gid, owner_gid, owner_gid)
                os.setresuid(owner_uid, owner_uid, owner_uid)
        child_options = {"preexec_fn": _drop_privileges} if os.geteuid() == 0 and (owner_uid != 0 or owner_gid != 0) else {}
        git_env = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
        selected = subprocess.run(["/usr/bin/git", "--no-optional-locks", "-C", str(repo),
                                   "rev-parse", "--verify", "HEAD"], env=git_env,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False, **child_options)
        if selected.returncode or len(selected.stdout) > 64:
            raise ValueError("observer_checkout_unverified")
        commit = selected.stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("observer_checkout_unverified")
        source_handles, source_holds = harden_node_sources(repo, commit, owner_uid, git_env, child_options)
        command = [sys.executable, "-I", "-S", str(repo / "scripts/stage-credential-monitor.py"),
                   "--checkout", str(repo), "--expected-commit", commit, "--prepare-active-root", str(evidence_root)]
        for producer, key in zip(("credential-monitor", "picker-excluded", "picker-quota-stale"), OBSERVER_KEYS):
            command += ["--current-" + producer, file_observer_value(original_text, key)]
            if key in overrides:
                command += ["--blocked-producer", producer.replace("-", "_")]
        staged = subprocess.run(command, env=git_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=15, check=False, **child_options)
        if staged.returncode or len(staged.stdout) > 16384:
            raise ValueError("node_observer_preparation_unavailable")
        receipt = json.loads(staged.stdout)
        if (not isinstance(receipt, dict) or receipt.get("kind") != "hub_node_observers_prepared_v2"
                or receipt.get("release_sha") != commit or receipt.get("activated") is not False
                or receipt.get("historical_coverage") is not False
                or not isinstance(receipt.get("pointers"), dict)
                or not set(receipt["pointers"]) <= set(OBSERVER_KEYS)
                or not isinstance(receipt.get("held"), dict)):
            raise ValueError("node_observer_preparation_unavailable")
        roles = dict(zip(("credential_monitor", "picker_excluded", "picker_quota_stale"), OBSERVER_KEYS))
        reasons = {"service_override", "selected_source_unverified", "current_identity_unverified",
                   "same_tuple_unqualified", "same_tuple_capacity_reached", "target_ownership_unverified",
                   "producer_state_unqualified"}
        if (set(receipt["held"]) != {role for role, key in roles.items() if key not in receipt["pointers"]}
                or any(reason not in reasons for reason in receipt["held"].values())
                or any(key in overrides for key in receipt["pointers"])):
            raise ValueError("node_observer_preparation_unavailable")
        source_reasons = {"source_directory_untrusted", "source_directory_unavailable", "source_file_unavailable",
                          "source_file_not_regular", "source_file_foreign_owner", "source_file_linked",
                          "source_file_unsafe_mode", "source_file_oversized", "source_file_changed",
                          "source_git_unverified", "source_hash_mismatch", "source_unclassified"}
        source_failures = receipt.get("source_failures", {})
        if (not isinstance(source_failures, dict)
                or set(source_failures) != {role for role, reason in receipt["held"].items() if reason == "selected_source_unverified"}
                or any(reason not in source_reasons for reason in source_failures.values())):
            raise ValueError("node_observer_preparation_unavailable")
        for key, value in receipt["pointers"].items():
            if (not isinstance(value, str) or not os.path.isabs(value)
                    or (Path(value).parent != evidence_root and value != file_observer_value(original_text, key))
                    or any(ord(c) < 33 or ord(c) == 127 for c in value)):
                raise ValueError("node_observer_preparation_unavailable")
        source_holds = recheck_node_sources(source_handles, source_holds, repo, commit, owner_uid, git_env, child_options)
        if source_holds:
            # Root's post-child fence may ONLY remove authority; it cannot mint
            # a pointer or repair a child receipt into a successful enrollment.
            role_keys = dict(zip(NODE_ROLES, OBSERVER_KEYS))
            if not isinstance(receipt, dict): raise ValueError("node_observer_preparation_unavailable")
            for role, reason in source_holds.items():
                receipt["pointers"].pop(role_keys[role], None)
                if receipt["held"].get(role) != "service_override":
                    receipt["held"][role] = "selected_source_unverified"
                    receipt.setdefault("source_failures", {})[role] = reason
        updates.update(receipt["pointers"])
        for role, reason in receipt["held"].items():
            print("node_observer_held:" + role + ":" + reason + "; no new coverage authorized", file=sys.stderr)
            if role in source_failures:
                print("node_observer_source:" + role + ":" + source_failures[role], file=sys.stderr)
    except Exception:
        # Do not print exceptions/config/paths. Existing pointers and histories
        # remain; loaded/current/boot checks determine UNKNOWN independently.
        print("node_observer_preparation_unavailable; no new coverage authorized", file=sys.stderr)
    finally:
        for role, reason in source_close(source_handles).items():
            updates.pop(dict(zip(NODE_ROLES, OBSERVER_KEYS))[role], None)
            print("node_observer_held:" + role + ":selected_source_unverified; no new coverage authorized", file=sys.stderr)
            print("node_observer_source:" + role + ":" + reason, file=sys.stderr)


# Keep every unrelated byte, including CRLF and absent final newline.
lines = re.findall(r"[^\n]*\n|[^\n]+$", original_text)
updated = []
inserted = set()
for line in lines:
    key, separator, _value = line.partition("=")
    key = key.strip(JS_SPACE)
    if separator and key in updates:
        if key not in inserted:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            updated.append(f"{key}={updates[key]}{ending}")
            inserted.add(key)
        continue
    updated.append(line)
for key, value in updates.items():
    if key not in inserted:
        if key in OBSERVER_KEYS:
            if updated and not updated[-1].endswith("\n"):
                updated.append("\n")
            updated.append(f"{key}={value}\n")
        else:
            updated.insert(0, f"{key}={value}\n")
contents = "".join(updated)

fd, temporary_name = tempfile.mkstemp(prefix=".dev.vars.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as temporary:
        temporary.write(contents)
        temporary.flush()
        os.fsync(temporary.fileno())
    os.chown(temporary_name, original_stat.st_uid, original_stat.st_gid)
    os.chmod(temporary_name, stat.S_IMODE(original_stat.st_mode))
    # Publish only against the config image this installer actually read.
    current = os.lstat(path)
    if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            != (original_stat.st_dev, original_stat.st_ino, original_stat.st_size, original_stat.st_mtime_ns, original_stat.st_ctime_ns)
            or path.read_bytes().decode("utf-8") != original_text):
        raise ValueError("release_configuration_changed")
    sync_fd = os.open(temporary_name, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(sync_fd)
    finally:
        os.close(sync_fd)
    os.replace(temporary_name, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
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
