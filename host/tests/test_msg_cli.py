"""`hub msg` identity selection and host credential provisioning (docs/SESSION-MESSAGING.md §8, §11)."""
import argparse
import json
import os
import stat
import sys
import tempfile
import unittest
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
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"),
                                               "XDG_STATE_HOME": str(self.home / "state"), "LOCALAPPDATA": str(self.home / "local")}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("HUB_CONFIG_FILE", "APLEXER_SESSION_ID", "HUB_TOKEN", "HUB_JOB_TOKEN"):
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
            return {"sessions": [{"session_id": HUB_SESSION, "native_id": NATIVE, "tag": "t", "workspace": "/w"}]}
        if path.startswith("/api/session-messaging/credentials/"):
            return {"actor": "session:" + HUB_SESSION, "access_token": "hubt_secret", "refresh_token": "hubr_secret"}
        if path.startswith("/api/bus/messages/"):
            return {"id": "m1", "from_actor": "agent:developer", "to_actor": "session:" + HUB_SESSION}
        return {"id": "new", "thread_id": "t"}

    def test_host_provisions_a_private_session_file_and_the_session_sends_as_itself(self):
        written = session_messages.provision_credentials(self.fake_request, "https://hub.example", force=True)
        self.assertEqual(written, [HUB_SESSION])
        path = msg_cli.session_auth_dir() / (NATIVE + ".json")
        stored = json.loads(path.read_text())
        self.assertEqual(stored["actor"], "session:" + HUB_SESSION)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        os.environ["APLEXER_SESSION_ID"] = NATIVE
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
