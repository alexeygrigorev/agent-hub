"""Token-free message ledger feed from Grok Bot desktop transcript replicas (intake 290b935a).

The Grok Bot desktop app keeps a plain-JSON rolling replica (~200 entries) of
every agent chat under ``%APPDATA%\\Grok Bot\\sand-client-persistence``. File
stems are base32 of ``sand.client.slice.account.grok|<user>.transcript.replicas.<agentId>``.
This step, run as a daemon thread of the session host (same Windows user, no
admin, no extra Scheduled Task), reads ONLY those replica blobs and sends both
sides of every chat (Alexey's ``tNNNNu`` messages and the agents'
``tNNNNsM`` sends, widget answers, attachment refs) to the Hub ledger
``/api/asks/ingest``. No Grok Bot turn runs, so no Grok/Developer tokens are
spent.

Catch-up (A8): the controller starts at boot/logon, and a wall-clock jump
against the monotonic clock marks a resume from sleep. Both open a fast
catch-up phase while the app syncs the replica; the per-agent cursor of
acknowledged message ids means everything sent from the phone meanwhile is
ingested. If the replica window no longer overlaps anything already
acknowledged, the lost range is reported as an explicit gap (A9), never
dropped silently.

Safety: every other blob in the directory (push state, webhook keys, tokens)
is identified by its decoded name and never opened. Logs carry counts only,
never message text. Attachments are sent as references (name, mime, size,
ref), never content. Every post, even an empty heartbeat, is the Hub's
liveness signal for the >2h silence alert. The same records are appended to
a local ``ledger.jsonl`` in the journal (A13 schema) as an offline copy.
"""
import base64
import binascii
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = 'tabo-replica'
INGEST_PATH = '/api/asks/ingest'
STATE_FILENAME = 'replica-watcher-state.json'
CONFIG_FILENAME = 'replica-watcher.json'
LEDGER_FILENAME = 'ledger.jsonl'
REPLICA_MARKER = '.transcript.replicas.'
POLL_S = 30
CATCHUP_POLL_S = 10
CATCHUP_S = 600
RESUME_JUMP_S = 120
HEARTBEAT_S = 600
MAX_BACKOFF_S = 600
MAX_MESSAGES_PER_AGENT = 200
MAX_AGENTS_PER_POST = 32
MAX_BODY_BYTES = 240 * 1024
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_ACKED_IDS = 2000
ENTRY_ID = re.compile(r'^t(\d+)([a-z]+)(\d*)$')
ROLES = {'u': 'user', 's': 'assistant'}
SKIPPED_KINDS = frozenset({'event'})
KNOWN_AGENTS = {
    'e96e39a2-c10d-48ea-a25b-e2c4240cf3ef': 'Developer',
}
KNOWN_AGENT_PREFIXES = {'1b5e58df': 'Orin', 'dd2cba39': 'Tofu'}
TEXT_KEYS = ('text', 'content', 'message', 'body', 'markdown', 'value', 'answer')
TS_KEYS = ('ts', 'timestamp', 'createdAt', 'created_at', 'sentAt', 'sent_at', 'time', 'date')
REPLY_KEYS = ('replyTo', 'reply_to', 'inReplyTo', 'in_reply_to', 'parentId', 'parent_id', 'replyToId')
ATTACHMENT_KEYS = ('attachments', 'files', 'attachment', 'file', 'images')
ATTACHMENT_FIELDS = {
    'name': ('name', 'filename', 'fileName', 'title'),
    'mime': ('mime', 'mimeType', 'contentType', 'type'),
    'size': ('size', 'bytes', 'byteLength'),
    'ref': ('ref', 'id', 'url', 'uri', 'path', 'key'),
}


def default_root():
    """The replica directory, or None off Windows / when Grok Bot is absent."""
    override = os.environ.get('AGENT_HUB_REPLICA_DIR')
    if override:
        return Path(override)
    appdata = os.environ.get('APPDATA')
    if sys.platform != 'win32' or not appdata:
        return None
    return Path(appdata) / 'Grok Bot' / 'sand-client-persistence'


