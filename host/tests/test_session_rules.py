"""Session rules preamble: no host-wide artificial load (intake f31b678f)."""
from __future__ import annotations

from agent_hub_host.session_rules import CPU_LOAD_RULE, SESSION_PREAMBLE, compose_prompt


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
