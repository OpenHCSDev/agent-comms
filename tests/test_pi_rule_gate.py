"""A compacted Pi thread is dealt its rules and refused every other tool until it has read each
in full and written the audit, through the same deal the Claude Code hooks judge."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_comms.rule_deal import Unengaged

from agent_comms import agent_events as events
from agent_comms.channel_targets import CommandTarget
from agent_comms.commander.package import CommanderPackage
from agent_comms.context_reading import ContextUsage
from agent_comms.native_tool_call import CodingCall
from agent_comms.native_tools import ReadTool
from agent_comms.pi_rule_gate import PiRedeal, RulesUnread
from agent_comms.repo import Checkout
from agent_comms.selected_tool_broker import SelectedToolRequest, SelectedWriteArguments
from agent_comms.task_attachments.owner_notices import RestartNotice
from agent_comms.threads import Thread
from agent_comms.cli_bridges.hook_read_gate import ReadInput
from agent_comms_hooks.skill import CommanderReader, FleetReader
from conftest import answer_deal, engaged


def call(name: str, arguments: dict[str, object]) -> CodingCall:
    return CodingCall.decode(f"call-{name}", name, arguments)


@pytest.fixture
def unit_deal(commander_bus):
    """The deal Core renews for the bus's ordinary unit, in the bus's own scratch."""
    deal = PiRedeal.of(commander_bus, commander_bus.registry.require("unit-x"))
    assert deal.scratch.is_relative_to(commander_bus.root)
    return deal


def test_a_spawned_thread_is_dealt_its_role_s_deck_sources_once(commander_bus):
    """The first start deals the registry role's deck sources; a restart keeps the deal."""
    for name, reader in (("claude-code", CommanderReader), ("unit-x", FleetReader)):
        deal = PiRedeal.of(commander_bus, commander_bus.registry.require(name))
        deal.start()
        assert deal.current().dealt == tuple(path.resolve() for path in reader.sources())
        deal.audit.write_text("{}")
        deal.start()
        assert deal.audit.exists()


def test_a_retired_package_source_redeals_before_owner_binding(unit_deal):
    unit_deal.start()
    old = unit_deal.current()
    first = old.paths[0]
    unit_deal.audit.write_text("{}")
    unit_deal.publish(replace(old, dealt=(first.with_name("retired-source.md"), *old.dealt[1:])))

    unit_deal.start()

    assert unit_deal.current().dealt == tuple(path.resolve() for path in FleetReader.sources())
    assert not unit_deal.audit.exists()
    with pytest.raises(RulesUnread):
        unit_deal.judge_tool("bash")


def test_every_dealt_file_fits_one_default_read_on_every_backend():
    """A dealt file over a read tool's cap could never count as read, locking the thread out."""
    sources = CommanderReader.sources()
    spans = (ReadTool.default_span(), ReadInput(sources[0]).span())
    assert all(span.covers(source) for span in spans for source in sources)