def _long(path):
    text = str(path)
    if sys.platform == 'win32' and not text.startswith('\\\\?\\'):
        return '\\\\?\\' + os.path.abspath(text)
    return text


def decode_name(filename):
    """Decoded blob key for a base32 file name, or None when it is not base32."""
    stem = filename.split('.', 1)[0].upper()
    if not stem or not re.fullmatch(r'[A-Z2-7=]+', stem):
        return None
    try:
        return base64.b32decode(stem + '=' * (-len(stem) % 8)).decode('utf-8')
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def replica_agent(filename):
    """Agent id of a transcript replica blob; None for every other blob."""
    decoded = decode_name(filename)
    if not decoded or REPLICA_MARKER not in decoded:
        return None
    agent = decoded.rsplit(REPLICA_MARKER, 1)[1].strip()
    return agent if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', agent) else None


def agent_name(agent_id):
    if agent_id in KNOWN_AGENTS:
        return KNOWN_AGENTS[agent_id]
    return KNOWN_AGENT_PREFIXES.get(agent_id[:8])


def list_replicas(root):
    """[(agent_id, path)] of replica blobs only; names are decoded before any open."""
    try:
        names = sorted(os.listdir(_long(root)))
    except OSError:
        return []
    found = []
    for name in names:
        agent = replica_agent(name)
        if agent:
            found.append((agent, Path(root) / name))
    return found


def read_entries(path):
    """Entries list of one replica blob ({schemaVersion, value:{entries}})."""
    with open(_long(path), 'rb') as handle:
        raw = handle.read(MAX_BLOB_BYTES + 1)
    if len(raw) > MAX_BLOB_BYTES:
        raise ValueError('replica_too_large')
    data = json.loads(raw.decode('utf-8-sig'))
    value = data.get('value') if isinstance(data, dict) else None
    entries = value.get('entries') if isinstance(value, dict) else None
    if not isinstance(entries, list):
        raise ValueError('replica_without_entries')
    return entries


def _text(value, depth=0):
    if isinstance(value, str):
        return value
    if depth > 3:
        return None
    if isinstance(value, dict):
        for key in TEXT_KEYS:
            if key in value:
                found = _text(value[key], depth + 1)
                if found is not None:
                    return found
        return None
    if isinstance(value, list):
        parts = [_text(item, depth + 1) for item in value]
        parts = [part for part in parts if part]
        return '\n'.join(parts) if parts else None
    return None


def entry_text(entry):
    """Verbatim text: the first text-like field, never a summary."""
    for key in TEXT_KEYS:
        if key in entry:
            found = _text(entry[key])
            if found is not None:
                return found
    return None


def _ms(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value > 1e14:   # microseconds
            return int(value / 1000)
        if value > 1e11:   # milliseconds
            return int(value)
        if value > 1e8:    # seconds
            return int(value * 1000)
        return None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return _ms(float(value)) if re.fullmatch(r'\d+(\.\d+)?', value) else None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


def entry_ts(entry):
    for key in TS_KEYS:
        if key in entry:
            found = _ms(entry[key])
            if found is not None:
                return found
    for nested in ('meta', 'metadata'):
        if isinstance(entry.get(nested), dict):
            found = entry_ts(entry[nested])
            if found is not None:
                return found
    return None


def _scalar(value, limit=512):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:limit]
    return None


def entry_attachments(entry):
    """Attachment references only (name, mime, size, ref); never file content."""
    found = []
    for key in ATTACHMENT_KEYS:
        value = entry.get(key)
        items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        for item in items[:20]:
            if not isinstance(item, dict):
                continue
            ref = {}
            for field, candidates in ATTACHMENT_FIELDS.items():
                for candidate in candidates:
                    scalar = _scalar(item.get(candidate))
                    if scalar is not None:
                        ref[field] = scalar
                        break
            if ref:
                found.append(ref)
    return found


