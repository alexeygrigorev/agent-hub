"""`hub msg` identity selection and host credential provisioning (docs/SESSION-MESSAGING.md §8, §11)."""
import argparse
import json
import os
import stat
import sys
import tempfile
import unittest
from urllib.error import URLError
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import msg_cli, session_messages  # noqa: E402

NATIVE = "33333333-3333-4333-8333-333333333333"
HUB_SESSION = "22222222-2222-4222-8222-222222222222"


def parse(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    msg_cli.add_commands(sub)
    return parser.parse_args(argv)


class MsgCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.workspace = self.home / "ws"  # the session's own (gitignored .local) workspace
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"),
                                               "XDG_STATE_HOME": str(self.home / "state"), "LOCALAPPDATA": str(self.home / "local")}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("HUB_CONFIG_FILE", "APLEXER_SESSION_ID", "APLEXER_WORKSPACE", "HUB_TOKEN", "HUB_JOB_TOKEN"):
            os.environ.pop(key, None)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        # Each real `hub` invocation is a fresh process: no identity carries over.
        os.environ.pop("HUB_CONFIG_FILE", None)
        return msg_cli.execute(parse(argv))

    def fake_request(self, method, path, payload=None):
        self.calls.append((method, path, payload, os.environ.get("HUB_CONFIG_FILE")))
        if path == "/api/session-messaging/credentials":
            return {"sessions": [{"session_id": HUB_SESSION, "native_id": NATIVE, "tag": "t", "workspace": str(self.workspace)}]}
        if path == "/api/sessions/" + HUB_SESSION + "/messaging-credential":
            return {"actor": "session:" + HUB_SESSION, "access_token": "hubt_secret", "refresh_token": "hubr_secret"}
        if path.startswith("/api/bus/messages/"):
            return {"id": "m1", "from_actor": "agent:developer", "to_actor": "session:" + HUB_SESSION}
        return {"id": "new", "thread_id": "t"}

    def test_host_provisions_a_private_session_file_and_the_session_sends_as_itself(self):
        written = session_messages.provision_credentials(self.fake_request, "https://hub.example", force=True)
        self.assertEqual(written, [HUB_SESSION])
        # AC3: <workspace>/.local/hub-session-auth/<native_id>.json, owner-only.
        path = msg_cli.session_auth_dir(str(self.workspace)) / (NATIVE + ".json")
        self.assertEqual(path, self.workspace / ".local" / "hub-session-auth" / (NATIVE + ".json"))
        stored = json.loads(path.read_text())
        self.assertEqual(stored["actor"], "session:" + HUB_SESSION)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # The CLI finds it through APLEXER_SESSION_ID + APLEXER_WORKSPACE only.
        os.environ["APLEXER_SESSION_ID"] = NATIVE
        os.environ["APLEXER_WORKSPACE"] = str(self.workspace)
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.fake_request):
            result = self.run_cli(["msg", "send", "--to", "agent:developer", "--body", "hi"])
            self.assertEqual(result["from"], "session:" + HUB_SESSION)
            method, url, payload, config = self.calls[-1]
            self.assertEqual((url, payload["from_actor"], config), ("/api/bus/send", "session:" + HUB_SESSION, str(path)))
            with self.assertRaises(ValueError):
                self.run_cli(["msg", "send", "--to", "x", "--body", "hi", "--from", "agent:developer"])
            reply = self.run_cli(["msg", "reply", "m1", "--body", "pong"])
            self.assertEqual(self.calls[-1][2]["to_actor"], "agent:developer")
            self.assertEqual(reply["from"], "session:" + HUB_SESSION)
        # Without APLEXER_WORKSPACE the session file is not discovered.
        del os.environ["APLEXER_WORKSPACE"]
        os.environ.pop("HUB_CONFIG_FILE", None)
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.fake_request):
            self.assertEqual(self.run_cli(["msg", "whoami"]), {"actor": None, "source": "default"})

    def test_named_profile_and_default_identity(self):
        profile = Path(os.environ["XDG_CONFIG_HOME"]) / "agent-hub" / "developer.json"
        profile.parent.mkdir(parents=True, mode=0o700)
        profile.write_text(json.dumps({"base_url": "https://hub.example", "actor": "agent:developer", "access_token": "x"}))
        profile.chmod(0o600)
        self.assertEqual(self.run_cli(["msg", "--profile", "developer", "whoami"]), {"actor": "agent:developer", "source": "profile:developer"})
        self.assertEqual(self.run_cli(["msg", "whoami"]), {"actor": None, "source": "default"})
        with self.assertRaises(ValueError):
            msg_cli.execute(parse(["msg", "--profile", "../evil", "whoami"]))
        with self.assertRaises(ValueError):
            self.run_cli(["msg", "send", "--to", "x", "--body", "hi"])  # default profile must name --from

    def test_agent_credential_writes_a_new_private_file_without_printing_tokens(self):
        out = self.home / "dev.json"
        with mock.patch("agent_hub_host.bus._do_request", return_value={"actor": "agent:developer", "access_token": "hubt_a", "refresh_token": "hubr_b", "expires_at": "z"}), \
                mock.patch("agent_hub_host.config.load_config", return_value={"base_url": "https://hub.example"}):
            result = msg_cli.execute(parse(["msg", "agent-credential", "developer", "--output", str(out)]))
        self.assertNotIn("hubt_a", json.dumps(result))
        self.assertEqual(json.loads(out.read_text())["access_token"], "hubt_a")
        with self.assertRaises(ValueError):
            msg_cli.execute(parse(["msg", "agent-credential", "developer", "--output", str(out)]))


