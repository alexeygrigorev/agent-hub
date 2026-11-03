"""Session rules preamble: no host-wide artificial load (intake f31b678f)."""
from __future__ import annotations

from pathlib import Path

from agent_hub_host.session_rules import (
    AWS_MAIN_TEMP_RULE,
    CONTAINER_HYGIENE_RULE,
    CPU_LOAD_RULE,
    GOAL_CHAR_LIMIT,
    SESSION_PREAMBLE,
    compose_prompt,
    launch_goal,
)


def test_preamble_forbids_host_wide_artificial_load():
    """AC1: the trusted brief preamble carries the no-host-wide-load rule."""
    assert "Hub session rules" in SESSION_PREAMBLE
    assert "Never generate artificial host-wide CPU or memory load" in SESSION_PREAMBLE
    for pattern in ("busy loops per nproc", "stress-ng", "yes>/dev/null"):
        assert pattern in CPU_LOAD_RULE


def test_preamble_requires_capped_in_cgroup_flake_checks():
    """AC1: load-dependent checks are capped, in-cgroup and bounded."""
    assert "own cgroup" in CPU_LOAD_RULE
    assert "CPUQuota" in CPU_LOAD_RULE
    assert "bounded duration" in CPU_LOAD_RULE
    assert "cleaned up" in CPU_LOAD_RULE


def test_compose_prompt_keeps_rules_in_front_of_brief():
    brief = "Do the work."
    composed = compose_prompt(brief)
    assert composed.startswith(SESSION_PREAMBLE + "\n\n" + brief)
    assert CPU_LOAD_RULE in composed
    # The existing AWS rule stays part of the preamble.
    assert "main-temp AWS profile" in composed
    assert "commit-msg hook" in SESSION_PREAMBLE
    assert "Scope: gray <intake-UUID>" in SESSION_PREAMBLE


def test_oversized_goal_uses_private_full_brief_file(tmp_path):
    brief = "Acceptance criteria: " + ("x" * 5000)
    goal = launch_goal("Hub prepared for intake abcdef12: /repo\n", brief, "offer-long",
                       brief_dir=tmp_path / "goal-briefs")

    assert len(goal) <= GOAL_CHAR_LIMIT
    assert goal.startswith("/goal Hub prepared for intake abcdef12: /repo\n")
    assert "Hub session rules and the full brief are in " in goal
    pointer_start = goal.index("Hub session rules and the full brief are in ") + len(
        "Hub session rules and the full brief are in ")
    pointer_end = goal.index(". Read that file completely", pointer_start)
    brief_path = Path(goal[pointer_start:pointer_end])
    assert brief_path.is_file()
    assert brief_path.read_text() == compose_prompt(brief) + "\n" + CONTAINER_HYGIENE_RULE
    assert brief_path.stat().st_mode & 0o777 == 0o600


def test_short_goal_stays_inline(monkeypatch):
    # The shared preamble is larger than the engine goal limit, so compact it
    # here to exercise the inline path. Full-preamble behavior (pointer for
    # every realistic goal) is covered by the test below.
    import agent_hub_host.session_rules as session_rules
    monkeypatch.setattr(session_rules, 'SESSION_PREAMBLE', 'Hub session rules:\n' + AWS_MAIN_TEMP_RULE)
    goal = launch_goal("intake abcdef12\n", "Fix the issue", "offer-short")
    assert len(goal) <= GOAL_CHAR_LIMIT
    assert goal.startswith("/goal intake abcdef12\n" + session_rules.SESSION_PREAMBLE + "\n\nFix the issue")


def test_full_preamble_short_goal_uses_pointer_within_limit(tmp_path):
    """With the real operator preamble (larger than the engine goal limit),
    even a trivial brief goes through the private-brief pointer, and the
    /goal itself still respects GOAL_CHAR_LIMIT (intake 7970260d AC1)."""
    goal = launch_goal("intake abcdef12\n", "Fix the issue", "offer-short-real",
                       brief_dir=tmp_path / "goal-briefs")
    assert len(goal.encode("utf-16-le")) // 2 <= GOAL_CHAR_LIMIT
    assert "Hub session rules and the full brief are in " in goal
    pointer_start = goal.index("Hub session rules and the full brief are in ") + len(
        "Hub session rules and the full brief are in ")
    pointer_end = goal.index(". Read that file completely", pointer_start)
    brief_path = Path(goal[pointer_start:pointer_end])
    assert brief_path.read_text() == compose_prompt("Fix the issue") + "\n" + CONTAINER_HYGIENE_RULE


def test_goal_limit_counts_non_bmp_text_safely():
    goal = launch_goal("intake abcdef12\n", "🙂" * 700, "offer-unicode")
    assert len(goal.encode("utf-16-le")) // 2 <= GOAL_CHAR_LIMIT
    assert "Hub session rules and the full brief are in " in goal
