"""Phase 1 work-claim bridge tests: validation, redaction, native scans.

Mirrors the TypeScript vectors in test/work-claim.test.ts so both
implementations of design section 7.10 stay in lockstep.
"""

import io
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

from agent_hub_host import workclaim as wc


VALID_DATA = {
    "schema": "hub.work-claim.v1",
    "event": "claim",
    "claim_id": "clm_01K8AN7D4XE9P0T2V5WYH6JM",
    "seq": 1,
    "work_key": "task:7142fe0b-d45c-4a3a-b92b-726ecc10575a",
    "project": "agent-hub",
    "workspace": "/home/alexey/git/agent-hub",
    "mode": "edit",
    "scopes": ["src/hub.ts"],
    "holder": {"machine_id": "rmthz", "session_id": "sess-1", "tag": "fixer"},
    "lease_ttl_sec": 1800,
    "summary": "Claiming",
}


def mutate(**over):
    data = dict(VALID_DATA)
    data.update(over)
    return data


class ValidationTests(unittest.TestCase):
    def test_valid_minimal(self):
        self.assertIsNone(wc.validate_work_claim_data(dict(VALID_DATA)))

    def test_violations(self):
        cases = [
            (mutate(schema="hub.other.v9"), "schema must be hub.work-claim.v1"),
            (mutate(event="start"), "event invalid"),
            (mutate(claim_id="c67d381d-uuid"), "claim_id invalid"),
            (mutate(claim_id="clm_01J8Z9DEIIGN7142"), "claim_id invalid"),
            (mutate(seq=0), "seq invalid"),
            (mutate(work_key="TASK:upper"), "work_key invalid"),
            (mutate(alias_keys=["bogus"]), "alias_keys entry invalid"),
            (mutate(project=""), "project invalid"),
            (mutate(mode="write"), "mode invalid"),
            (mutate(scopes=["../escape"]), "scope entry not workspace-relative"),
            (mutate(holder="native:x"), "holder must be an object"),
            (mutate(holder={**VALID_DATA["holder"], "token": "hubt_x"}),
             "holder has forbidden field token"),
            (mutate(lineage={"stolen_reason": "nope"}), "lineage.stolen_reason invalid"),
            (mutate(lease_ttl_sec=30), "lease_ttl_sec invalid"),
            (mutate(outcome={"fix_commit_sha": "ZZZ"}), "outcome.fix_commit_sha invalid"),
            (mutate(summary="x" * 281), "summary invalid"),
            (mutate(raw_cli_args="--token hubt_x"), "forbidden field raw_cli_args"),
        ]
        for data, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(wc.validate_work_claim_data(data), reason)


class TaskStringTests(unittest.TestCase):
    def test_convention(self):
        parsed = wc.parse_work_task_string(
            "task:d8d475bb-1b51-44e6-afc1-81faf633a4f4 [claim=clm_01K8AN7D4XE9P0T2V5WYH6JM] [ttl=1800s] | Surface messaging")
        self.assertEqual(parsed["work_key"], "task:d8d475bb-1b51-44e6-afc1-81faf633a4f4")
        self.assertEqual(parsed["claim_id"], "clm_01K8AN7D4XE9P0T2V5WYH6JM")
        self.assertEqual(parsed["ttl_sec"], 1800)
        self.assertEqual(parsed["summary"], "Surface messaging")

    def test_free_text(self):
        parsed = wc.parse_work_task_string("Urgent owned host recovery")
        self.assertIsNone(parsed["work_key"])
        self.assertIsNone(parsed["claim_id"])
        self.assertEqual(parsed["summary"], "")

    def test_synthesized_ids_are_stable_and_valid(self):
        first = wc.synthesized_claim_id("sid", "/ws", "task:abc")
        second = wc.synthesized_claim_id("sid", "/ws", "task:abc")
        self.assertEqual(first, second)
        self.assertRegex(first, r"\Aclm_[0-9A-HJKMNP-TV-Z]{10,26}\Z")
        self.assertNotEqual(first, wc.synthesized_claim_id("other", "/ws", "task:abc"))


