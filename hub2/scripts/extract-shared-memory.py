#!/usr/bin/env python3
"""Extract Hub1 shared memory to private, replayable POST import batches.

Offline only: never reads credentials or contacts a server. POST each batch to
/api/shared-memory/import using the existing operator credential after review.
Retain the manifest and server receipts, and compare every count/digest before
accepting the migration. Never commit the output (it contains private messages).
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path

TABLES = {'intake_rules', 'asks', 'ask_messages', 'ask_agents', 'ask_counts',
          'ask_gaps', 'ask_meta', 'ask_sources', 'ask_reconcile_runs'}


class Stream:
    """Decode one row at a time; do not materialize unrelated corpus tables."""
    def __init__(self, source):
        self.source, self.buf, self.pos = source, '', 0
        self.decoder = json.JSONDecoder()

    def value(self):
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos].isspace():
                self.pos += 1
            try:
                result, end = self.decoder.raw_decode(self.buf, self.pos)
                self.pos = end
                return result
            except json.JSONDecodeError:
                chunk = self.source.read(65536)
                if not chunk:
                    raise ValueError('truncated or invalid export') from None
                self.buf = self.buf[self.pos:] + chunk
                self.pos = 0
                if len(self.buf) > 16 * 1024 * 1024:
                    raise ValueError('export row exceeds 16 MiB; inspect offline before retrying')

    def char(self):
        while True:
            if self.pos >= len(self.buf):
                self.buf, self.pos = self.source.read(65536), 0
            if not self.buf:
                raise ValueError('truncated export')
            value = self.buf[self.pos]
            self.pos += 1
            if not value.isspace():
                return value

    def expect(self, char):
        if self.char() != char:
            raise ValueError('invalid export structure')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def extract(source, output):
    os.umask(0o077)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    counts, expected, batches, rows = {}, None, [], []
    digest = hashlib.sha256()

    def flush():
        if not rows:
            return
        body = encode({'rows': rows})
        name = f'batch-{len(batches):05d}.json'
        (output / name).write_bytes(body)
        # Server receipt hashes the exact rows array, reserialized as JSON.
        batches.append({'file': name, 'rows': len(rows), 'sha256': hashlib.sha256(body).hexdigest(),
                        'rows_digest': hashlib.sha256(encode(rows)).hexdigest()})
        rows.clear()

    with gzip.open(source, 'rt', encoding='utf-8') as handle:
        stream = Stream(handle)
        stream.expect('{')
        while True:
            key = stream.value()
            stream.expect(':')
            if key != 'tables':
                value = stream.value()
                if key == 'row_counts':
                    if any(k.startswith('ask') and k not in TABLES for k in value):
                        raise ValueError('unknown ask ledger table; extend lossless import before migrating')
                    expected = {k: v for k, v in value.items() if k in TABLES}
            else:
                stream.expect('{')
                while True:
                    table = stream.value()
                    stream.expect(':')
                    stream.expect('[')
                    take, count = table in TABLES, 0
                    char = stream.char()
                    if char != ']':
                        stream.pos -= 1
                        while True:
                            row = stream.value()
                            count += 1
                            if take:
                                entry = {'table': table, 'row': row}
                                # Leave room for JSON envelope below the 96 KiB API cap.
                                if len(encode({'rows': [entry]})) > 90000:
                                    raise ValueError('shared-memory row exceeds import batch cap')
                                if rows and (len(rows) >= 100 or len(encode({'rows': rows + [entry]})) > 90000):
                                    flush()
                                rows.append(entry)
                                digest.update(encode(entry) + b'\n')
                            char = stream.char()
                            if char == ']':
                                break
                            if char != ',':
                                raise ValueError('invalid table array')
                    if take:
                        counts[table] = count
                    char = stream.char()
                    if char == '}':
                        break
                    if char != ',':
                        raise ValueError('invalid tables object')
            char = stream.char()
            if char == '}':
                break
            if char != ',':
                raise ValueError('invalid export object')
    if expected is None or counts != expected:
        raise ValueError('export row_counts mismatch; no migration manifest produced')
    flush()
    manifest = {'format': 'hub1-shared-memory-v1', 'counts': counts,
                'rows': sum(counts.values()), 'content_sha256': digest.hexdigest(), 'batches': batches}
    (output / 'manifest.json').write_bytes(encode(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='New private directory under gitignored .local')
    args = parser.parse_args()
    manifest = extract(args.source, args.output)
    # Counts and hashes only; never print rule/message content.
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
