import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from agent_hub_host.session_observation import native_session_model

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

    def test_terminal_sessions_do_not_locate_transcripts(self):
        def forbidden(_):
            self.fail('terminal census must not locate transcripts')
        for phase in ('exited', 'dead', 'pruned', 'forgotten', 'stopped'):
            self.assertIsNone(native_session_model({'id':'old','engine':'codex',
                'state':phase,'created_at_ms':1700000000000}, SimpleNamespace(locate=forbidden)))

    def test_census_never_invents_a_default_model(self):
        row = {"id": "native", "tag": "manual", "workspace": "/repo",
               "engine": "codex", "profile": None, "state": "running"}
        for command in (None, ["codex"], ["codex", "--model", "a", "--model", "b"]):
            with self.subTest(command=command):
                result = session_metadata([{**row, "command": command}],
                                          {"subscriptions": []}, collect_tokens=False)[0]
                self.assertIsNone(result["model"])

    def test_bound_codex_and_claude_transcripts_resolve_default_models(self):
        row = {'id': 'native', 'engine': 'codex', 'created_at_ms': 1700000000000}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'transcript.jsonl'
            locate = SimpleNamespace(locate=lambda _: path)
            path.write_text(json.dumps({'type':'turn_context','payload':{'model':'gpt-6.1-sol'}})+'\n')
            self.assertEqual(native_session_model(row,locate),'gpt-6.1-sol')
            path.write_text(json.dumps({'type':'assistant','message':{'model':'claude-opus-4-8','content':'private content'}})+'\n')
            self.assertEqual(native_session_model({**row,'engine':'claude'},locate),'claude-opus-4-8')

    def test_claude_hook_binds_exact_conversation_and_current_incarnation(self):
        sid='11111111-1111-4111-8111-111111111111'
        row={'id':'native-hook','engine':'claude','created_at_ms':1700000000000,
             'reported_state_at_ms':1700000001000,'reported_state_engine_session_id':sid,
             'cwd':'/repo/project','state':'running'}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'projects'/'-repo-project'/(sid+'.jsonl')
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'type':'assistant','message':{'model':'claude-opus-4-8','content':'private'}})+'\n')
            native={**row,'env':{'CLAUDE_CONFIG_DIR':folder}}
            locator=SimpleNamespace(locate=lambda _:None)
            self.assertEqual(native_session_model(native,locator),'claude-opus-4-8')
            self.assertIsNone(native_session_model({**native,'reported_state_at_ms':1699999999999},locator))
            self.assertIsNone(native_session_model({**native,'reported_state_engine_session_id':'../wrong'},locator))
            self.assertIsNone(native_session_model({**native,'cwd':'/different/project'},locator))

    def test_latest_bounded_context_wins_and_untyped_model_labels_are_ignored(self):
        row = {'id': 'native-tail', 'engine': 'codex', 'created_at_ms': 1700000000000}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'transcript.jsonl'
            path.write_bytes(json.dumps({'type':'turn_context','payload':{'model':'old-model'}}).encode()+b'\n'+b'\n'*(256*1024)+
                             json.dumps({'type':'turn_context','payload':{'model':'new-model'}}).encode()+b'\n'+
                             json.dumps({'type':'user','model':'spoofed-model'}).encode()+b'\n')
            self.assertEqual(native_session_model(row,SimpleNamespace(locate=lambda _:path)),'new-model')

    def test_absent_bound_metadata_is_unknown_and_shell_is_not_applicable(self):
        locate = SimpleNamespace(locate=lambda _:None)
        self.assertIsNone(native_session_model({'id':'native','engine':'codex','created_at_ms':1700000000000},locate))
        self.assertEqual(native_session_model({'id':'shell','engine':'shell'}),'not_applicable')
        self.assertEqual(native_session_model({'model':'runtime-model','command':['codex','--model','requested-model']}),'runtime-model')


if __name__ == "__main__":
    unittest.main()
