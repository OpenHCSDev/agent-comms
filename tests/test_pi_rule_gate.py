"""A compacted Pi thread is dealt its rules and refused every other tool until it has read each
in full and written the audit, through the same deal the Claude Code hooks judge."""

from __future__ import annotations

import json

import pytest

from agent_comms import agent_events as events
from agent_comms.native_tool_call import CodingCall
from agent_comms.pi_rule_gate import PiRuleGate, RulesUnread
from agent_comms.rule_deal import CommanderSources, DealtSources, FleetSources


def call(name: str, arguments: dict[str, object]) -> CodingCall:
    return CodingCall.decode(f"call-{name}", name, arguments)


@pytest.fixture
def unit_gate(tmp_path, monkeypatch, commander_bus):
    """The scratch-directory gate of the bus's ordinary unit, under a private home."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return PiRuleGate.of(commander_bus, commander_bus.registry.require("unit-x"))


def test_the_registry_role_picks_the_dealt_sources(commander_bus):
    snapshot = commander_bus.registry.snapshot()
    commander, unit = snapshot.require("claude-code"), snapshot.require("unit-x")
    assert type(DealtSources.for_thread(snapshot, commander)) is CommanderSources
    assert type(DealtSources.for_thread(snapshot, unit)) is FleetSources


def test_a_compacted_pi_thread_reads_every_dealt_rule_then_writes_its_audit(tmp_path, unit_gate):
    worktree = tmp_path
    bash = call("bash", {"command": "ls"})
    bash.judge_under(unit_gate, worktree)  # never dealt: nothing to read
    events.CompactionEnd(aborted=True).deal_rules(unit_gate, FleetSources())
    bash.judge_under(unit_gate, worktree)  # an aborted compaction kept the context
    events.CompactionEnd().deal_rules(unit_gate, FleetSources())
    with pytest.raises(RulesUnread, match="You just tried bash before reading. Call #1"):
        bash.judge_under(unit_gate, worktree)
    sources = FleetSources.sources()
    first = call("read", {"path": str(sources[0])})
    first.judge_under(unit_gate, worktree)  # reading is the way through
    call("read", {"path": str(sources[0]), "limit": 3}).completed_under(unit_gate, worktree)
    with pytest.raises(RulesUnread, match=str(sources[0].resolve())):
        bash.judge_under(unit_gate, worktree)  # a partial read is not a reading
    for source in sources:
        call("read", {"path": str(source)}).completed_under(unit_gate, worktree)
    with pytest.raises(RulesUnread, match="before writing the required audit"):
        bash.judge_under(unit_gate, worktree)
    with pytest.raises(RulesUnread):
        call("write", {"path": "notes.md", "content": "x"}).judge_under(unit_gate, worktree)
    call("write", {"path": str(unit_gate.audit), "content": "{}"}).judge_under(unit_gate, worktree)
    unit_gate.audit.write_text(json.dumps({
        "rules": [{"rule": rule.name} for rule in FleetSources.rules()], "owner_words": []}))
    bash.judge_under(unit_gate, worktree)
    events.CompactionEnd().deal_rules(unit_gate, FleetSources())  # the next compaction
    with pytest.raises(RulesUnread, match="Call #1 since this compaction"):
        bash.judge_under(unit_gate, worktree)
