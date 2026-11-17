"""Qualified one-shot interactive wake. Inbox persistence is never delivery proof.

A durable command intent precedes message creation. After an interrupted unknown
creation we never send again. A recorded original envelope can be reconciled by
its native guarded deliver operation (which never creates another envelope).
No PTY injection, identity override, provider action or headless resume.
"""
import hashlib
import json
import time
import uuid


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
    db = bridge.journal.db
    identity = command.get('id')
    binding = command.get('wake')
    fields = {'id','session_id','operation_id','epoch','native_id','tag','workspace',
              'created_at_ms','deadline_ms','transport'}
    if (not isinstance(binding, dict) or set(binding) != fields
            or binding['transport'] != 'strict-submit:v1' or command.get('kind') != 'send'
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
    start = time.monotonic()
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
        result = json.loads(old['result'])
        bridge.journal.emit('command:'+identity,'/api/sessions/'+binding['session_id']+'/commands/'+identity+'/ack',
                            {'epoch':binding['epoch'],'result':result})
        return result
    qualified()
    if not old:
        with db: bridge.begin_command(identity,digest)
    # A repeated CLI RPC uses EXACTLY the same worker-persisted once-ID/digest/
    # original deadline. Uncertain worker fences never write a second input.
    remaining()
    sent = bridge.native('send',binding['native_id'],'--enter','--strict',
                         '--expect-tag',binding['tag'],'--expect-workspace',binding['workspace'],
                         '--expect-created-at-ms',str(binding['created_at_ms']),
                         '--idempotency-key',identity,'--deadline-ms',str(binding['deadline_ms']),
                         '--json','--',text,allow_failure=True,timeout=remaining())
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
        binding['created_at_ms'],binding['deadline_ms'],text],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    fields = {'version','session_id','operation_id','request_sha256','deadline_ms','status',
              'mutation','reason','landed','submitted','consumed'}
    if (not isinstance(receipt,dict) or set(receipt) not in (fields,fields|{'replayed'})
            or ('replayed' in receipt and type(receipt['replayed']) is not bool)
            or type(receipt.get('version')) is not int or receipt['version'] != 1
            or receipt.get('session_id') != binding['native_id'] or receipt.get('operation_id') != identity
            or receipt.get('request_sha256') != expected or type(receipt.get('deadline_ms')) is not int
            or receipt['deadline_ms'] != binding['deadline_ms']):
        raise RuntimeError('wake_receipt_unverified')
    if (sent.returncode != 0 or receipt.get('status') != 'submitted_verified'
            or receipt.get('mutation') is not True or receipt.get('landed') is not True
            or receipt.get('submitted') is not True or receipt.get('consumed') is not None
            or receipt.get('reason') != 'submission_observed'):
        return {'wake_pending':True,'transport':'strict-submit:v1'}
    result = {'delivery':'native_acknowledged','delivered_at_ms':int(time.time()*1000),
              'wake_proof':{'kind':'strict_submit_v1','epoch':binding['epoch'],
                            'operation_id':binding['operation_id'],'receipt':receipt}}
    with db: bridge.finish_command(identity,command,result)
    return result
