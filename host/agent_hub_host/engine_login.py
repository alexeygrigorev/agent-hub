"""Native Gemini login relay. PTY output stays in memory; only an allowlisted URL is reported."""
import os
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

_active = {}
_guard = threading.Lock()
URL = re.compile(r'https://accounts\.google\.com/[^\s\x1b<>"\']+')


def login_url(text):
    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    for match in URL.finditer(clean):
        value = match.group(0)
        parsed = urlparse(value)
        if parsed.hostname == 'accounts.google.com' and not parsed.username and len(value) <= 4096:
            return value
    return None


def _run(item, bundle, dest, request, stop, is_windows=None):
    from . import provisioning as p
    master = slave = proc = reader_thread = None
    path = '/api/engines/logins/' + item['id']
    try:
        home = dest / 'home'
        settings = home / '.gemini/settings.json'
        # Login selection is native-client configuration, not a third-party OAuth exchange.
        settings_data = p.read_json(settings, {})
        settings_data.setdefault('security', {}).setdefault('auth', {})['selectedType'] = 'oauth-personal'
        settings_data.setdefault('general', {})['disableAutoUpdate'] = True
        p.write_json(settings, settings_data)
        env = p.engine_environment(bundle, home)
        env.update(NO_BROWSER='true', TERM='xterm-256color')
        cmd = p.engine_argv(bundle, dest)
        if bundle.get('provider') == 'gemini':
            cmd = list(cmd) + ['--skip-trust']
        if is_windows is None:
            is_windows = (os.name == 'nt')
        is_posix = not is_windows

        tail = ''
        last_url = None
        submitted = False
        last_poll = 0
        deadline = time.monotonic() + 300

        if is_posix:
            import pty
            import select
            master, slave = pty.openpty()
            proc = subprocess.Popen(cmd, stdin=slave, stdout=slave,
                                    stderr=subprocess.DEVNULL, env=env, cwd=home, start_new_session=True)
            os.close(slave)
            slave = None
        else:
            # Native Windows memory-only safe transport via ConPTY pseudoconsole.
            from . import win_console
            try:
                proc = win_console.ConsoleProcess(cmd, env=env, cwd=str(home))
            except Exception:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, env=env, cwd=home)
            reader_buf = []
            reader_lock = threading.Lock()

            def _stdout_reader():
                while not stop.is_set():
                    try:
                        if hasattr(proc.stdout, 'read1'):
                            chunk = proc.stdout.read1(1024)
                        elif hasattr(proc.stdout, 'fileno'):
                            chunk = os.read(proc.stdout.fileno(), 1024)
                        else:
                            break
                        if not chunk:
                            break
                        with reader_lock:
                            if len(reader_buf) < 16:
                                reader_buf.append(chunk)
                    except Exception:
                        break

            reader_thread = threading.Thread(target=_stdout_reader, daemon=True, name='gemini-login-reader')
            reader_thread.start()

        while time.monotonic() < deadline and not stop.is_set():
            if (home / '.gemini/oauth_creds.json').is_file():
                # No model prompt is ever sent. Stop the login-only native CLI on success.
                credential = home / '.gemini/oauth_creds.json'
                with p.private_fs.parent(credential) as anchor:
                    fd = p.private_fs._open(anchor, credential, os.O_RDONLY, private=False)
                    try:
                        if sys.platform != 'win32' and os.fstat(fd).st_uid != os.getuid():
                            raise PermissionError('Unowned native credential')
                        if sys.platform != 'win32':
                            os.fchmod(fd, 0o600)
                    finally:
                        os.close(fd)
                request('POST', path, {'status': 'authenticated'})
                return
            if proc.poll() is not None:
                break
            if is_posix:
                ready, _, _ = select.select([master], [], [], .2)
                if ready:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        break
                    tail = (tail + chunk.decode(errors='replace'))[-16384:]
                    url = login_url(tail)
                    if url and url != last_url:
                        request('POST', path, {'status': 'waiting', 'url': url})
                        last_url = url
            else:
                time.sleep(0.2)
                with reader_lock:
                    if reader_buf:
                        raw = b''.join(reader_buf)
                        reader_buf.clear()
                        tail = (tail + raw.decode(errors='replace'))[-16384:]
                url = login_url(tail)
                if url and url != last_url:
                    request('POST', path, {'status': 'waiting', 'url': url})
                    last_url = url

            if time.monotonic() - last_poll >= 2:
                status = request('GET', path)
                if status.get('status') in ('cancelled', 'expired'):
                    return
                code = status.get('code')
                # Never type into a model prompt: only this newly spawned login PTY/pipe.
                if code and not submitted and 'authorization code' in tail.lower():
                    if is_posix:
                        os.write(master, code.encode() + b'\r')
                    else:
                        proc.stdin.write(code.encode() + b'\r\n')
                        proc.stdin.flush()
                    submitted = True
                    tail = ''
                last_poll = time.monotonic()
        request('POST', path, {'status': 'failed'})
    except Exception:
        try: request('POST', path, {'status': 'failed'})
        except Exception: pass
    finally:
        if proc is not None:
            if hasattr(proc, 'close') and callable(proc.close):
                try: proc.close()
                except Exception: pass
            elif is_posix and proc.poll() is None:
                if hasattr(os, 'killpg'):
                    try: os.killpg(proc.pid, signal.SIGTERM); proc.wait(timeout=1)
                    except Exception:
                        try: os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=1)
                        except Exception: pass
                else:
                    try: proc.terminate(); proc.wait(timeout=1)
                    except Exception:
                        try: proc.kill(); proc.wait(timeout=1)
                        except Exception: pass
            elif not is_posix and proc.poll() is None:
                try: proc.terminate(); proc.wait(timeout=1)
                except Exception:
                    try: proc.kill(); proc.wait(timeout=1)
                    except Exception: pass
            if getattr(proc, 'stdout', None) is not None:
                try: proc.stdout.close()
                except Exception: pass
            if getattr(proc, 'stdin', None) is not None:
                try: proc.stdin.close()
                except Exception: pass
        if reader_thread is not None:
            try: reader_thread.join(timeout=1.0)
            except Exception: pass
        for fd in (master, slave):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        with _guard:
            _active.pop(item['id'], None)


def tick(items, bundles, request, is_windows=None):
    from . import provisioning as p
    by_label = {b['label']: b for b in bundles}
    if is_windows is None:
        is_windows = (os.name == 'nt')
    if not is_windows and os.name != 'posix':
        return
    for item in items:
        if item.get('status') not in ('requested', 'waiting', 'submitted'):
            continue
        b = by_label.get(item.get('label'))
        if not b or b['provider'] != 'gemini':
            continue
        with _guard:
            if item['id'] in _active or len(_active) >= 4:
                continue
            dest = p.root() / (b['label'] + '-' + b['revision'])
            stop = threading.Event()
            thread = threading.Thread(target=_run, args=(item, b, dest, request, stop, is_windows), daemon=True, name='hub-native-login')
            _active[item['id']] = {'thread': thread, 'stop': stop, 'label': b['label'], 'revision': b['revision']}
            thread.start()


def cancel_removed(bundles):
    desired = {(b['label'], b['revision']) for b in bundles}
    with _guard:
        removed = [v for v in _active.values() if (v['label'], v['revision']) not in desired]
        for item in removed:
            item['stop'].set()
    for item in removed:
        item['thread'].join(timeout=15)
        if item['thread'].is_alive():
            raise ValueError('Native login cancellation pending')
