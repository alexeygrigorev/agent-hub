"""`hub msg` on the Hub2 mailbox (/api/messages): identity, human:me inbox posting (t3280u), loud Hub1-only refusals."""
import argparse
import json
import os
import stat
import sys
import tempfile
import unittest
from urllib.error import URLError
from urllib.parse import unquote
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import cli, msg_cli, session_messages  # noqa: E402

NATIVE = "33333333-3333-4333-8333-333333333333"
HUB_SESSION = "22222222-2222-4222-8222-222222222222"


def parse(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    msg_cli.add_commands(sub)
    return parser.parse_args(argv)


HUMAN = "human:alexey"


class FakeMailbox:
    """The Hub2 /api/messages contract (hub2/src/mailbox-do.ts): the sender is
    always the requesting credential; human:me is resolved by the server."""

    def __init__(self, workspace=None):
        self.calls = []
        self.messages = {}
        self.workspace = workspace

    def credential_actor(self, path):
        if "?as=" in path:
            return path.split("?as=", 1)[1].split("&", 1)[0].replace("%3A", ":")
        profile = os.environ.get("HUB_CONFIG_FILE")
        if profile:
            return json.loads(Path(profile).read_text()).get("actor") or HUMAN
        return HUMAN

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload, os.environ.get("HUB_CONFIG_FILE")))
        if path == "/api/session-messaging/credentials":
            return {"sessions": [{"session_id": HUB_SESSION, "native_id": NATIVE, "tag": "t", "workspace": str(self.workspace)}]}
        if path == "/api/sessions/" + HUB_SESSION + "/messaging-credential":
            return {"actor": "session:" + HUB_SESSION, "access_token": "hubt_secret", "refresh_token": "hubr_secret"}
        bare = path.split("?", 1)[0]
        if method == "POST" and bare == "/api/messages":
            recipient = HUMAN if payload["recipient"] == "human:me" else payload["recipient"]
            message = {"id": "m%d" % (len(self.messages) + 1), "created_at": len(self.messages) + 1, "sender": self.credential_actor(path), "recipient": recipient,
                       "body": payload["body"], "key": payload["key"], "thread_id": "t", "reply_to": payload.get("reply_to"),
                       "epoch": 1, "state": "received", "read_at": None}
            self.messages[message["id"]] = message
            return {"message": message}
        if method == "GET" and bare == "/api/auth/me":
            if os.environ.get("HUB_CONFIG_FILE"):
                raise ValueError("HTTP request failed with status 403: human_required")
            return {"actor": HUMAN, "kind": "human"}
        if method == "GET" and bare == "/api/messages":
            # Oldest-first pages with a (created_at, id) cursor, like mailbox-do.ts list().
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&")) if "?" in path else {}
            limit = int(query.get("limit", 25))
            rows = sorted(self.messages.values(), key=lambda m: (m.get("created_at", 0), m["id"]))
            if "cursor" in query:
                after = json.loads(unquote(query["cursor"]))
                rows = [m for m in rows if (m.get("created_at", 0), m["id"]) > (after[0], after[1])]
            page = rows[:limit]
            more = len(rows) > limit
            return {"items": page, "filtered_page": False, "next": json.dumps([page[-1].get("created_at", 0), page[-1]["id"]]) if more else None}
        parts = bare.split("/")
        if len(parts) >= 4 and parts[3] in self.messages:
            message = self.messages[parts[3]]
            if method == "GET" and len(parts) == 4:
                return {"message": message}
            if method == "GET" and parts[4] == "status":
                return {"message": message, "events": []}
            if method == "POST" and parts[4] == "read":
                message["read_at"] = 1
                return {"recorded": True}
            if method == "POST":
                return {"recorded": True}
        raise ValueError("HTTP request failed with status 404: message_not_found")


class MsgCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.workspace = self.home / "ws"  # the session's own (gitignored .local) workspace
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"),
                                               "XDG_STATE_HOME": str(self.home / "state"), "LOCALAPPDATA": str(self.home / "local")}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("HUB_CONFIG_FILE", "APLEXER_SESSION_ID", "APLEXER_WORKSPACE", "HUB_TOKEN", "HUB_JOB_TOKEN", "HUB_ALLOW_LEGACY_ROUTES", "APLEXER_TAG", "HUB2_CONFIG"):
            os.environ.pop(key, None)
        self.hub = FakeMailbox(self.workspace)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        # Each real `hub` invocation is a fresh process: no identity carries over.
        os.environ.pop("HUB_CONFIG_FILE", None)
        return msg_cli.execute(parse(argv))

    def write_profile(self, name="developer", actor="agent:developer"):
        profile = Path(os.environ["XDG_CONFIG_HOME"]) / "agent-hub" / (name + ".json")
        profile.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        profile.write_text(json.dumps({"base_url": "https://hub.example", "actor": actor, "access_token": "x"}))
        profile.chmod(0o600)

    def test_default_login_posts_to_its_own_web_inbox_without_from(self):
        # t3280u: `hub msg send --to human:me --inbox` failed with "no messaging
        # identity; pass --from" and then not_supported_on_hub2 (/api/bus/send).
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            result = self.run_cli(["msg", "send", "--to", "human:me", "--inbox", "--body", "Research t3273u https://example.test/moats"])
        method, path, payload, _ = self.hub.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/messages"))
        self.assertEqual(payload["recipient"], "human:me")
        self.assertEqual(set(payload), {"recipient", "body", "key"})
        self.assertEqual((result["from"], result["to"]), (HUMAN, HUMAN))
        self.assertEqual(result["idempotency_key"], payload["key"])
        self.assertFalse(any("/api/bus/" in call[1] for call in self.hub.calls))

    def test_real_cli_entrypoint_sends_to_human_me_and_prints_the_message(self):
        with mock.patch.object(cli.sys, "argv", ["hub", "msg", "send", "--to", "human:me", "--body", "link"]), \
                mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub), \
                mock.patch("builtins.print") as printed:
            cli.main()
        out = json.loads(printed.call_args.args[0])
        self.assertEqual((out["to"], out["id"]), (HUMAN, "m1"))

    def test_hub1_bus_route_fails_loudly_with_the_replacement(self):
        from agent_hub_host.session_control import ensure_hub2_supported
        with self.assertRaises(ValueError) as ctx:
            ensure_hub2_supported("POST", "/api/bus/send")
        self.assertIn("not_supported_on_hub2", str(ctx.exception))
        self.assertIn("hub msg send --to human:me", str(ctx.exception))

    def test_hub1_only_flags_are_refused_before_any_request(self):
        cases = [(["--tui"], "hub sessions send"), (["--thread", "t1"], "--thread"),
                 (["--ref", '{"kind":"url"}'], "--ref"), (["--deliver-by", "24h"], "--deliver-by")]
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            for extra, hint in cases:
                with self.assertRaises(msg_cli.MsgError) as ctx:
                    self.run_cli(["msg", "send", "--to", "human:me", "--body", "x", *extra])
                self.assertIn("not_supported_on_hub2", str(ctx.exception))
                self.assertIn(hint, str(ctx.exception))
                self.assertEqual(ctx.exception.exit_code, 2)
        self.assertEqual(self.hub.calls, [])

    def test_oversized_body_is_refused_locally(self):
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            with self.assertRaises(msg_cli.MsgError):
                self.run_cli(["msg", "send", "--to", "human:me", "--body", "x" * 12001])
        self.assertEqual(self.hub.calls, [])

    def test_host_provisions_a_private_session_file_and_the_session_sends_as_itself(self):
        written = session_messages.provision_credentials(self.hub, "https://hub.example", force=True)
        self.assertEqual(written, [HUB_SESSION])
        path = msg_cli.session_auth_dir(str(self.workspace)) / (NATIVE + ".json")
        self.assertEqual(path, self.workspace / ".local" / "hub-session-auth" / (NATIVE + ".json"))
        stored = json.loads(path.read_text())
        self.assertEqual(stored["actor"], "session:" + HUB_SESSION)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        os.environ["APLEXER_SESSION_ID"] = NATIVE
        os.environ["APLEXER_WORKSPACE"] = str(self.workspace)
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            result = self.run_cli(["msg", "send", "--to", "human:me", "--body", "hi"])
            self.assertEqual(result["from"], "session:" + HUB_SESSION)
            self.assertEqual(self.hub.calls[-1][3], str(path))
            with self.assertRaises(msg_cli.MsgError):
                self.run_cli(["msg", "send", "--to", "x", "--body", "hi", "--from", "agent:developer"])
            # The human replies from the web; the session replies back to that human.
            os.environ.pop("APLEXER_SESSION_ID")
            self.hub.messages["h1"] = {"id": "h1", "sender": HUMAN, "recipient": "session:" + HUB_SESSION, "epoch": 1}
            os.environ["APLEXER_SESSION_ID"] = NATIVE
            reply = self.run_cli(["msg", "reply", "h1", "--body", "pong"])
            method, url, payload, _ = self.hub.calls[-1]
            self.assertEqual((method, url), ("POST", "/api/messages"))
            self.assertEqual((payload["recipient"], payload["reply_to"], payload["parent_sender"]), (HUMAN, "h1", HUMAN))
            self.assertEqual(reply["from"], "session:" + HUB_SESSION)
            delegated = self.run_cli(["msg", "reply", "h1", "--body", "as oncall", "--as", "agent:oncall"])
            self.assertEqual(delegated["from"], "agent:oncall")
            self.assertEqual(self.hub.calls[-1][1], "/api/messages?as=agent%3Aoncall")
        del os.environ["APLEXER_WORKSPACE"]
        os.environ.pop("HUB_CONFIG_FILE", None)
        self.assertEqual(self.run_cli(["msg", "whoami"]), {"actor": None, "source": "default"})

    def test_named_profile_and_default_identity(self):
        self.write_profile()
        self.assertEqual(self.run_cli(["msg", "--profile", "developer", "whoami"]), {"actor": "agent:developer", "source": "profile:developer"})
        self.assertEqual(self.run_cli(["msg", "whoami"]), {"actor": None, "source": "default"})
        with self.assertRaises(ValueError):
            msg_cli.execute(parse(["msg", "--profile", "../evil", "whoami"]))
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            sent = msg_cli.execute(parse(["msg", "--profile", "developer", "send", "--to", "human:me", "--body", "hi"]))
        self.assertEqual((sent["from"], sent["to"]), ("agent:developer", HUMAN))

    def test_agent_credential_writes_a_new_private_file_without_printing_tokens(self):
        out = self.home / "dev.json"
        with mock.patch("agent_hub_host.bus._do_request", return_value={"actor": "agent:developer", "access_token": "hubt_a", "refresh_token": "hubr_b", "expires_at": "z"}), \
                mock.patch("agent_hub_host.config.load_config", return_value={"base_url": "https://hub.example"}):
            result = msg_cli.execute(parse(["msg", "agent-credential", "developer", "--output", str(out)]))
        self.assertNotIn("hubt_a", json.dumps(result))
        self.assertEqual(json.loads(out.read_text())["access_token"], "hubt_a")
        with self.assertRaises(ValueError):
            msg_cli.execute(parse(["msg", "agent-credential", "developer", "--output", str(out)]))