class RedactionTests(unittest.TestCase):
    def test_vectors_match_typescript_suite(self):
        self.assertEqual(wc.redact_for_bus("token=hubt_secret123 body"), "token=[REDACTED] body")
        self.assertEqual(wc.redact_for_bus("key ghp_abcdefghijklmnopqrstuvwxyz012345 end"),
                         "key [REDACTED_TOKEN] end")
        self.assertEqual(wc.redact_for_bus("Authorization: Bearer abcdef0123456789 rest"),
                         "Authorization: Bearer [REDACTED] rest")
        self.assertEqual(wc.redact_for_bus("AKIAIOSFODNN7EXAMPLE here"), "[REDACTED_AWS_KEY] here")
        self.assertEqual(
            wc.redact_for_bus("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"),
            "[REDACTED_PRIVATE_KEY]")
        self.assertEqual(
            wc.redact_for_bus("see https://user:pass@example.com/hook?token=abc123&x=1 done"),
            "see https://example.com/hook?token=[REDACTED]&x=1 done")
        self.assertEqual(wc.redact_for_bus("blob " + "a" * 48 + " tail"), "blob [REDACTED_HIGH_ENTROPY] tail")
        # Non-secrets survive: paths, task keys, UUIDs, short SHAs, counts.
        self.assertEqual(
            wc.redact_for_bus("COMPLETE task:7142fe0b report=/home/alexey/tmp/agent-reports/hub-work-claim-protocol.md"),
            "COMPLETE task:7142fe0b report=/home/alexey/tmp/agent-reports/hub-work-claim-protocol.md")
        self.assertEqual(
            wc.redact_for_bus("session b3c55563-5238-43b2-a02c-d8fdbc55d1e7 c87719b 280"),
            "session b3c55563-5238-43b2-a02c-d8fdbc55d1e7 c87719b 280")
        self.assertEqual(wc.redact_for_bus("sha " + "a" * 40 + " kept"), "sha " + "a" * 40 + " kept")
        self.assertEqual(wc.redact_for_bus(42), 42)

    def test_deep_redact(self):
        clean = wc.redact_claim_data({"summary": "x token=hubt_abc", "holder": {"tag": "t"}, "n": 3})
        self.assertEqual(clean["summary"], "x token=[REDACTED]")
        self.assertEqual(clean["holder"]["tag"], "t")
        self.assertEqual(clean["n"], 3)


class ScanTests(unittest.TestCase):
    def _session_dir(self, root, session_id, tag, declarations):
        entry = root / "sessions" / session_id
        entry.mkdir(parents=True)
        (entry / "session.json").write_text(json.dumps(
            {"id": session_id, "tag": tag, "engine": "opencode", "workspace": "/repo-wt"}))
        (entry / "coordination.json").write_text(json.dumps(
            {"session_id": session_id, "declarations": declarations}))

    def test_scan_declarations(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = __import__("pathlib").Path(tmp)
            self._session_dir(root, "sess-1", "fixer", [{
                "workspace": "/repo", "mode": "edit", "scopes": ["src"],
                "task": "task:7142fe0b-d45c-4a3a-b92b-726ecc10575a [ttl=900s] | Fix",
                "updated_at_ms": 1700000000000, "created_at_ms": 1700000000000,
            }])
            rows = {"sess-1": {"id": "sess-1", "phase": "Running", "worker_alive": True}}
            decls = wc.scan_declarations(str(root), rows, "rmthz")
            self.assertEqual(len(decls), 1)
            decl = decls[0]
            self.assertEqual(decl["work_key"], "task:7142fe0b-d45c-4a3a-b92b-726ecc10575a")
            self.assertEqual(decl["ttl_sec"], 900)
            self.assertEqual(decl["session_state"], "running")
            self.assertEqual(decl["tag"], "fixer")

    def test_exited_session_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = __import__("pathlib").Path(tmp)
            self._session_dir(root, "sess-9", "ghost", [{
                "workspace": "/repo", "mode": "edit", "scopes": ["src"],
                "task": "task:aaaa | Old", "updated_at_ms": 1700000000000,
            }])
            rows = {"sess-9": {"id": "sess-9", "phase": "Exited"}}
            decls = wc.scan_declarations(str(root), rows, "rmthz")
            self.assertEqual(decls[0]["session_state"], "exited")

    def test_scan_work_messages_only_canonical(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = __import__("pathlib").Path(tmp)
            canonical = {"/repo": "/repo"}
            for ws, name in (("/repo", "k1"), ("/elsewhere", "k2")):
                box = root / "messages" / name
                (box / "msgs").mkdir(parents=True)
                (box / "workspace.json").write_text(json.dumps({"workspace": ws}))
                (box / "msgs" / "01a15cc1-71d4-7d43-80cb-6642f9b52718.json").write_text(json.dumps({
                    "id": "01a15cc1-71d4-7d43-80cb-6642f9b52718", "kind": "work.claim",
                    "body": "CLAIM", "from": {"tag": "t"}, "created_at": 1700000000,
                }))
                (box / "msgs" / "01a15cc2-71d4-7d43-80cb-6642f9b52718.json").write_text(json.dumps({
                    "id": "01a15cc2-71d4-7d43-80cb-6642f9b52718", "kind": "note",
                    "body": "chatter", "from": {"tag": "t"},
                }))
            messages, cursors, stats = wc.scan_work_messages(str(root), canonical, {})
            self.assertEqual(stats["mailboxes"], 1)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["kind"], "work.claim")
            # Cursor advances: a second scan with the cursor finds nothing new.
            messages2, _, _ = wc.scan_work_messages(str(root), canonical, cursors)
            self.assertEqual(messages2, [])

    def test_build_claim_items_derive_path_keys(self):
        now_ms = int(time.time() * 1000)
        decls = [{
            "session_id": "s", "tag": "t", "engine": "e", "machine_id": "m",
            "session_state": "running", "mode": "read", "scopes": [],
            "task": "free text triage", "work_key": None, "claim_id": None,
            "ttl_sec": 1800, "summary": "", "workspace": "/repo",
            "worktree": "/repo", "updated_at_ms": now_ms,
        }]
        items, stats = wc.build_claim_items(decls, {"/repo": "/repo"}, "m", now_ms)
        self.assertEqual(stats["invalid"], 0)
        self.assertEqual(items[0]["data"]["work_key"], "path:repo:all")
        self.assertGreater(items[0]["remaining_ttl_ms"], 0)

    def test_build_message_items_mirror_and_validate(self):
        envelopes = [{
            "native_id": "01a15cc1-71d4-7d43-80cb-6642f9b52718", "kind": "work.complete",
            "body": "COMPLETE task:abc token=hubt_x", "data": None,
            "from": {"tag": "fixer", "session_id": "s"}, "mailbox_workspace": "/repo",
            "canonical_workspace": "/repo", "created_at_ms": None,
        }]
        items, stats = wc.build_message_items(envelopes, "m")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["project"], "repo")
        self.assertEqual(items[0]["body"], "COMPLETE task:abc token=[REDACTED]")