def entry_reply_to(entry, role, seq, ids):
    for key in REPLY_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and ENTRY_ID.match(value):
            return value
    if role == 'assistant' and f't{seq}u' in ids:
        return f't{seq}u'
    return None


def extract(entries):
    """Window bounds over all entries and both sides of the chat, oldest first.

    ``window['diag']`` carries counts and field names only (never values), so
    the Hub shows whether the replica format still parses as expected.
    """
    parsed, kinds = [], {}
    diag = {'user_entries': 0, 'assistant_entries': 0, 'without_text': 0, 'without_ts': 0,
            'user_keys': [], 'assistant_keys': []}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get('kind'))[:40]
        kinds[kind] = kinds.get(kind, 0) + 1
        match = ENTRY_ID.match(entry['id']) if isinstance(entry.get('id'), str) else None
        if match:
            parsed.append((entry, match, kind))
    ids = {entry['id'] for entry, _, _ in parsed}
    seqs, messages = [], []
    for position, (entry, match, kind) in enumerate(parsed):
        seq = int(match.group(1))
        seqs.append((seq, position, entry['id']))
        if kind in SKIPPED_KINDS:
            continue
        role = ROLES.get(match.group(2), 'system')
        if role in ('user', 'assistant'):
            diag[role + '_entries'] += 1
            if not diag[role + '_keys']:
                diag[role + '_keys'] = sorted(str(key)[:40] for key in entry)[:20]
        text, ts, attachments = entry_text(entry), entry_ts(entry), entry_attachments(entry)
        if ts is None:
            diag['without_ts'] += 1
        if text is None:
            diag['without_text'] += 1
            if not attachments:
                continue
        messages.append({'position': position, 'msg_id': entry['id'], 't_id': f't{seq}', 'seq': seq, 'role': role,
                         'kind': kind, 'ts': ts, 'text': text,
                         'reply_to': entry_reply_to(entry, role, seq, ids), 'attachments': attachments})
    diag['kinds'] = dict(sorted(kinds.items())[:20])
    window = {'entry_count': len(entries)}
    if seqs:
        first, last = min(seqs), max(seqs)
        window.update(first_msg_id=first[2], last_msg_id=last[2], first_seq=first[0], last_seq=last[0])
    window['diag'] = diag
    unique = {}
    for message in sorted(messages, key=lambda item: (item['seq'], item.pop('position'))):
        unique.setdefault(message['msg_id'], message)
    return window, list(unique.values())


def _berlin_offset(utc):
    """CET/CEST offset; EU rule (last Sunday of March/October, 01:00 UTC)."""
    def last_sunday(month):
        day = datetime(utc.year, month + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        return day - timedelta(days=(day.weekday() + 1) % 7)
    return timedelta(hours=2) if last_sunday(3) <= utc < last_sunday(10) else timedelta(hours=1)


def berlin_iso(ms):
    utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc.astimezone(ZoneInfo('Europe/Berlin')).isoformat(timespec='seconds')
    except Exception:  # Windows Python without tzdata
        offset = _berlin_offset(utc)
        return utc.astimezone(timezone(offset)).isoformat(timespec='seconds')


def ledger_record(agent_id, name, message):
    """One A13 JSONL object: stable fields, verbatim text."""
    ts = message.get('ts')
    return {
        'agent_id': agent_id, 'agent_name': name, 'msg_id': message['msg_id'], 't_id': message['t_id'],
        'role': message['role'], 'kind': message['kind'],
        'ts_utc': datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z') if ts else None,
        'ts_berlin': berlin_iso(ts) if ts else None,
        'text': message['text'], 'reply_to': message['reply_to'],
        'attachments': message['attachments'], 'source': SOURCE,
    }


def _load_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path, state):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state, sort_keys=True), encoding='utf-8')
    os.replace(tmp, path)


def _payload_bytes(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))


