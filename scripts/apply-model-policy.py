#!/usr/bin/env python3
"""Show/apply policy, or prepare/apply/rollback/commit a private CAS recovery receipt.

Recovery requires --receipt and --source-sha. Ambiguous apply responses never
establish rollback ownership merely because the candidate is observed active.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = 'https://hub.dtcdev.click'
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / 'config/model-policy.json'
# Public Hub2 Worker forwarding limit. Control accepts a larger internal body.
WIRE_LIMIT = 98_304
HTTP_LIMIT = 128000
LIMIT = 4 * HTTP_LIMIT
SAFE_INT = 9007199254740991
PHASES = {'prepared', 'applying', 'applied', 'uncertain', 'rolling_back', 'rolled_back', 'committed'}


class Problem(Exception):
    pass


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise Problem('invalid_json')
        result[key] = value
    return result


def parse(raw, limit=LIMIT):
    if len(raw) > limit: raise Problem('metadata_too_large')
    try: return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError): raise Problem('invalid_json') from None


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def compact_utf8(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError):
        raise Problem('invalid_wire_json') from None


def wire_body(value, too_large='policy_request_body_too_large'):
    raw = compact_utf8(value)
    if len(raw) > WIRE_LIMIT: raise Problem(too_large)
    return raw


def candidates(value, empty=False):
    if not isinstance(value, list) or len(value) > 32 or not value and not empty: raise Problem('invalid_candidates')
    result, ids = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) - {'id','engine','model','effort','account_id','weight','capabilities','enabled'}: raise Problem('invalid_candidates')
        for key in ('id','engine','model','account_id'):
            if not isinstance(row.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}', row[key]): raise Problem('invalid_candidates')
        weight = row.get('weight'); caps = row.get('capabilities')
        if (type(weight) not in (int,float) or not math.isfinite(weight) or not 0 < weight <= 1000000
            or type(row.get('enabled')) is not bool or not isinstance(caps,list) or len(caps)>32
            or any(not isinstance(cap,str) or not cap.strip() or len(cap.encode())>80 for cap in caps)
            or 'effort' in row and row['effort'] not in ('low','medium','max') or row['id'] in ids): raise Problem('invalid_candidates')
        ids.add(row['id']); item = dict(row); item['capabilities'] = sorted(set(caps)); item['weight'] = int(weight) if weight == int(weight) else weight
        result.append(item)
    return result


def version(value):
    if type(value) is not int or not 0 <= value <= SAFE_INT: raise Problem('invalid_version')
    return value


def url(raw):
    parsed = urllib.parse.urlsplit(raw)
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path not in ('','/')
        or parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1')
        or any(ord(c)<33 for c in raw)):
        raise Problem('invalid_url')
    try: parsed.port
    except ValueError: raise Problem('invalid_url') from None
    return urllib.parse.urlunsplit((parsed.scheme,parsed.netloc.lower(),'','',''))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def request(base, token, method, body=None):
    req = urllib.request.Request(base+'/api/model-policy', data=None if body is None else wire_body(body), method=method,
                                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        response = urllib.request.build_opener(NoRedirect).open(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, ValueError): raise Problem('http_outcome_unknown') from None
    with response:
        raw = response.read(HTTP_LIMIT+1)
        return response.code, parse(raw, HTTP_LIMIT)


def snapshot(status, payload):
    if not isinstance(payload,dict): raise Problem('invalid_policy_response')
    at = version(payload.get('version'))
    if status==404 and payload.get('error')=='model_policy_unconfigured': return {'version':at,'candidates':[]}
    if status!=200 or at==0: raise Problem('policy_request_failed')
    return {'version':at,'candidates':candidates(payload.get('candidates'),empty=True)}


def read_policy(args, token): return snapshot(*request(args.url,token,'GET'))


def load_token(args):
    token = os.environ.get('HUB2_TOKEN')
    if not token:
        path = args.token_file or Path.home()/'.config/agent-hub/cli.json'
        with Path(path).open('rb') as stream: data = parse(stream.read(LIMIT+1))
        token = data.get('access_token') if isinstance(data,dict) else None
    if not isinstance(token,str) or not re.fullmatch(r'[^\s]{32,4096}',token): raise Problem('invalid_token')
    return token


def desired(args):
    with args.file.open('rb') as stream: data = parse(stream.read(HTTP_LIMIT+1), HTTP_LIMIT)
    if not isinstance(data,dict) or set(data)!={'candidates'}: raise Problem('invalid_candidates')
    return candidates(data['candidates'])


@contextlib.contextmanager
def lock(path):
    if not path.is_absolute(): raise Problem('receipt_path_not_absolute')
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077: raise Problem('receipt_parent_untrusted')
    fd = os.open(str(path)+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1: raise Problem('receipt_lock_untrusted')
        fcntl.flock(fd,fcntl.LOCK_EX); yield
    finally: os.close(fd)


def receipt_bytes(data):
    raw = compact_utf8(data)
    if len(raw) > LIMIT: raise Problem('receipt_too_large')
    return raw


def save(path, data):
    raw = receipt_bytes(data)
    if path.is_symlink() or path.exists() and not stat.S_ISREG(path.lstat().st_mode): raise Problem('receipt_untrusted')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally: Path(temporary).unlink(missing_ok=True)


def saved(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>LIMIT: raise Problem('receipt_untrusted')
        with os.fdopen(fd,'rb') as stream:
            fd=None; data=parse(stream.read(LIMIT+1))
    finally:
        if fd is not None: os.close(fd)
    if (not isinstance(data,dict) or set(data)-{'transaction_token'}!={'format','source_sha','source_digest','url','desired','before','phase','applied','changed'}
        or type(data['format']) is not int or data['format']!=1 or data['phase'] not in PHASES
        or not isinstance(data['source_sha'],str) or not re.fullmatch('[0-9a-f]{40}',data['source_sha'])
        or type(data['changed']) is not bool): raise Problem('receipt_invalid')
    if 'transaction_token' in data and (not isinstance(data['transaction_token'],str) or not re.fullmatch('[0-9a-f]{32}',data['transaction_token'])): raise Problem('receipt_invalid')
    data['desired']=candidates(data['desired'])
    if data['source_digest']!=hashlib.sha256(canonical(data['desired']).encode()).hexdigest() or url(data['url'])!=data['url']: raise Problem('receipt_invalid')
    for field in ('before','applied'):
        row=data[field]
        if row is None and field=='applied': continue
        if not isinstance(row,dict) or set(row)!={'version','candidates'}: raise Problem('receipt_invalid')
        version(row['version']); candidates(row['candidates'],empty=True)
        if row['version']==0 and row['candidates']: raise Problem('receipt_invalid')
    if data['changed']!=(data['before']['candidates']!=data['desired']): raise Problem('receipt_invalid')
    if data['applied'] is not None and (data['applied']['candidates']!=data['desired'] or data['applied']['version']!=data['before']['version']+int(data['changed'])): raise Problem('receipt_invalid')
    if data['phase'] in ('applied','rolling_back','rolled_back','committed') and data['applied'] is None: raise Problem('receipt_invalid')
    if data['phase'] in ('prepared','applying','uncertain') and data['applied'] is not None: raise Problem('receipt_invalid')
    return data


RECOVERY_PROTOCOL = 'model-policy-recovery-v1'


def recovery_body(action, before, target):
    return {'protocol': RECOVERY_PROTOCOL, 'action': action,
            'expected_version': before['version'], 'candidates': target}


def recovery_request(args, token, action, before, target):
    status, payload = request(args.url, token, 'POST', recovery_body(action, before, target))
    if status == 409 and isinstance(payload, dict):
        if payload.get('error') == 'model_policy_prior_unrestorable':
            raise Problem('rollback_policy_unrestorable')
        if payload.get('error') == 'model_policy_rollback_body_too_large':
            raise Problem('rollback_policy_body_too_large')
    if (status != 200 or not isinstance(payload, dict) or payload.get('protocol') != RECOVERY_PROTOCOL
            or payload.get('action') != action):
        raise Problem('policy_recovery_protocol_unverified')
    return payload


def validate_recovery(args, token, before, target):
    payload = recovery_request(args, token, 'validate_recovery', before, target)
    validated = {'version': version(payload.get('version')), 'candidates': candidates(payload.get('candidates'), empty=True)}
    if validated != before or payload.get('changed') is not (before['candidates'] != target):
        raise Problem('policy_recovery_preflight_mismatch')


def rollback_body(before, applied_version):
    body = {'candidates': before['candidates'], 'expected_version': applied_version}
    if not before['candidates']: body['restore_unconfigured'] = True
    return body


def check_recovery_sizes(data):
    applied_version = version(data['before']['version'] + int(data['changed']))
    if data['changed']:
        version(applied_version + 1)
        wire_body(rollback_body(data['before'], applied_version), 'rollback_policy_body_too_large')
    # The longest later phase also stores a second copy of the desired policy.
    applied = {'version': applied_version, 'candidates': data['desired']}
    receipt_bytes({**data, 'phase': 'rolling_back', 'applied': applied})


def recovery(args, token):
    if not args.receipt or not args.source_sha or not re.fullmatch('[0-9a-f]{40}',args.source_sha): raise Problem('recovery_identity_required')
    transaction_token=getattr(args,'transaction_token',None)
    if transaction_token is not None and not re.fullmatch('[0-9a-f]{32}',transaction_token): raise Problem('invalid_transaction_token')
    with lock(args.receipt):
        if args.receipt.is_symlink() or args.receipt.exists() and not stat.S_ISREG(args.receipt.lstat().st_mode): raise Problem('receipt_untrusted')
        new_receipt = args.action=='prepare' and not args.receipt.exists()
        if new_receipt:
            target=desired(args); before=read_policy(args,token)
            data={'format':1,'source_sha':args.source_sha,'source_digest':hashlib.sha256(canonical(target).encode()).hexdigest(),
                  'url':args.url,'desired':target,'before':before,'phase':'prepared','applied':None,'changed':before['candidates']!=target}
            if transaction_token is not None: data['transaction_token']=transaction_token
        else: data=saved(args.receipt)
        if data.get('transaction_token')!=transaction_token: raise Problem('recovery_transaction_mismatch')
        if data['source_sha']!=args.source_sha or data['url']!=args.url: raise Problem('recovery_identity_mismatch')
        if args.action in ('prepare','apply','commit') and desired(args)!=data['desired']: raise Problem('source_policy_changed')
        if args.action=='prepare':
            if data['phase']=='prepared':
                check_recovery_sizes(data)
                validate_recovery(args, token, data['before'], data['desired'])
            if new_receipt:
                save(args.receipt,data)
            return {'status':data['phase'],'version':data['before']['version']}
        if args.action=='commit':
            if data['phase'] not in ('applied','committed'): raise Problem('commit_ownership_unproved')
            if read_policy(args,token)!=data['applied']: raise Problem('commit_policy_conflict_manual_reconciliation_required')
            data['phase']='committed'; save(args.receipt,data)
            return {'status':'committed','version':data['applied']['version']}
        if args.action=='apply':
            if data['phase']=='applied':
                if read_policy(args,token)!=data['applied']: raise Problem('apply_policy_conflict_manual_reconciliation_required')
                return {'status':'applied','version':data['applied']['version']}
            if data['phase']=='uncertain': raise Problem('apply_uncertain_manual_reconciliation_required')
            if data['phase'] not in ('prepared','applying'): raise Problem('receipt_phase_conflict')
            check_recovery_sizes(data)
            wire_body(recovery_body('apply_recoverable', data['before'], data['desired']))
            current=read_policy(args,token)
            if current!=data['before']:
                data['phase']='uncertain'; save(args.receipt,data); raise Problem('apply_uncertain_manual_reconciliation_required')
            data['phase']='applying'; save(args.receipt,data)  # durable before any mutation
            try:
                payload=recovery_request(args,token,'apply_recoverable',data['before'],data['desired'])
                applied=snapshot(200,payload)
                if applied!={'version':data['before']['version']+int(data['changed']),'candidates':data['desired']}: raise Problem('apply_response_invalid')
            except Problem:
                # Observe, but NEVER acquire ownership through GET equality.
                try: observed=read_policy(args,token)
                except Problem: observed=None
                data['phase']='applying' if observed==data['before'] else 'uncertain'; save(args.receipt,data)
                raise Problem('apply_uncertain_manual_reconciliation_required') from None
            data['applied']=applied; data['phase']='applied'; save(args.receipt,data)
            return {'status':'applied','version':applied['version'],'changed':data['changed']}
        if args.action!='rollback': raise Problem('invalid_recovery_action')
        if data['phase']=='rolled_back': return {'status':'rolled_back'}
        if data['phase'] not in ('applied','rolling_back'): raise Problem('rollback_ownership_unproved_manual_reconciliation_required')
        if not data['changed']:
            data['phase']='rolled_back'; save(args.receipt,data); return {'status':'rolled_back','changed':False}
        restored={'version':data['applied']['version']+1,'candidates':data['before']['candidates']}
        current=read_policy(args,token)
        if data['phase']=='rolling_back' and current==restored:
            data['phase']='rolled_back'; save(args.receipt,data); return {'status':'rolled_back','observed_restored':True}
        if current!=data['applied']: raise Problem('rollback_policy_conflict_manual_reconciliation_required')
        data['phase']='rolling_back'; save(args.receipt,data)
        body=rollback_body(data['before'],data['applied']['version'])
        try:
            status,payload=request(args.url,token,'PUT',body)
            if status!=200 or snapshot(status,payload)!=restored: raise Problem('rollback_response_invalid')
        except Problem:
            try: current=read_policy(args,token)
            except Problem: current=None
            if current!=restored: raise Problem('rollback_uncertain_manual_reconciliation_required') from None
        data['phase']='rolled_back'; save(args.receipt,data)
        return {'status':'rolled_back','version':restored['version']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['show','prepare','apply','rollback','commit'],nargs='?',default='show')
    parser.add_argument('--url',default=DEFAULT_URL); parser.add_argument('--file',type=Path,default=DEFAULT_CONFIG)
    parser.add_argument('--token-file',type=Path); parser.add_argument('--expected-version',type=int)
    parser.add_argument('--receipt',type=Path); parser.add_argument('--source-sha'); parser.add_argument('--transaction-token')
    args=parser.parse_args(argv)
    try:
        args.url=url(args.url); token=load_token(args)
        if (args.source_sha or args.transaction_token) and not args.receipt: raise Problem('receipt_required_for_source_identity')
        if args.receipt or args.action in ('prepare','rollback','commit'):
            if args.expected_version is not None: raise Problem('recovery_uses_receipt_cas')
            result=recovery(args,token)
        elif args.action=='show':
            status,payload=request(args.url,token,'GET')
            if status==200: result=snapshot(status,payload)
            else:
                result={'error':'model_policy_unconfigured' if isinstance(payload,dict) and payload.get('error')=='model_policy_unconfigured' else 'policy_request_failed'}
                if isinstance(payload,dict) and type(payload.get('version')) is int: result['version']=version(payload['version'])
            print(json.dumps(result,sort_keys=True)); return 0 if status==200 else 1
        else:
            body={'candidates':desired(args)}
            if args.expected_version is not None: body['expected_version']=version(args.expected_version)
            status,payload=request(args.url,token,'PUT',body)
            if status!=200: raise Problem('policy_apply_failed')
            result=snapshot(status,payload)
            if type(payload.get('unchanged')) is bool: result['unchanged']=payload['unchanged']
        print(json.dumps(result,sort_keys=True)); return 0
    except (Problem,OSError,ValueError,TypeError,KeyError) as error:
        slug=str(error) if isinstance(error,Problem) and re.fullmatch('[a-z_]{1,100}',str(error)) else 'policy_recovery_failed'
        print(json.dumps({'error':slug,'manual_reconciliation_required':True})); return 1


if __name__=='__main__': sys.exit(main())
