"""Characterization tests for the Hub2 historical reconciliation fixture
(canary 5, workstream `historical`; scripts/hub2-hist/).

Two layers, both self-contained (no /home/alexey/tmp evidence dependency):

1. Fixture invariants: the committed hub2-hist-mapping-fixture.json is the
   additive migration-mapping artifact the Hub2 coordinator imports. These
   tests pin its shape so any rebuild that breaks the contract fails loudly.
2. Builder decision logic: synthetic evidence pinning the verdict precedence
   (KEEP -> superseded/duplicate -> external owner -> finished -> unresolved),
   the sanitization of credential-bearing fields, and exact-once member ids.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIXTURE = os.path.join(REPO, "scripts", "hub2-hist", "fixtures",
                       "hub2-hist-mapping-fixture.json")
sys.path.insert(0, os.path.join(REPO, "scripts", "hub2-hist"))
import build_hist_fixture as builder  # noqa: E402


@pytest.fixture(scope="module")
def fixture():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- fixture ---

def test_fixture_exists_and_is_additive_fixture(fixture):
    assert fixture["kind"] == "hub2-historical-migration-mapping"
    assert fixture["additive_only"] is True
    assert fixture["fixture_version"] >= 1


def test_exact_once_member_ids(fixture):
    members = fixture["members"]
    assert len(members) == 160
    brief_ids = [m["brief_member_id"] for m in members]
    ids = [m["id"] for m in members]
    assert len(set(brief_ids)) == 160
    assert len(set(ids)) == 160
    # brief ids are canonical verbatim; brief_member_id == id for every member
    assert brief_ids == ids


def test_member_ids_match_brief_file(fixture):
    with open(os.path.join(REPO, "scripts", "hub2-hist", "brief-member-ids.txt"),
              "r", encoding="utf-8") as f:
        brief_ids = [line.strip() for line in f if line.strip()]
    assert sorted(brief_ids) == sorted(m["id"] for m in fixture["members"])


def test_every_member_has_verdict_and_reason(fixture):
    for m in fixture["members"]:
        assert m["verdict"] in builder.VERDICTS, m["id"]
        assert m["verdict_category"], m["id"]
        assert isinstance(m["reason"], str) and len(m["reason"]) > 10, m["id"]


def test_verdict_counts_match_summary(fixture):
    counts = {"keep": 0, "skip": 0, "reclassify": 0}
    for m in fixture["members"]:
        counts[m["verdict"]] += 1
    assert counts == fixture["summary"]["verdicts"]
    assert sum(counts.values()) == fixture["summary"]["members"] == 160


def test_inputs_are_hash_pinned(fixture):
    inputs = fixture["inputs"]
    assert len(inputs) == len(builder.INPUTS)
    for entry in inputs:
        assert set(entry) == {"path", "sha256"}
        assert len(entry["sha256"]) == 64
    assert any(e["path"] == "/home/alexey/tmp/agent-reports/backlog-triage-20261029.md"
               for e in inputs)


def test_no_credential_fields_in_fixture(fixture):
    forbidden = {k.lower() for k in builder.FORBIDDEN_KEYS}

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in forbidden, key
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(fixture)


def test_recreations_carry_mapping_layers(fixture):
    recreations = [m for m in fixture["members"] if m["recreated_after_loss"]]
    assert fixture["summary"]["recreated_after_loss"] == len(recreations) == 154
    for m in recreations:
        assert m["original"], m["id"]
        assert m["original"]["id"], m["id"]
        assert m["original"]["live_404_confirmed_2026_10_26"] is True
        assert m["original"]["present_in_snapshots"] is False
        assert m["pre_loss_2026_11_03"], m["id"]
        assert m["reopen_2026_11_03"], m["id"]
        assert m["original"]["evidence_paths"], m["id"]


def test_keep_members_are_the_capability_gaps(fixture):
    keeps = [m for m in fixture["members"] if m["verdict"] == "keep"]
    gaps = [m for m in keeps if m["verdict_category"] == "current-capability-gap"]
    unresolved = [m for m in keeps if m["verdict_category"] == "unresolved-evidence"]
    assert len(gaps) == 6
    for m in gaps:
        assert m["ledger"]["class"] == "KEEP needed in Hub2", m["id"]
        assert not m["recreated_after_loss"], m["id"]
        assert m["suggested_lane"] in builder.HUB2_LANES, m["id"]
    assert unresolved == [], "unexpected unresolved members; re-examine builder"


def test_reclassify_lanes_are_external(fixture):
    for m in fixture["members"]:
        if m["verdict"] == "reclassify":
            lane = m["suggested_lane"]
            assert lane and lane.startswith("external:"), m["id"]
            assert lane.split(":", 1)[1] in builder.EXTERNAL_PROJECTS, m["id"]
        else:
            assert not (m["suggested_lane"] or "").startswith("external:")


def test_skip_reasons_cite_sources(fixture):
    for m in fixture["members"]:
        if m["verdict"] == "skip":
            t = m["triage"]
            pre = (m["pre_loss_2026_11_03"] or {}).get("status")
            snap = next((v.get("status") for v in
                         reversed(list(m["snapshots"].values()))
                         if v.get("status")), None)
            assert (t["drain_rows"] or t["backlog_2026_10_29"]
                    or t["history_note_kinds"]
                    or (m["pre_loss_2026_11_03"] or {}).get("marker_inline")
                    or pre in ("done", "cancelled")
                    or snap in ("done", "cancelled")
                    or m["original_final_status"] in ("done", "cancelled")), m["id"]


def test_shared_original_prefixes_recorded(fixture):
    shared = fixture["summary"]["shared_original_prefixes"]
    assert shared["3f9cbb65"] == [
        "0c02db0a-e2f1-4b13-b97f-13cb1fdce0d4",
        "1a721cfd-174f-4b86-8abb-8a0a4eb3c3ec",
    ]
    for prefix, ids in shared.items():
        assert len(ids) > 1


def test_prefix_only_original_flagged(fixture):
    assert fixture["summary"]["prefix_only_originals"] == [
        "c0af9bc8-b296-43af-b9fa-2b045c797b1c"]
    member = next(m for m in fixture["members"]
                  if m["id"] == "c0af9bc8-b296-43af-b9fa-2b045c797b1c")
    assert member["original"]["prefix_only"] is True
    assert member["original"]["id"] == "4cd05ea7"


# ---------------------------------------------------------------- builder ---

def _recreation(ledger_class="NEEDS DECISION"):
    return {
        "id": "aaaa1111-0000-4000-8000-000000000001",
        "title": "[recreated, orig bbbb2222] stub",
        "class": ledger_class,
        "reason": "r",
        "historical_status": "open",
        "project": "agent-hub",
    }


def test_decide_keep_for_ledger_keep_class():
    verdict, category, reason, lane = builder.decide(
        bt_row=None, drain_hits=[], notes={}, restored_row=None, reopen_row=None,
        snapshot_status_now="open", original_status="unknown", owner=None,
        ledger_class="KEEP needed in Hub2",
        canonical_id="955e9381-c06a-405f-a29a-74ce9cf2b498")
    assert (verdict, category) == ("keep", "current-capability-gap")
    assert lane == "records"


def test_decide_superseded_before_owner_and_finished():
    bt = {"class": "SUPERSEDED", "project": "agent-hub",
          "evidence": "e", "next": "work concerns dataops"}
    verdict, _, reason, lane = builder.decide(
        bt_row=bt, drain_hits=[], notes={}, restored_row=None, reopen_row=None,
        snapshot_status_now="done", original_status="done", owner="dataops",
        ledger_class="NEEDS DECISION", canonical_id="x")
    assert verdict == "skip"
    assert lane is None
    assert "duplicate/obsolete/superseded" in reason


def test_decide_owner_reclassifies_before_finished():
    bt = {"class": "DONE_ON_MAIN", "project": "agent-hub",
          "evidence": "fix on their main", "next": "close; work concerns dtc-website"}
    verdict, category, reason, lane = builder.decide(
        bt_row=bt, drain_hits=[], notes={}, restored_row=None, reopen_row=None,
        snapshot_status_now="open", original_status="unknown", owner="dtc-website",
        ledger_class="NEEDS DECISION", canonical_id="x")
    assert (verdict, category, lane) == ("reclassify", "external-owner-project",
                                         "external:dtc-website")
    assert "finished" in reason


def test_decide_owner_from_drain_project_column():
    verdict, _, _, lane = builder.decide(
        bt_row=None,
        drain_hits=[("triaged-done", {"project": "machine-management", "evidence": "x"})],
        notes={}, restored_row=None, reopen_row=None, snapshot_status_now="done",
        original_status="unknown", owner="machine-management",
        ledger_class="NEEDS DECISION", canonical_id="x")
    assert verdict == "reclassify"
    assert lane == "external:machine-management"


def test_decide_finished_report_only_vs_repo_evidence():
    common = dict(bt_row=None, restored_row=None, reopen_row=None,
                  owner=None, ledger_class="NEEDS DECISION", canonical_id="x")
    # repo-level row present -> verified category
    v, cat, _, _ = builder.decide(
        drain_hits=[("triaged-done", {"project": "agent-hub", "evidence": "e"})],
        notes={}, snapshot_status_now="done", original_status="unknown", **common)
    assert (v, cat) == ("skip", "finished-with-repo-evidence")
    # only the pre-loss done status -> report-only, honestly labelled
    v, cat, reason, _ = builder.decide(
        drain_hits=[], notes={}, snapshot_status_now="open",
        original_status="unknown",
        restored_row={"status": "done", "marker_inline": False},
        **{k: v for k, v in common.items() if k != "restored_row"})
    assert (v, cat) == ("skip", "finished-report-only")
    assert "pre-2026-11-03-loss status done" in reason


def test_decide_unresolved_residue_keeps_in_historical():
    verdict, category, _, lane = builder.decide(
        bt_row=None, drain_hits=[], notes={}, restored_row=None, reopen_row=None,
        snapshot_status_now="open", original_status="unknown", owner=None,
        ledger_class="NEEDS DECISION", canonical_id="x")
    assert (verdict, category, lane) == ("keep", "unresolved-evidence", "historical")


def test_sanitize_record_drops_credential_fields():
    rec = {"id": "x", "actor": "human:admin-token:deadbeef", "token": "t",
           "nested": {"authorization": "Bearer x", "keep": 1},
           "list": [{"credential": "c", "ok": 2}]}
    clean = builder.sanitize_record(rec)
    assert "actor" not in clean and "token" not in clean
    assert clean["nested"] == {"keep": 1}
    assert clean["list"] == [{"ok": 2}]
    assert clean["id"] == "x"


def test_history_notes_extracts_recreation_facts():
    record = {"payload_data": {"history": [
        {"text": "recreated after loss, original id bbbb2222-3333-4ccc-8ddd-"
                 "eeeeeeeeeeee.\nOriginal final state (recovered): done.\n"
                 "Data sources:\n  - /tmp/report.md\nRecovery intake 955e9381. "
                 "Never close this intake; recovery status lives on 955e9381."},
        {"text": "TRIAGE DONE: analysis-only deliverable"},
    ]}}
    notes = builder.history_notes(record)
    assert notes["recreated_from"] == ("bbbb2222-3333-4ccc-8ddd-eeeeeeeeeeee")
    assert notes["original_final_status"] == "done"
    assert notes["data_sources"] == ["/tmp/report.md"]
    assert notes["never_close"] is True
    assert notes["triage_kinds"] == ["DONE"]


def test_recovery_map_and_lost_ids_parsers():
    rec = builder.parse_recovery_map(
        "| bbbb2222 (prefix only; full original id unrecoverable) "
        "| aaaa1111-0000-4000-8000-000000000001 "
        "| /tmp/a.md; /tmp/b.md | done |")
    assert rec["aaaa1111-0000-4000-8000-000000000001"] == {
        "original": "bbbb2222 (prefix only; full original id unrecoverable)",
        "data_sources": ["/tmp/a.md", "/tmp/b.md"],
        "original_final_status": "done",
    }
    lost = builder.parse_lost_ids("bbbb2222\t/tmp/a.md; /tmp/b.md\n")
    assert lost == {"bbbb2222": ["/tmp/a.md", "/tmp/b.md"]}


def test_load_member_ids_is_exact_once_and_canonical(tmp_path):
    ids = ["aaaa1111-0000-4000-8000-00000000000%d" % i for i in (1, 2)]
    p = tmp_path / "ids.txt"
    p.write_text("\n".join(ids) + "\n")
    assert builder.load_member_ids(str(p), expected_count=2) == ids
    dup = tmp_path / "dup.txt"
    dup.write_text(ids[0] + "\n" + ids[0] + "\n")
    with pytest.raises(SystemExit):
        builder.load_member_ids(str(dup), expected_count=2)
    malformed = tmp_path / "bad.txt"
    malformed.write_text("3aceeec0-7ceb-417c-bdc-8d0533d9dc74\n")
    with pytest.raises(SystemExit):
        builder.load_member_ids(str(malformed), expected_count=1)


def test_bundled_member_list_loads_clean():
    ids = builder.load_member_ids()
    assert len(ids) == 160
    assert builder.load_member_ids(expected_count=160) == ids