def test_a_compacted_pi_thread_reads_every_dealt_rule_then_writes_its_audit(
    tmp_path, unit_deal, commander_bus
):
    words = ("read the rules", "then fix the gate")
    for body in words:
        commander_bus.messaging.send_user_message(
            CommandTarget.named("unit-x"), body, worktree=Checkout(tmp_path)
        )
        # Core's notice publishes under the owner's thread, but nobody typed it.
        RestartNotice().publish(commander_bus.messaging, CommandTarget.named("unit-x"),
                                "COMMANDER · unit-x · continue your orders")
    bash = call("bash", {"command": "ls"})
    bash.judge_under(unit_deal)  # never dealt: nothing to read
    events.CompactionAbort().deal_rules(unit_deal)
    bash.judge_under(unit_deal)  # an aborted compaction kept the context
    events.CompactionEnd().deal_rules(unit_deal)
    assert unit_deal.current().due.owner_words == words
    with pytest.raises(RulesUnread, match="You just tried bash before reading. Call #1"):
        bash.judge_under(unit_deal)
    with pytest.raises(RulesUnread):  # a file nobody dealt is not a way through
        call("read", {"path": "notes.md"}).judge_under(unit_deal)
    with pytest.raises(RulesUnread):  # neither is the selected write tool
        SelectedToolRequest("call-write", SelectedWriteArguments("notes.md", "x")).judge_under(
            unit_deal
        )
    sources = FleetReader.sources()
    call("read", {"path": str(sources[0])}).judge_under(unit_deal)  # a dealt file is
    call("read", {"path": str(sources[0]), "limit": 3}).completed_under(unit_deal)
    with pytest.raises(RulesUnread, match=str(sources[0].resolve())):
        bash.judge_under(unit_deal)  # a partial read is not a reading
    for source in sources:
        call("read", {"path": str(source)}).completed_under(unit_deal)
    with pytest.raises(RulesUnread, match="template was in your first refusal"):
        bash.judge_under(unit_deal)  # the whole template is printed once per deal
    answered = [engaged(rule.name, rule.path.resolve()) for rule in FleetReader.rules()]
    unit_deal.audit.write_text(json.dumps({"rules": answered, "owner_words": []}))
    with pytest.raises(RulesUnread, match="then fix the gate"):
        bash.judge_under(unit_deal)  # the audit answers the owner's words too
    unit_deal.audit.unlink()
    with pytest.raises(RulesUnread):
        call("write", {"path": "notes.md", "content": "x"}).judge_under(unit_deal)
    call("write", {"path": str(unit_deal.audit), "content": "{}"}).judge_under(unit_deal)
    unit_deal.audit.write_text(
        json.dumps(
            {
                "rules": [engaged(rule.name, rule.path.resolve()) for rule in FleetReader.rules()],
                "owner_words": [{"words": text, "doing": "reading the gate"} for text in words],
            }
        )
    )
    bash.judge_under(unit_deal)
    call("read", {"path": "notes.md"}).judge_under(unit_deal)
    events.CompactionEnd().deal_rules(unit_deal)  # the next compaction
    with pytest.raises(RulesUnread, match="Call #1 since this deal"):
        bash.judge_under(unit_deal)


def refused(deal: PiRedeal) -> bool:
    try:
        call("bash", {"command": "ls"}).judge_under(deal)
    except RulesUnread:
        return True
    return False


def test_a_refresh_deal_interleaves_like_any_other_and_pi_may_edit_the_audit(unit_deal):
    """A refresh deals no papers, and a batch of its rules is answered before the next opens;
    the audit is edited in place by Pi's edit tool, which waits like a write elsewhere."""
    events.CompactionEnd().deal_rules(unit_deal)
    answer_deal(unit_deal)
    unit_deal.observe_context(ContextUsage(35, 100))
    unit_deal.audit.write_text(json.dumps({"rules": [], "owner_words": []}))
    dealt = unit_deal.current().dealt
    rules = [source for source in dealt if source.stem in unit_deal.current().due.rules]
    batch, following = rules[:Unengaged.batch], rules[Unengaged.batch]
    for source in dealt[:dealt.index(following)]:
        call("read", {"path": str(source)}).judge_under(unit_deal)
        unit_deal.record_read(source, ReadInput(source).span())
    with pytest.raises(RulesUnread, match="does not answer it"):
        call("read", {"path": str(following)}).judge_under(unit_deal)
    first = batch[0]
    call("read", {"path": str(first)}).judge_under(unit_deal)
    edit = {"edits": [{"oldText": "a", "newText": "b"}]}
    call("edit", {"path": str(unit_deal.audit), **edit}).judge_under(unit_deal)
    with pytest.raises(RulesUnread):
        call("edit", {"path": "notes.md", **edit}).judge_under(unit_deal)
    unit_deal.audit.write_text(json.dumps(
        {"rules": [engaged(source.stem, source) for source in batch], "owner_words": []}))
    call("read", {"path": str(following)}).judge_under(unit_deal)


