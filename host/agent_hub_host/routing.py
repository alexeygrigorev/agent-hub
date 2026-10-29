"""Small stdlib client for approved, capability-routed tasks."""
import json
import base64
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote
from . import bus


def add_commands(sub):
    submit = sub.add_parser('submit', help='Submit one approved action or a batch to the task queue')
    submit.add_argument('--needs', default='', help='Comma-separated capabilities, e.g. browser:orin,phone-sms')
    submit.add_argument('--prompt-file')
    submit.add_argument('--approved-action-file', help='Exact authorized action; defaults to the self-contained prompt')
    submit.add_argument('--batch-file', help='JSON list of {prompt, approved_action}; all tasks share --needs')
    submit.add_argument('--workspace', default='tasks')
    submit.add_argument('--tier', help='deep/daily/quick or an alias; default daily')
    submit.add_argument('--model', help='Configured model alias or exact model id')
    submit.add_argument('--difficulty', choices=['hard','normal','concrete'])
    submit.add_argument('--runner', dest='preferred_runner')
    submit.add_argument('--machine', dest='preferred_machine')
    submit.add_argument('--key', help='Reuse this idempotency key for safe retries')
    sub.add_parser('routing-machines', help='List capability advertisements, model routes and host health')
    policy = sub.add_parser('model-policy', help='Show routing policy or save Grok preference (global policy keeps routing disabled)')
    policy.add_argument('provider', nargs='?', choices=['grok'])
    policy.add_argument('action', nargs='?', choices=['set'])
    policy.add_argument('mode', nargs='?', choices=['minimal', 'disabled', 'normal'])
    policy.add_argument('--reason')
    policy.add_argument('--json', action='store_true')
    release = sub.add_parser('host-release', help='Operator: pin the exact main commit a checkout-installed host updates itself to')
    release.add_argument('--machine', required=True, help='Machine id')
    target = release.add_mutually_exclusive_group(required=True)
    target.add_argument('--sha', help='Full 40-hex commit on origin/main')
    target.add_argument('--clear', action='store_true', help='Remove the pin; the host stays on its current commit')
    release = sub.add_parser('launcher-release', help='Operator: pin the exact main commit a checkout-installed launcher updates itself to')
    release.add_argument('--machine', required=True, help='Machine id')
    target = release.add_mutually_exclusive_group(required=True)
    target.add_argument('--sha', help='Full 40-hex commit on origin/main')
    target.add_argument('--clear', action='store_true', help='Remove the pin; the host stays on its current commit')
    sub.add_parser('jobs', help='List this client’s most recent 100 jobs')
    job = sub.add_parser('job', help='Get a job’s status, redacted report and artifacts')
    job.add_argument('id')
    upload = sub.add_parser('task-artifact', help='Assigned host only: upload a reviewed, sanitized PNG')
    upload.add_argument('id')
    upload.add_argument('--name', required=True)
    upload.add_argument('--file', required=True)
    upload.add_argument('--sanitized', action='store_true', required=True, help='Confirm pixels and metadata contain no secrets')
    download = sub.add_parser('artifact', help='Download a task PNG to a new private file')
    download.add_argument('id')
    download.add_argument('name')
    download.add_argument('--output', required=True)
    result = sub.add_parser('task-result', help='Host-only: report progress or final result for a routed task')
    result.add_argument('id')
    result.add_argument('--status', required=True, choices=['running', 'waiting-on-human', 'stuck', 'done', 'failed'])
    result.add_argument('--report-file', required=True)
    result.add_argument('--artifacts-file', help='JSON list of {name,text}; sanitized text only')
    result.add_argument('--confirmed-exit', action='store_true')


def read_text(path):
    p = Path(path)
    if p.stat().st_size > 24000:
        raise ValueError('Input exceeds task size limit')
    return p.read_text(encoding='utf-8')


def execute(args):
    if args.cmd == 'submit':
        payload = {'needs': [n.strip() for n in args.needs.split(',') if n.strip()], 'workspace': args.workspace,
                   'idempotency_key': args.key or str(uuid.uuid4())}
        if args.batch_file:
            if args.prompt_file or args.approved_action_file:
                raise ValueError('Choose prompt or batch')
            payload['tasks'] = json.loads(read_text(args.batch_file))
        else:
            if not args.prompt_file:
                raise ValueError('A self-contained prompt file is required')
            payload['prompt'] = read_text(args.prompt_file)
            payload['approved_action'] = read_text(args.approved_action_file) if args.approved_action_file else payload['prompt']
        for key in ('preferred_machine', 'preferred_runner', 'tier', 'model', 'difficulty'):
            if getattr(args, key):
                payload[key] = getattr(args, key)
        return bus._do_request('POST', '/api/fleet/tasks', payload)
    if args.cmd == 'model-policy' and args.provider:
        path = '/api/fleet/routing/policy/grok'
        if args.action == 'set':
            if not args.mode or not args.reason or not args.reason.strip():
                raise ValueError('Setting Grok policy requires mode and --reason TEXT')
            return bus._do_request('POST', path, {'mode': args.mode, 'reason': args.reason})
        if args.mode or args.reason:
            raise ValueError('Use grok set MODE --reason TEXT')
        return bus._do_request('GET', path)
    if args.cmd == 'model-policy' and (args.action or args.mode or args.reason):
        raise ValueError('Use model-policy grok set MODE --reason TEXT')
    if args.cmd in ('routing-machines', 'model-policy'):
        return bus._do_request('GET', '/api/fleet/routing/' + ('machines' if args.cmd == 'routing-machines' else 'policy'))
    if args.cmd in ('host-release', 'launcher-release'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.machine):
            raise ValueError('Invalid machine id')
        if args.sha is not None and not re.fullmatch(r'[0-9a-fA-F]{40}', args.sha):
            raise ValueError('A full 40-hex commit is required')
        return bus._do_request('PUT', '/api/fleet/routing/' + args.cmd + '/' + quote(args.machine, safe=''),
                               {'sha': None if args.clear else args.sha.lower()})
    if args.cmd == 'jobs':
        return bus._do_request('GET', '/api/fleet/tasks')
    if not re.fullmatch(r'[a-f0-9-]{36}', args.id):
        raise ValueError('Invalid job id')
    path = '/api/fleet/tasks/' + quote(args.id, safe='')
    if args.cmd == 'task-artifact':
        source = Path(args.file)
        if source.is_symlink() or source.stat().st_size > 196608:
            raise ValueError('PNG must be a bounded regular private file')
        data = source.read_bytes()
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('PNG required')
        return bus._do_request('POST', path + '/artifacts', {'name': args.name, 'media_type': 'image/png', 'data_base64': base64.b64encode(data).decode('ascii'), 'sanitized': args.sanitized})
    if args.cmd == 'artifact':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.name): raise ValueError('Invalid artifact name')
        result = bus._do_request('GET', path + '/artifacts/' + quote(args.name, safe=''))
        data = base64.b64decode(result['data_base64'], validate=True)
        if len(data) > 196608 or not data.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('Invalid artifact')
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(data)
        return {'saved': True, 'media_type': 'image/png'}
    if args.cmd == 'job':
        return bus._do_request('GET', path)
    payload = {'status': args.status, 'report': read_text(args.report_file),
               'artifacts': json.loads(read_text(args.artifacts_file)) if args.artifacts_file else [],
               'confirmed_exit': args.confirmed_exit}
    return bus._do_request('POST', path + '/result', payload)
