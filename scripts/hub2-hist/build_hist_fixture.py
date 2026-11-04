#!/usr/bin/env python3
"""Hub2 historical reconciliation fixture builder (canary 5, workstream `historical`).

Reads the immutable local evidence layers for the 2026-10-26 Hub intake loss and
the 2026-11-03 loss/restore/reopen wave, reconciles the 160 member records of the
`historical` backlog workstream, and emits an ADDITIVE migration-mapping fixture:

  fixtures/hub2-hist-mapping-fixture.json

Per member the fixture carries: the exact-once brief member id (typo preserved),
the canonical recreation id, the original/recreated mapping, per-snapshot status
layers, the 2026-11-03 pre-loss status and reopen result, triage evidence, an
explicit keep/skip/reclassify verdict with a reason, and a suggested evidence
lane. It never mutates any input, never touches the live DB, and strips
credential-bearing fields (actors, tokens).

Inputs are hash-pinned; any mismatch or missing file fails closed. The output is
byte-deterministic (no wall-clock fields) so re-runs diff cleanly.

Usage: python3 scripts/hub2-hist/build_hist_fixture.py [--outdir DIR]
Inputs live outside the repo (/home/alexey/tmp evidence); this builder is only
expected to run on the Hetzner host that holds them. Tests cover the pure
decision/sanitization logic with synthetic data and the committed fixture.
"""
import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TMP = "/home/alexey/tmp"

# All 160 brief member ids are canonical and used verbatim (verified
# well-formed at extraction; no repairs needed).
CANONICALIZE = {}

# Inputs with pinned sha256 where the evidence chain itself records one (the
# relevance ledger's source descriptors). The remaining entries are pinned by
# the hashes recorded in the committed fixture's "inputs" block.
INPUTS = [
    (TMP + "/intakes.json", "ab746e696000aa2fb36f84203736107489fb56f086d3787f924cc3b0167289b4"),
    (TMP + "/intakes-1031.json", "b57d65fdd8c065612a4af6a8ca5c5ab7a7fde987cd0bd71408a5165a418d32d6"),
    (TMP + "/open-intakes.json", "cb8364b54d3e5d8948f5926e86082fb1e6dc7aca538ed4b03d6c72b611ba4ee3"),
    (TMP + "/agent-reports/hub2-backlog-relevance.json",
     "36132ca14297d95f73509962550e511c506592111fd29d4de8d733133c5e7bd4"),
    (TMP + "/agent-reports/hub2-backlog-workstreams.json",
     "5910a35c59d96ef0b725f2841a1f4cd13dff82d39036c7367964a4b94fa4b1cd"),
    (TMP + "/agent-reports/hub-intake-loss-recovery-2026-10-26.md",
     "bf1b2416d37fd1ced3bd68fe36151650dc0ea468e0c8295dcd850120e92335db"),
    (TMP + "/intake-loss-2026-10-26/lost-ids.tsv",
     "9c89ba141265439d6f235350d0e2d1f736286651a0b7892b8a9cf22fc75ed864"),
    (TMP + "/intake-loss-2026-10-26/drain4-evidence/recreated-status-verification.json",
     "caf97086b0fdcd41d4973693d628dd0d1592a01b4bfe079630f627c9d179b70d"),
    (TMP + "/agent-reports/hub-intake-loss-2026-11-03-restored-status.json",
     "95d7ad03c6c01695fb141a9444c1d6f1be4bc43d5672e102b202e3c7d5130e3f"),
    (TMP + "/agent-reports/hub-intake-loss-2026-11-03-reopen-progress-v2.json",
     "4f26fabaca2879ba83c9b3b2228781fb8c9a77224f1e88dee491b9aa119ef115"),
    (TMP + "/agent-reports/backlog-triage-20261029.md",
     "eb773a2f44fb2d51264d0baaf3e9ccac508a35995ce342964b868d2f8a45346b"),
    (TMP + "/agent-reports/drain/triaged-done.tsv",
     "58cf68cefb8a84ce67cb627536e683ed3fdfb667d15a4cc4bad7c00bc513ad9c"),
    (TMP + "/agent-reports/drain/triaged-actual.tsv",
     "ce384e3f109ab1d273b9abc0e8b087a89a8b7abaad94fba3db8dbe0872e4629f"),
    (TMP + "/agent-reports/drain/triaged-duplicate.tsv",
     "6595bf69b96eb831df37c1f097edb6f88c375854510b9c23001ce3d585b2169f"),
    (TMP + "/agent-reports/drain/triaged-obsolete.tsv",
     "5ed9f239848c8564757dd5f00c308db8658e9e9fb57917da9ad678ec7e9a4a87"),
]