if __name__ == "__main__":
    unittest.main()


class MsgCliM5Test(unittest.TestCase):
    """M5 client surface: §11 exit codes, same-key retries, --ref/--deliver-by,
    long-poll inbox flags, operator readdress/delegate (intake 3bdf9715)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"),
                                               "XDG_STATE_HOME": str(self.home / "state"), "LOCALAPPDATA": str(self.home / "local")}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("HUB_CONFIG_FILE", "APLEXER_SESSION_ID", "HUB_TOKEN", "HUB_JOB_TOKEN"):
            os.environ.pop(key, None)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/status") or path.startswith("/api/bus/messages/"):
            return {"id": "m1", "from_actor": "agent:developer", "to_actor": "session:x"}
        return {"id": "new", "thread_id": "t"}

    def run_cli(self, argv):
        os.environ.pop("HUB_CONFIG_FILE", None)
        return msg_cli.execute(parse(argv))

    def write_profile(self, name="developer", actor="agent:developer"):
        profile = Path(os.environ["XDG_CONFIG_HOME"]) / "agent-hub" / (name + ".json")
        profile.parent.mkdir(parents=True, mode=0o700)
        profile.write_text(json.dumps({"base_url": "https://hub.example", "actor": actor, "access_token": "x"}))
        profile.chmod(0o600)

    def test_exit_code_mapping(self):
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 404: unknown_recipient")), 3)
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 409: ambiguous_recipient")), 3)
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 403: forbidden")), 2)
        self.assertEqual(msg_cli.exit_code_for(ValueError("HTTP request failed with status 503")), 4)
        self.assertEqual(msg_cli.exit_code_for(ValueError("A non-empty body is required")), 2)
        self.assertEqual(msg_cli.exit_code_for(URLError("conn reset")), 4)
        self.assertEqual(msg_cli.exit_code_for(msg_cli.MsgError("--from differs from this identity")), 2)

    def test_retries_http_failures_with_the_same_idempotency_key(self):
        self.write_profile()
        payload = {"idempotency_key": "fixed"}
        responses = [ValueError("HTTP request failed with status 502"), {"id": "new"}]
        with mock.patch("agent_hub_host.bus._do_request", side_effect=lambda m, p, b=None: responses.pop(0) if isinstance(responses[0], dict) else (_ for _ in ()).throw(responses.pop(0))):
            result = msg_cli._request("POST", "/api/bus/send", dict(payload, body="hi"), retries=True)
        self.assertEqual(result, {"id": "new"})
        self.assertEqual(len(self.calls), 0)  # the flaky attempt is invisible here; key stability asserted below
        key = str(__import__("uuid").uuid4())
        calls = []
        flaky = [ValueError("HTTP request failed with status 503"), {"id": "ok"}]

        def flaky_request(method, path, body=None):
            calls.append(body)
            item = flaky.pop(0)
            if isinstance(item, dict):
                return item
            raise item

        with mock.patch("agent_hub_host.bus._do_request", side_effect=flaky_request):
            argv = parse(["msg", "send", "--to", "agent:developer", "--body", "hi", "--key", key])
            argv.profile = "developer"
            result = msg_cli.execute(argv)
        self.assertEqual(result["idempotency_key"], key)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["idempotency_key"] == key for c in calls))

    def test_transport_errors_raise_exit_code_4(self):
        self.write_profile()
        with mock.patch("agent_hub_host.bus._do_request", side_effect=URLError("refused")):
            argv = parse(["msg", "send", "--to", "agent:developer", "--body", "hi"])
            argv.profile = "developer"
            with self.assertRaises(msg_cli.MsgError) as ctx:
                msg_cli.execute(argv)
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_send_flags_map_to_the_api_payload(self):
        self.write_profile()
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.fake_request):
            argv = parse(["msg", "send", "--to", "agent:developer", "--body", "hi", "--tui", "--thread", "t1",
                          "--ref", '{"kind":"url","value":"https://x"}', "--deliver-by", "24h", "--key", "k1"])
            argv.profile = "developer"
            msg_cli.execute(argv)
        method, path, payload = self.calls[-1]
        self.assertEqual(path, "/api/bus/send")
        self.assertEqual(payload["delivery"], "tui")
        self.assertEqual(payload["thread_id"], "t1")
        self.assertEqual(payload["deliver_by_s"], 86400)
        self.assertEqual(payload["ref"], {"kind": "url", "value": "https://x"})
        self.assertEqual(payload["from_actor"], "agent:developer")

    def test_inbox_long_poll_clamps_wait_and_marks_read(self):
        rows = [{"id": "a"}, {"id": "b"}]

        def request(method, path, payload=None):
            self.calls.append((method, path, payload))
            if path.startswith("/api/bus/inbox"):
                return rows
            return {"marked": 2}

        with mock.patch("agent_hub_host.bus._do_request", side_effect=request):
            argv = parse(["msg", "inbox", "--actor", "agent:developer", "--wait", "120", "--unread", "--thread", "t9", "--mark-read"])
            result = msg_cli.execute(argv)
        method, path, _ = self.calls[0]
        self.assertIn("actor=agent%3Adeveloper", path)
        self.assertIn("unread=1", path)
        self.assertIn("thread=t9", path)
        self.assertIn("wait=60", path)  # clamped from 120
        self.assertEqual(result["marked_read"], {"marked": 2})
        self.assertEqual(self.calls[-1][:2], ("POST", "/api/bus/messages/read"))
        self.assertEqual(self.calls[-1][2], {"ids": ["a", "b"]})

    def test_top_level_send_resolves_profile_identity(self):
        self.write_profile()
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.fake_request):
            args = argparse.Namespace(profile="developer", to_actor="human:me", body="hi", body_file=None, from_actor=None,
                                      kind=None, delivery=None, thread_id=None, thread=None, reply_to=None, lease_epoch=None,
                                      idempotency_key="k-top", ref=None, deliver_by=None)
            result = msg_cli.top_send(args)
        self.assertEqual(result["from"], "agent:developer")
        self.assertEqual(self.calls[-1][2]["from_actor"], "agent:developer")
        # A --from that differs from the profile credential is refused (§11).
        args.from_actor = "agent:impersonator"
        with self.assertRaises(msg_cli.MsgError):
            msg_cli.top_send(args)

    def test_readdress_and_agent_delegate_requests(self):
        with mock.patch("agent_hub_host.bus._do_request", side_effect=self.fake_request):
            result = self.run_cli(["msg", "readdress", "m1", "--to", "session:22222222-2222-4222-8222-222222222222"])
            self.assertEqual(self.calls[-1], ("POST", "/api/bus/messages/m1/readdress",
                                              {"to": "session:22222222-2222-4222-8222-222222222222"}))
            self.run_cli(["msg", "agent-delegate", "developer"])
            self.assertEqual(self.calls[-1][:2], ("GET", "/api/agents/named/developer/delegates"))
            self.run_cli(["msg", "agent-delegate", "developer", "--client", "cursor-mcp"])
            self.assertEqual(self.calls[-1], ("POST", "/api/agents/named/developer/delegates",
                                              {"client": "cursor-mcp", "remove": False}))
            self.run_cli(["msg", "agent-delegate", "developer", "--client", "cursor-mcp", "--remove"])
            self.assertEqual(self.calls[-1][2], {"client": "cursor-mcp", "remove": True})
