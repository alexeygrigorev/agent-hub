"""Pure fake native-launch tests; no native or provider executable is invoked."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import native_launch as nl

TEMP_BASE = Path(__file__).resolve().parents[3] / ".local/tmp"

FILTER = "error,opentelemetry_sdk=off,opentelemetry_otlp=off,codex_tool_lifecycle=info"
# EXEC_DEFAULT_LOG_FILTER in exact ZCode81 exec/src/lib.rs (81b945a4).
# These exclusions must survive through actual launch argv, not just FILTER.
ZCODE81_DEFAULT_DIRECTIVES = {"error", "opentelemetry_sdk=off", "opentelemetry_otlp=off"}
NATIVE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
OPERATION_ID = "33333333-3333-4333-8333-333333333333"

class ZaiLifecycleTraceTests(unittest.TestCase):
    def launch(self, provider="zai", *, interactive=False, central=False, trusted_filter=None):
        TEMP_BASE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_BASE) as temporary:
            workspace = Path(temporary)
            task_id = "trace-test"
            prompt = "SYNTHETIC useful task"
            specification = dict(engine="route", model={"zai":"glm-5.3-flash", "claude":"sonnet", "codex":"gpt-6.1", "go":nl.OPENCODE_MODEL}[provider],
                effort=None if provider == "go" else "medium", prompt=prompt, workspace=str(workspace),
                timeout_seconds=120, interactive=interactive, session_id=SESSION_ID,
                operation_id=OPERATION_ID, epoch=7, account_id="synthetic-account")
            route = dict(provider=provider, profile="hub-zai-central" if provider == "zai" else "synthetic-profile")
            if provider == "codex":
                route["profile"] = nl.CODEX_WRAPPER_PROFILE
            captured = {}
            def runner(argv, **options):
                captured.update(argv=argv, options=options)
                return subprocess.CompletedProcess(argv, 0, json.dumps(dict(id=NATIVE_ID,
                    tag="task-"+task_id, workspace=str(workspace), command=argv[argv.index("--")+1:])), "")
            adapter = nl.NativeLauncher(dict(private_provider_homes=central, routes={"route":route}), runner=runner)
            home = workspace / "trusted-home"
            native_env = nl.provider_home.home_environment(home, provider)
            if trusted_filter is not None:
                native_env["RUST_LOG"] = trusted_filter
            if central:
                specification.update(provider_home=str(workspace/"synthetic-manifest.json"), client=object())
            with patch.dict(os.environ, {"HOME":str(workspace), "RUST_LOG":"synthetic-parent-override"}, clear=True), \
                 patch.object(nl, "session_binding", return_value={"runtime_deadline":9999999999999}) as binding, \
                 patch.object(nl, "native_environment", return_value=native_env) as environment, \
                 patch.object(nl, "codex_launch_wrapper", return_value=Path("/synthetic/launch-codex.sh")), \
                 patch.object(adapter, "codex_exec_readiness", return_value={"synthetic":True}), \
                 patch.object(adapter, "record_evidence"), patch.object(nl, "validate_first_action", return_value=False):
                receipt = adapter.launch(task_id, specification)
            argv = captured["argv"]
            prefix = argv[:argv.index("--")]
            explicit = [prefix[i+1].split("=",1) for i,x in enumerate(prefix) if x == "--env"]
            env = dict(explicit)
            self.assertEqual(len(explicit), len(env), "no duplicate child env assignment")
            self.assertEqual(receipt["session_id"], NATIVE_ID)
            self.assertFalse(receipt["first_action_verified"])
            self.assertEqual(prefix[prefix.index("--cwd")+1], str(workspace))
            self.assertEqual(prefix[prefix.index("--workspace")+1], str(workspace))
            child = argv[argv.index("--")+1:]
            local = not central and not interactive
            if local:
                self.assertEqual(child[:5], ["a","task","run","--profile",route["profile"]])
                self.assertEqual(child[child.index("--cwd")+1], str(workspace))
                prompt_path = Path(child[child.index("--prompt-file")+1])
                expected = nl.aplexer_prompt(prompt+nl.FIRST_ACTION_INSTRUCTION.format(path=workspace/".local"/f"first-action-{task_id}.json"))
                self.assertEqual(prompt_path.read_text(), expected)
                self.assertIn("--engine-arg=--model", child)
                self.assertIn("--engine-arg="+specification["model"], child)
                self.assertEqual(specification["_task_output"]["profile"], route["profile"])
                self.assertEqual(specification["_task_output"]["operation_id"], OPERATION_ID)
                self.assertEqual(specification["_task_output"]["epoch"], 7)
                self.assertEqual(specification["_task_output"]["prompt_sha256"], hashlib.sha256(expected.encode()).hexdigest())
            if central:
                binding.assert_called_once_with(specification["provider_home"], specification["client"], SESSION_ID, OPERATION_ID, 7)
                environment.assert_called_once_with(specification["provider_home"])
                for key,value in native_env.items():
                    self.assertEqual(env[key], value)
            else:
                binding.assert_not_called(); environment.assert_not_called()
            self.assertEqual(captured["options"]["env"]["RUST_LOG"], "synthetic-parent-override", "spawn client is not globally changed")
            self.assertNotIn("RUST_LOG", specification)
            if provider == "zai" and trusted_filter is None:
                directives = env.get("RUST_LOG", "").split(",")
                self.assertEqual(len(directives), len(set(directives)))
                self.assertEqual(set(directives) - {"codex_tool_lifecycle=info"}, ZCODE81_DEFAULT_DIRECTIVES)
            return env, child

    def test_profile_headless_zai_gets_fixed_filter_and_retains_real_profile_prompt_output(self):
        env,child = self.launch()
        self.assertEqual(env.get("RUST_LOG"), FILTER)
        self.assertIn('--engine-arg=model_reasoning_effort="medium"', child)

    def test_direct_interactive_zai_gets_fixed_filter_without_changing_model_effort(self):
        env,child = self.launch(interactive=True)
        self.assertEqual(env.get("RUST_LOG"), FILTER)
        self.assertEqual(child[0], "zcodex")
        self.assertEqual(child[-3:-1], ["-c", 'model_reasoning_effort="medium"'])
        self.assertIn("glm-5.3-flash", child)

    def test_private_home_zai_keeps_actual_home_environment_and_exact_binding(self):
        env,child = self.launch(central=True)
        self.assertEqual(env.get("RUST_LOG"), FILTER)
        self.assertTrue(env["CODEX_HOME"].endswith("trusted-home/.zcodex"))
        self.assertTrue(env["ZCODE_HOME"].endswith("trusted-home/.zcode"))
        self.assertEqual(child[0], "zcodex")

    def test_trusted_explicit_child_filter_is_not_overwritten(self):
        env,_ = self.launch(central=True, trusted_filter="error,synthetic_allowed=info")
        self.assertEqual(env["RUST_LOG"], "error,synthetic_allowed=info")

    def test_other_providers_do_not_gain_filter_or_new_child_assignments(self):
        for provider in ("claude", "codex", "go"):
            for interactive in (False, True):
                with self.subTest(provider=provider, interactive=interactive):
                    env,_ = self.launch(provider, interactive=interactive)
                    self.assertNotIn("RUST_LOG", env)

if __name__ == "__main__":
    unittest.main()