def build_posts(machine_id, agents, now_ms, reason='poll'):
    """Split agent snapshots into ordered posts within the agreed ingest limits."""
    def body(items):
        return {'source': SOURCE, 'machine_id': machine_id, 'observed_at': now_ms,
                'reason': reason, 'agents': items}

    items = []
    for agent in agents:
        item = dict(agent, messages=[])
        for message in agent['messages']:
            if len(item['messages']) >= MAX_MESSAGES_PER_AGENT or (
                    item['messages'] and _payload_bytes(body([dict(item, messages=item['messages'] + [message])])) > MAX_BODY_BYTES):
                items.append(item)
                item = dict(agent, messages=[])
            item['messages'].append(message)
        items.append(item)
    posts, current = [], []
    for item in items:
        candidate = current + [item]
        if current and (len(candidate) > MAX_AGENTS_PER_POST or _payload_bytes(body(candidate)) > MAX_BODY_BYTES):
            posts.append(body(current))
            candidate = [item]
        current = candidate
    if current:
        posts.append(body(current))
    return posts


class LocalLedger:
    """Append-only JSONL copy, deduplicated on (agent_id, msg_id)."""

    def __init__(self, path):
        self.path = Path(path)
        self.keys = set()
        try:
            with self.path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        self.keys.add((row['agent_id'], row['msg_id']))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass

    def append(self, agent_id, name, messages):
        rows = []
        for message in messages:
            key = (agent_id, message['msg_id'])
            if key not in self.keys:
                rows.append(json.dumps(ledger_record(agent_id, name, message), ensure_ascii=False))
                self.keys.add(key)
        if rows:
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write('\n'.join(rows) + '\n')
        return len(rows)


