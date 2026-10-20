"""Clean-machine lifecycle over real HTTP and aplexer; no paid model calls or real secrets."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

BASE = os.environ.get('TEST_HUB_URL', 'http://127.0.0.1:19487')

def http(path, data=None, headers=None, method=None):
    req = urllib.request.Request(BASE + path, data=None if data is None else json.dumps(data).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})}, method=method or ('POST' if data is not None else 'GET'))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def run(*argv, **kwargs):
    # Never echo command output unless assertions below explicitly allow public metadata.
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs).stdout

def wait_for(check, description, timeout=150):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            if check():
                print('PASS: ' + description, flush=True)
                return
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        time.sleep(2)
    raise AssertionError('Timeout: ' + description)

run('aplexer', '--version')
# Install exactly the published CLI's documented way, checksum/manifest included.
assert not Path.home().joinpath('.config/agent-hub').exists()
assert subprocess.run(['sh', '-c', 'command -v codex'], stdout=subprocess.DEVNULL).returncode != 0
script = urllib.request.urlopen(BASE + '/cli/install.sh').read()
subprocess.run(['sh'], input=script, env={**os.environ, 'HUB_BASE_URL': BASE}, check=True, stdout=subprocess.DEVNULL)
print('PASS: clean container installed Hub using /cli/install.sh', flush=True)
fixture = http('/__docker_fixture', {}, {'Authorization': 'Bearer local-docker-fixture-only'})
admin = {'Cookie': fixture['cookie'], 'X-CSRF-Token': 'docker-csrf'}
code = http('/api/device/code', {'client_kind': 'host', 'name': 'Docker lifecycle', 'hostname': 'docker-test', 'requested_scopes': ['host']})
http('/api/device/approve/' + code['user_code'], {'granted_scopes': ['host']}, admin)
tokens = http('/api/device/token', {'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': code['device_code']})
# Installed zipapp is also the Python package import source for this test driver.
import sys
sys.path.insert(0, str(Path.home() / '.local/bin/hub'))
from agent_hub_host import config, api
cfg = {'base_url': BASE, 'client_kind': 'host', 'access_token': tokens['access_token'], 'refresh_token': tokens['refresh_token'], 'machine_id': tokens['machine_id']}
config.save_host_config(cfg)
api.register_machine(BASE)
print('PASS: enrolled and registered container through real Hub device/host APIs', flush=True)
# Keep the real installed machine daemon running: add/remove must appear without manual sync.
daemon = subprocess.Popen([str(Path.home() / '.local/bin/hub'), 'run'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    bundle = {'label': 'docker-codex', 'provider': 'codex', 'version': '0.163.0',
              'files': {'.codex/auth.json': '{"synthetic":"docker-private-marker"}'}, 'env': {}, 'args': ['--version']}
    http('/api/engines/docker-codex', bundle, admin, 'PUT')
    def profile_present():
        return 'hub-docker-codex' in json.loads(run('aplexer', 'profiles', '--json'))
    wait_for(profile_present, 'central add automatically installed engine and aplexer profile')
    native_config = Path.home() / '.config/aplexer/config.toml'
    assert 'docker-private-marker' not in native_config.read_text()
    creds = list(Path.home().glob('.config/agent-hub/managed-engines/docker-codex-*/home/.codex/auth.json'))
    assert len(creds) == 1 and creds[0].stat().st_mode & 0o777 == 0o600
    profiles = json.loads(run('aplexer', 'profiles', '--json'))
    # Preserve exited metadata only in this disposable test to inspect the version run.
    text = native_config.read_text().replace('version = 1', 'version = 1\nkeep_exited = true', 1)
    native_config.write_text(text)
    started = json.loads(run('aplexer', 'start', '--workspace', str(Path.home()), '--tag', 'docker-smoke', '--profile', 'hub-docker-codex', '--no-skip-permissions', '--json'))
    native_id = started['id']
    try:
        wait_for(lambda: 'codex-cli 0.163.0' in run('aplexer', 'capture', native_id), 'real aplexer session executed installed Codex 0.163.0 (--version, no model request)', 30)
    except Exception:
        print(run('aplexer', 'capture', native_id).replace('docker-private-marker', '[redacted]'), flush=True)
        raise
    print('PASS: credentials mode 0600; native config/metadata contain no credential marker', flush=True)
    http('/api/engines/docker-codex', headers=admin, method='DELETE')
    wait_for(lambda: not profile_present(), 'central removal automatically withdrew aplexer profile')
    assert not list(Path.home().glob('.config/agent-hub/managed-engines/docker-codex-*'))
    assert subprocess.run(['aplexer', 'start', '--workspace', str(Path.home()), '--tag', 'removed', '--profile', 'hub-docker-codex', '--json'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    print('PASS: credentials and engine deleted; removed profile cannot start', flush=True)
    # Regression for native npm placeholders: verify every other supported engine
    # actually executes, without auth, inference, install scripts or host config.
    for provider, version in [('claude', '2.1.304'), ('go', '1.19.3'), ('gemini', '0.64.0')]:
        label = 'smoke-' + provider
        args = ['--model', 'opencode-go/muse-spark-1.3-contributor'] if provider == 'go' else []
        extra = {'auth_method': 'machine-login'} if provider == 'gemini' else {}
        http('/api/engines/' + label, {'label':label,'provider':provider,'version':version,'files':{},'env':{},'args':args+['--version'],**extra}, admin, 'PUT')
        wait_for(lambda: 'hub-' + label in json.loads(run('aplexer','profiles','--json')), provider + ' native package installed', 240)
        started = json.loads(run('aplexer','start','--workspace',str(Path.home()),'--tag','smoke-'+provider,'--profile','hub-'+label,'--no-skip-permissions','--json'))
        wait_for(lambda: version in run('aplexer','capture',started['id']), provider + ' real native session executed pinned version', 30)
        http('/api/engines/' + label, headers=admin, method='DELETE')
        wait_for(lambda: 'hub-' + label not in json.loads(run('aplexer','profiles','--json')), provider + ' removal reconciled')

finally:
    daemon.terminate()
    try: daemon.wait(timeout=10)
    except subprocess.TimeoutExpired: daemon.kill(); daemon.wait()
print('DOCKER ENGINE LIFECYCLE PASSED', flush=True)
