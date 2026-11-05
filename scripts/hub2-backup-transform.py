# /// script
# requires-python = ">=3.11"
# dependencies = ["ijson==3.4.0.post0"]
# ///
"""Offline, streaming Hub1 recovery. Rebuild ONLY the derived intake_link_refs.
Run with uv run; input is immutable, output must be a fresh private directory.
Never contacts Hub, starts dispatch, runs alarms, or overwrites an earlier restore.
"""
import argparse
import base64
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

import ijson
from ijson.common import ObjectBuilder

DERIVED = "intake_link_refs"
MOD = 1 << 256


def ident(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("invalid_schema_identifier")
    return '"' + value + '"'


def file_hash(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"__hub_backup_bigint"}:
            return int(value["__hub_backup_bigint"])
        if set(value) == {"__hub_backup_blob_b64"}:
            return base64.b64decode(value["__hub_backup_blob_b64"], validate=True)
        raise ValueError("unrecoverable_or_unknown_value_marker")
    if isinstance(value, (list, bool)):
        raise ValueError("unexpected_sql_value")
    return value


def row_hash(row):
    normalized = {key: {"blob_b64": base64.b64encode(value).decode()} if isinstance(value, bytes) else int(value) if isinstance(value, float) and value.is_integer() else value
                  for key, value in row.items()}
    return int.from_bytes(hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                                                  separators=(",", ":"), allow_nan=False).encode()).digest(), "big")


class Digest:
    def __init__(self):
        self.count, self.total, self.xor = 0, 0, 0

    def add(self, row):
        value = row_hash(row)
        self.count += 1
        self.total = (self.total + value) % MOD
        self.xor ^= value

    def summary(self):
        return {"rows": self.count, "sha256_multiset_sum": f"{self.total:064x}", "sha256_multiset_xor": f"{self.xor:064x}"}


def rebuild_refs(connection):
    # Same link-kind/text-field semantics as initLinkRefs in src/worker-access.ts.
    parts = []
    for field in ("hub_session_id", "native_id"):
        for owner, source in (("p.id", "intake_task_projection p, json_each(p.links) e"),
                              ("t.id", "intake_tasks t, json_each(t.data,'$.history') e")):
            parts.append(f"SELECT json_extract(e.value,'$.{field}'), {owner} FROM {source} "
                         f"WHERE e.type='object' AND json_extract(e.value,'$.kind')='link' "
                         f"AND json_type(e.value,'$.{field}')='text'")
    connection.execute(f"INSERT INTO {DERIVED}(ref,task_id) " + " UNION ".join(parts) + " ON CONFLICT DO NOTHING")


