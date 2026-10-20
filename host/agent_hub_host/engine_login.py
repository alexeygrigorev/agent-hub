"""Native Gemini login relay. PTY output stays in memory; only an allowlisted URL is reported."""
import os
import re
import signal
import subprocess
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


def _run(item, bundle, dest, request, stop):
    from . import provisioning as p
    import pty
    import select
    master = slave = proc = None
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
        master, slave = pty.openpty()
        proc = subprocess.Popen([str(dest / 'engine/node_modules/.bin/gemini')], stdin=slave, stdout=slave,
                                stderr=subprocess.DEVNULL, env=env, cwd=home, start_new_session=True)
        os.close(slave); slave = None
        tail = ''; last_url = None; submitted = False; last_poll = 0; deadline = time.monotonic() + 300
        while time.monotonic() < deadline and not stop.is_set():
            if (home / '.gemini/oauth_creds.json').is_file():
                # No model prompt is ever sent. Stop the login-only native CLI on success.
                credential = home / '.gemini/oauth_creds.json'
                with p.private_fs.parent(credential) as anchor:
                    fd = p.private_fs._open(anchor, credential, os.O_RDONLY, private=False)
                    try:
                        if os.fstat(fd).st_uid != os.getuid():
                            raise PermissionError('Unowned native credential')
                        os.fchmod(fd, 0o600)
                    finally:
                        os.close(fd)
                request('POST', path, {'status': 'authenticated'})
                return
            if proc.poll() is not None:
                break
            ready, _, _ = select.select([master], [], [], .2)
            if ready:
                try: chunk = os.read(master, 8192)
                except OSError: break
                tail = (tail + chunk.decode(errors='replace'))[-16384:]
                url = login_url(tail)
                if url and url != last_url:
                    request('POST', path, {'status': 'waiting', 'url': url})
                    last_url = url
            if time.monotonic() - last_poll >= 2:
                status = request('GET', path)
                if status.get('status') in ('cancelled', 'expired'):
                    return
                code = status.get('code')
                # Never type into a model prompt: only this newly spawned login PTY.
                if code and not submitted and 'authorization code' in tail.lower():
                    os.write(master, code.encode() + b'\r')
                    submitted = True
                    tail = ''
                last_poll = time.monotonic()
        request('POST', path, {'status': 'failed'})
    except Exception:
        try: request('POST', path, {'status': 'failed'})
        except Exception: pass
    finally:
        if proc is not None and proc.poll() is None:
            try: os.killpg(proc.pid, signal.SIGTERM); proc.wait(timeout=3)
            except Exception:
                try: os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=3)
                except Exception: pass
        for fd in (master, slave):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        with _guard:
            _active.pop(item['id'], None)


def tick(items, bundles, request):
    from . import provisioning as p
    by_label = {b['label']: b for b in bundles}
    if os.name != 'posix':
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
            thread = threading.Thread(target=_run, args=(item, b, dest, request, stop), daemon=True, name='hub-native-login')
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
