"""Preserve the controller child's output in a bounded supervisor-owned log.

aplexer removes a finished session's record and history within seconds of a
workload death, destroying the stderr that explains why the session host
died (intake 4d33b30d). The supervisor starts its child through this
wrapper, so stdout and stderr land in a bounded journal file before any
host import or startup step can fail. Run in-process afterwards: the
workload pid, signal handling and the real exit code stay the child's own.
"""
import argparse
import os
import runpy
import sys
import threading
import time
import traceback

# Only the session host may be launched through this wrapper; it must never
# become a generic exec primitive.
PINNED_MODULE = 'agent_hub_host.session_host'
ROTATE_ABOVE_BYTES = 262144
ROTATE_KEEP_BYTES = 131072


def rotate(path):
    """Keep the last ROTATE_KEEP_BYTES when the log grew past the bound."""
    try:
        if os.path.getsize(path) <= ROTATE_ABOVE_BYTES:
            return
        with open(path, 'rb') as stream:
            stream.seek(-ROTATE_KEEP_BYTES, os.SEEK_END)
            tail = stream.read()
        marker = ('... bootstrap log truncated to the last %d bytes ...\n'
                  % ROTATE_KEEP_BYTES).encode()
        with open(path, 'wb') as stream:
            stream.write(marker + tail.split(b'\n', 1)[-1])
    except OSError:
        pass


def rotate_while_running(path, stop):
    while not stop.wait(1.0):
        rotate(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', required=True, help='Bounded bootstrap log path')
    parser.add_argument('command', nargs=argparse.REMAINDER,
                        help='-- -m <pinned module> [args...]')
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if command[:2] != ['-m', PINNED_MODULE]:
        parser.error('only -m %s may be launched' % PINNED_MODULE)
    rotate(args.log)
    # One controller per journal (advisory lock) writes this file; append
    # keeps prior attempts' dying output for the supervisor's crash report.
    handle = open(args.log, 'ab')
    marker = ('=== session_host bootstrap %s ===\n'
              % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())).encode()
    handle.write(marker)
    handle.flush()
    os.dup2(handle.fileno(), 1)
    os.dup2(handle.fileno(), 2)
    handle.close()
    # Even without an outer -u, dying tracebacks must reach the file.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    rest = command[2:]
    sys.argv = [PINNED_MODULE] + rest
    rotation_stop = threading.Event()
    rotator = threading.Thread(target=rotate_while_running, args=(args.log, rotation_stop),
                               name='bootstrap-log-rotation', daemon=True)
    rotator.start()
    # run_module keeps this process: aplexer still supervises the same pid,
    # TERM reaches the session host directly and the real exit code surfaces.
    try:
        try:
            runpy.run_module(PINNED_MODULE, run_name='__main__', alter_sys=True)
        except SystemExit as error:
            code = error.code
            exit_code = code if type(code) is int else (0 if code is None else 1)
            if isinstance(code, str):
                print(code, file=sys.stderr)
            print('session_host_exit_code=%d' % exit_code, file=sys.stderr, flush=True)
            raise SystemExit(exit_code)
        except KeyboardInterrupt:
            traceback.print_exc(file=sys.stderr)
            print('session_host_exit_code=130', file=sys.stderr, flush=True)
            raise SystemExit(130) from None
        except BaseException:
            traceback.print_exc(file=sys.stderr)
            print('session_host_exit_code=1', file=sys.stderr, flush=True)
            raise SystemExit(1) from None
        else:
            print('session_host_exit_code=0', file=sys.stderr, flush=True)
    finally:
        rotation_stop.set()
        rotator.join(timeout=2)
        rotate(args.log)


if __name__ == '__main__':
    main()