def restore(source, output, expected_sha256=None):
    source = Path(source).resolve(strict=True)
    if not stat.S_ISREG(source.stat().st_mode):
        raise ValueError("input_not_regular")
    source_sha = file_hash(source)
    if expected_sha256 and source_sha != expected_sha256:
        raise ValueError("source_checksum_mismatch")
    output = Path(output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(output, 0o700)
    db_path = output / "restored.sqlite"
    fd = os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    schema, expected, metadata, digests, columns = {}, {}, {}, {}, {}
    builder = None
    building = None
    depth = 0
    table = None
    stage = "metadata"
    try:
        with gzip.open(source, "rb") as stream:
            for prefix, event, value in ijson.parse(stream, use_float=True):
                if builder is not None:
                    builder.event(event, value)
                    if event in ("start_map", "start_array"):
                        depth += 1
                    elif event in ("end_map", "end_array"):
                        depth -= 1
                    if depth == 0:
                        obj = builder.value
                        builder = None
                        if building == "schema":
                            schema = obj
                            for name, ddl in sorted(schema.items()):
                                ident(name)
                                if re.match(r"^\s*CREATE\s+(?:VIRTUAL\s+)?TABLE\b", ddl, re.I):
                                    connection.execute(ddl)
                        elif building == "row_counts":
                            expected = obj
                        elif building == "value_markers":
                            if int(obj.get("unreadable", 0)) != 0:
                                raise ValueError("unrecoverable_value_markers")
                        else:
                            if not isinstance(obj, dict):
                                raise ValueError("row_not_object")
                            row = {key: decode(v) for key, v in obj.items()}
                            digests[table].add(row)
                            if table != DERIVED:
                                available = columns[table]
                                if set(row) != set(available):
                                    raise ValueError("row_column_mismatch")
                                writable = [key for key in row if available[key] == 0]
                                connection.execute("INSERT INTO " + ident(table) + "(" + ",".join(map(ident, writable))
                                                   + ") VALUES(" + ",".join("?" for _ in writable) + ")",
                                                   [row[key] for key in writable])
                        building = None
                    continue
                if prefix in ("schema", "row_counts", "value_markers") and event == "start_map":
                    builder, building, depth = ObjectBuilder(), prefix, 1
                    builder.event(event, value)
                elif prefix in ("snapshot", "exported_at") and event in ("string", "null"):
                    metadata[prefix] = value
                elif prefix == "tables" and event == "map_key":
                    if metadata.get("snapshot") != "hub-do-sqlite" or not schema or not expected:
                        raise ValueError("unsupported_metadata_order")
                    stage = "rows"
                    table = value
                    ident(table)
                    if table in digests or table not in expected:
                        raise ValueError("duplicate_or_unknown_table")
                    digests[table] = Digest()
                    columns[table] = {row[1]: row[6] for row in connection.execute("PRAGMA table_xinfo(" + ident(table) + ")")}
                elif table and prefix == "tables." + table + ".item":
                    if event != "start_map":
                        raise ValueError("row_not_object")
                    builder, building, depth = ObjectBuilder(), "row", 1
                    builder.event(event, value)
        if builder is not None or set(digests) != set(expected) or DERIVED not in digests:
            raise ValueError("incomplete_export")
        if any(digests[name].count != count for name, count in expected.items()):
            raise ValueError("export_count_mismatch")
        stage = "derived_rebuild"
        rebuild_refs(connection)
        stage = "schema"
        for name, ddl in sorted(schema.items()):
            if not re.match(r"^\s*CREATE\s+(?:VIRTUAL\s+)?TABLE\b", ddl, re.I):
                if not re.match(r"^\s*CREATE\s+(?:UNIQUE\s+)?(?:INDEX|TRIGGER|VIEW)\b", ddl, re.I):
                    raise ValueError("unsupported_schema_object")
                connection.execute(ddl)
        connection.commit()
        stage = "verification"
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if [row[0] for row in integrity] != ["ok"]:
            raise ValueError("integrity_check_failed")
        with open(output / "source-digests.json", "x", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)
            json.dump({name: digest.summary() for name, digest in digests.items()}, stream, sort_keys=True)
        tables = {}
        for name in sorted(digests):
            table = name
            actual = Digest()
            for row in connection.execute("SELECT * FROM " + ident(name)):
                actual.add(dict(row))
            original = digests[name].summary()
            current = actual.summary()
            if name != DERIVED and original != current:
                raise ValueError("canonical_content_mismatch")
            tables[name] = {"source": original, "restored": current, "derived_rebuilt": name == DERIVED}
        proof = {"version": 1, "source_sha256": source_sha, "snapshot_exported_at": metadata.get("exported_at"),
                 "integrity_check": "ok", "schema_objects": len(schema), "tables": tables,
                 "canonical_tables_unchanged": len(tables) - 1, "only_rebuilt_table": DERIVED,
                 "source_total_rows": sum(d.count for d in digests.values()),
                 "restored_total_rows": sum(v["restored"]["rows"] for v in tables.values()),
                 "current_live_state_verified": False}
        connection.close()
        proof["restored_database_sha256"] = file_hash(db_path)
        if file_hash(source) != source_sha:
            raise ValueError("source_changed_during_restore")
        with open(output / "proof.json", "x", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)
            json.dump(proof, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return proof
    except Exception as error:
        connection.close()
        # Keep every partial artifact privately for diagnosis; do not print source rows/errors.
        with open(output / "failed.json", "x", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)
            json.dump({"stage": stage, "table": table, "error_type": type(error).__name__, "error": str(error)[:240], "source_sha256": source_sha, "complete": False}, stream)
        raise RuntimeError("restore_failed_at_" + stage) from None


def prepare_plan(restored, output):
    """Prepare original-ID task/history transfers; immutable full SQLite remains authoritative."""
    import datetime
    import time
    import uuid
    import_run_id = str(uuid.uuid4())
    restored, output = Path(restored), Path(output)
    proof = json.loads((restored / "proof.json").read_text())
    database = restored / "restored.sqlite"
    if proof.get("integrity_check") != "ok" or file_hash(database) != proof["restored_database_sha256"]:
        raise ValueError("verified_restore_required")
    output.mkdir(mode=0o700, exist_ok=False)
    now = int(time.time() * 1000)
    generation = proof["source_sha256"]
    def stamp(value):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                delta = parsed.astimezone(datetime.timezone.utc) - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
                return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
        raise ValueError("unknown_source_timestamp")
    total = events_total = unknown_times = missing_ac = 0
    payload_digests = []
    statuses = {"open": "open", "done": "accepted", "cancelled": "archived",
                "blocked": "needs_attention", "in_progress": "needs_attention"}
    plan = output / "tasks.ndjson"
    with closing(sqlite3.connect("file:" + str(database.resolve()) + "?mode=ro", uri=True)) as connection, plan.open("x", encoding="utf-8") as stream:
        os.chmod(plan, 0o600)
        for identity, raw in connection.execute("SELECT id,data FROM intake_tasks ORDER BY id"):
            record = json.loads(raw)
            if record.get("id") != identity or not isinstance(record.get("project"), str):
                raise ValueError("invalid_original_identity_or_project")
            history = record.get("history", [])
            original = next((e for e in history if isinstance(e, dict) and e.get("kind") == "intake"), None)
            if not original or not isinstance(original.get("text"), str):
                raise ValueError("original_ask_unavailable")
            ordered, next_sequence = record.get("_history_order") or {}, record.get("_history_next_seq") or 0
            events = []
            for event in history:
                if event.get("id") in ordered:
                    sequence = ordered[event["id"]]
                else:
                    sequence, next_sequence = next_sequence, next_sequence + 1
                events.append((sequence, event))
            archive_count = 0
            for sequence, event in connection.execute("SELECT seq,data FROM intake_note_archive WHERE task_id=? ORDER BY seq", (identity,)):
                events.append((sequence, json.loads(event)))
                archive_count += 1
            if archive_count != (record.get("history_archive") or {}).get("count", 0):
                raise ValueError("archived_history_count_mismatch")
            events.sort(key=lambda e: e[0])
            if len({e[0] for e in events}) != len(events):
                raise ValueError("history_sequence_conflict")
            converted = []
            for sequence, event in events:
                try:
                    at, unknown = stamp(event.get("captured_at")), False
                except (ValueError, TypeError):
                    at, unknown = now, True
                    unknown_times += 1
                converted.append({"sequence": sequence, "event": event, "at": at, "source_time_unknown": unknown})
            criteria = record.get("acceptance_criteria")
            if criteria is None:
                criteria = []
                missing_ac += 1
            elif isinstance(criteria, str):
                criteria = [criteria]
            else:
                raise ValueError("unsupported_legacy_criteria")
            task = {"import_run_id": import_run_id, "source_sha256": proof["source_sha256"], "generation": generation, "id": identity,
                    "project": record["project"], "record": record, "title": record["title"], "text": original["text"],
                    "acceptance_criteria": criteria, "status": "needs_attention" if not criteria and record["status"] == "open" else statuses.get(record["status"], "needs_attention"),
                    "parent_intake_id": original.get("parent_intake_id"), "live_state_verified": proof.get("current_live_state_verified") is True,
                    "created_at": stamp(record["created_at"]), "updated_at": stamp(record["updated_at"]),
                    "imported_at": now, "events": converted}
            line = json.dumps(task, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(line.encode()) > 8 * 1024 * 1024:
                raise ValueError("task_transfer_bound")
            payload_digests.append(hashlib.sha256(line.encode()).hexdigest())
            stream.write(line + chr(10))
            total += 1
            events_total += len(events)
        stream.flush()
        os.fsync(stream.fileno())
    levels = [[hashlib.sha256(("leaf:" + digest).encode()).hexdigest() for digest in payload_digests]]
    while len(levels[-1]) > 1:
        row = levels[-1]
        levels.append([hashlib.sha256(("node:" + row[i] + row[min(i + 1, len(row) - 1)]).encode()).hexdigest()
                       for i in range(0, len(row), 2)])
    proofs = []
    for index in range(len(payload_digests)):
        merkle_proof = []
        for row in levels[:-1]:
            left = index % 2 == 1
            merkle_proof.append({"side": "left" if left else "right", "hash": row[index - 1 if left else min(index + 1, len(row) - 1)]})
            index //= 2
        proofs.append(merkle_proof)
    result = {"version": 1, "import_run_id": import_run_id, "source_sha256": proof["source_sha256"], "generation": generation,
              "plan_sha256": file_hash(plan), "merkle_root": levels[-1][0], "payload_digests": payload_digests, "proofs": proofs, "task_count": total, "event_count": events_total,
              "unknown_source_event_times": unknown_times, "tasks_without_legacy_ac": missing_ac,
              "live_state_verified": proof.get("current_live_state_verified") is True,
              "project_resolution": "literal-source-awaits-canonical-registry-reconciliation",
              "other_domain_records_preserved_in_sqlite": True, "other_domains_verified": False,
              "queue_effects_enabled": False, "schedule_effects_enabled": False, "archive32_applied": False}
    with (output / "manifest.json").open("x") as stream:
        os.chmod(stream.name, 0o600)
        json.dump(result, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?")
    parser.add_argument("--prepare-plan-from")
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.prepare_plan_from:
            prepared = prepare_plan(args.prepare_plan_from, args.output)
            print(json.dumps({k: prepared[k] for k in ("source_sha256", "plan_sha256", "merkle_root", "task_count", "event_count", "unknown_source_event_times", "tasks_without_legacy_ac", "live_state_verified")}))
            sys.exit(0)
        if not args.source:
            raise ValueError("source_required")
        proof = restore(args.source, args.output, args.expected_sha256)
        print(json.dumps({key: proof[key] for key in ("source_sha256", "integrity_check", "canonical_tables_unchanged",
                                                     "source_total_rows", "restored_total_rows", "restored_database_sha256")}))
    except Exception as error:
        print(type(error).__name__ + ": offline recovery refused; private partial evidence retained", file=sys.stderr)
        sys.exit(1)
