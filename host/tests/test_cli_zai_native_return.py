"""Offline proof-reader cases; no CLI, provider, network or filesystem actions."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("canary_native_reader", ROOT / "scripts/hub_start_canary.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
MARKER = "CANARY-OK:reader-test"


def frame():
    # Exact NativeToolResultItem contract from the producer's synthetic fixture.
    return {"type": "item.completed", "item": {"id": "item_1", "type": "native_tool_result",
        "thread_id": "outer-thread", "turn_id": "outer-turn", "observation": {
            "kind": "result", "native_event": "tool_call_result", "native_session_id": "native-session",
            "native_turn_id": "native-turn", "call_id": "actual-call", "tool": "Bash",
            "command": "printf '%s%s\\n' CANARY- OK:reader-test", "content": MARKER,
            "native_success": True, "exit_code": 0, "status": "completed", "timed_out": False,
            "native_truncated": False, "native_original_bytes": len(MARKER.encode()),
            "native_returned_bytes": len(MARKER.encode()), "local_truncated": False,
            "observation_complete": True}}}


def fault():
    return {"type": "item.completed", "item": {"id": "item_fault", "type": "native_tool_result",
        "thread_id": "outer-thread", "turn_id": "outer-turn",
        "observation": {"kind": "invalidated", "reason": "native_stream_unqualified"}}}


def text(*frames):
    return "\n".join(json.dumps(value) for value in frames)


def canary():
    value = mod.Canary.__new__(mod.Canary)
    value.mode, value.marker, value.engine = "full", MARKER, "zai"
    value.prompt = "Print concatenation of CANARY- and OK:reader-test"
    return value


class NativeReturnedTextTests(unittest.TestCase):
    def test_genuine_contract_canonical_return_is_proof(self):
        result = mod.zai_native_return_proof(text(frame()), MARKER)
        self.assertTrue(result["proven"])
        self.assertFalse(result["invalidated"])
        self.assertEqual(result["binding"], ("outer-thread", "outer-turn", "native-session"))
        self.assertTrue(canary().output_has_marker(text(frame()), engine="zai"))

    def test_command_and_prose_preview_and_nested_json_are_not_output(self):
        for content in ["", "printf " + MARKER, "The tool returned " + MARKER, repr(MARKER),
                        json.dumps({"aggregated_output": MARKER}), "<tool_result>" + MARKER + "</tool_result>",
                        MARKER + "\n<system-reminder>extra</system-reminder>", " " + MARKER, MARKER + "\n"]:
            value = frame(); value["item"]["observation"]["content"] = content
            value["item"]["observation"]["native_returned_bytes"] = len(content.encode())
            self.assertFalse(canary().output_has_marker(text(value), engine="zai"))
        for value in [
                {"type": "item.completed", "item": {"type": "agent_message", "text": text(frame())}},
                {"type": "item.started", "item": {"type": "command_execution", "command": MARKER}},
                {"type": "result", "result": text(frame())}]:
            self.assertFalse(canary().output_has_marker(text(value), engine="zai"))
        value = frame(); value["item"]["observation"].update(content="", display=MARKER)
        self.assertFalse(canary().output_has_marker(text(value), engine="zai"))

    def test_independent_success_exit_status_timeout_matrix(self):
        for key, values in {
            "native_success": [None, False, 1, "true"], "exit_code": [None, 1, -1, False, 0.0],
            "status": [None, "failed", "timed_out", "cancelled", "running", "backgrounded", "unknown"],
            "timed_out": [None, True, 0], "native_event": ["tool_call_error", "tool_call_scheduled"]}.items():
            for bad in values:
                value = frame(); value["item"]["observation"][key] = bad
                with self.subTest(key=key, value=bad):
                    self.assertFalse(canary().output_has_marker(text(value), engine="zai"))

    def test_truncation_completeness_and_native_size_matrix(self):
        for key, values in {
            "native_truncated": [None, True, 0], "local_truncated": [None, True, 0],
            "observation_complete": [None, False, 1], "native_original_bytes": [None, -1, True, 2**64, len(MARKER.encode()) + 1],
            "native_returned_bytes": [None, -1, False, 1, 2**64], "native_turn_id": [None, ""]}.items():
            for bad in values:
                value = frame(); value["item"]["observation"][key] = bad
                with self.subTest(key=key, value=bad):
                    self.assertFalse(canary().output_has_marker(text(value), engine="zai"))
        value = frame(); value["item"]["observation"]["native_original_bytes"] = 0
        # Original size is pre-budget/formatted content, not inferred stdout.
        self.assertTrue(canary().output_has_marker(text(value), engine="zai"))

    def test_field_omission_unknown_fields_malformed_and_wrong_tool_refuse(self):
        for key in frame()["item"]["observation"]:
            value = frame(); del value["item"]["observation"][key]
            with self.subTest(missing=key):
                self.assertFalse(canary().output_has_marker(text(value), engine="zai"))
        for change in [{"tool": "Read"}, {"command": ""}, {"command": "x\0y"},
                       {"native_session_id": "bad\nid"}, {"call_id": ""}, {"artifact_path": MARKER}]:
            value = frame(); value["item"]["observation"].update(change)
            self.assertFalse(canary().output_has_marker(text(value), engine="zai"))
        raw = text(frame())
        self.assertFalse(canary().output_has_marker(raw + "\n{malformed", engine="zai"))
        duplicate = raw.replace('"kind": "result"', '"kind":"result","kind":"result"')
        self.assertFalse(canary().output_has_marker(raw + "\n" + duplicate, engine="zai"))

    def test_replayed_item_or_call_and_contradictory_headers_refuse(self):
        value = frame()
        self.assertFalse(canary().output_has_marker(text(value, value), engine="zai"))
        value2 = copy.deepcopy(value); value2["item"]["id"] = "different-item"
        self.assertFalse(canary().output_has_marker(text(value, value2), engine="zai"))
        self.assertFalse(canary().output_has_marker(text({"type": "thread.started", "thread_id": "foreign"}, value), engine="zai"))
        value2["item"]["observation"]["call_id"] = "different-call"
        for container, key in [("item", "thread_id"), ("item", "turn_id"), ("observation", "native_session_id")]:
            other = copy.deepcopy(value2)
            target = other["item"] if container == "item" else other["item"]["observation"]
            target[key] = "foreign"
            self.assertFalse(canary().output_has_marker(text(value, other), engine="zai"))

    def test_invalidation_anywhere_in_capture_and_across_snapshots_is_sticky(self):
        for frames in [(frame(), fault()), (fault(), frame())]:
            value = canary()
            self.assertFalse(value.output_has_marker(text(*frames), engine="zai"))
            self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
        value = canary()
        self.assertFalse(value.output_has_marker(text(fault()), engine="zai"))
        legacy = {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": MARKER}}
        self.assertFalse(value.output_has_marker(text(legacy), engine="zai"))

    def test_identity_pin_is_only_metadata_and_overlapping_captures_are_not_replay(self):
        value = canary(); first = frame()
        first["item"]["observation"].update(content="actual whoami output", native_returned_bytes=len("actual whoami output".encode()), native_original_bytes=len("actual whoami output".encode()))
        self.assertFalse(value.output_has_marker(text(first), engine="zai"))
        marker = frame(); marker["item"]["id"] = "marker-item"; marker["item"]["observation"]["call_id"] = "marker-call"
        self.assertTrue(value.output_has_marker(text(marker), engine="zai"))
        self.assertTrue(value.output_has_marker(text(marker), engine="zai"))
        foreign = copy.deepcopy(marker); foreign["item"]["observation"]["native_session_id"] = "foreign"
        self.assertFalse(value.output_has_marker(text(foreign), engine="zai"))
        self.assertFalse(value.output_has_marker(text(marker), engine="zai"))

    def test_local_overflow_never_heals_but_incomplete_snapshot_never_stitches(self):
        value = canary(); bad = frame(); bad["item"]["observation"]["local_truncated"] = True
        self.assertFalse(value.output_has_marker(text(bad), engine="zai"))
        self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
        value = canary(); bad = frame(); bad["item"]["observation"]["observation_complete"] = False
        self.assertFalse(value.output_has_marker(text(bad), engine="zai"))
        no_output = frame(); no_output["item"]["observation"].update(content="", native_returned_bytes=0)
        self.assertFalse(value.output_has_marker(text(no_output), engine="zai"))
        # No earlier content is retained and repaired with later completion.
        self.assertFalse(value.output_has_marker(text({"type": "turn.completed"}), engine="zai"))

    def test_whole_capture_and_frame_budgets(self):
        huge = frame(); huge["item"]["observation"]["command"] = "x" * 8193
        self.assertFalse(canary().output_has_marker(text(huge), engine="zai"))
        self.assertFalse(canary().output_has_marker(text(frame()) + "x" * 65536, engine="zai"))
        rows = [{"type": "turn.started"}] * 128 + [frame()]
        self.assertFalse(canary().output_has_marker(text(*rows), engine="zai"))
        value = frame(); value["item"]["observation"]["content"] = "\ud800"
        self.assertFalse(canary().output_has_marker(text(value), engine="zai"))

    def test_new_item_completion_and_identifier_types_are_required(self):
        for kind in ["item.started", "item.updated", "system"]:
            value = frame(); value["type"] = kind
            self.assertFalse(canary().output_has_marker(text(value), engine="zai"))
        for key in ["id", "thread_id", "turn_id"]:
            for bad in [None, True, "", "x" * 257, "bad\nid"]:
                value = frame(); value["item"][key] = bad
                with self.subTest(key=key, bad_type=type(bad).__name__):
                    self.assertFalse(canary().output_has_marker(text(value), engine="zai"))

    def test_partial_native_frames_are_never_repaired_or_healed(self):
        value = canary(); raw = text(frame()); cut = len(raw) // 2
        self.assertFalse(value.output_has_marker(raw[:cut], engine="zai"))
        self.assertFalse(value.output_has_marker(raw[cut:], engine="zai"))
        # A recognizable malformed native frame is UNKNOWN/loss, not an
        # ignorable clipped prefix. A later complete view cannot heal it.
        self.assertFalse(value.output_has_marker(raw, engine="zai"))

    def test_legacy_zai_codex_go_shell_and_engine_isolation_preserved(self):
        legacy = {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": MARKER}}
        for engine in ["zai", "codex"]:
            self.assertTrue(canary().output_has_marker(text(legacy), engine=engine))
        go = {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {"status": "completed", "output": MARKER}}}
        self.assertTrue(canary().output_has_marker(text(go), engine="go"))
        for engine in ["codex", "go", "claude", "antigravity"]:
            self.assertFalse(canary().output_has_marker(text(frame()), engine=engine))
        value = canary(); value.mode = "shell"
        self.assertTrue(value.output_has_marker(MARKER))

    def test_complete_duplicate_key_fault_on_first_line_is_sticky_loss(self):
        original = json.dumps(fault())
        bad = original.replace('"reason": "native_stream_unqualified"',
            '"reason":"native_stream_unqualified","reason":"native_stream_unqualified"')
        self.assertNotEqual(bad, original)
        value = canary()
        self.assertFalse(value.output_has_marker(bad + "\n" + text(frame()), engine="zai"))
        self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))

    def test_malformed_poll_without_native_item_after_new_mode_never_heals(self):
        value = canary(); before = frame()
        before["item"]["observation"].update(content="whoami", native_original_bytes=6, native_returned_bytes=6)
        self.assertFalse(value.output_has_marker(text(before), engine="zai"))
        bad = json.dumps(fault()).replace('"reason": "native_stream_unqualified"',
            '"reason":"native_stream_unqualified","reason":"native_stream_unqualified"')
        malformed_poll = text({"type": "turn.started"}) + "\n" + bad
        self.assertFalse(value.output_has_marker(malformed_poll, engine="zai"))
        self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))

    def test_only_clear_initial_tail_fragments_can_precede_complete_proof(self):
        # A clipped nonproof tail cannot be distinguished from a hidden fault.
        # Retain this old fixture but strengthen it to UNKNOWN, not fake recovery.
        value = canary()
        self.assertFalse(value.output_has_marker('old output tail"}}\n' + text(frame()), engine="zai"))
        self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
        for first in ['{"type":"item.completed","item":',
                      '"type":"native_tool_result","observation":{"kind":"invalidated"}}']:
            value = canary()
            self.assertFalse(value.output_has_marker(first + "\n" + text(frame()), engine="zai"))
            self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))

    def test_recognizable_malformed_native_fault_cannot_choose_legacy_fallback(self):
        bad = json.dumps(fault()).replace('"reason": "native_stream_unqualified"',
            '"reason":"native_stream_unqualified","reason":"native_stream_unqualified"')
        legacy = {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": MARKER}}
        value = canary()
        self.assertFalse(value.output_has_marker(bad + "\n" + text(legacy), engine="zai"))
        self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))


    def test_escaped_duplicate_native_fault_latches_before_legacy_or_new_mode(self):
        bad = json.dumps(fault()).replace('"reason": "native_stream_unqualified"',
            '"reason":"native_stream_unqualified","reason":"native_stream_unqualified"')
        bad = bad.replace('native_tool_result', r'native_tool_\u0072esult').replace('invalidated', r'\u0069nvalidated')
        legacy = text({"type":"item.completed","item":{"type":"command_execution","aggregated_output":MARKER}})
        for first in [bad, bad + "\n" + legacy, text({"type":"turn.started"}) + "\n" + bad]:
            value = canary()
            self.assertFalse(value.output_has_marker(first, engine="zai"))
            self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
            self.assertFalse(value.output_has_marker(legacy, engine="zai"))

    def test_all_decoded_duplicate_discriminators_survive_refusal_recovery(self):
        original = text(fault())
        variants = [
            original.replace('"item":', r'"\u0069tem":').replace('"type": "native_tool_result"', r'"ty\u0070e": "native_tool_\u0072esult"'),
            original.replace('"type": "native_tool_result"', '"type":"native_tool_result","type":"agent_message"'),
            original.replace('"type": "native_tool_result"', '"type":"agent_message","type":"native_tool_result"'),
            original[:-1] + ',"item":{"type":"agent_message","text":"ordinary"}}',
            '{"item":{"type":"agent_message"},' + original[1:],
            original.replace('"kind": "invalidated"', r'"k\u0069nd":"\u0069nvalidated","kind":"result"'),
            original.replace('"kind": "invalidated"', r'"kind":"result","k\u0069nd":"\u0069nvalidated"'),
            original.replace('"kind": "invalidated"', r'"kind" "\u0069nvalidated"'),
        ]
        for raw in variants:
            raw = raw.replace("native_tool_result", r"native_tool_\u0072esult").replace("invalidated", r"\u0069nvalidated")
            with self.subTest(raw=raw):
                value = canary()
                self.assertFalse(value.output_has_marker(raw, engine="zai"))
                self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))

    def test_assistant_strings_are_opaque_to_recovery_not_native_objects(self):
        legacy = {"type":"item.completed","item":{"type":"command_execution","aggregated_output":MARKER}}
        for quoted in [text(fault()), text(frame()), r'{"item":{"ty\u0070e":"native_tool_\u0072esult"}}']:
            assistant = text({"type":"item.completed","item":{"type":"agent_message","text":quoted}})
            self.assertTrue(canary().output_has_marker(assistant + "\n" + text(legacy), engine="zai"))


    def test_structural_array_fault_cannot_fall_back_or_heal_before_native_mode(self):
        legacy = text({"type":"item.completed","item":{"type":"command_execution","aggregated_output":MARKER}})
        for raw in [json.dumps([fault()]), json.dumps([[fault()]]), "[]"]:
            for suffix in ["", "\n" + legacy, "\n" + text(frame())]:
                with self.subTest(raw=raw, suffix=suffix):
                    value = canary()
                    self.assertFalse(value.output_has_marker(raw + suffix, engine="zai"))
                    self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
                    self.assertFalse(value.output_has_marker(legacy, engine="zai"))

    def test_clipped_type_key_quote_is_refusal_only_and_never_heals(self):
        raw = text(fault())
        clipped = raw[raw.index('"type": "native_tool_result"') + 1:]
        escaped = clipped.replace('type"', r'ty\u0070e"', 1).replace('native_tool_result', r'native_tool_\u0072esult')
        legacy = text({"type":"item.completed","item":{"type":"command_execution","aggregated_output":MARKER}})
        for first in [clipped, escaped]:
            for suffix in ["", "\n" + legacy, "\n" + text(frame())]:
                value = canary()
                self.assertFalse(value.output_has_marker(first + suffix, engine="zai"))
                self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
                self.assertFalse(value.output_has_marker(legacy, engine="zai"))


    def test_argon_exact_outer_item_id_and_mid_string_clips_never_heal(self):
        raw = text(fault())
        variants = [raw[1:], raw[raw.index('"item":'):], raw[raw.index('"item":') + 1:],
                    raw[raw.index('"id":') + 1:], raw[raw.index('item.completed'):]]
        legacy = text({"type":"item.completed","item":{"type":"command_execution","aggregated_output":MARKER}})
        for bad in variants:
            with self.subTest(prefix=bad[:24]):
                for suffix in [legacy, text(frame())]:
                    value = canary()
                    self.assertFalse(value.output_has_marker(bad + "\n" + suffix, engine="zai"))
                    self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
                    self.assertFalse(value.output_has_marker(legacy, engine="zai"))

    def test_every_clipping_boundary_before_retained_native_discriminator_refuses(self):
        legacy = text({"type":"item.completed","item":{"type":"command_execution","aggregated_output":MARKER}})
        for original in [fault(), frame()]:
            reordered = copy.deepcopy(original)
            reordered["item"] = {"observation": reordered["item"]["observation"],
                                 **{k:v for k,v in reordered["item"].items() if k != "observation"}}
            root_item_first = {"item": original["item"], "type": original["type"]}
            for event in [original, reordered, root_item_first]:
                raw = text(event)
                escaped = raw.replace('"item"', r'"\u0069tem"').replace('"type"', r'"ty\u0070e"').replace('native_tool_result', r'native_tool_\u0072esult')
                for envelope in [raw, escaped]:
                    discriminator = 'native_tool_result' if 'native_tool_result' in envelope else r'native_tool_\u0072esult'
                    stop = envelope.index(discriminator)
                    for cut in range(1, stop + 1):
                        bad = envelope[cut:]
                        with self.subTest(event=original["item"]["observation"]["kind"], cut=cut, escaped=envelope==escaped):
                            value = canary()
                            self.assertFalse(value.output_has_marker(bad + "\n" + legacy, engine="zai"))
                            self.assertFalse(value.output_has_marker(text(frame()), engine="zai"))
                            self.assertFalse(value.output_has_marker(legacy, engine="zai"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