SNAPSHOTS = [
    ("2026-11-01", TMP + "/open-intakes.json"),
    ("2026-10-31", TMP + "/intakes-1031.json"),
    ("2026-10-29", TMP + "/intakes.json"),
]

# Sibling workstream lanes (hub2-backlog-workstreams.json local_key values).
HUB2_LANES = {
    "records", "execution", "fleet", "identity", "quota",
    "messaging", "knowledge", "autonomy", "clients", "operations",
    "historical",
}

# Explicit lane decisions for the six non-recreated KEEP members (evidence
# anchors). Keys are canonical ids.
KEEP_LANE_OVERRIDES = {
    "c005a30c-8339-4efe-8051-f96cf7cb84d1": ("quota", "admission_refused blocks auto-placed starts: account admission policy"),
    "955e9381-c06a-405f-a29a-74ce9cf2b498": ("records", "no-hard-delete guard + additive original/recreated mapping is the records/migration AC"),
    "5b522912-a2e8-49db-a26b-771adc806d48": ("records", "intake scope matcher decision semantics"),
    "dcac7cd4-6736-4654-8c25-850ec3d73056": ("execution", "session hold needs_unmet reason quality in dispatch"),
    "83511a62-5e50-4e86-9b50-e73da549b442": ("historical", "cancelled umbrella for no-AC stub tails triage; stays with the stubs it lists"),
    "de6f2139-b514-4e89-9c62-8c7ec5125d07": ("execution", "zcodex native session exit handling and routing trust"),
}

# Known non-Hub owner projects seen in triage/drain evidence. When one of these
# is named as the actual owner the member reclassifies outward.
EXTERNAL_PROJECTS = {
    "machine-management", "dataops", "dataops-knowledge", "dtc-website",
    "course-management-platform", "relay", "aws-infra", "quse", "aplexer",
    "pocketshell", "pocketshell-cli", "coolify-observability",
    "ai-shipping-labs", "dapier", "dtc-control-room", "slack-moderation",
    "slack-mod-app", "openscan", "community-base", "website",
    "telegram-writing-assistant", "faq-assistant", "agent-quota-launcher",
    "infrastructure", "demo", "rds-export",
}

WORK_CONCERNS = re.compile(r"work concerns ([a-z0-9-]+)")
REALLY_PROJECT = re.compile(r"really ([a-z0-9-]+)")
RECOVERED_ROW = re.compile(
    r"^\| (.+?) \| ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) \| (.+?) \| (\w+) \|$")
TRIAGE_ROW = re.compile(
    r"^\| ([0-9a-f]{8}) \| ([A-Za-z0-9 ()-]+?) \| ([A-Z_]+) \| (.+?) \| (.+?) \|$")
UUID_COL = re.compile(r"^[0-9a-f]{8}-")
UUID_FULL = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

VERDICTS = ("keep", "skip", "reclassify")
# Fields that must never reach the committed fixture.
FORBIDDEN_KEYS = {"actor", "token", "credential", "authorization", "secret",
                  "password", "bearer"}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_member_ids(path=None, expected_count=160):
    path = path or os.path.join(HERE, "brief-member-ids.txt")
    with open(path, "r", encoding="utf-8") as f:
        ids = [line.strip() for line in f if line.strip()]
    if len(ids) != expected_count or len(set(ids)) != expected_count:
        raise SystemExit("brief member list must be %d exact-once ids, got %d"
                         % (expected_count, len(ids)))
    malformed = [i for i in ids if not UUID_FULL.match(i)]
    if malformed:
        raise SystemExit("malformed member ids: %s" % malformed)
    return ids


