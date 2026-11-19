"""Qualified one-shot interactive wake. Inbox persistence is never delivery proof.

A durable command intent precedes message creation. After an interrupted unknown
creation we never send again. A recorded original envelope can be reconciled by
its native guarded deliver operation (which never creates another envelope).
No PTY injection, identity override, provider action or headless resume.
"""
import hashlib
import itertools
import os
import json
import logging
import queue
import threading
import time
import uuid

# Closed private strict-attempt diagnostics: fixed 10-key record schema with
# per-field closed vocabularies. Unknown values collapse to 'unrecognized',
# explicit native nulls to 'unknown', missing flags to 'absent'; booleans are
# never accepted where a vocabulary string is required. Every record binds one
# opaque command correlation plus one process-local per-invocation attempt
# token, so interleaved commands and retries stay attributable without raw
# request, text, path or environment data. Emission is bounded best-effort and
# never blocks the native-control path: the record is serialized in the caller,
# dropped into a bounded in-process queue (drop-on-full) and drained by one
# lifecycle-contained daemon worker that writes through the existing Host
# logging sink. A slow or broken sink delays or loses records only. The
# monotonic budget is captured before the first emission; diagnostics cost
# microseconds and the original remaining/deadline checks still gate every
# native effect and finalization, so logging neither widens nor renews the
# attempt budget. Logging never authorizes, retries or alters effects.
STRICT_DIAG_LOGGER='agent_hub_host.wake_send.strict_diag'
_STRICT_DIAG=logging.getLogger(STRICT_DIAG_LOGGER)
_STRICT_DIAG.addHandler(logging.NullHandler())
_STRICT_DIAG.setLevel(logging.INFO)
_STRICT_DIAG_RAISED={'invalid_wake_command':'validation_refused','command_id_conflict':'validation_refused',
    'wake_fence_conflict':'identity_refused','headless_send_refused':'identity_refused',
    'wake_identity_unverified':'identity_refused','wake_local_context_unverified':'context_refused',
    'wake_deadline_exceeded':'deadline_exceeded','wake_outcome_uncertain':'outcome_uncertain',
    'wake_receipt_unverified':'receipt_unverified','duplicate_receipt_field':'receipt_unverified'}
_STRICT_DIAG_EVENTS=frozenset(('attempt_start','validation_refused','identity_refused','context_refused',
    'deadline_exceeded','outcome_uncertain','receipt_unverified','command_intent_committed','envelope_stored',
    'envelope_reread','submit_attempted','submit_pending','submit_verified','done_replay','unrecognized'))
_STRICT_DIAG_PHASES=frozenset(('entry','validated','identity','context','envelope','submit','receipt','terminal'))
_STRICT_DIAG_STATUSES=frozenset(('submitted_verified','refused','uncertain'))
_STRICT_DIAG_REASONS=frozenset(_STRICT_DIAG_RAISED)|frozenset(('submission_observed','preflight_refused',
    'input_busy','invalid_text','dispatch_fenced','prior_dispatch_uncertain','receipt_unreadable',
    'deadline_expired','identity_mismatch','not_idle','input_client_attached','input_not_settled',
    'unknown_layout','unsafe_composer','readiness_changed','paste_uncertain','landing_uncertain',
    'landing_changed','enter_uncertain'))
_STRICT_DIAG_QUEUE=queue.Queue(maxsize=128)
_STRICT_DIAG_WORKER_LOCK=threading.Lock()
_STRICT_DIAG_WORKER_THREAD=[]
_STRICT_DIAG_ATTEMPTS=itertools.count()
_ABSENT=object()

def _strict_diag_status(value):
    'Closed status vocabulary; only exact vocabulary strings qualify.'
    if value is _ABSENT:return 'absent'
    return value if isinstance(value,str) and value in _STRICT_DIAG_STATUSES else 'unrecognized'

def _strict_diag_reason(value):
    'Closed reason vocabulary; only exact vocabulary strings qualify.'
    if value is _ABSENT:return 'absent'
    return value if isinstance(value,str) and value in _STRICT_DIAG_REASONS else 'unrecognized'

def _strict_diag_flag(value,present):
    'Native flags: exact bool, explicit null is unknown, missing is absent, any other type is unrecognized.'
    if not present:return 'absent'
    if value is None:return 'unknown'
    if value is True:return 'true'
    if value is False:return 'false'
    return 'unrecognized'

