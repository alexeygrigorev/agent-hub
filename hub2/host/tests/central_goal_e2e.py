"""One controlled LOCAL dev native goal canary; real central quota, no fallback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agent_hub_host.client import ApiError, Client, private_file, save_config


def require(value, reason):
    if not value:
        raise RuntimeError(reason)



def fixture(client, path):
    # Test-only namespaces stay outside the production Client API allowlist.
    require(client.base_url.startswith('http://127.0.0.1:') and path.startswith('/_fixture/'),
            'isolated_fixture_path_required')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = Request(client.base_url + path, headers={'Authorization': 'Bearer ' + client.token})
    with build_opener(NoRedirect).open(request, timeout=30) as response:
        body = response.read(1_048_577)
    require(len(body) <= 1_048_576, 'fixture_response_too_large')
    return json.loads(body)


def main(args):
    config = json.loads(args.operator_config.read_text())
    require(config['server'].startswith('http://127.0.0.1:'), 'isolated_loopback_only')
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_file(output)
    operator = Client(config['server'], config['token'])
    launcher = json.loads(args.launcher_config.read_text())
    require(launcher.get('fixture_provisional_goal') is True, 'explicit_unproven_dev_capability_required')
    require(launcher.get('routes', {}).get('codex', {}).get('account_profile') == 'hub-zai-central', 'central_profile_required')
    require(args.account_source.name == 'config.json' and args.account_source.parent.name == 'goz', 'approved_goz_source_required')
    source = args.account_source.read_text()
    source_key = json.loads(source)['zai_token']
    require(isinstance(source_key, str) and not source_key.startswith('enc:'), 'approved_plaintext_central_source_required')
    states = json.loads(args.managed_state.read_text())
    central = [item for item in states if item.get('label') == 'zai-central' and item.get('provider') == 'zai']
    require(len(central) == 1, 'approved_central_identity_ambiguous')
    approved_home = args.managed_state.parent / ('zai-central-' + central[0]['revision']) / 'home'
    require(args.account_source.resolve() == (approved_home / '.config/goz/config.json').resolve(), 'unapproved_credential_source')
    sdk = json.loads((approved_home / '.zcode/v2/provider_config.json').read_text())['config']
    selected = sdk['defaultModelSelection']['providerId']
    require(sdk['defaultModelSelection']['modelId'].casefold() == args.native_model.casefold(), 'configured_native_model_mismatch')
    rules = [rule for rule in sdk['providerConfigRules']['providerRules'] if rule['providerId'] == selected]
    require(len(rules) == 1 and rules[0]['config']['access']['apiKey'] == source_key, 'approved_sdk_selected_source_mismatch')
    suffix = uuid.uuid4().hex[:12]
    project, machine = 'fixture/central-goal-' + suffix, 'central-goal-host-' + suffix
    workspace = output / 'native-workspace'
    workspace.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    project_query = '?' + urlencode({'project': project})
    account_prefix = '/api/accounts/zai-central/api/account'
    proof = {'development_only': True, 'provisional_capability': True, 'account_id': 'zai-central',
             'project': project, 'machine_id': machine, 'stages': [], 'passed': False}
    agent = None
    host_log = None
    native_id = None
    child = None
    try:
        operator.request('PATCH', '/api/repos/' + quote(project, safe=''), {
            'canonical_url': 'https://github.com/' + project, 'purpose': 'isolated central goal acceptance fixture',
            'status': 'active', 'aliases': [], 'domains': []})
        operator.request('PATCH', account_prefix, {'provider': 'zai', 'capacity': 1, 'enabled': True, 'min_remaining': 0})
        imported = operator.request('PUT', account_prefix + '/credentials', {
            'account_id': 'zai-central', 'provider': 'zai', 'expected_generation': 0,
            'files': {'.config/goz/config.json': source}})
        operator.request('POST', account_prefix + '/discovery', {})
        quota = operator.request('GET', account_prefix + '/quota')
        require(quota.get('fresh') is True and quota.get('quota', {}).get('remaining', 0) > 0
                and quota['quota'].get('source') == 'provider:zai:generation:' + str(imported['generation']), 'fresh_central_quota_required')
        save_config(output / 'actual-central-quota.json', quota)
        proof['quota_remaining'] = quota['quota']['remaining']
        proof['quota_reset_at'] = quota['quota'].get('reset_at')
        proof['stages'].append('actual_central_provider_quota')
        paired = operator.request('POST', '/api/enrollments/pair', {'machine_id': machine, 'alias': machine})
        enrolled = Client(config['server']).request('POST', '/api/enrollments/complete', {'code': paired['pairing_code']})
        operator.request('PUT', account_prefix + '/hosts', {'hosts': [machine]})
        operator.request('PUT', '/api/model-policy', {'expected_version': 0, 'candidates': [{
            'id': 'glm-flash', 'engine': 'codex', 'model': args.native_model, 'effort': 'medium',
            'account_id': 'zai-central', 'weight': 1, 'capabilities': ['headless', 'interactive', 'goal', 'effort'], 'enabled': True}]})
        machine_path = output / 'machine.json'
        save_config(machine_path, {'server': config['server'], 'token': enrolled['token'], 'machine_id': machine,
                                  'roots': [str(workspace)], 'engines': ['codex'], 'launcher': launcher})
        expected = 'Hub2 approved central goal passed'
        hello = workspace / 'HELLO.txt'
        hub_read = workspace / 'HUB_READ.json'
        prompt = (f'Work only in {workspace}. Complete the mandatory launcher first-action proof before other work. '
                  f'Use the provided assigned Hub CLI and private worker config to read your own intake, then save its id and status only to {hub_read}. '
                  f'Write {hello} using pathlib.Path.write_text({expected!r},encoding="utf-8") with exactly those bytes. '
                  'Never print credentials, edit other projects, or contact other services. After these checks, mark this native goal complete and wait for controller cleanup; do not start more work.')
        intake = operator.request('POST', '/api/intakes' + project_query, {'key': 'intake-' + suffix,
            'title': 'Approved central native goal acceptance fixture', 'text': prompt,
            'acceptance_criteria': ['Correct central SDK credential source', 'Authoritative exact-thread goal proof t1',
                                    'Real worker scoped CLI read t1', 'Owned native cleanup and lease release t1']})['intake']
        proof['intake_id'] = intake['id']
        host_log = (output / 'host.log').open('w')
        private_file(output / 'host.log')
        environment = os.environ.copy()
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        agent = subprocess.Popen([sys.executable, '-m', 'agent_hub_host.cli', '--config', str(machine_path), 'serve'],
                                 env=environment, stdout=host_log, stderr=host_log, start_new_session=os.name != 'nt')
        # This is an explicit, isolated test capability, bound to reviewed runtime
        # source and approved central credential fingerprint. It is never a
        # production advertisement or an assertion of a previously proven route.
        host = Client(config['server'], enrolled['token'])
        host.heartbeat(['central-goal-dev-fixture'], ['codex'], ['headless', 'interactive:codex', 'goal:codex', 'effort:codex'])
        started = operator.request('POST', '/api/intakes/' + intake['id'] + '/start' + project_query, {
            'key': 'start-' + suffix, 'workspace': str(workspace), 'prompt': prompt,
            'interactive': True, 'with_goal': True, 'timeout_seconds': 180})
        session = started['session']['id']
        proof['session_id'] = session
        record_path = workspace / '.local' / ('launch-hub2-' + session + '.json')
        deadline = time.monotonic() + 240
        native = None
        while time.monotonic() < deadline:
            require(agent.poll() is None, 'host_exited')
            if record_path.exists():
                native = json.loads(record_path.read_text())
                native_id = native.get('session_id')
                if native.get('outcome') in {'no-valid-route', 'resource-refused', 'launch-failed'}:
                    raise RuntimeError('native_launch_refused')
                if native.get('first_action_validated_at') and native.get('goal_registration') and hello.exists() and hub_read.exists():
                    break
            time.sleep(2)
        else:
            raise RuntimeError('central_goal_proof_timeout')
        require(hello.read_text() == expected, 'native_artifact_mismatch')
        require(json.loads(hub_read.read_text()).get('id') == intake['id'], 'native_worker_cli_read_unproven')
        require(native.get('model_verified') is True and native.get('observed_model') == args.native_model, 'native_model_unverified')
        require(native.get('interactive_verified') is True and native.get('effort_verified') is True, 'native_modes_unverified')
        status = subprocess.run(['a', 'status', native_id, '--json'], capture_output=True, text=True, timeout=15, check=True)
        row = json.loads(status.stdout)
        require(row.get('engine') == 'codex' and row.get('workspace') == str(workspace), 'native_identity_mismatch')
        sys.path.insert(0, launcher['repository'])
        from launcher.interactive import goal_store
        from launcher.provider_home import verify
        actual_goal = goal_store(row)
        require(actual_goal and actual_goal.get('thread_id') == native['goal_registration'].get('thread_id')
                and actual_goal.get('objective_hash') == native['goal_registration'].get('objective_hash'), 'authoritative_goal_mismatch')
        homes = list((output / 'providers').glob('zai-central-*/manifest.json'))
        require(len(homes) == 1, 'central_provider_home_ambiguous')
        provider = verify(homes[0])
        require(provider['credential_hash'] == hashlib.sha256(source_key.encode()).hexdigest(), 'selected_sdk_source_not_central')
        require(source_key not in json.dumps(row) + json.dumps(native), 'provider_secret_in_native_metadata')
        child_paths = list((workspace / '.local/hub2-contexts').glob(session + '-*.json'))
        require(len(child_paths) == 1, 'worker_context_ambiguous')
        child_path = child_paths[0]
        child_config = json.loads(child_path.read_text())
        child = Client(child_config['server'], child_config['token'])
        child.request('GET', '/api/intakes/' + intake['id'] + project_query)
        proof.update(native_id=native_id, model=args.native_model, engine='codex', profile='hub-zai-central',
                     goal_registration=actual_goal, central_sdk_fingerprint_match=True, native_worker_cli_read=True,
                     first_action_verified=True, interactive_verified=True, effort_verified=True)
        proof['stages'].append('public_project_host_central_native_goal_and_worker_read')
        operator.request('POST', '/api/sessions/' + session + '/commands', {'key': 'stop-' + suffix, 'kind': 'stop'})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            fleet = operator.request('GET', '/api/fleet')
            status = next((item for item in fleet.get('items', []) if item.get('id') == machine), None)
            require(status is not None, 'owned_machine_not_in_fleet')
            deadlines = fixture(operator, '/_fixture/machine/' + machine + '/machine-deadlines')
            account = operator.request('GET', account_prefix)
            if not status.get('sessions') and not any(row['state'] == 'pending' for row in deadlines['outbox']) and account['account']['occupied'] == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError('native_cleanup_or_release_pending')
        try:
            child.request('GET', '/api/intakes/' + intake['id'] + project_query)
            raise RuntimeError('completed_worker_token_not_revoked')
        except ApiError as error:
            require(error.status == 401, 'unexpected_worker_revocation_refusal')
        metrics = {}
        for kind, identity in [('control', 'global'), ('project', project), ('machine', machine), ('account', 'zai-central')]:
            metrics[kind] = fixture(operator, '/_fixture/' + kind + '/' + quote(identity, safe='') + '/state')
            require(metrics[kind].get('alarm') is None, 'completed_alarm_not_null_' + kind)
        proof.update(passed=True, account_occupied=0, worker_token_revoked=True, all_completed_alarms_null=True,
                     metrics=metrics, outcome='native_goal_start_and_owned_cleanup; no business acceptance claim')
        proof['stages'].append('owned_stop_verified_exit_revocation_release_quiescence')
        return 0
    except Exception as error:
        proof['error_type'] = type(error).__name__
        proof['failure'] = str(error) if isinstance(error, (ApiError, RuntimeError)) else 'inspect_private_driver_stage'
        return 1
    finally:
        if native_id and not proof['passed']:
            # Only the exact native identity created in this disposable workspace.
            result = subprocess.run(['a', 'status', native_id, '--json'], capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                row = json.loads(result.stdout)
                if row.get('workspace') == str(workspace) and row.get('id') == native_id:
                    subprocess.run(['a', 'kill', native_id], capture_output=True, timeout=15)
        if agent and agent.poll() is None:
            if os.name == 'nt':
                agent.terminate()
            else:
                os.killpg(agent.pid, signal.SIGTERM)
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait()
        if host_log:
            host_log.close()
        save_config(output / 'result.json', proof)
        print(json.dumps({'passed': proof['passed'], 'stages': proof['stages'], 'native_id': native_id,
                          'evidence': str(output / 'result.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-config', type=Path, required=True)
    parser.add_argument('--launcher-config', type=Path, required=True)
    parser.add_argument('--account-source', type=Path, required=True)
    parser.add_argument('--managed-state', type=Path, default=Path.home() / '.config/agent-hub/managed-engines/state.json')
    parser.add_argument('--native-model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    sys.exit(main(parser.parse_args()))