def load_pinned(expected_hashes):
    """Read every pinned input, verifying hashes. Fails closed."""
    data, hashes = {}, {}
    for path, expected in INPUTS:
        if not os.path.exists(path):
            raise SystemExit("FAIL CLOSED: missing evidence input %s" % path)
        digest = sha256_file(path)
        want = expected or expected_hashes.get(path)
        if want and want != digest:
            raise SystemExit("FAIL CLOSED: evidence drift for %s (expected %s, got %s)"
                             % (path, want, digest))
        hashes[path] = digest
        with open(path, "r", encoding="utf-8") as f:
            data[path] = f.read()
    return data, hashes


def parse_recovery_map(text):
    """recreated id -> {original, data_sources, original_final_status}."""
    rows = {}
    for line in text.splitlines():
        m = RECOVERED_ROW.match(line)
        if m:
            orig, recreated, source, final = m.groups()
            rows[recreated] = {
                "original": orig,
                "data_sources": [s.strip() for s in source.split("; ") if s.strip()],
                "original_final_status": final,
            }
    return rows


def parse_lost_ids(text):
    out = {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        prefix, paths = line.split("\t", 1)
        out[prefix.strip()] = [p.strip() for p in paths.split(";") if p.strip()]
    return out


def parse_backlog_triage(text):
    rows = {}
    for line in text.splitlines():
        m = TRIAGE_ROW.match(line)
        if m:
            id8, project, cls, evidence, next_step = m.groups()
            rows[id8] = {"class": cls, "project": project.strip(),
                         "evidence": evidence.strip(), "next": next_step.strip()}
    return rows


def parse_drain_tsv(text):
    rows = {}
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3 and UUID_COL.match(parts[0]):
            rows[parts[0]] = {"project": parts[1].strip(), "evidence": parts[2].strip()}
    return rows


def snapshot_items(data):
    out = {}
    for label, path in SNAPSHOTS:
        parsed = json.loads(data[path])
        items = parsed["items"] if isinstance(parsed, dict) else parsed
        out[label] = {r["id"]: r for r in items}
    return out


def history_notes(record):
    """Recreation/original-state facts from a snapshot record's payload history."""
    notes = {"recreated_from": None, "original_final_status": None,
             "data_sources": [], "triage_kinds": [], "never_close": False}
    payload = record.get("payload_data") or {}
    for ev in payload.get("history") or []:
        t = ev.get("text", "")
        m = re.search(r"recreated after loss, original id ([0-9a-f-]+)", t)
        if m:
            notes["recreated_from"] = m.group(1)
        m = re.search(r"Original final state \(recovered\): (\w+)", t)
        if m:
            notes["original_final_status"] = m.group(1)
        if "Data sources:" in t:
            tail = t.split("Data sources:", 1)[1]
            tail = tail.split("Recovery intake")[0]
            for ds in tail.splitlines():
                ds = ds.strip().lstrip("-").strip()
                if ds:
                    notes["data_sources"].append(ds)
        if "Never close this intake" in t:
            notes["never_close"] = True
        tm = re.match(r"TRIAGE (\w+)", t)
        if tm:
            notes["triage_kinds"].append(tm.group(1))
    return notes


def best_original_status(recovery_row, notes, reopen, rsv_row, restored_row):
    """Most explicit original-final-state evidence wins; unknown otherwise."""
    for candidate in (
        (recovery_row or {}).get("original_final_status"),
        notes.get("original_final_status"),
        (reopen or {}).get("source_status"),
        (rsv_row or {}).get("original_status"),
        (restored_row or {}).get("source_status"),
    ):
        if candidate and candidate != "unknown":
            return candidate
    return "unknown"


def owner_project(bt_row, drain_rows):
    """Actual owner project named by triage evidence, if not agent-hub."""
    for row in drain_rows:
        proj = row.get("project", "")
        if proj in EXTERNAL_PROJECTS:
            return proj
    if bt_row:
        m = REALLY_PROJECT.search(bt_row["project"])
        if m and m.group(1) in EXTERNAL_PROJECTS:
            return m.group(1)
        # "work concerns <project>" appears in either the evidence or the
        # keep-id/next-step column of the triage row.
        for col in ("evidence", "next"):
            m = WORK_CONCERNS.search(bt_row.get(col, ""))
            if m and m.group(1) in EXTERNAL_PROJECTS:
                return m.group(1)
    return None


def is_finished(bt_row, drain_hits, notes, restored_row, snapshot_status_now,
                original_status):
    if drain_hits or bt_row:
        return True
    if notes.get("triage_kinds"):
        return True
    if original_status in ("done", "cancelled"):
        return True
    if (restored_row or {}).get("marker_inline"):
        return True
    if (restored_row or {}).get("status") in ("done", "cancelled"):
        return True
    if snapshot_status_now in ("done", "cancelled"):
        return True
    return False


def finish_sources(bt_row, drain_hits, notes, restored_row, reopen_row,
                   snapshot_status_now):
    parts = []
    if drain_hits:
        parts.append("drain closure rows: " + ", ".join(k for k, _ in drain_hits))
    if bt_row:
        parts.append("backlog-triage-20261029 row %s" % bt_row["class"])
    if notes.get("triage_kinds"):
        parts.append("inline TRIAGE %s markers" % "/".join(notes["triage_kinds"]))
    if (restored_row or {}).get("marker_inline"):
        parts.append("ORIGINAL FINAL STATE marker inline (restored-status)")
    pre = (restored_row or {}).get("status")
    if pre in ("done", "cancelled"):
        parts.append("pre-2026-11-03-loss status %s" % pre)
    if snapshot_status_now in ("done", "cancelled"):
        parts.append("snapshot status %s" % snapshot_status_now)
    if (reopen_row or {}).get("result"):
        parts.append("11-03 reopen result %s (record open again)" % reopen_row["result"])
    return "; ".join(parts) or "closure recorded without repo-level row"


def decide(bt_row, drain_hits, notes, restored_row, reopen_row,
           snapshot_status_now, original_status, owner, ledger_class, canonical_id):
    """Return (verdict, category, reason, suggested_lane).

    Precedence follows the workstream AC: separate finished/rejected/unverified
    and non-Hub work from current capability gaps; attach evidence to its lane.
      1. ledger KEEP -> keep (current capability gap).
      2. duplicate/obsolete/superseded evidence -> skip.
      3. non-Hub owner named -> reclassify outward (finish state in reason).
      4. finish evidence -> skip (verified vs report-only recorded honestly).
      5. residual -> keep in historical as unresolved evidence.
    """
    if ledger_class == "KEEP needed in Hub2":
        lane, why = KEEP_LANE_OVERRIDES.get(
            canonical_id, ("historical", "retained Hub2 capability per relevance ledger"))
        return ("keep", "current-capability-gap",
                "relevance ledger class KEEP needed in Hub2: %s" % why, lane)

    triage_cls = (bt_row or {}).get("class")
    drain_kinds = sorted(k for k, _ in drain_hits)
    if ("triaged-duplicate" in drain_kinds or "triaged-obsolete" in drain_kinds
            or triage_cls in ("SUPERSEDED", "EMPTY_OR_MALFORMED")):
        return ("skip", "superseded-or-duplicate",
                "triage evidence marks this record duplicate/obsolete/superseded (%s)"
                % ", ".join(drain_kinds + ([triage_cls] if triage_cls else [])), None)

    if owner:
        finished = is_finished(bt_row, drain_hits, notes, restored_row,
                               snapshot_status_now, original_status)
        return ("reclassify", "external-owner-project",
                "triage evidence names non-Hub owner project '%s' (%s); attach evidence there"
                % (owner, "finished" if finished else "finish not proven"), "external:%s" % owner)

    if is_finished(bt_row, drain_hits, notes, restored_row, snapshot_status_now,
                   original_status):
        verified = bool(drain_hits) or bt_row is not None
        return ("skip",
                "finished-with-repo-evidence" if verified else "finished-report-only",
                "finish evidence: %s" % finish_sources(bt_row, drain_hits, notes,
                                                       restored_row, reopen_row,
                                                       snapshot_status_now),
                None)

    return ("keep", "unresolved-evidence",
            "no triage/closure evidence and original final state unknown; needs coordinator triage",
            "historical")


def sanitize_record(rec):
    """Drop credential-bearing fields; keep evidence content."""
    out = {}
    for key, value in rec.items():
        if key.lower() in FORBIDDEN_KEYS:
            continue
        if isinstance(value, dict):
            out[key] = sanitize_record(value)
        elif isinstance(value, list):
            out[key] = [sanitize_record(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def build(expected_hashes=None, member_ids=None):
    data, hashes = load_pinned(expected_hashes or {})
    ids = member_ids or load_member_ids()
    canonical = {raw: CANONICALIZE.get(raw, raw) for raw in ids}

    ledger = json.loads(data[TMP + "/agent-reports/hub2-backlog-relevance.json"])
    ledger_by_id = {it["id"]: it for it in ledger["items"]}
    ws = json.loads(data[TMP + "/agent-reports/hub2-backlog-workstreams.json"])
    ws_titles = {w["local_key"]: w["title"] for w in ws["workstreams"]}

    recovery = parse_recovery_map(data[TMP + "/agent-reports/hub-intake-loss-recovery-2026-10-26.md"])
    lost = parse_lost_ids(data[TMP + "/intake-loss-2026-10-26/lost-ids.tsv"])
    rsv = {r["recreated"]: r for r in json.loads(
        data[TMP + "/intake-loss-2026-10-26/drain4-evidence/recreated-status-verification.json"])
        if isinstance(r.get("recreated"), str) and len(r["recreated"]) > 30}
    restored = {r["id"]: r for r in json.loads(
        data[TMP + "/agent-reports/hub-intake-loss-2026-11-03-restored-status.json"])["results"]
        if UUID_COL.match(r.get("id", ""))}
    reopen = {r["id"]: r for r in json.loads(
        data[TMP + "/agent-reports/hub-intake-loss-2026-11-03-reopen-progress-v2.json"])["results"]}

    bt = parse_backlog_triage(data[TMP + "/agent-reports/backlog-triage-20261029.md"])
    drain = {}
    for name in ("triaged-done", "triaged-actual", "triaged-duplicate", "triaged-obsolete"):
        for rid, row in parse_drain_tsv(
                data[TMP + "/agent-reports/drain/%s.tsv" % name]).items():
            drain.setdefault(rid, []).append((name, row))

    snaps = snapshot_items(data)

    members = []
    counts = {"keep": 0, "skip": 0, "reclassify": 0}
    for raw_id in ids:
        cid = canonical[raw_id]
        item = ledger_by_id.get(cid)
        if item is None:
            raise SystemExit("member %s missing from relevance ledger" % cid)

        # Snapshot view + richest history notes (prefer a record that carries
        # the recreation note; earliest snapshot wins as fallback).
        snap_view = {}
        notes = {"recreated_from": None, "original_final_status": None,
                 "data_sources": [], "triage_kinds": [], "never_close": False}
        for label, _path in SNAPSHOTS:
            rec = snaps[label].get(cid)
            if rec:
                snap_view[label] = {"status": rec.get("status"),
                                    "version": rec.get("version")}
                hn = history_notes(rec)
                if hn["recreated_from"] and not notes["recreated_from"]:
                    notes = hn
        # richest full-record notes from the snapshot that has the id with
        # recreation note; fall back to 10-29 (which contains all members).
        for label in ("2026-10-29", "2026-10-31", "2026-11-01"):
            rec = snaps[label].get(cid)
            if rec:
                hn = history_notes(rec)
                if hn["recreated_from"]:
                    notes = hn
                    break

        status_now = None
        for label, _p in SNAPSHOTS:
            if cid in snaps[label]:
                status_now = snaps[label][cid].get("status")
                break

        rec_row = recovery.get(cid)
        rsv_row = rsv.get(cid)
        restored_row = restored.get(cid)
        reopen_row = reopen.get(cid)
        is_recreated = bool(rec_row or rsv_row or notes["recreated_from"]
                            or "[recreated, orig" in item["title"])

        orig_ref = None
        if is_recreated:
            mo = re.search(r"\[recreated, orig ([0-9a-f-]+)\]", item["title"])
            orig_field = None
            for candidate in ((rec_row or {}).get("original"), notes["recreated_from"],
                              (mo.group(1) if mo else None),
                              (rsv_row or {}).get("original")):
                if candidate:
                    orig_field = candidate
                    break
            prefix_only = bool(orig_field and "(" in str(orig_field))
            orig_clean = re.sub(r" \(.*$", "", str(orig_field)) if orig_field else None
            evidence_paths = []
            if rec_row and rec_row.get("data_sources"):
                evidence_paths = rec_row["data_sources"]
            elif orig_clean:
                evidence_paths = lost.get(orig_clean[:8], [])
            orig_ref = {
                "id": orig_clean,
                "prefix_only": prefix_only,
                "present_in_snapshots": any(orig_clean in snaps[l] for l in snaps)
                if orig_clean else False,
                "live_404_confirmed_2026_10_26": True,
                "evidence_paths": evidence_paths,
            }

        original_status = None
        if is_recreated:
            original_status = best_original_status(rec_row, notes, reopen_row,
                                                   rsv_row, restored_row)

        bt_row = bt.get(cid[:8])
        drain_hits = drain.get(cid, [])
        owner = owner_project(bt_row, [r for _, r in drain_hits]) if is_recreated else None
        verdict, category, reason, lane = decide(
            bt_row, drain_hits, notes, restored_row, reopen_row, status_now,
            original_status, owner, item.get("class"), cid)
        counts[verdict] += 1

        members.append(sanitize_record({
            "brief_member_id": raw_id,
            "id": cid,
            "title": item["title"],
            "project": item["project"],
            "recreated_after_loss": is_recreated,
            "ledger": {
                "class": item.get("class"),
                "reason": item.get("reason"),
                "historical_status": item.get("historical_status"),
                "historical_version": item.get("historical_version"),
                "latest_record_source": item.get("latest_record_source"),
                "source_version_count": len(item.get("source_versions") or []),
                "ac_variant_count": len(item.get("ac_variants") or []),
            },
            "snapshots": snap_view,
            "original": orig_ref,
            "original_final_status": original_status,
            "loss_2026_10_26": ({"status": rsv_row.get("status"),
                                 "has_ac": rsv_row.get("has_ac")}
                                if rsv_row else None),
            "pre_loss_2026_11_03": ({"status": restored_row.get("status"),
                                     "archived_at": restored_row.get("archived_at"),
                                     "version": restored_row.get("version"),
                                     "marker_inline": restored_row.get("marker_inline")}
                                    if restored_row else None),
            "reopen_2026_11_03": reopen_row,
            "triage": {
                "backlog_2026_10_29": bt_row,
                "drain_rows": [{"list": k, "project": r.get("project"),
                                "evidence": r.get("evidence")}
                               for k, r in drain_hits],
                "history_note_kinds": notes["triage_kinds"],
                "never_close_note": notes["never_close"],
            },
            "verdict": verdict,
            "verdict_category": category,
            "reason": reason,
            "suggested_lane": lane,
        }))

    prefix_map = {}
    prefix_only = []
    for m in members:
        orig = m.get("original") or {}
        if orig.get("id"):
            prefix_map.setdefault(orig["id"][:8], []).append(m["id"])
        if orig.get("prefix_only"):
            prefix_only.append(m["id"])
    shared = {p: ids for p, ids in sorted(prefix_map.items()) if len(ids) > 1}

    return {
        "fixture_version": 1,
        "kind": "hub2-historical-migration-mapping",
        "additive_only": True,
        "workstream": {
            "parent": "historical",
            "title": ws_titles.get("historical"),
            "member_policy": "exact-once member ids from the brief; mapping preserved",
        },
        "inputs": [{"path": p, "sha256": h} for p, h in sorted(hashes.items())],
        "summary": {
            "members": len(members),
            "verdicts": counts,
            "recreated_after_loss": sum(1 for m in members if m["recreated_after_loss"]),
            "shared_original_prefixes": shared,
            "prefix_only_originals": prefix_only,
            "brief_id_repairs": [
                {"brief_id": k, "canonical_id": v,
                 "cause": "brief id is malformed; canonical record id substituted"}
                for k, v in CANONICALIZE.items()],
        },
        "verdict_legend": {
            "keep": "stays in Hub2 scope: current capability gap or unresolved evidence",
            "skip": "no Hub2 action: finished/superseded/duplicate/obsolete; evidence attached",
            "reclassify": "evidence belongs to another named lane/project",
        },
        "members": members,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=os.path.join(HERE, "fixtures"))
    args = ap.parse_args(argv)
    fixture = build()
    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, "hub2-hist-mapping-fixture.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("wrote %s: %d members, verdicts %s"
          % (out, len(fixture["members"]), fixture["summary"]["verdicts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