class MsgCliM5Test(MsgCliTest):
    """Exit codes, same-key retries, inbox flags, stages and operator readdress on Hub2."""

    def test_exit_code_mapping(self):
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 404: unknown_recipient")), 3)
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 404: recipient_unknown")), 3)
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 409: ambiguous_recipient")), 3)
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 403: forbidden")), 2)
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 503")), 4)
        self.assertEqual(msg_cli.exit_code_for(ValueError("A non-empty body is required")), 2)
        self.assertEqual(msg_cli.exit_code_for(URLError("conn reset")), 4)
        self.assertEqual(msg_cli.exit_code_for(msg_cli.MsgError("--from differs from this identity")), 2)

    def test_retries_http_failures_with_the_same_idempotency_key(self):
        key = str(__import__("uuid").uuid4())
        calls = []
        flaky = [ValueError("HTTP request failed with status 503"), None]

        def flaky_request(method, path, body=None):
            calls.append(body)
            item = flaky.pop(0)
            if item is None:
                return self.hub(method, path, body)
            raise item

        with mock.patch("agent_hub_host.bus._do_request", side_effect=flaky_request), mock.patch("time.sleep"):
            result = self.run_cli(["msg", "send", "--to", "human:me", "--body", "hi", "--key", key])
        self.assertEqual(result["idempotency_key"], key)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["key"] == key for c in calls))

    def test_transport_errors_raise_exit_code_4(self):
        with mock.patch("agent_hub_host.bus._do_request", side_effect=URLError("refused")), mock.patch("time.sleep"):
            with self.assertRaises(msg_cli.MsgError) as ctx:
                self.run_cli(["msg", "send", "--to", "human:me", "--body", "hi"])
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_inbox_filters_unread_clamps_wait_and_marks_read_with_epoch(self):
        self.hub.messages["a"] = {"id": "a", "sender": "session:s", "recipient": HUMAN, "epoch": 3, "read_at": None}
        self.hub.messages["b"] = {"id": "b", "sender": "session:s", "recipient": HUMAN, "epoch": 1, "read_at": 5}
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            result = self.run_cli(["msg", "inbox", "--wait", "120", "--unread", "--thread", "t9", "--mark-read", "--limit", "500"])
        self.assertEqual([m["id"] for m in result["messages"]], ["a"])
        list_call = self.hub.calls[0][1]
        self.assertTrue(list_call.startswith("/api/messages?limit=50"))
        self.assertIn("thread=t9", list_call)
        self.assertEqual(result["marked_read"], {"marked": 1})
        self.assertEqual(self.hub.calls[-1][:3], ("POST", "/api/messages/a/read", {"epoch": 3}))

    def test_inbox_wait_polls_until_a_message_arrives(self):
        polls = []

        def request(method, path, payload=None):
            polls.append(path)
            if len(polls) == 2:
                self.hub.messages["late"] = {"id": "late", "sender": "session:s", "recipient": HUMAN, "epoch": 1, "read_at": None}
            return self.hub(method, path, payload)

        with mock.patch("agent_hub_host.bus._do_request", side_effect=request), mock.patch("time.sleep"):
            result = self.run_cli(["msg", "inbox", "--wait", "30"])
        self.assertEqual([m["id"] for m in result["messages"]], ["late"])
        self.assertEqual(len(polls), 2)

    def test_named_agent_inbox_reads_through_delegation(self):
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            self.run_cli(["msg", "inbox", "--actor", "agent:developer"])
        self.assertIn("as=agent%3Adeveloper", self.hub.calls[-1][1])

    def test_root_profile_inbox_reads_the_named_agent_inbox(self):
        with mock.patch.object(cli.sys, "argv", ["hub", "--profile", "developer", "inbox"]), \
                mock.patch.object(msg_cli, "resolve_identity", return_value=("agent:developer", "profile:developer")) as identity, \
                mock.patch.object(msg_cli, "_request", return_value={"items": []}) as request, \
                mock.patch("builtins.print"):
            cli.main()
        identity.assert_called_once_with("developer")
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertTrue(request.call_args.args[1].startswith("/api/messages?"))

    def test_top_level_send_resolves_profile_identity_and_delegation(self):
        self.write_profile()
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            args = argparse.Namespace(profile="developer", to_actor="human:me", body="hi", body_file=None, from_actor=None, as_actor=None,
                                      kind="note", delivery=None, thread_id=None, thread=None, reply_to=None, lease_epoch=None,
                                      idempotency_key="k-top", ref=None, deliver_by=None)
            result = msg_cli.top_send(args)
            self.assertEqual((result["from"], result["to"]), ("agent:developer", HUMAN))
            args.as_actor = "agent:oncall"
            delegated = msg_cli.top_send(args)
            self.assertEqual(delegated["from"], "agent:oncall")
            self.assertEqual(self.hub.calls[-1][1], "/api/messages?as=agent%3Aoncall")
            args.from_actor = "agent:impersonator"
            with self.assertRaises(msg_cli.MsgError):
                msg_cli.top_send(args)
            args.from_actor, args.as_actor, args.kind = None, None, "task"
            with self.assertRaises(msg_cli.MsgError) as ctx:
                msg_cli.top_send(args)
            self.assertIn("not_supported_on_hub2", str(ctx.exception))

    def test_stages_are_epoch_bound_and_lease_epoch_is_refused(self):
        self.hub.messages["m9"] = {"id": "m9", "sender": "session:s", "recipient": HUMAN, "epoch": 4, "read_at": None}
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            msg_cli.top_stage(argparse.Namespace(id="m9", profile=None, lease_epoch=None), "ack")
            self.assertEqual(self.hub.calls[-1][:3], ("POST", "/api/messages/m9/ack?mailbox=human%3Aalexey", {"epoch": 4}))
            with self.assertRaises(msg_cli.MsgError):
                msg_cli.top_stage(argparse.Namespace(id="m9", profile=None, lease_epoch=2), "ack")
            self.assertEqual(self.run_cli(["msg", "status", "m9"])["message"]["id"], "m9")
            self.assertEqual(self.run_cli(["msg", "show", "m9"])["id"], "m9")

    def test_inbox_walks_oldest_first_pages_and_returns_the_newest_unread(self):
        for i in range(120):
            self.hub.messages["o%03d" % i] = {"id": "o%03d" % i, "created_at": i, "sender": "session:s", "recipient": HUMAN,
                                              "epoch": 1, "read_at": None if i >= 115 else 1}
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            result = self.run_cli(["msg", "inbox", "--unread", "--limit", "3"])
            self.assertEqual([m["id"] for m in result["messages"]], ["o117", "o118", "o119"])
            latest = self.run_cli(["msg", "inbox", "--limit", "2"])
            self.assertEqual([m["id"] for m in latest["messages"]], ["o118", "o119"])

    def test_inbox_wait_only_returns_mail_newer_than_the_start(self):
        self.hub.messages["old"] = {"id": "old", "created_at": 1, "sender": "session:s", "recipient": HUMAN, "epoch": 1, "read_at": 1}
        polls = []

        def request(method, path, payload=None):
            polls.append(path)
            if len(polls) == 3:
                self.hub.messages["new"] = {"id": "new", "created_at": 2, "sender": "session:s", "recipient": HUMAN, "epoch": 1, "read_at": None}
            return self.hub(method, path, payload)

        with mock.patch("agent_hub_host.bus._do_request", side_effect=request), mock.patch("time.sleep"):
            result = self.run_cli(["msg", "inbox", "--unread", "--wait", "30"])
        self.assertEqual([m["id"] for m in result["messages"]], ["new"])
        self.assertIn("cursor=", polls[-1])

    def test_reply_to_own_message_goes_to_the_other_end_for_a_login_without_actor(self):
        # Review finding: the default login has no actor in its profile; replying to
        # a message it sent must not address the reply to itself.
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            sent = self.run_cli(["msg", "send", "--to", "session:abc", "--body", "first"])
            reply = self.run_cli(["msg", "reply", sent["id"], "--body", "follow-up"])
        self.assertEqual((reply["from"], reply["to"]), (HUMAN, "session:abc"))
        self.assertIn(("GET", "/api/auth/me"), [c[:2] for c in self.hub.calls])

    def test_hub2_session_worker_is_the_sender_and_profile_is_refused(self):
        worker = {"base_url": "https://hub.example", "_hub2_worker": {"actor": "session:" + HUB_SESSION}}
        with mock.patch("agent_hub_host.worker_auth.is_v2_worker", return_value=True), \
                mock.patch("agent_hub_host.config.load_config", return_value=worker), \
                mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            self.assertEqual(self.run_cli(["msg", "whoami"]), {"actor": "session:" + HUB_SESSION, "source": "hub2-session"})
            with self.assertRaises(msg_cli.MsgError) as ctx:
                self.run_cli(["msg", "--profile", "developer", "send", "--to", "human:me", "--body", "x"])
            self.assertIn("--profile", str(ctx.exception))
            self.hub.messages["mine"] = {"id": "mine", "created_at": 1, "sender": "session:" + HUB_SESSION, "recipient": HUMAN, "epoch": 1}
            reply = self.run_cli(["msg", "reply", "mine", "--body", "follow-up"])
        self.assertEqual(self.hub.calls[-1][2]["recipient"], HUMAN)
        self.assertEqual(reply["to"], HUMAN)

    def test_readdress_and_agent_delegate_requests(self):
        self.hub.messages["m1"] = {"id": "m1", "sender": HUMAN, "recipient": "session:x", "epoch": 2}
        target = "session:22222222-2222-4222-8222-222222222222"
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.hub):
            self.run_cli(["msg", "readdress", "m1", "--to", target])
        self.assertEqual(self.hub.calls[-1][:3], ("POST", "/api/messages/m1/readdress",
                                                  {"recipient": target, "key": "readdress:m1:2:" + target, "epoch": 2}))
        calls = []
        with mock.patch("agent_hub_host.bus._do_request", side_effect=lambda m, p, b=None: calls.append((m, p, b)) or {}):
            self.run_cli(["msg", "agent-delegate", "developer"])
            self.assertEqual(calls[-1][:2], ("GET", "/api/agents/named/developer/delegates"))
            self.run_cli(["msg", "agent-delegate", "developer", "--client", "cursor-mcp", "--remove"])
            self.assertEqual(calls[-1][2], {"client": "cursor-mcp", "remove": True})


if __name__ == "__main__":
    unittest.main()
