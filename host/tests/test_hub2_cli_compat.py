"""Hub2 CLI compatibility: keyset `--all` walk, session-credential discovery,
inbox wait/delegation, and Hub2 error-slug exit codes (intakes 08658ee8,
166af2de, 3bdf9715)."""
import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import intake, msg_cli, private_fs  # noqa: E402


def parse(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    intake.add_commands(sub)
    msg_cli.add_commands(sub)
    return parser.parse_args(argv)


class KeysetWalkTest(unittest.TestCase):
    """`hub intake list --all` against a Hub2 keyset listing (08658ee8)."""

    def _walk(self, pages, params=None):
        calls = []

        def fake_request(method, path, payload=None):
            calls.append(path)
            query = path.split("?", 1)[1] if "?" in path else ""
            after = ""
            for part in query.split("&"):
                if part.startswith("after="):
                    from urllib.parse import unquote_plus
                    after = unquote_plus(part[6:])
            page = pages.get(after)
            if page is None:
                raise AssertionError("unexpected page request after=%r" % after)
            return page

        with mock.patch.object(intake, "request", fake_request):
            result = intake._keyset_all_once(params or {}, pages[""])
        return result, calls

    def test_walks_all_keyset_pages_and_reports_total(self):
        pages = {
            "": {"items": [{"id": "b"}, {"id": "a"}], "next": json.dumps([1, "a"])},
            json.dumps([1, "a"]): {"items": [{"id": "c"}], "next": None},
        }
        result, calls = self._walk(pages)
        self.assertEqual([t["id"] for t in result["items"]], ["b", "a", "c"])
        self.assertEqual(result["total"], 3)
        self.assertTrue(result["keyset_walk"])
        # The first page is passed in; only the continuation is fetched.
        self.assertEqual(len(calls), 1)

    def test_repeated_cursor_refuses_instead_of_looping(self):
        pages = {
            "": {"items": [{"id": "b"}], "next": json.dumps([1, "a"])},
            json.dumps([1, "a"]): {"items": [], "next": json.dumps([1, "a"])},
        }
        with self.assertRaises(intake.IntakeInputError):
            self._walk(pages)

    def test_list_all_selects_hub2_walk_without_snapshot_fields(self):
        first = {"items": [{"id": "x"}], "next": None}

        def fake_page(page_params, params):
            return page_params

        with mock.patch.object(intake, "_keyset_all_once", return_value={"items": [], "total": 0}) as walk, \
                mock.patch.object(intake, "_request_page", return_value=first):
            intake._list_all_once({"limit": 100})
            walk.assert_called_once()


class SessionCredentialTest(unittest.TestCase):
    """The session's own provisioned credential drives intake commands (166af2de)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "ws"
        self.native = "44444444-4444-4444-8444-444444444444"
        for key in ("HUB_CONFIG_FILE", "APLEXER_SESSION_ID", "APLEXER_WORKSPACE"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key in ("HUB_CONFIG_FILE", "APLEXER_SESSION_ID", "APLEXER_WORKSPACE"):
            os.environ.pop(key, None)

    def _provision(self):
        credential = {"base_url": "https://hub.example", "actor": "session:" + self.native,
                      "access_token": "t_secret", "refresh_token": "r_secret"}
        self.workspace.mkdir(mode=0o700, exist_ok=True)
        directory = msg_cli.session_auth_dir(str(self.workspace))
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(directory.parent, 0o700)
        private_fs.atomic_write(str(directory / (self.native + ".json")),
                                json.dumps(credential).encode(), msg_cli.CREDENTIAL_LIMIT)

    def test_applies_only_when_session_env_and_file_present(self):
        self.assertIsNone(msg_cli.apply_session_credential())
        os.environ["APLEXER_SESSION_ID"] = self.native
        os.environ["APLEXER_WORKSPACE"] = str(self.workspace)
        self.assertIsNone(msg_cli.apply_session_credential())
        self._provision()
        applied = msg_cli.apply_session_credential()
        self.assertEqual(applied, str(msg_cli.session_auth_dir(str(self.workspace)) / (self.native + ".json")))
        self.assertEqual(os.environ["HUB_CONFIG_FILE"], applied)
        # An operator-selected config always wins.
        os.environ["HUB_CONFIG_FILE"] = "/elsewhere/cli.json"
        self.assertIsNone(msg_cli.apply_session_credential())
        self.assertEqual(os.environ["HUB_CONFIG_FILE"], "/elsewhere/cli.json")

    def test_resolve_identity_reports_the_session_actor(self):
        os.environ["APLEXER_SESSION_ID"] = self.native
        os.environ["APLEXER_WORKSPACE"] = str(self.workspace)
        self._provision()
        actor, source = msg_cli.resolve_identity(None)
        self.assertEqual(actor, "session:" + self.native)
        self.assertEqual(source, "session")


class InboxWaitTest(unittest.TestCase):
    """`hub inbox --wait` is a bounded client-side poll; delegated agent inboxes act `as` (3bdf9715)."""

    def setUp(self):
        self.calls = []

    def _inbox(self, responses, wait=0, actor=None, profile_actor=None):
        sequence = iter(responses)

        def fake_request(method, path, payload=None):
            self.calls.append((method, path))
            if next(sequence, "done") != "hit":
                return {"items": [], "messages": []}
            return {"items": [{"id": "m1", "sender": "s", "recipient": "r"}], "messages": [{"id": "m1"}]}

        args = argparse.Namespace(actor=actor, unread=False, thread=None, cursor=None,
                                  limit=None, wait=wait, mark_read=False)
        with mock.patch.object(msg_cli, "_request", fake_request), \
                mock.patch.object(msg_cli.time, "sleep", lambda s: None):
            return msg_cli.inbox(args, profile_actor)

    def test_wait_polls_until_messages_arrive(self):
        result = self._inbox(responses=["empty", "empty", "hit"], wait=30, profile_actor="human:dev")
        self.assertEqual([m["id"] for m in result["messages"]], ["m1"])
        self.assertEqual(len(self.calls), 3)

    def test_no_wait_is_a_single_request(self):
        result = self._inbox(responses=["empty"], wait=0, profile_actor="human:dev")
        self.assertEqual(result["messages"], [])
        self.assertEqual(len(self.calls), 1)

    def test_delegated_agent_actor_passes_as(self):
        self._inbox(responses=["hit"], actor="agent:developer", profile_actor="human:dev")
        method, path = self.calls[0]
        self.assertIn("actor=agent%3Adeveloper", path)
        self.assertIn("as=agent%3Adeveloper", path)

    def test_limit_clamps_to_hub2_page_bound(self):
        args = argparse.Namespace(actor="human:dev", unread=False, thread=None, cursor=None,
                                  limit=100, wait=0, mark_read=False)
        with mock.patch.object(msg_cli, "_request", return_value={"items": [], "messages": []}) as req, \
                mock.patch.object(msg_cli.time, "sleep", lambda s: None):
            msg_cli.inbox(args, "human:dev")
        self.assertIn("limit=50", req.call_args[0][1])


class ExitCodeTest(unittest.TestCase):
    """Hub2 slugs map to the same §11 exit codes as the old-hub slugs."""

    def test_hub2_recipient_and_ambiguity_slugs_exit_three(self):
        class FakeError(Exception):
            pass

        self.assertEqual(msg_cli.exit_code_for(FakeError("HTTP request failed with status 404: recipient_unknown")), 3)
        self.assertEqual(msg_cli.exit_code_for(FakeError("HTTP request failed with status 409: ambiguous_prefix")), 3)
        self.assertEqual(msg_cli.exit_code_for(FakeError("HTTP request failed with status 403: cross_project_forbidden")), 2)
        self.assertEqual(msg_cli.exit_code_for(FakeError("HTTP request failed with status 503: native_delivery_authority_unavailable")), 4)


if __name__ == "__main__":
    unittest.main()
