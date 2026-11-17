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