class SyncOnceTests(unittest.TestCase):
    def test_sync_posts_and_never_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = __import__("pathlib").Path(tmp)
            entry = root / "sessions" / "sess-1"
            entry.mkdir(parents=True)
            (entry / "session.json").write_text(json.dumps({"id": "sess-1", "tag": "fixer"}))
            (entry / "coordination.json").write_text(json.dumps({"session_id": "sess-1", "declarations": [{
                "workspace": "/repo", "mode": "edit", "scopes": ["src"],
                "task": "task:7142fe0b-d45c-4a3a-b92b-726ecc10575a | Fix", "updated_at_ms": int(time.time() * 1000),
            }]}))
            posted = {}

            def fake_request(method, path, payload=None):
                posted["method"] = method
                posted["path"] = path
                posted["payload"] = payload
                return {"status": "ok", "synced_claims": 1, "synced_messages": 0}

            settings = {"workspaces": {"repo": "/repo"}, "machine_id": "m",
                        "journal_dir": os.path.join(tmp, "journal")}
            summary = wc.sync_once(fake_request, settings=settings,
                                   runner=lambda argv: {"sessions": [{"id": "sess-1", "phase": "Running"}]},
                                   state_dir=str(root))
            self.assertTrue(summary["ok"])
            self.assertEqual(posted["path"], "/api/bus/native-sync")
            claims = posted["payload"]["claims"]
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["data"]["holder"]["machine_id"], "m")

    def test_sync_failure_returns_error_summary(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = __import__("pathlib").Path(tmp)
            entry = root / "sessions" / "sess-1"
            entry.mkdir(parents=True)
            (entry / "session.json").write_text(json.dumps({"id": "sess-1", "tag": "fixer"}))
            (entry / "coordination.json").write_text(json.dumps({"session_id": "sess-1", "declarations": [{
                "workspace": "/repo", "mode": "edit", "scopes": ["src"],
                "task": "task:7142fe0b-d45c-4a3a-b92b-726ecc10575a | Fix", "updated_at_ms": int(time.time() * 1000),
            }]}))

            def boom(method, path, payload=None):
                raise ValueError("hub down")

            settings = {"workspaces": {"repo": "/repo"}, "machine_id": "m"}
            summary = wc.sync_once(boom, settings=settings,
                                   runner=lambda argv: {"sessions": [{"id": "sess-1", "phase": "Running"}]},
                                   state_dir=str(root))
            self.assertFalse(summary["ok"])
            self.assertIn("error", summary)

    def test_empty_state_posts_nothing_and_stays_ok(self):
        posted = []

        def fake_request(method, path, payload=None):
            posted.append(path)
            return {"status": "ok"}

        settings = {"workspaces": {}, "machine_id": "m"}
        summary = wc.sync_once(fake_request, settings=settings,
                               runner=lambda argv: {"sessions": []}, state_dir="/nonexistent")
        self.assertTrue(summary["ok"])
        self.assertEqual(posted, [])


class CliWiringTests(unittest.TestCase):
    def test_workclaim_sync_dry_run_no_network(self):
        import tempfile
        from agent_hub_host import cli
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["cli.py", "workclaim-sync", "--dry-run", "--state-dir", tmp]
            with patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.main()
            data = json.loads(out.getvalue())
            self.assertTrue(data["dry_run"])
            self.assertIn("claims", data)
            self.assertIn("messages", data)

    def test_bus_native_sync_posts_bounded_batch(self):
        from agent_hub_host import bus
        with patch("agent_hub_host.bus._do_request") as mock_req:
            mock_req.return_value = {"status": "ok"}
            res = bus.native_sync([{"data": dict(VALID_DATA)}], [])
            self.assertEqual(res, {"status": "ok"})
            method, path, payload = mock_req.call_args[0]
            self.assertEqual((method, path), ("POST", "/api/bus/native-sync"))
            self.assertEqual(len(payload["claims"]), 1)
        with self.assertRaises(ValueError):
            bus.native_sync([{}] * 65, [])


if __name__ == "__main__":
    unittest.main()
