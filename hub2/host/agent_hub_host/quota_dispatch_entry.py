"""Maintained entries for staged quota dispatch and the natural quota Host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import time
import types

from . import alert_authority as ram
from .quota_dispatch import configured_same_process, run_same_process_quota
from .quota_dispatch_manager import (
    FanotifyReader,
    SourceGate,
    StableRuntime,
    close_held_source,
    consume_handoff_fd,
    hold_source_fd,
    prepare_selected_quota,
)


def _run_verified_original_once(verified_quota_raw, quota_path, primary_args):
    """Execute already-verified retained original bytes once unqualified."""
    selected = types.ModuleType("selected_quota_monitor")
    selected.__file__ = quota_path
    exec(compile(verified_quota_raw, quota_path, "exec"), selected.__dict__)
    return selected.main(primary_args, _managed_observer=None)


def run_quota_dispatch_cli(args, config):
    """Run staged default-OFF same-process quota dispatch CLI command."""
    spec = configured_same_process(config.get("quota_same_process"))
    if not getattr(args, "enable", False) or spec is None:
        print(json.dumps(run_same_process_quota(enabled=False)))
        return 0

    src_dir = spec["source_directory"]
    gate_dir = spec["gate_directory"]
    unit = spec["unit"]
    cgroup = spec["cgroup"]
    primary_args = list(spec.get("primary_args") or ["--check"])
    quota_path = str(Path(src_dir) / "hub-quota-publish.py")
    client_path = str(Path(src_dir) / "hub_alert_client.py")
    prelude_path = str(Path(src_dir) / "hub-alert-quota-dispatch.py")

    held = []
    gate = None
    group = None
    interp_fd = None
    entry_fd = None
    entry_pin = None
    verified_quota_raw = None
    sealed_quota_path = quota_path
    sealed_primary_args = list(primary_args)
    try:
        if getattr(args, "handoff_fd", None) is not None:
            adopted = consume_handoff_fd(args.handoff_fd, gate_dir)
            quota_fd, client_fd, prelude_fd = adopted["source"], adopted["client"], adopted["prelude"]
            held.extend((quota_fd, client_fd, prelude_fd))
            verified_quota_raw = quota_fd.raw
            gate = adopted["gate"]
            group = adopted["fan_group"]
            interp_fd = adopted["interp_fd"]
            entry_fd = adopted["entry_fd"]
            interp_pin = adopted["interpreter_pin"]
            entry_pin = adopted["entry_pin"]
            py_bin = interp_pin["path"]
            sealed_plan = adopted["plan"]
            sealed_quota_path = str(Path(sealed_plan["source_directory"]) / "hub-quota-publish.py")
            sealed_primary_args = list(sealed_plan["primary_args"])
            ram.require(
                src_dir == sealed_plan["source_directory"]
                and gate_dir == sealed_plan["gate_directory"]
                and unit == sealed_plan["unit"]
                and cgroup == sealed_plan["cgroup"]
                and primary_args == sealed_primary_args
            )
        else:
            quota_fd = hold_source_fd(src_dir, "hub-quota-publish.py")
            held.append(quota_fd)
            verified_quota_raw = quota_fd.raw
            client_fd = hold_source_fd(src_dir, "hub_alert_client.py")
            held.append(client_fd)
            prelude_fd = hold_source_fd(src_dir, "hub-alert-quota-dispatch.py")
            held.append(prelude_fd)
            gate = SourceGate(gate_dir)
            group = FanotifyReader.open_group((quota_fd,))
            interp_pin = None
            py_bin = "/usr/bin/python3"

        deadline = time.monotonic() + 40.0
        if entry_pin is not None:
            exec_argv = lambda dl_ns: [
                py_bin, "-I", entry_pin["path"], "primary-prelude",
                str(dl_ns), quota_fd.pin.sha256, client_fd.pin.sha256,
                gate_dir, quota_path, client_path, "--", *primary_args,
            ]
        else:
            exec_argv = lambda dl_ns: [
                py_bin, "-I", "-S", prelude_path,
                str(dl_ns), quota_fd.pin.sha256, client_fd.pin.sha256,
                gate_dir, quota_path, client_path, "--", *primary_args,
            ]
        plan = {"units": {"healthy": unit}, "cgroups": {"healthy": cgroup}}

        def _close_handoff_extras():
            for extra in (interp_fd, entry_fd):
                if extra is not None:
                    try:
                        os.close(extra)
                    except OSError:
                        pass

        prepared = prepare_selected_quota(
            source=quota_fd, companions=(client_fd, prelude_fd), gate=gate, group=group,
            plan=plan, exec_argv=exec_argv, service_deadline=deadline,
            interpreter_pin=interp_pin, entry_pin=entry_pin, retained_source_fd=quota_fd.fd,
            extra_cleanup=_close_handoff_extras if (interp_fd is not None or entry_fd is not None) else None,
        )
    except (OSError, ValueError, ram.Unknown) as error:
        if gate is not None:
            gate.cleanup()
        for item in held:
            close_held_source(item)
        if group is not None:
            os.close(group)
        for extra in (interp_fd, entry_fd):
            if extra is not None:
                try:
                    os.close(extra)
                except OSError:
                    pass
        reason = error.args[0] if isinstance(error, ram.Unknown) and error.args else type(error).__name__
        if verified_quota_raw is None:
            print(json.dumps({"enabled": True, "dispatched": False, "source_rejected": True,
                              "error": str(reason), "complete": False}))
            return 1
        launch_rc = _run_verified_original_once(verified_quota_raw, sealed_quota_path, sealed_primary_args)
        print(json.dumps({"enabled": True, "dispatched": False, "fail_open_business": True,
                          "launch_rc": launch_rc, "error": str(reason), "complete": False}))
        return launch_rc

    def _launch_unit(ticket):
        if ticket is None:
            return _run_verified_original_once(verified_quota_raw, sealed_quota_path, sealed_primary_args)
        dl_ns = int(ticket.deadline * 1e9)
        retained_dup = os.dup(quota_fd.fd)
        try:
            os.set_inheritable(retained_dup, True)
            argv = ["/usr/bin/systemd-run", "--user", "--unit=" + unit, "--no-block", "--"] + exec_argv(dl_ns)
            child_env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_ALERT_")}
            child_env.update(StableRuntime.user_manager_env(os.getuid()))
            child_env["HUB_ALERT_RETAINED_FD"] = str(retained_dup)
            return subprocess.run(argv, capture_output=True, timeout=5, check=False,
                                  env=child_env, pass_fds=(retained_dup,)).returncode
        finally:
            os.close(retained_dup)

    result = run_same_process_quota(prepared, _launch_unit, enabled=True, broker_directory=spec["broker_directory"])
    print(json.dumps({"enabled": result["enabled"], "dispatched": result["dispatched"],
                      "reserved": result["reserved"], "phase": result["phase"], "complete": False}))
    return 0


class _ManagedObserver:
    def __init__(self, client):
        self.client = client

    def fail(self):
        self.client.dirty = True
        self.client.context = None

    def emit(self, phase, decision=None, condition=None, flags=None, *, recipient_class="none"):
        try:
            return self.client.emit(
                phase,
                decision=decision,
                condition=condition,
                flags=flags,
                recipient_class=recipient_class,
            )
        except BaseException as error:
            self.fail()
            if not isinstance(error, Exception):
                raise
            return False


def _read_verified_fd(fd, digest, *, allow_unlinked=False):
    if type(fd) is not int or fd < 0 or len(digest) != 64:
        raise RuntimeError("source_unqualified")
    info = os.fstat(fd)
    allowed_nlink = (0, 1) if allow_unlinked else (1,)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_nlink not in allowed_nlink or (info.st_mode & 0o222)
            or not 0 < info.st_size <= 262144):
        raise RuntimeError("source_unqualified")
    raw = os.pread(fd, info.st_size + 1, 0)
    if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
        raise RuntimeError("source_unqualified")
    return raw


def _acquired(path_str, digest):
    path = Path(path_str)
    if not path.is_absolute() or ".." in path.parts or len(digest) != 64:
        raise RuntimeError("source_unqualified")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _read_verified_fd(fd, digest)
    finally:
        os.close(fd)


def _recv_pkt(channel, end):
    remaining = end - time.monotonic()
    if remaining <= 0 or not select.select([channel], [], [], remaining)[0]:
        raise TimeoutError()
    raw, anc, flags, _addr = channel.recvmsg(2049, 0, socket.MSG_DONTWAIT)
    if not raw or len(raw) > 2048 or anc or (flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
        raise RuntimeError("channel_unqualified")
    return raw


def _recv_open(channel, end):
    remaining = end - time.monotonic()
    if remaining <= 0 or not select.select([channel], [], [], remaining)[0]:
        raise TimeoutError()
    raw, anc, flags, _addr = channel.recvmsg(2049, socket.CMSG_SPACE(4), socket.MSG_DONTWAIT)
    if not raw or len(raw) > 2048 or (flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
        for _lvl, ctype, cdata in anc:
            if ctype == socket.SCM_RIGHTS:
                for i in range(0, len(cdata) - 3, 4):
                    try:
                        os.close(struct.unpack_from("i", cdata, i)[0])
                    except OSError:
                        pass
        raise RuntimeError("channel_unqualified")
    if not anc:
        return raw, None
    if len(anc) == 1 and anc[0][0] == socket.SOL_SOCKET and anc[0][1] == socket.SCM_RIGHTS and len(anc[0][2]) == 4:
        return raw, struct.unpack("i", anc[0][2])[0]
    for _lvl, ctype, cdata in anc:
        if ctype == socket.SCM_RIGHTS:
            for i in range(0, len(cdata) - 3, 4):
                try:
                    os.close(struct.unpack_from("i", cdata, i)[0])
                except OSError:
                    pass
    raise RuntimeError("channel_unqualified")


def _send_pkt(channel, raw, end):
    remaining = end - time.monotonic()
    if remaining <= 0 or not select.select([], [channel], [], remaining)[1] or channel.send(raw) != len(raw):
        raise TimeoutError()


def run_primary_prelude(argv):
    """Execute the primary quota monitor once under immutable archive bootstrap."""
    if len(argv) < 7 or argv[6] != "--":
        raise ValueError("invalid_prelude_args")
    deadline_ns, quota_sha, client_sha, gate_dir, quota_path, client_path = argv[:6]
    primary_args = argv[7:]
    try:
        end = min(int(deadline_ns) / 1e9, time.monotonic() + 0.15)
    except Exception:
        end = time.monotonic()
    channel = None
    context = None
    qualified = False
    transferred_fd = None
    if end > time.monotonic():
        try:
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            channel.setblocking(False)
            try:
                channel.connect(str(Path(gate_dir) / "source-gate.sock"))
            except BlockingIOError:
                pass
            _send_pkt(channel, b"held-before-quota-source", end)
            custody_cmd, transferred_fd = _recv_open(channel, end)
            if custody_cmd == b"custody":
                command, extra_fd = _recv_open(channel, end)
                if extra_fd is not None:
                    try:
                        os.close(extra_fd)
                    except OSError:
                        pass
            else:
                command = custody_cmd
            if len(command) == 37 and command.startswith(b"open:"):
                candidate = command[5:].decode("ascii")
                if len(candidate) == 32 and all(c in "0123456789abcdef" for c in candidate):
                    context = candidate
        except Exception:
            context = None
    try:
        quota_raw = _acquired(quota_path, quota_sha)
    except Exception:
        quota_raw = _read_verified_fd(transferred_fd, quota_sha, allow_unlinked=True)
        context = None
        qualified = False
    finally:
        if transferred_fd is not None:
            try:
                os.close(transferred_fd)
            except OSError:
                pass
    helper_raw = None
    if context is not None:
        try:
            helper_raw = _acquired(client_path, client_sha)
            _send_pkt(channel, b"quota-source-opened", end)
            qualified = _recv_pkt(channel, end) == b"qualified-run"
        except Exception:
            qualified = False
    selected = types.ModuleType("selected_quota_monitor")
    selected.__file__ = quota_path
    exec(compile(quota_raw, quota_path, "exec"), selected.__dict__)
    observer = None
    if qualified and helper_raw is not None:
        try:
            helper = types.ModuleType("selected_quota_client")
            helper.__file__ = client_path
            exec(compile(helper_raw, client_path, "exec"), helper.__dict__)

            def exchange(_path, request, *, deadline=None):
                stop = min(time.monotonic() + 0.15, deadline) if deadline is not None else time.monotonic() + 0.15
                _send_pkt(channel, helper.encode(request, helper.FRAME), stop)
                return helper.decode(_recv_pkt(channel, stop), helper.REPLY)

            helper.exchange = exchange
            observer = _ManagedObserver(helper.Client(None, context, None, "quota_monitor"))
        except Exception:
            observer = None
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass
                channel = None
    original = None
    try:
        return selected.main(primary_args, _managed_observer=observer)
    except BaseException as error:
        original = error
        raise
    finally:
        if channel is not None:
            try:
                channel.close()
            except BaseException as cleanup_error:
                if observer is not None:
                    try:
                        observer.fail()
                    except BaseException:
                        pass
                if original is None and not isinstance(cleanup_error, Exception):
                    raise


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "natural-host":
        # The preparer must drop all privilege before loading the Host package.
        ram.require(os.getuid() > 0 and os.geteuid() > 0)
        from .cli import main as host_main
        return host_main(raw_argv[1:])
    if raw_argv and raw_argv[0] == "natural-primary":
        started_ns = time.monotonic_ns()
        from .quota_natural_primary import run_primary, archive_payloads
        if len(raw_argv) != 4:
            raise ValueError('natural_selection_arguments')
        return run_primary(*raw_argv[1:], started_ns=started_ns,
                           payloads=archive_payloads())
    if raw_argv and raw_argv[0] == "primary-prelude":
        return run_primary_prelude(raw_argv[1:])
    parser = argparse.ArgumentParser(prog="agent-hub-quota-dispatcher")
    parser.add_argument("--config", default=str(Path.home() / ".config/agent-hub/v2.json"), type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    qd = sub.add_parser("quota-dispatch")
    qd.add_argument("--enable", action="store_true")
    qd.add_argument("--handoff-fd", type=int, default=None)
    args = parser.parse_args(raw_argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Quota dispatcher config error: {type(error).__name__}", file=sys.stderr)
        return 1
    return run_quota_dispatch_cli(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
