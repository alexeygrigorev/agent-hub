"""Current Node replacement for the one legacy composed session-loop case.

No Worker/Miniflare fixture, seeded host binding, fake auto-reply or retired
receiver lease is used. The canonical test also runs in required Node CI.
"""
import os
import signal
import sys
import unittest


class TestSessionRealWorker(unittest.TestCase):
    @classmethod
    def _cleanup_proc(cls):
        handle = getattr(cls, "handle", None)
        proc = getattr(cls, "proc", None)
        cleanup_exc = None
        try:
            if handle is not None:
                try:
                    handle.cleanup()
                except Exception as e:
                    cleanup_exc = e
            elif proc is not None:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                            proc.wait(timeout=2)
                        except Exception:
                            pass
        finally:
            if proc is not None:
                for stream in (proc.stdout, proc.stderr, proc.stdin):
                    if stream:
                        try:
                            stream.close()
                        except Exception:
                            pass
            cls.handle = None
            cls.proc = None

        if cleanup_exc is not None and sys.exc_info()[0] is None:
            raise cleanup_exc


    def test_real_worker_session_loop_via_cli_and_receiver(self):
        from test_node_intake_e2e import test_real_worker_session_loop_via_cli_and_receiver
        test_real_worker_session_loop_via_cli_and_receiver()


if __name__ == '__main__':
    unittest.main()
