"""Shared native list snapshot: coalescing and freshness bounds (intake 083607ae)."""
import subprocess
import threading
import types

import pytest

from agent_hub_host import hook_command, native_list_cache, native_sessions


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    native_list_cache._entries.clear()
    now = [1000.0]
    monkeypatch.setattr(native_list_cache, "_clock", lambda: now[0])
    monkeypatch.delenv("AGENT_HUB_NATIVE_LIST_TTL_S", raising=False)
    yield now
    native_list_cache._entries.clear()


def _fake_native(monkeypatch, calls):
    row = {"id": "11111111-1111-4111-8111-111111111111", "tag": "t", "workspace": "/w",
           "phase": "running"}
    gone = dict(row, id="22222222-2222-4222-8222-222222222222", tag="g", phase="exited")

    def fake(executable, args, **kw):
        calls.append(list(args))
        import json
        return types.SimpleNamespace(returncode=0, stdout=json.dumps([row, gone]), stderr="")
    monkeypatch.setattr(native_sessions, "run_native", fake)


def test_concurrent_consumers_share_one_subprocess(monkeypatch):
    calls = []
    _fake_native(monkeypatch, calls)
    out = []
    ts = [threading.Thread(target=lambda: out.append(native_sessions.list_sessions("aplexer", include_exited=True)))
          for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and len(out) == 20


def test_live_and_all_views_share_one_snapshot(monkeypatch):
    calls = []
    _fake_native(monkeypatch, calls)
    live = native_sessions.list_sessions("aplexer")
    allr = native_sessions.list_sessions("aplexer", include_exited=True)
    assert [s.tag for s in live] == ["t"] and len(allr) == 2
    assert len(calls) == 1


def test_at_most_one_list_per_ttl_then_refresh(monkeypatch, _reset):
    calls = []
    _fake_native(monkeypatch, calls)
    for _ in range(50):
        native_sessions.list_sessions("aplexer", include_exited=True)
        _reset[0] += 0.19  # 9.5 s of polling
    assert len(calls) == 1
    _reset[0] += 1.0
    native_sessions.list_sessions("aplexer", include_exited=True)
    assert len(calls) == 2


def test_fresh_paths_bounded_to_one_second(monkeypatch, _reset):
    calls = []
    _fake_native(monkeypatch, calls)
    native_sessions.list_sessions("aplexer", include_exited=True)
    _reset[0] += 0.5
    native_sessions.list_sessions("aplexer", include_exited=True, fresh=True)
    assert len(calls) == 1  # within the 1 s bound
    _reset[0] += 0.6
    native_sessions.list_sessions("aplexer", include_exited=True, fresh=True)
    assert len(calls) == 2  # older than the bound: refetched
    native_sessions.list_sessions("aplexer", include_exited=True)
    assert len(calls) == 2  # and the refresh serves ordinary readers too


def test_effects_invalidate_snapshot(monkeypatch):
    seen = []

    def fake_uncached(executable, args, timeout, cwd, env):
        seen.append(args[0])
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(native_sessions, "_run_native_uncached", fake_uncached)
    native_list_cache.list_raw("aplexer", lambda: ["old"])
    native_sessions.run_native("aplexer", ["kill", "--tag", "x"])
    assert native_list_cache.list_raw("aplexer", lambda: ["new"]) == ["new"]


def test_failed_fetch_is_not_cached(monkeypatch):
    n = []

    def boom():
        n.append(1)
        raise RuntimeError("x")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            native_list_cache.list_raw("aplexer", boom)
    assert len(n) == 2


def test_message_log_coalesced_and_invalidated_by_send(monkeypatch, _reset):
    n = []

    def fetch():
        n.append(1)
        return [{"id": 1}]
    for _ in range(5):
        native_list_cache.message_log("aplexer", "/w", fetch)
    assert len(n) == 1
    monkeypatch.setattr(native_sessions, "_run_native_uncached",
                        lambda *a: types.SimpleNamespace(returncode=0, stdout="{}", stderr=""))
    native_sessions.run_native("aplexer", ["message", "send", "--workspace", "/w", "--to", "t", "hi"])
    native_list_cache.message_log("aplexer", "/w", fetch)
    assert len(n) == 2


def test_awareness_hook_throttle_skips_spawn(tmp_path):
    cmd = hook_command.throttle_awareness_command(
        f"touch {tmp_path}/ran # {hook_command.AWARENESS_MARKER}", 60)
    env = {"TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    for _ in range(3):
        subprocess.run(["bash", "-c", cmd], input=b"{}", env=env, cwd=tmp_path, check=True)
    assert (tmp_path / "ran").exists()
    (tmp_path / "ran").unlink()
    subprocess.run(["bash", "-c", cmd], input=b"{}", env=env, cwd=tmp_path, check=True)
    assert not (tmp_path / "ran").exists()  # throttled: original never ran


def test_throttle_only_touches_post_tool_use_awareness():
    t = {"hooks": {"PostToolUse": [{"hooks": [{"command": f"x # {hook_command.AWARENESS_MARKER}"}]}],
                   "SessionStart": [{"hooks": [{"command": f"x # {hook_command.AWARENESS_MARKER}"}]}]}}
    out = hook_command.throttle_post_tool_use_hooks(t)
    assert "BASH_VERSION" in out["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert out["hooks"]["SessionStart"] == t["hooks"]["SessionStart"]
    assert hook_command.throttle_post_tool_use_hooks(out) == out  # idempotent
