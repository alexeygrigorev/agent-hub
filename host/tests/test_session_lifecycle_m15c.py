"""M15c live transport/CLI suite (synthetic only, no I/O, no effects).

Covers: strict timestamp offsets consistent with the TS parser, the
allowlisted observation payload builder (identity keys dropped), and the
`hub sessions` default grouped listing + `cleanup` dry-run + `observe`
transport mapping (dry-run performs no mutation; --apply refusal surfaces
from the server and is never rewritten into success here).
"""

import argparse
import unittest
from unittest.mock import patch

from agent_hub_host import session_control
from agent_hub_host import session_lifecycle as lifecycle


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="cmd", required=True)
    session_control.add_commands(sub)
    return root


def parse(*args):
    return parser().parse_args(["sessions", *args])


class TestTimestampOffsets(unittest.TestCase):
    def test_invalid_offsets_rejected_like_server(self):
        # Date.parse yields NaN (null) server-side for these; Python must agree.
        for bad in (
            "2026-10-17T14:00:00+99:99",
            "2026-10-17T14:00:00+24:00",
            "2026-10-17T14:00:00+02:60",
            "2026-10-17T14:00:00",
            "2026-10-21 13:16",
            "corrupt",
            None,
            "",
        ):
            self.assertIsNone(lifecycle._parse_ms(bad), msg=repr(bad))

    def test_valid_offsets_agree(self):
        z = lifecycle._parse_ms("2026-10-17T12:00:00Z")
        helsinki = lifecycle._parse_ms("2026-10-17T15:00:00+03:00")
        self.assertIsNotNone(z)
        self.assertAlmostEqual(z, helsinki, delta=1.0)
        # +23:59 is the widest offset V8 accepts; both sides agree.
        self.assertIsNotNone(lifecycle._parse_ms("2026-10-17T14:00:00+23:59"))


class TestObservationPayload(unittest.TestCase):
    def test_identity_keys_dropped_never_sent(self):
        payload = lifecycle.build_observation_payload(
            {"limit_hit": True, "owner": "intruder", "human_owned": True,
             "starter": "x", "machine_id": "h", "actor": "y", "engine": "z"}
        )
        self.assertEqual(payload, {"limit_hit": True})

    def test_unknown_and_mistyped_fields_fail_closed(self):
        with self.assertRaises(ValueError):
            lifecycle.build_observation_payload({"bogus_flag": True})
        with self.assertRaises(ValueError):
            lifecycle.build_observation_payload({"limit_hit": "yes"})
        with self.assertRaises(ValueError):
            lifecycle.build_observation_payload({"terminal_evidence": "vibes"})
        with self.assertRaises(ValueError):
            lifecycle.build_observation_payload("not-a-mapping")

    def test_note_opt_in_only(self):
        payload = lifecycle.build_observation_payload(
            {"limit_hit": True, "observation_note": "quota banner seen"}
        )
        self.assertEqual(payload["note_text_opt_in"], True)
        self.assertIn("quota banner seen", payload["observation_note"])
        with self.assertRaises(ValueError):
            lifecycle.build_observation_payload({"observation_note": "A" * 641})


class TestSessionsDefaultAndCleanup(unittest.TestCase):
    def test_bare_sessions_lists_grouped_lifecycle(self):
        with patch.object(session_control, "request", return_value={"groups": {}}) as transport:
            result = session_control.execute(parse())
        self.assertEqual(result, {"groups": {}})
        self.assertEqual(transport.call_args.args[:2], ("GET", "/api/sessions?view=lifecycle"))

    def test_cleanup_dry_run_posts_no_apply(self):
        with patch.object(session_control, "request", return_value={"dry_run": True}) as transport:
            result = session_control.execute(parse("cleanup"))
        self.assertEqual(result, {"dry_run": True})
        method, path, body = transport.call_args.args[:3]
        self.assertEqual(method, "POST")
        self.assertIn("/api/sessions/cleanup", path)
        self.assertNotIn("apply", body)
        self.assertTrue(body.get("dry_run"))

    def test_cleanup_apply_refusal_surfaces_unsweetened(self):
        refused = ValueError("Session HTTP request failed with status 409 (apply_not_available)")

        def fake(*args):
            raise refused

        with patch.object(session_control, "request", side_effect=fake) as transport:
            with self.assertRaises(ValueError) as ctx:
                session_control.execute(parse("cleanup", "--apply"))
        self.assertIn("apply_not_available", str(ctx.exception))
        self.assertEqual(transport.call_args.args[2], {"apply": True})

    def test_observe_builds_allowlisted_transport(self):
        with patch.object(session_control, "request", return_value={"session_key": "h/n"}) as transport:
            session_control.execute(parse(
                "observe", "--machine-id", "h", "--native-id", "n",
                "--limit-hit", "--limit-window", "5h", "--limit-reset", "2026-10-18T00:00:00Z",
            ))
        method, path, body = transport.call_args.args[:3]
        self.assertEqual((method, path), ("POST", "/api/sessions/observations"))
        self.assertEqual(body["machine_id"], "h")
        self.assertEqual(body["native_id"], "n")
        self.assertEqual(body["payload"]["limit_hit"], True)
        self.assertEqual(body["payload"]["limit_window"], "5h")
        self.assertEqual(body["payload"]["limit_reset_at"], "2026-10-18T00:00:00Z")
        self.assertNotIn("owner", body["payload"])
        self.assertNotIn("human_owned", body["payload"])

    def test_existing_list_start_send_capture_stop_preserved(self):
        with patch.object(session_control, "request", return_value={"sessions": []}) as transport:
            session_control.execute(parse("list", "--limit", "10"))
        self.assertEqual(transport.call_args.args[:2], ("GET", "/api/sessions?limit=10"))


if __name__ == "__main__":
    unittest.main()
