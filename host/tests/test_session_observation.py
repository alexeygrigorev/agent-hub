import unittest

from agent_hub_host.fleet import session_metadata


class SessionObservationTests(unittest.TestCase):
    def test_census_reports_explicit_model_and_parent_without_command(self):
        row = {"id": "native", "tag": "manual", "workspace": "/repo",
               "engine": "zcodex", "profile": None, "state": "running",
               "command": ["zcodex", "--model", "glm-5.3-flash", "private goal"],
               "parent_session": "parent-native"}
        result = session_metadata([row], {"subscriptions": []}, collect_tokens=False)[0]
        self.assertEqual(result["model"], "glm-5.3-flash")
        self.assertEqual(result["parent_id"], "parent-native")
        self.assertNotIn("command", result)
        self.assertNotIn("private goal", str(result))

    def test_census_never_invents_a_default_model(self):
        row = {"id": "native", "tag": "manual", "workspace": "/repo",
               "engine": "codex", "profile": None, "state": "running"}
        for command in (None, ["codex"], ["codex", "--model", "a", "--model", "b"]):
            with self.subTest(command=command):
                result = session_metadata([{**row, "command": command}],
                                          {"subscriptions": []}, collect_tokens=False)[0]
                self.assertIsNone(result["model"])


if __name__ == "__main__":
    unittest.main()