def _strict_diag_receipt_field(receipt,key):
    'Typed receipt access: distinguishes an explicit native null from an omitted flag.'
    if isinstance(receipt,dict) and key in receipt:return receipt[key]
    return _ABSENT

def _strict_diag_worker():
    'One daemon worker for the whole Host process lifetime: bounded, self-contained, never joined.'
    while True:
        try:line=_STRICT_DIAG_QUEUE.get(timeout=5)
        except Exception:continue
        try:_STRICT_DIAG.info('%s',line)
        except Exception:pass  # A diagnostic sink failure never alters the guarded attempt.

def _strict_diag_emit(line):
    try:
        if _STRICT_DIAG_QUEUE.full():return  # Drop-on-full keeps the control path free of head-of-line blocking.
        _STRICT_DIAG_QUEUE.put_nowait(line)
    except Exception:
        return
    if _STRICT_DIAG_WORKER_THREAD:return
    with _STRICT_DIAG_WORKER_LOCK:
        if _STRICT_DIAG_WORKER_THREAD:return
        worker=threading.Thread(target=_strict_diag_worker,name='wake-strict-diag',daemon=True)
        _STRICT_DIAG_WORKER_THREAD.append(worker)
        worker.start()

def _strict_diag_correlation(command):
    'Bounded opaque correlation for one strict attempt: the canonical command UUID when valid, else a truncated SHA256 over closed identity fields only. Never payload text, paths, environment or receipt content.'
    value=command.get('id') if isinstance(command,dict) else None
    if isinstance(value,str):
        try:
            if str(uuid.UUID(value))==value:return value
        except ValueError:pass
    binding=command.get('wake') if isinstance(command,dict) and isinstance(command.get('wake'),dict) else {}
    try:
        canonical=json.dumps([value,command.get('kind') if isinstance(command,dict) else None,
            binding.get('session_id'),binding.get('operation_id'),binding.get('epoch'),
            binding.get('native_id'),binding.get('tag'),binding.get('workspace'),
            binding.get('created_at_ms'),binding.get('deadline_ms'),binding.get('transport')],
            ensure_ascii=True,separators=(',',':'))
    except (TypeError,ValueError):
        return 'unknown'
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]

def _strict_diag_record(correlation,attempt,category,phase,status=_ABSENT,reason=_ABSENT,
                        mutation=_ABSENT,landed=_ABSENT,submitted=_ABSENT,replayed=_ABSENT):
    try:
        record={'event':category if category in _STRICT_DIAG_EVENTS else 'unrecognized',
                'phase':phase if phase in _STRICT_DIAG_PHASES else 'unrecognized',
                'command':correlation,'attempt':attempt,
                'status':_strict_diag_status(status),
                'reason':_strict_diag_reason(reason),
                'mutation':_strict_diag_flag(mutation,mutation is not _ABSENT),
                'landed':_strict_diag_flag(landed,landed is not _ABSENT),
                'submitted':_strict_diag_flag(submitted,submitted is not _ABSENT),
                'replayed':_strict_diag_flag(replayed,replayed is not _ABSENT)}
        line='wake_strict_diag '+json.dumps(record,sort_keys=True,separators=(',',':'))
    except Exception:
        return  # All diagnostic construction is guarded; nothing here can alter the attempt.
    _strict_diag_emit(line)