def test_the_rules_come_back_once_at_each_declared_share_of_context_left(unit_deal):
    """Pi's own context report deals the rules again when 66% and then 33% of the window is
    left, once each, without the papers still in its context; each compaction arms both again
    and deals the papers too."""
    assert [refresh.remaining for refresh in unit_deal.refreshes] == [0.66, 0.33]
    events.CompactionEnd().deal_rules(unit_deal)
    assert unit_deal.current().dealt[:len(CommanderPackage.paper())] == tuple(
        path.resolve() for path in CommanderPackage.paper())
    answer_deal(unit_deal)
    for used, closes in ((30, False), (35, True), (40, False), (70, True), (95, False)):
        unit_deal.observe_context(ContextUsage(used, 100))
        assert refused(unit_deal) is closes, used
        if closes:
            assert not set(unit_deal.current().dealt) & {
                path.resolve() for path in CommanderPackage.paper()}
        answer_deal(unit_deal)
    events.CompactionEnd().deal_rules(unit_deal)
    answer_deal(unit_deal)
    unit_deal.observe_context(ContextUsage(40, 100))
    assert refused(unit_deal)


def test_a_bus_with_no_commander_project_deals_a_pi_unit_the_fleet_rules(comms, tmp_path):
    """No commander project leaves every thread commanded: a Pi unit is dealt the fleet deck,
    as the Claude Code hooks deal it on the same bus."""
    unit = comms.registry.declare(Thread("unit-y", frozenset(), Checkout(tmp_path)))
    deal = PiRedeal.of(comms, unit)
    deal.start()
    assert deal.current().dealt == tuple(path.resolve() for path in FleetReader.sources())


def test_every_refusal_opens_with_why_its_deal_was_dealt(unit_deal):
    """Start, compaction and refresh each say their own cause first, so a fresh context reads
    why it was dealt instead of diagnosing the gate (rules/after-compaction.md)."""
    import re

    from agent_comms.rule_deal import RefreshDeal, StartedDeal

    bash = call("bash", {"command": "ls"})
    unit_deal.start()
    with pytest.raises(RulesUnread, match=re.escape(StartedDeal().reason())):
        bash.judge_under(unit_deal)
    events.CompactionEnd().deal_rules(unit_deal)
    with pytest.raises(RulesUnread, match="Re-dealt on purpose: your context compacted at "):
        bash.judge_under(unit_deal)
    answer_deal(unit_deal)
    unit_deal.observe_context(ContextUsage(35, 100))
    with pytest.raises(RulesUnread, match=re.escape(RefreshDeal(35).reason())):
        bash.judge_under(unit_deal)


def test_the_dealt_commander_skill_owes_an_answer_like_a_rule(commander_bus):
    """The commander skill is dealt and read, so it fills the unanswered window like a rule:
    on 2026-10-30 claude-nra read it owing nothing and broke its rotation within the hour."""
    import sys

    from agent_comms.rule_deal import ReadSpan, StartedDeal

    deal = PiRedeal.of(commander_bus, commander_bus.registry.require("claude-code"))
    deal.redeal(StartedDeal())
    record = deal.current()
    skill = CommanderPackage.skill().resolve()
    assert skill.stem in record.due.rules
    order = list(record.dealt)
    before = [source for source in order[:order.index(skill)] if source.stem in record.due.rules]
    unanswered = {*before[-(Unengaged.batch - 1):], skill}
    answered = []
    for source in order[:order.index(skill) + 1]:
        deal.record_read(source, ReadSpan(1, sys.maxsize, sys.maxsize))
        if source.stem in record.due.rules and source not in unanswered:
            answered.append(engaged(source.stem, source))
        deal.audit.write_text(json.dumps({"rules": answered, "owner_words": []}))
    with pytest.raises(RulesUnread, match=f"audit's entry for {skill.stem} does not answer it"):
        deal.judge_read("read", order[order.index(skill) + 1])
