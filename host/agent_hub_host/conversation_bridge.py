"""Opt-in, foreground bridge for one genuinely bound conversation participant.

The native sender is the participant itself. This is not host delegation.
Native transport has no idempotent send: an uncertain attempt is reconciled from
its durable log and is NEVER blindly repeated, even when the log is empty.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

from . import config, conversations, private_fs
from .bridge import get_authoritative_runtime_dir, get_authoritative_state_dir
from .native_bridge import (BridgeCapabilityGap, canonicalize_workspace,
                            qualify_native_service_binding, validate_native_send_envelope)

HOLD_REASON = ('Conversation bridge held for M3b unless explicit native opt-in and participant/project/workspace/'
               'machine/conversation configuration; check private journal for uncertain effects. '
               'For missing retained history see hub bridge-resume --help; never reset the journal.')
MAX_STATE = 8 * 1024 * 1024
MAX_SEQ = 9007199254740991
MAX_GAPS = 128


def sequence(value):
    return type(value) is int and 0 <= value <= MAX_SEQ


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeCapabilityGap('Duplicate journal field')
        result[key] = value
    return result


def reject_constant(value):
    raise BridgeCapabilityGap('Non-finite journal value')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def segment(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid conversation identifier')
    return value


class NativeMailbox:
    """Client catalog + native mailbox CLI; preserve ambient genuine binding."""
    def __init__(self, executable='aplexer'):
        self.executable = executable

    def call(self, *args):
        from .aplexer_client import client_call
        result = client_call(self.executable, [*args, '--json'])
        if result is None:
            result = subprocess.run([self.executable, '--json', *args], capture_output=True,
                                    text=True, timeout=30, check=True)
        elif result.returncode:
            raise BridgeCapabilityGap('Native client operation failed')
        if len(result.stdout.encode()) > 16 * 1024 * 1024:
            raise BridgeCapabilityGap('Native response exceeds bridge bound')
        return json.loads(result.stdout)

    def qualify(self, workspace):
        own = self.call('whoami')
        if own.get('workspace') != workspace or own.get('state', own.get('phase')) not in ('running', 'idle'):
            raise BridgeCapabilityGap('Native caller workspace/state mismatch')
        rows = [r for r in self.call('list') if r.get('workspace') == workspace
                and r.get('tag') == own.get('tag')]
        if len(rows) != 1 or rows[0].get('id') != own.get('id'):
            raise BridgeCapabilityGap('Native catalog is ambiguous or changed')
        binding = qualify_native_service_binding(
            workspace, get_authoritative_state_dir(), get_authoritative_runtime_dir(),
            own.get('tag'), own.get('id'))
        if binding is None or binding.session_id != own.get('id'):
            raise BridgeCapabilityGap('Native authoritative session record missing')
        return binding

    def log(self, workspace):
        return self.call('message', 'log', '--workspace', workspace)

    def send(self, binding, body, data):
        # --to accepts TAG, never UUID. Qualify tag/catalog immediately before this.
        return self.call('message', 'send', '--workspace', binding.workspace,
                         '--to', binding.tag, '--kind', 'hub-conversation',
                         '--data', json.dumps(data), '--', body)


class ConversationBridge:
    def __init__(self, *, actor, project, workspace, machine_id, conversation_ids,
                 origin, native=None, request=None):
        self.actor, self.project, self.machine = actor, project, machine_id
        if not all(isinstance(v, str) and v.strip() == v and v for v in
                   (actor, project, workspace, machine_id, origin)):
            raise ValueError('Explicit participant mapping required')
        if not Path(workspace).is_absolute() or canonicalize_workspace(workspace) != workspace:
            raise ValueError('Workspace must be an explicit canonical absolute path')
        self.workspace, self.origin = workspace, origin
        self.ids = sorted(set(segment(v) for v in conversation_ids))
        if not 1 <= len(self.ids) <= 32:
            raise ValueError('Specify 1..32 conversations')
        self.native, self.request = native or NativeMailbox(), request or conversations.request
        self.path = Path(workspace) / '.local' / ('conversation-bridge-' + digest([actor, project])[:24] + '.json')
        self.state = None

    def qualify(self):
        binding = self.native.qualify(self.workspace)
        if self.actor != self.project + ':' + binding.tag:
            raise BridgeCapabilityGap('Canonical participant/project/tag mismatch')
        participants = self.request('GET', '/api/conversations/participants')
        if participants.get('self', {}).get('actor') != self.actor or participants['self'].get('kind') != 'agent':
            raise BridgeCapabilityGap('Credential does not authenticate configured agent')
        rows = [r for r in participants.get('participants', []) if r.get('actor') == self.actor]
        if len(rows) != 1:
            raise BridgeCapabilityGap('Registered participant is missing or ambiguous')
        row = rows[0]
        metadata = row.get('native_binding') or {}
        if (row.get('kind') != 'agent' or row.get('project') != self.project
                or row.get('machine_id') != self.machine or metadata.get('tag') != binding.tag
                or metadata.get('project') != self.project or metadata.get('machine_id') != self.machine):
            raise BridgeCapabilityGap('Registered actor/project/machine mismatch')
        # Registration hints are never live attestation, but contradictory hints fail closed.
        if (metadata.get('workspace') not in (None, self.workspace)
                or metadata.get('session_id') not in (None, binding.session_id)):
            raise BridgeCapabilityGap('Registered native hints contradict current session')
        return binding

    def member(self, cid):
        self.binding = self.qualify()
        if self.state and self.state['identity'][-2:] != [self.binding.session_id, self.binding.tag]:
            raise BridgeCapabilityGap('Native session changed during cycle')
        result = self.request('GET', '/api/conversations/' + cid)
        if result.get('self_actor') != self.actor or not any(
                r.get('actor') == self.actor and r.get('removed_at') is None
                for r in result.get('members', [])):
            raise BridgeCapabilityGap('Current conversation membership required')

    @contextmanager
    def journal(self, *, create=True):
        try:
            with self._journal(create=create):
                yield
        except (PermissionError, ValueError) as exc:
            raise BridgeCapabilityGap('Unsafe bridge journal or file safety proof failed') from exc
        except OSError as exc:
            if exc.errno in (20, 40):  # ENOTDIR / ELOOP, no-follow path rejection
                raise BridgeCapabilityGap('Unsafe bridge journal path') from exc
            raise

    @contextmanager
    def _journal(self, *, create=True):
        with private_fs.exclusive_lock(str(self.path) + '.lock'):
            if not create:
                try:
                    private_fs.read_bytes(self.path, MAX_STATE)
                except FileNotFoundError:
                    raise BridgeCapabilityGap('Existing journal required; never reset history to recover a gap') from None
            self.binding = self.qualify()
            identity = [self.origin, self.actor, self.project, self.workspace, self.machine,
                        self.binding.session_id, self.binding.tag]
            try:
                raw = private_fs.read_bytes(self.path, MAX_STATE)
            except FileNotFoundError:
                raw = None
            if raw is not None:
                self.state = json.loads(raw, object_pairs_hook=strict_object,
                                        parse_constant=reject_constant)
                if (not isinstance(self.state, dict) or self.state.get('identity') != identity
                        or type(self.state.get('version')) is not int or self.state.get('version') != 1):
                    raise BridgeCapabilityGap('Journal binding changed; explicit reconciliation required')
                self.validate_state()
            else:
                self.state = dict(version=1, identity=identity, cursors={}, deliveries={}, replies={})
                self.save()
            yield

    def validate_state(self):
        """Validate evidence without rebuilding state or discarding extension fields."""
        state = self.state
        for field in ('cursors', 'deliveries', 'replies'):
            if not isinstance(state.get(field), dict):
                raise BridgeCapabilityGap('Malformed journal records')
        for cid, cursor in state['cursors'].items():
            segment(cid)
            if not sequence(cursor):
                raise BridgeCapabilityGap('Malformed journal cursor')
        for key, row in state['deliveries'].items():
            if not isinstance(row, dict) or not isinstance(row.get('message'), dict):
                raise BridgeCapabilityGap('Malformed delivery record')
            msg, cid = row['message'], row.get('cid')
            segment(cid)
            self.validate_message(msg, cid)
            body = json.dumps({'conversation_id': cid, 'message_id': msg['id'], 'seq': msg['seq'],
                               'sender_actor': msg['sender_actor'], 'body': msg['body']}, ensure_ascii=False)
            if (key != cid + ':' + str(msg['seq']) or row.get('body') != body
                    or row.get('data') != {'hub_conversation_bridge': digest([state['identity'], cid, msg])}
                    or type(row.get('attempted')) is not bool
                    or 'native_id' not in row
                    or not (row.get('native_id') is None or isinstance(row['native_id'], str) and row['native_id'])
                    or row.get('native_id') is not None and not row['attempted']):
                raise BridgeCapabilityGap('Malformed delivery evidence')
        for native_id, row in state['replies'].items():
            if not isinstance(row, dict) or not isinstance(row.get('payload'), dict) or type(row.get('done')) is not bool:
                raise BridgeCapabilityGap('Malformed reply intent')
            payload, cid = row['payload'], row.get('cid')
            segment(cid)
            if (not isinstance(native_id, str) or not native_id or not isinstance(payload.get('body'), str)
                    or not payload['body'].strip() or len(payload['body'].encode()) > 16384
                    or payload.get('kind') != 'reply' or payload.get('bridge_ref') != native_id
                    or payload.get('idempotency_key') != 'native:' + digest([state['identity'], native_id])
                    or not any(r['cid'] == cid and r['native_id'] and r['message']['id'] == payload.get('reply_to')
                               for r in state['deliveries'].values())):
                raise BridgeCapabilityGap('Malformed reply evidence')
        gaps = state.get('gaps', [])
        if not isinstance(gaps, list) or len(gaps) > MAX_GAPS:
            raise BridgeCapabilityGap('Malformed or full gap history')
        previous = {}
        for gap in gaps:
            if not isinstance(gap, dict):
                raise BridgeCapabilityGap('Malformed gap record')
            cid = gap.get('conversation_id')
            segment(cid)
            old, new = gap.get('old_cursor'), gap.get('new_cursor')
            if (not sequence(old) or not sequence(new) or not old < new
                    or not sequence(gap.get('missing_from_seq')) or not sequence(gap.get('missing_through_seq'))
                    or gap.get('missing_from_seq') != old + 1 or gap.get('missing_through_seq') != new
                    or gap.get('identity') != state['identity'] or gap.get('acknowledged') is not True
                    or gap.get('reason') != 'operator_acknowledged_missing_history'
                    or not isinstance(gap.get('verified_at'), str)
                    or not sequence(gap.get('server_last_seq')) or gap['server_last_seq'] < new
                    or not sequence(gap.get('server_read_seq')) or gap['server_read_seq'] > gap['server_last_seq']
                    or old < previous.get(cid, 0) or state['cursors'].get(cid, 0) < new
                    or any(r['cid'] == cid and old < r['message']['seq'] <= new for r in state['deliveries'].values())):
                raise BridgeCapabilityGap('Malformed gap evidence or cursor rollback')
            previous[cid] = new

    @staticmethod
    def validate_message(msg, cid):
        if (not isinstance(msg, dict) or msg.get('conversation_id') != cid
                or not sequence(msg.get('seq')) or msg['seq'] == 0
                or not all(isinstance(msg.get(k), str) and msg[k] for k in ('id', 'sender_actor', 'body'))
                or len(msg['body'].encode()) > 16384):
            raise BridgeCapabilityGap('History is not canonical')

    def empty_range_page(self, cid, cursor, target):
        """One ascending canonical page proves emptiness before its first row.

        Never follow resume_after_seq/gap flags or a latest/default history page.
        The immutable server source uses ORDER BY seq ASC for explicit after_seq.
        """
        page = self.request('GET', '/api/conversations/' + cid + '/messages?after_seq=' + str(cursor) + '&limit=100')
        required = {'self_actor', 'conversation_id', 'messages', 'last_seq', 'my_read_seq', 'read_seq',
                    'oldest_seq', 'newest_seq', 'oldest_retained_seq', 'newest_retained_seq',
                    'has_more_before', 'has_more_after', 'next_after_seq', 'prev_before_seq'}
        if (not isinstance(page, dict) or not required <= page.keys()
                or len(json.dumps(page, ensure_ascii=False).encode()) > 2 * 1024 * 1024
                or page.get('self_actor') != self.actor or page.get('conversation_id') != cid
                or not sequence(page.get('last_seq')) or not cursor < target <= page['last_seq']
                or not sequence(page.get('my_read_seq')) or page.get('read_seq') != page['my_read_seq']
                or not sequence(page.get('read_seq')) or not sequence(page.get('next_after_seq'))
                or page['my_read_seq'] > page['last_seq']
                or not isinstance(page.get('messages'), list) or len(page['messages']) > 100
                or type(page.get('has_more_after')) is not bool or type(page.get('has_more_before')) is not bool):
            raise BridgeCapabilityGap('Unprovable history page or future recovery target')
        rows = page['messages']
        prior = cursor
        for msg in rows:
            self.validate_message(msg, cid)
            if not prior < msg['seq'] <= page['last_seq']:
                raise BridgeCapabilityGap('History page is not ascending')
            prior = msg['seq']
        low, high = page.get('oldest_retained_seq'), page.get('newest_retained_seq')
        if not ((low is None and high is None) or
                sequence(low) and sequence(high) and 1 <= low <= high <= page['last_seq']):
            raise BridgeCapabilityGap('Ambiguous retained watermarks')
        if rows:
            first, last = rows[0]['seq'], rows[-1]['seq']
            if (low is None or not low <= first <= last <= high
                    or low > cursor and first != low
                    or not all(sequence(page.get(k)) for k in ('oldest_seq', 'newest_seq', 'prev_before_seq'))
                    or page.get('oldest_seq') != first or page.get('newest_seq') != last
                    or page.get('next_after_seq') != last or page.get('prev_before_seq') != first
                    or page['has_more_before'] != (first > low)
                    or page['has_more_after'] != (last < high)
                    or page['has_more_after'] and len(rows) != 100):
                raise BridgeCapabilityGap('Ambiguous history pagination')
            if first <= target:
                raise BridgeCapabilityGap('Recovery would skip a retained message')
        elif (high is not None and high > cursor or page['has_more_after'] or page['has_more_before']
              or page.get('oldest_seq') is not None or page.get('newest_seq') is not None
              or page.get('next_after_seq') != page['last_seq'] or page.get('prev_before_seq') is not None):
            raise BridgeCapabilityGap('Empty page contradicts retained watermarks')
        return page

    def resume(self, cid, target, *, acknowledge_gap=False):
        if cid not in self.ids or not sequence(target) or acknowledge_gap is not True:
            raise ValueError('Explicit valid conversation, sequence and gap acknowledgement required')
        # Current server auth, scope and membership before even creating a lock file.
        # This preflight is repeated using the reloaded cursor under the same cycle lock.
        self.member(cid)
        self.request('GET', '/api/conversations/' + cid + '/messages?after_seq=0&limit=1')
        with self.journal(create=False):
            self.member(cid)
            cursor = self.state['cursors'].get(cid, 0)
            if target <= cursor:
                raise BridgeCapabilityGap('Recovery target must advance current cursor; replay refused')
            if len(self.state.get('gaps', [])) >= MAX_GAPS:
                raise BridgeCapabilityGap('Gap history full; preserve journal for reconciliation')
            if (any(r['message']['seq'] > self.state['cursors'].get(r['cid'], 0)
                    or r['native_id'] is None for r in self.state['deliveries'].values())
                    or any(not r['done'] for r in self.state['replies'].values())):
                raise BridgeCapabilityGap('Unresolved delivery/read or reply intent; reconcile before gap recovery')
            page = self.empty_range_page(cid, cursor, target)
            self.member(cid)  # revalidate native binding + server membership after proof
            # Re-read after the final authority check; monotonic append cannot insert old seqs.
            confirmed = self.empty_range_page(cid, cursor, target)
            if (confirmed['last_seq'] < page['last_seq'] or confirmed['my_read_seq'] < page['my_read_seq']):
                raise BridgeCapabilityGap('Server watermark rollback during recovery')
            self.member(cid)
            gap = dict(conversation_id=cid, old_cursor=cursor, new_cursor=target,
                       missing_from_seq=cursor + 1, missing_through_seq=target,
                       reason='operator_acknowledged_missing_history', acknowledged=True,
                       identity=list(self.state['identity']), verified_at=datetime.now(timezone.utc).isoformat(),
                       server_last_seq=confirmed['last_seq'], server_read_seq=confirmed['my_read_seq'])
            self.state.setdefault('gaps', []).append(gap)
            self.state['cursors'][cid] = target
            self.save()  # audit record and cursor in ONE atomic, fsynced replacement
        return dict(status='gap_recorded', gap=gap, server_read_advanced=False,
                    notice='Missing history acknowledged locally; no missing messages delivered or marked read. Run the normal bridge to consume retained messages.')

    def save(self):
        raw = json.dumps(self.state, ensure_ascii=False, sort_keys=True).encode()
        if len(raw) > MAX_STATE:
            raise BridgeCapabilityGap('Journal full; retain state and reconcile before continuing')
        private_fs.atomic_write(self.path, raw, MAX_STATE)

    def own_envelope(self, msg):
        validate_native_send_envelope(msg, self.binding, self.workspace)
        if (msg.get('workspace') != self.workspace
                or msg.get('from', {}).get('workspace') != self.workspace
                or msg.get('to', {}).get('session_id') != self.binding.session_id
                or msg.get('to', {}).get('tag') != self.binding.tag
                or msg.get('delivery') != 'inbox' or not isinstance(msg.get('id'), str)):
            raise BridgeCapabilityGap('Native envelope is not own durable inbox mail')

    def deliver(self, cid, msg):
        key = cid + ':' + str(msg['seq'])
        body = json.dumps({'conversation_id': cid, 'message_id': msg['id'], 'seq': msg['seq'],
                           'sender_actor': msg['sender_actor'], 'body': msg['body']}, ensure_ascii=False)
        data = {'hub_conversation_bridge': digest([self.state['identity'], cid, msg])}
        intent = {'cid': cid, 'message': msg, 'body': body, 'data': data, 'attempted': False, 'native_id': None}
        row = self.state['deliveries'].get(key)
        if row is None:
            row = self.state['deliveries'][key] = intent
            self.save()  # durable pending intent before native effect
        elif any(row[k] != intent[k] for k in ('cid', 'message', 'body', 'data')):
            raise BridgeCapabilityGap('Delivery retry payload conflict')
        self.member(cid)
        log = self.native.log(self.workspace)
        matches = [m for m in log if m.get('data') == data and m.get('body') == body
                   and m.get('kind') == 'hub-conversation' and not m.get('reply_to')]
        if len(matches) > 1:
            raise BridgeCapabilityGap('Duplicate native intent effects require reconciliation')
        if not matches and not row['attempted']:
            row['attempted'] = True
            self.save()  # uncertainty reservation: never clear it or blind resend
            self.member(cid)
            try:
                self.native.send(self.binding, body, data)
            except (subprocess.SubprocessError, OSError, ValueError):
                pass  # authoritative log, not CLI exit status, determines durable effect
            matches = [m for m in self.native.log(self.workspace) if m.get('data') == data
                       and m.get('body') == body and m.get('kind') == 'hub-conversation'
                       and not m.get('reply_to')]
        if len(matches) != 1:
            raise BridgeCapabilityGap('Native send uncertain; no blind retry; retain journal')
        self.own_envelope(matches[0])
        if row['native_id'] not in (None, matches[0]['id']):
            raise BridgeCapabilityGap('Native mapping conflict')
        row['native_id'] = matches[0]['id']
        self.save()

    def relay_replies(self):
        mapping = {r['native_id']: r for r in self.state['deliveries'].values() if r['native_id']}
        for msg in self.native.log(self.workspace):
            original = mapping.get(msg.get('reply_to'))
            if original is None or original['cid'] not in self.ids or msg.get('kind') != 'reply':
                continue
            self.member(original['cid'])
            try:
                self.own_envelope(msg)
            except BridgeCapabilityGap:
                continue  # foreign authors, unrelated mail and loops are never relayed
            if not isinstance(msg.get('body'), str) or not msg['body'].strip() or len(msg['body'].encode()) > 16384:
                continue
            payload = {'body': msg['body'], 'kind': 'reply', 'reply_to': original['message']['id'],
                       'bridge_ref': msg['id'], 'idempotency_key': 'native:' + digest([self.state['identity'], msg['id']])}
            entry = {'cid': original['cid'], 'payload': payload, 'done': False}
            row = self.state['replies'].get(msg['id'])
            if row is None:
                row = self.state['replies'][msg['id']] = entry
                self.save()
            if row['cid'] != entry['cid'] or row['payload'] != payload:
                raise BridgeCapabilityGap('Reply retry payload conflict')
        # Flush persisted intents even when native retention has removed their log rows.
        # Authorship and original mapping were proved before this immutable intent was saved.
        for row in self.state['replies'].values():
            if row['done'] or row['cid'] not in self.ids:
                continue
            payload = row['payload']
            self.member(row['cid'])
            response = self.request('POST', '/api/conversations/' + row['cid'] + '/messages', payload)
            result = response.get('message', response)
            if (result.get('sender_actor') != self.actor or result.get('conversation_id') != row['cid']
                    or any(result.get(k) != v for k, v in payload.items())):
                raise BridgeCapabilityGap('Hub reply receipt mismatch; preserve retry intent')
            row['done'] = True
            self.save()

    def cycle(self):
        with self.journal():
            for cid in self.ids:
                self.member(cid)
                cursor = self.state['cursors'].get(cid, 0)
                page = self.request('GET', '/api/conversations/' + cid + '/messages?after_seq=' + str(cursor) + '&limit=100')
                if page.get('self_actor') != self.actor or page.get('conversation_id') != cid:
                    raise BridgeCapabilityGap('History identity mismatch')
                for msg in page.get('messages', []):
                    if (msg.get('conversation_id') != cid or type(msg.get('seq')) is not int
                            or msg['seq'] != cursor + 1 or not isinstance(msg.get('id'), str)
                            or not isinstance(msg.get('sender_actor'), str) or not isinstance(msg.get('body'), str)):
                        raise BridgeCapabilityGap('History is not contiguous/canonical')
                    if msg['sender_actor'] != self.actor:
                        self.deliver(cid, msg)
                    # Self-authored messages are already known to this participant; no loop delivery.
                    self.member(cid)
                    response = self.request('POST', '/api/conversations/' + cid + '/read', {'seq': msg['seq']})
                    if response.get('actor') != self.actor or response.get('conversation_id') != cid or response.get('read_seq', -1) < msg['seq']:
                        raise BridgeCapabilityGap('Read receipt mismatch')
                    cursor = msg['seq']
                    self.state['cursors'][cid] = cursor
                    self.save()  # after durable envelope + monotonic server read, never before
            self.relay_replies()
        return {'status': 'completed', 'conversations': len(self.ids)}


def run_cycles(cycles=1, *, enabled=False, **kwargs):
    if type(cycles) is not int or not 1 <= cycles <= 1000:
        raise ValueError('cycles must be between 1 and 1000')
    if not enabled:
        raise BridgeCapabilityGap(HOLD_REASON)
    bridge = ConversationBridge(**kwargs)
    return {'cycles': [bridge.cycle() for _ in range(cycles)]}


def resume_from_args(args):
    if not args.enable_native or not args.acknowledge_gap:
        raise BridgeCapabilityGap('Native opt-in and explicit gap acknowledgement required')
    cfg = config.load_config()
    if cfg.get('machine_id') != args.machine_id:
        raise BridgeCapabilityGap('Bridge machine must match existing authenticated host config')
    origin = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    conversations.api.check_url(origin)
    bridge = ConversationBridge(actor=args.actor, project=args.project, workspace=args.workspace,
                                machine_id=args.machine_id, conversation_ids=[args.conversation], origin=origin.rstrip('/'))
    return bridge.resume(args.conversation, args.resume_after_seq, acknowledge_gap=args.acknowledge_gap)