def execute(bridge, command, digest):
    if isinstance(command.get('wake'),dict) and 'transport' in command['wake']:
        return execute_strict(bridge,command,digest)
    db = bridge.journal.db
    identity = command['id']
    binding = command.get('wake')
    if not isinstance(binding, dict) or command.get('kind') != 'send':
        raise ValueError('invalid_wake_command')
    fields = {'id', 'session_id', 'operation_id', 'epoch', 'native_id', 'tag', 'workspace', 'created_at_ms', 'deadline_ms'}
    upgraded = set(binding) == fields | {'native_message_id', 'ack_deadline_ms'}
    if (set(binding) != fields and not upgraded or binding['session_id'] != command.get('session_id')
            or binding['epoch'] != command.get('epoch') or type(binding['epoch']) is not int
            or type(binding['created_at_ms']) is not int or binding['created_at_ms'] <= 0
            or type(binding['deadline_ms']) is not int):
        raise ValueError('invalid_wake_command')
    if upgraded and (type(binding['ack_deadline_ms']) is not int or not binding['deadline_ms'] <= binding['ack_deadline_ms'] <= binding['deadline_ms']+600000
                     or str(uuid.UUID(binding['native_message_id'])) != binding['native_message_id']):
        raise ValueError('invalid_wake_command')
    text = command.get('text')
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 8192:
        raise ValueError('invalid_wake_command')
    start = time.monotonic()
    def remaining():
        left = min(30 - (time.monotonic() - start), (binding.get('ack_deadline_ms', binding['deadline_ms']) - time.time()*1000)/1000)
        if left <= 0:
            raise RuntimeError('wake_deadline_exceeded')
        return left
    def qualified():
        row = db.execute('SELECT * FROM launches WHERE session_id=?', (binding['session_id'],)).fetchone()
        if (not row or row['operation_id'] != binding['operation_id'] or row['epoch'] != binding['epoch']
                or row['native_id'] != binding['native_id'] or row['tag'] != binding['tag']
                or row['workspace'] != binding['workspace'] or row['state'] != 'running'):
            raise ValueError('wake_fence_conflict')
        if row['interactive'] != 1:
            raise ValueError('headless_send_refused')
        status = bridge.lookup(binding['native_id'], row, timeout=remaining())
        if (not status or status.get('phase', status.get('state')) != 'running'
                or status.get('created_at_ms') != binding['created_at_ms']):
            raise ValueError('wake_identity_unverified')
        return row
    old = db.execute('SELECT * FROM commands WHERE id=?', (identity,)).fetchone()
    if old and old['digest'] != digest:
        # One server-authorized phase transition, not a renewed caller deadline.
        original = dict(command);original['wake'] = {k:v for k,v in binding.items() if k not in {'ack_deadline_ms','native_message_id'}}
        original_digest=hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        progress=json.loads(old['result']) if old['result'] else None
        if (not upgraded or old['state']!='executing' or old['digest']!=original_digest
                or not isinstance(progress,dict) or progress.get('envelope_id')!=binding['native_message_id']):
            raise ValueError('command_id_conflict')
        with db:db.execute('UPDATE commands SET digest=? WHERE id=? AND digest=?',(digest,identity,old['digest']))
        old=db.execute('SELECT * FROM commands WHERE id=?',(identity,)).fetchone()
    if old and old['state'] == 'done':
        result = json.loads(old['result'])
        bridge.journal.emit('command:'+identity, '/api/sessions/'+binding['session_id']+'/commands/'+identity+'/ack',
                            {'epoch':binding['epoch'], 'result':result})
        return result
    row = qualified()
    progress = json.loads(old['result']) if old and old['result'] else None
    if old and not progress:
        raise RuntimeError('wake_outcome_uncertain')
    if not old and upgraded:
        raise RuntimeError('wake_receipt_unverified')
    if not old:
        with db:
            bridge.begin_command(identity, digest)
        # Correlation is carried in the actual native envelope, never invented
        # after the native effect. Its returned id is the only deliver target.
        sent = bridge.native('message', 'send', '--workspace', row['workspace'], '--to', row['tag'], '--kind', 'hub-wake',
                             '--data', json.dumps({'correlation_id':identity,
                                                   'body_digest':hashlib.sha256(text.encode()).hexdigest()}),
                             '--json', '--', text, timeout=remaining())
        envelope = json.loads(sent.stdout)
        if not isinstance(envelope, dict):
            raise RuntimeError('wake_receipt_unverified')
        envelope_id = str(uuid.UUID(envelope.get('id', '')))
        target = envelope.get('to')
        if not isinstance(target, dict) or target.get('tag') != row['tag'] or target.get('session_id') != row['native_id']:
            raise RuntimeError('wake_receipt_unverified')
        progress = {'envelope_id':envelope_id}
        with db:
            db.execute("UPDATE commands SET result=? WHERE id=? AND state='executing'", (json.dumps(progress), identity))
    if (not isinstance(progress, dict) or set(progress) != {'envelope_id'}
            or str(uuid.UUID(progress.get('envelope_id', ''))) != progress['envelope_id']):
        raise RuntimeError('wake_receipt_unverified')
    if not upgraded:
        qualified();remaining()
        return {'wake_pending':True,'native_message_id':progress['envelope_id']}
    if binding['native_message_id'] != progress['envelope_id']:
        raise RuntimeError('wake_receipt_unverified')
    qualified()  # Original native incarnation must still own the effect.
    delivery = bridge.native('message', 'deliver', '--workspace', row['workspace'], '--json',
                             progress['envelope_id'], allow_failure=True, timeout=remaining())
    receipt = json.loads(delivery.stdout)
    status = receipt.get('status') if isinstance(receipt, dict) else None
    qualified()
    remaining()
    if delivery.returncode != 0 or status not in {'submitted', 'already-submitted', 'recipient-acked'}:
        # Unknown/unsupported/busy outcomes remain pending, never success. The
        # server owns the immutable10minute deadline and missed handoff.
        return {'wake_pending':True,'native_message_id':progress['envelope_id']}
    result = {'delivery':'native_acknowledged', 'delivered_at_ms':int(time.time()*1000),
              'wake_proof':{'kind':'native_delivery','status':status,
                            'native_message_id':progress['envelope_id'],
                            'sender_native_id':binding['native_id'],'sender_tag':binding['tag'],
                            'sender_workspace':binding['workspace'],'sender_generation':binding['epoch'],
                            'sender_created_at_ms':binding['created_at_ms']}}
    with db:
        bridge.finish_command(identity, command, result)
    return result