class ReplicaWatcher:
    """One poll per ``step``; the session host thread calls ``loop``."""

    def __init__(self, request, machine_id, journal_dir, *, root=None, clock=time.time,
                 monotonic=time.monotonic, log=print):
        self.request = request
        self.machine_id = machine_id
        self.root = Path(root) if root else default_root()
        self.state_path = Path(journal_dir) / STATE_FILENAME
        self.config = _load_json(Path(journal_dir) / CONFIG_FILENAME)
        self.ledger = LocalLedger(Path(journal_dir) / LEDGER_FILENAME)
        self.clock = clock
        self.monotonic = monotonic
        self.log = log
        self.state = _load_json(self.state_path)
        self.state.setdefault('agents', {})
        self.last_post_at = 0.0
        self.failures = 0
        self.next_attempt_at = 0.0
        self.reason = 'startup'
        self.catchup_until = clock() + CATCHUP_S
        self._wall = clock()
        self._mono = monotonic()

    def enabled(self):
        return self.config.get('enabled', True) is not False and self.root is not None and os.path.isdir(_long(self.root))

    def detect_resume(self):
        """A wall-clock jump well beyond monotonic time means the box slept."""
        wall, mono = self.clock(), self.monotonic()
        jumped = (wall - self._wall) - (mono - self._mono) > RESUME_JUMP_S
        self._wall, self._mono = wall, mono
        if jumped:
            self.reason = 'resume'
            self.catchup_until = wall + CATCHUP_S
            self.next_attempt_at = 0.0
            self.log(json.dumps({'event': 'replica_watcher_resume'}), flush=True)
        return jumped

    def interval(self):
        return CATCHUP_POLL_S if self.clock() < self.catchup_until else POLL_S

    def snapshot(self):
        """Per-agent window plus messages not yet acknowledged by the Hub."""
        agents, signatures, skipped = [], {}, 0
        for agent_id, path in list_replicas(self.root):
            try:
                stat = os.stat(_long(path))
                entries = read_entries(path)
            except (OSError, ValueError):
                skipped += 1
                continue
            window, messages = extract(entries)
            record = self.state['agents'].get(agent_id, {})
            acked = set(record.get('acked_ids') or [])
            acked_seq = record.get('acked_seq')
            gap = None
            if isinstance(acked_seq, int) and window.get('first_seq') is not None and window['first_seq'] > acked_seq + 1:
                # The window no longer overlaps anything acknowledged: entries
                # between the two were rotated out while this host was away.
                gap = {'after_msg_id': record.get('acked_last_id'), 'after_seq': acked_seq,
                       'first_seq_in_window': window['first_seq']}
            fresh = [message for message in messages if message['msg_id'] not in acked]
            agents.append({'agent_id': agent_id, 'agent_name': agent_name(agent_id),
                           'window': window, 'gap': gap, 'messages': fresh})
            signatures[agent_id] = [stat.st_size, int(stat.st_mtime_ns)]
        return agents, signatures, skipped

    def _acknowledge(self, agent):
        record = self.state['agents'].setdefault(agent['agent_id'], {})
        acked = list(record.get('acked_ids') or [])
        known = set(acked)
        for message in agent['messages']:
            if message['msg_id'] not in known:
                acked.append(message['msg_id'])
                known.add(message['msg_id'])
        record['acked_ids'] = acked[-MAX_ACKED_IDS:]
        last_seq = agent['window'].get('last_seq')
        if isinstance(last_seq, int) and last_seq >= int(record.get('acked_seq') or 0):
            record['acked_seq'] = last_seq
            record['acked_last_id'] = agent['window'].get('last_msg_id')

    def step(self):
        """Read replicas; post when anything changed or a heartbeat is due."""
        self.detect_resume()
        now = self.clock()
        if now < self.next_attempt_at:
            return {'skipped': 'backoff'}
        agents, signatures, skipped = self.snapshot()
        changed = (signatures != self.state.get('signatures') or self.reason != 'poll'
                   or any(a['messages'] or a['gap'] for a in agents))
        if not changed and now - self.last_post_at < HEARTBEAT_S:
            return {'skipped': 'unchanged', 'agents': len(agents)}
        reason = self.reason if changed else 'heartbeat'
        for agent in agents:
            self.ledger.append(agent['agent_id'], agent['agent_name'], agent['messages'])
        totals = {'posts': 0, 'accepted': 0, 'duplicates': 0, 'conflicts': 0, 'rejected': 0, 'gaps': 0}
        try:
            for post in build_posts(self.machine_id, agents, int(now * 1000), reason):
                response = self.request('POST', INGEST_PATH, post) or {}
                if response.get('ok') is not True:
                    raise RuntimeError('ingest_not_ok')
                totals['posts'] += 1
                for key in ('accepted', 'duplicates'):
                    totals[key] += int(response.get(key) or 0)
                for key in ('conflicts', 'rejected', 'gaps'):
                    totals[key] += len(response.get(key) or [])
                for agent in post['agents']:
                    self._acknowledge(agent)
        except Exception as exc:
            self.failures += 1
            delay = min(MAX_BACKOFF_S, POLL_S * (2 ** min(self.failures, 5)))
            self.next_attempt_at = now + delay
            _save_state(self.state_path, self.state)
            status = getattr(exc, 'code', None)
            result = {'event': 'replica_watcher_error', 'type': type(exc).__name__,
                      'status': status if isinstance(status, int) else None, 'retry_s': delay}
            self.log(json.dumps(result), flush=True)
            return result
        self.failures = 0
        self.last_post_at = now
        self.reason = 'poll'
        self.state['signatures'] = signatures
        _save_state(self.state_path, self.state)
        result = dict(event='replica_watcher_cycle', reason=reason, agents=len(agents),
                      skipped_blobs=skipped, local_gaps=sum(1 for a in agents if a['gap']), **totals)
        self.log(json.dumps(result), flush=True)
        return result

    def loop(self, stop):
        if not self.enabled():
            self.log(json.dumps({'event': 'replica_watcher_disabled'}), flush=True)
            return
        while not stop.is_set():
            started = self.monotonic()
            try:
                self.step()
            except Exception as exc:
                self.log(json.dumps({'event': 'replica_watcher_error', 'type': type(exc).__name__}), flush=True)
            stop.wait(max(1.0, self.interval() - (self.monotonic() - started)))


def loop(request, stop, journal_dir, machine_id):
    """Session host entry point (daemon thread)."""
    ReplicaWatcher(request, machine_id, journal_dir).loop(stop)