def execute_strict(bridge, command, digest):
    """A native once-ID guards retries; no mailbox or sender impersonation."""
    # One monotonic budget, one command correlation and one process-local
    # attempt token, all fixed before the first diagnostic emission. Emission
    # itself never blocks and cannot widen or renew this budget.
    start = time.monotonic()
    try:
        correlation = _strict_diag_correlation(command)
    except Exception:
        correlation = 'unknown'
    try:
        attempt = '%012x' % next(_STRICT_DIAG_ATTEMPTS)
    except Exception:
        attempt = 'unknown'
    phases=['entry']
    try:
        return _execute_strict_locked(bridge,command,digest,phases,correlation,attempt,start)
    except Exception as error:
        # Typed known-reason extraction only; arbitrary exception formatting
        # (including __str__) is never invoked, and the exact original object
        # is always re-raised below.
        try:
            reason='unrecognized'
            if isinstance(error,(ValueError,RuntimeError)):
                args=error.args
                if args and type(args[0]) is str and args[0] in _STRICT_DIAG_RAISED:
                    reason=args[0]
            _strict_diag_record(correlation,attempt,_STRICT_DIAG_RAISED.get(reason,'unrecognized'),
                                phases[0],reason=reason)
        except Exception:
            pass
        raise

def _execute_strict_locked(bridge,command,digest,phases,correlation,attempt,start):
    """Strict attempt body; phases[0] carries the closed diagnostic phase."""
    def diag(category,**fields):_strict_diag_record(correlation,attempt,category,phases[0],**fields)
    diag('attempt_start')
    inbox = command.get('wake', {}).get('transport') == 'strict-inbox:v1'
    db = bridge.journal.db
    identity = command.get('id')
    binding = command.get('wake')
    fields = {'id','session_id','operation_id','epoch','native_id','tag','workspace',
              'created_at_ms','deadline_ms','transport'}
    if (not isinstance(binding, dict) or set(binding) != fields
            or binding['transport'] not in {'strict-submit:v1','strict-inbox:v1'} or command.get('kind') != 'send'
            or binding['session_id'] != command.get('session_id')
            or binding['epoch'] != command.get('epoch') or type(binding['epoch']) is not int
            or binding['epoch'] <= 0 or type(binding['created_at_ms']) is not int
            or binding['created_at_ms'] <= 0 or type(binding['deadline_ms']) is not int
            or not isinstance(identity,str) or str(uuid.UUID(identity)) != identity
            or not isinstance(binding['native_id'],str)
            or str(uuid.UUID(binding['native_id'])) != binding['native_id']):
        raise ValueError('invalid_wake_command')
    text = command.get('text')
    if not isinstance(text,str) or not text.strip() or len(text.encode()) > 8192:
        raise ValueError('invalid_wake_command')
    phases[0]='validated'
    def remaining():
        left = min(30-(time.monotonic()-start), (binding['deadline_ms']-time.time()*1000)/1000)
        if left <= 0:
            raise RuntimeError('wake_deadline_exceeded')
        return left
    def qualified():
        row = db.execute('SELECT * FROM launches WHERE session_id=?',(binding['session_id'],)).fetchone()
        if (not row or row['operation_id'] != binding['operation_id'] or row['epoch'] != binding['epoch']
                or row['native_id'] != binding['native_id'] or row['tag'] != binding['tag']
                or row['workspace'] != binding['workspace'] or row['state'] != 'running'):
            raise ValueError('wake_fence_conflict')
        if row['interactive'] != 1:
            raise ValueError('headless_send_refused')
        status = bridge.lookup(binding['native_id'],row,timeout=remaining())
        if (not status or status.get('phase',status.get('state')) != 'running'
                or status.get('created_at_ms') != binding['created_at_ms']):
            raise ValueError('wake_identity_unverified')
    old = db.execute('SELECT * FROM commands WHERE id=?',(identity,)).fetchone()
    if old and old['digest'] != digest:
        raise ValueError('command_id_conflict')
    if old and old['state'] == 'done':
        phases[0]='terminal'
        diag('done_replay')
        result = json.loads(old['result'])
        bridge.journal.emit('command:'+identity,'/api/sessions/'+binding['session_id']+'/commands/'+identity+'/ack',
                            {'epoch':binding['epoch'],'result':result})
        return result
    phases[0]='identity'
    qualified()
    wire_text = text
    envelope = None
    if inbox:
        phases[0]='context'
        # No identity/workspace override. Native also discovers ancestor IDs;
        # only its explicit silent no-identity result qualifies this route.
        if os.environ.get('APLEXER_WORKSPACE'):
            raise RuntimeError('wake_local_context_unverified')
        who = bridge.native('whoami','--json',allow_failure=True,timeout=remaining(),cwd=binding['workspace'])
        if who.returncode != 1 or who.stdout != '' or who.stderr != '':
            raise RuntimeError('wake_local_context_unverified')
        qualified();remaining()
        progress = json.loads(old['result']) if old and old['result'] else None
        if old and not progress:
            raise RuntimeError('wake_outcome_uncertain')
        data = {'command_id':identity,'wake_binding':binding,
                'body_sha256':hashlib.sha256(text.encode()).hexdigest()}
        def validated(value):
            keys={'schema_version','id','workspace','created_at','from','to','kind','body','data','delivery'}
            if (not isinstance(value,dict) or set(value) != keys
                    or type(value['schema_version']) is not int or value['schema_version'] != 1
                    or type(value['created_at']) is not int or value['created_at'] <= 0
                    or value['workspace'] != binding['workspace'] or value['kind'] != 'hub-wake'
                    or value['body'] != text or value['data'] != data or value['delivery'] != 'inbox'
                    or value['from'] != {'external':True} or value['from'].get('external') is not True
                    or value['to'] != {'tag':binding['tag'],'session_id':binding['native_id']}
                    or not isinstance(value['id'],str) or str(uuid.UUID(value['id'])) != value['id']):
                raise RuntimeError('wake_receipt_unverified')
            return value
        def decode(raw):
            def unique(pairs):
                value={}
                for key,item in pairs:
                    if key in value: raise ValueError('duplicate_receipt_field')
                    value[key]=item
                return value
            try:return json.loads(raw,object_pairs_hook=unique)
            except (ValueError,RecursionError):raise RuntimeError('wake_receipt_unverified') from None
        if not old:
            phases[0]='envelope'
            # A crash/unknown result beyond this committed boundary NEVER
            # creates another envelope. Availability is sacrificed to custody.
            # command_intent_committed marks the Host begin_command/executing
            # journal intent only; the native input mutation fact stays the
            # receipt's own mutation flag.
            with db: bridge.begin_command(identity,digest)
            diag('command_intent_committed')
            remaining()
            sent = bridge.native('message','send','--to',binding['tag'],'--kind','hub-wake',
                                 '--data',json.dumps(data,ensure_ascii=False,separators=(',',':')),
                                 '--json','--',text,timeout=remaining(),cwd=binding['workspace'])
            envelope = validated(decode(sent.stdout))
            qualified();remaining()
            progress={'inbox_envelope':envelope}
            with db:
                db.execute("UPDATE commands SET result=? WHERE id=? AND state='executing'",(json.dumps(progress),identity))
            diag('envelope_stored')
        if not isinstance(progress,dict) or set(progress) != {'inbox_envelope'}:
            raise RuntimeError('wake_receipt_unverified')
        envelope = validated(progress['inbox_envelope'])
        def reread():
            result=bridge.native('message','show','--json',envelope['id'],timeout=remaining(),cwd=binding['workspace'])
            if validated(decode(result.stdout)) != envelope:
                raise RuntimeError('wake_receipt_unverified')
            qualified();remaining()
            diag('envelope_reread')
        reread()
        wire_text = 'Read inbox message '+envelope['id']
    elif not old:
        phases[0]='submit'
        # Same Host journal intent boundary as the inbox path; not a native
        # input mutation fact.
        with db: bridge.begin_command(identity,digest)
        diag('command_intent_committed')
    # A repeated CLI RPC uses EXACTLY the same worker-persisted once-ID/digest/
    # original deadline. Uncertain worker fences never write a second input.
    phases[0]='submit'
    diag('submit_attempted')
    remaining()
    sent = bridge.native('send',binding['native_id'],'--enter','--strict',
                         '--expect-tag',binding['tag'],'--expect-workspace',binding['workspace'],
                         '--expect-created-at-ms',str(binding['created_at_ms']),
                         '--idempotency-key',identity,'--deadline-ms',str(binding['deadline_ms']),
                         '--json','--',wire_text,allow_failure=True,timeout=remaining())
    def unique(pairs):
        value = {}
        for key,item in pairs:
            if key in value:
                raise ValueError('duplicate_receipt_field')
            value[key] = item
        return value
    try:
        receipt = json.loads(sent.stdout,object_pairs_hook=unique)
    except (ValueError,RecursionError):
        raise RuntimeError('wake_receipt_unverified') from None
    qualified()
    remaining()
    expected = hashlib.sha256(json.dumps([binding['native_id'],identity,binding['tag'],binding['workspace'],
        binding['created_at_ms'],binding['deadline_ms'],wire_text],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    fields = {'version','session_id','operation_id','request_sha256','deadline_ms','status',
              'mutation','reason','landed','submitted','consumed'}
    if (not isinstance(receipt,dict) or set(receipt) not in (fields,fields|{'replayed'})
            or ('replayed' in receipt and type(receipt['replayed']) is not bool)
            or type(receipt.get('version')) is not int or receipt['version'] != 1
            or receipt.get('session_id') != binding['native_id'] or receipt.get('operation_id') != identity
            or receipt.get('request_sha256') != expected or type(receipt.get('deadline_ms')) is not int
            or receipt['deadline_ms'] != binding['deadline_ms']):
        raise RuntimeError('wake_receipt_unverified')
    phases[0]='receipt'
    if (sent.returncode != 0 or receipt.get('status') != 'submitted_verified'
            or receipt.get('mutation') is not True or receipt.get('landed') is not True
            or receipt.get('submitted') is not True or receipt.get('consumed') is not None
            or receipt.get('reason') != 'submission_observed'):
        diag('submit_pending',status=_strict_diag_receipt_field(receipt,'status'),
             reason=_strict_diag_receipt_field(receipt,'reason'),
             mutation=_strict_diag_receipt_field(receipt,'mutation'),
             landed=_strict_diag_receipt_field(receipt,'landed'),
             submitted=_strict_diag_receipt_field(receipt,'submitted'),
             replayed=_strict_diag_receipt_field(receipt,'replayed'))
        return {'wake_pending':True,'transport':binding['transport']}
    result = {'delivery':'native_acknowledged','delivered_at_ms':int(time.time()*1000),
              'wake_proof':{'kind':'strict_submit_v1','epoch':binding['epoch'],
                            'operation_id':binding['operation_id'],'receipt':receipt}}
    if inbox:
        reread()
        result['wake_proof'].update(kind='strict_inbox_notice_v1',
            envelope_id=envelope['id'],body_sha256=hashlib.sha256(text.encode()).hexdigest())
    phases[0]='terminal'
    diag('submit_verified',status=_strict_diag_receipt_field(receipt,'status'),
         reason=_strict_diag_receipt_field(receipt,'reason'),
         mutation=_strict_diag_receipt_field(receipt,'mutation'),
         landed=_strict_diag_receipt_field(receipt,'landed'),
         submitted=_strict_diag_receipt_field(receipt,'submitted'),
         replayed=_strict_diag_receipt_field(receipt,'replayed'))
    with db: bridge.finish_command(identity,command,result)
    return result
