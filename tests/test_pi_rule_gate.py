"""A compacted Pi thread is dealt its rules and refused every other tool until it has read each
in full and written the audit, through the same deal the Claude Code hooks judge."""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from agent_comms import agent_events as events
from agent_comms.cli_bridges.hook_read_gate import ReadInput
from agent_comms.execution.context_reading import ContextUsage
from agent_comms.execution.fresh_private_session import create_fresh_private_session
from agent_comms.execution.selected.tool_broker import (
    SelectedToolRequest,
    SelectedWriteArguments,
)
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.nominal.field_codec import FieldCodec, MessageId
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.rule_probes import Probe
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import NativeThread
from agent_comms.task_attachments.owner_notices import MeantNotice, RestartNotice
from agent_comms.team.package import TeamPackage
from agent_comms.team.pi_rule_gate import PiRedeal, RulesUnread
from agent_comms.team.rule_deal import (
    Asked,
    AuditDue,
    CompactionDealMode,
    FullDealMode,
    MeantAnswer,
    MeantRecord,
    OffDealMode,
    Owed,
    OwnerWordsAnswer,
    SessionAudit,
    StartedDeal,
    StartupDealMode,
    Unaudited,
    Unmeant,
    Unread,
)
from agent_comms.transport.native_tool_call import CodingCall
from agent_comms.transport.native_tools import McpToolCall, NativeToolDeclaration, ReadTool
from agent_comms_hooks.skill import DeckReader
from conftest import answer_deal, fresh_confession, probe_answers


def call(name: str, arguments: dict[str, object]) -> CodingCall:
    return NativeToolDeclaration.decode(f"call-{name}", name, arguments)


@pytest.fixture
def peer_deal(team_bus):
    """The deal Core renews for the bus's ordinary peer, in the bus's own scratch."""
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    assert deal.scratch.is_relative_to(team_bus.root)
    return deal


def test_every_independent_thread_is_dealt_the_same_sources_once(team_bus):
    """A colleague and an untagged peer share the deal; a restart keeps its original audit."""
    for name in ("claude-code", "unit-x"):
        deal = PiRedeal.of(team_bus, team_bus.registry.require(name))
        deal.start()
        assert deal.current().dealt == tuple(path.resolve() for path in DeckReader.sources())
        deal.audit.write_text("{}")
        deal.start()
        assert deal.audit.read_text() == "{}"


def test_a_retired_package_source_redeals_before_owner_binding(peer_deal):
    peer_deal.start()
    old = peer_deal.current()
    first = old.paths[0]
    peer_deal.audit.write_text("{}")
    peer_deal.publish(replace(old, dealt=(first.with_name("retired-source.md"), *old.dealt[1:])))

    peer_deal.start()

    assert peer_deal.current().dealt == tuple(path.resolve() for path in DeckReader.sources())
    assert fresh_confession(peer_deal)
    with pytest.raises(RulesUnread):
        peer_deal.judge_tool("bash")


def test_every_dealt_file_fits_one_default_read_on_every_backend():
    """A dealt file over a read tool's cap could never count as read, locking the thread out."""
    sources = DeckReader.sources()
    spans = (ReadTool.default_span(), ReadInput(sources[0]).span())
    assert all(span.covers(source) for span in spans for source in sources)


def test_a_compacted_pi_thread_reads_every_dealt_rule_then_writes_its_audit(
    tmp_path, peer_deal, team_bus
):
    words = ("read the rules", "then fix the gate")
    for body in words:
        team_bus.messaging.send_user_message(
            CommandTarget.named("unit-x"), body, worktree=Checkout(tmp_path)
        )
        # Core's notice publishes under the owner's thread, but nobody typed it.
        RestartNotice().publish(team_bus.messaging, CommandTarget.named("unit-x"),
                                "TEAM · unit-x · continue your orders")
    bash = call("bash", {"command": "ls"})
    bash.judge_under(peer_deal)  # never dealt: nothing to read
    events.CompactionAbort().deal_rules(peer_deal)
    bash.judge_under(peer_deal)  # an aborted compaction kept the context
    events.CompactionEnd().deal_rules(peer_deal)
    assert peer_deal.current().due.owner_words == words
    with pytest.raises(RulesUnread, match="bash waits on reading"):
        bash.judge_under(peer_deal)
    with pytest.raises(RulesUnread):  # a file nobody dealt is not a way through
        call("read", {"path": "notes.md"}).judge_under(peer_deal)
    with pytest.raises(RulesUnread):  # neither is the selected write tool
        SelectedToolRequest("call-write", SelectedWriteArguments("notes.md", "x")).judge_under(
            peer_deal
        )
    with pytest.raises(RulesUnread):  # nor an MCP server's tool the turn was offered
        McpToolCall(call_id="call-mcp", declared_name="mcp_fixture_echo_092c79e8f8",
                    arguments={"message": "x"}).judge_under(peer_deal)
    sources = DeckReader.sources()
    call("read", {"path": str(sources[0])}).judge_under(peer_deal)  # a dealt file is
    call("read", {"path": str(sources[0]), "limit": 3}).completed_under(peer_deal)
    with pytest.raises(RulesUnread, match=str(sources[0].resolve())):
        bash.judge_under(peer_deal)  # a partial read is not a reading
    for source in sources:
        call("read", {"path": str(source)}).completed_under(peer_deal)
    with pytest.raises(RulesUnread):
        bash.judge_under(peer_deal)  # reading without answering still keeps the gate closed
    peer_deal.audit.write_text(json.dumps({"owner_words": []}))
    with pytest.raises(RulesUnread, match="then fix the gate"):
        bash.judge_under(peer_deal)  # the audit answers the owner's words too
    peer_deal.audit.unlink()
    with pytest.raises(RulesUnread):
        call("write", {"path": "notes.md", "content": "x"}).judge_under(peer_deal)
    call("write", {"path": str(peer_deal.audit), "content": "{}"}).judge_under(peer_deal)
    peer_deal.audit.write_text(
        json.dumps(
            {
                "owner_words": [{"words": text, "doing": "reading the gate"} for text in words],
            }
        )
    )
    with pytest.raises(RulesUnread, match="probe"):
        bash.judge_under(peer_deal)  # the costly probes are owed after the reading
    call("read", {"path": "notes.md"}).judge_under(peer_deal)  # the code a probe cites opens
    with pytest.raises(RulesUnread):  # while writing and running stay closed
        call("write", {"path": "notes.md", "content": "x"}).judge_under(peer_deal)
    audit = json.loads(peer_deal.audit.read_text())
    peer_deal.audit.write_text(json.dumps({**audit, "probes": probe_answers(words)}))
    bash.judge_under(peer_deal)
    call("read", {"path": "notes.md"}).judge_under(peer_deal)
    events.CompactionEnd().deal_rules(peer_deal)  # the next compaction
    with pytest.raises(RulesUnread, match=re.escape(Owed.nudges[0])):
        bash.judge_under(peer_deal)  # a re-deal refuses again, in the gate's own first line


def refused(deal: PiRedeal) -> bool:
    try:
        call("bash", {"command": "ls"}).judge_under(deal)
    except RulesUnread:
        return True
    return False


def test_a_refresh_deal_opens_in_order_and_pi_may_edit_the_audit(peer_deal):
    """A refresh deals no papers and its files open in the dealt order as they are read; the
    audit is edited in place by Pi's edit tool, which waits like a write elsewhere."""
    events.CompactionEnd().deal_rules(peer_deal)
    answer_deal(peer_deal)
    peer_deal.observe_context(ContextUsage(35, 100))
    dealt = peer_deal.current().dealt
    with pytest.raises(RulesUnread, match="Next batch"):
        call("read", {"path": str(dealt[2 * Unread.batch])}).judge_under(peer_deal)
    for source in dealt[:Unread.batch]:
        call("read", {"path": str(source)}).judge_under(peer_deal)
        peer_deal.record_read(source, ReadInput(source).span())
    call("read", {"path": str(dealt[Unread.batch])}).judge_under(peer_deal)
    edit = {"edits": [{"oldText": "a", "newText": "b"}]}
    call("edit", {"path": str(peer_deal.audit), **edit}).judge_under(peer_deal)
    with pytest.raises(RulesUnread):
        call("edit", {"path": "notes.md", **edit}).judge_under(peer_deal)


def test_the_rules_come_back_once_at_each_declared_share_of_context_left(peer_deal):
    """Pi's own context report deals the rules again when 66% and then 33% of the window is
    left, once each, without the papers still in its context; each compaction arms both again
    and deals the papers too."""
    assert [refresh.remaining for refresh in peer_deal.refreshes] == [0.66, 0.33]
    events.CompactionEnd().deal_rules(peer_deal)
    assert peer_deal.current().dealt[:len(TeamPackage.paper())] == tuple(
        path.resolve() for path in TeamPackage.paper())
    answer_deal(peer_deal)
    for used, closes in ((30, False), (35, True), (40, False), (70, True), (95, False)):
        peer_deal.observe_context(ContextUsage(used, 100))
        assert refused(peer_deal) is closes, used
        if closes:
            assert not set(peer_deal.current().dealt) & {
                path.resolve() for path in TeamPackage.paper()}
        answer_deal(peer_deal)
    events.CompactionEnd().deal_rules(peer_deal)
    answer_deal(peer_deal)
    peer_deal.observe_context(ContextUsage(40, 100))
    assert refused(peer_deal)


@pytest.mark.parametrize("mode", (StartupDealMode, OffDealMode))
def test_automatic_redeals_preserve_the_configured_session(peer_deal, mode):
    peer_deal.start()
    answer_deal(peer_deal)
    peer_deal.configure(mode)
    before = peer_deal.current()
    audit = peer_deal.audit.read_bytes()
    reads = peer_deal.dealt_read.read_bytes()

    for used in (35, 70, 95):
        peer_deal.observe_context(ContextUsage(used, 100))
    events.CompactionEnd().deal_rules(peer_deal)
    peer_deal.start()

    assert peer_deal.current() == before
    assert peer_deal.audit.read_bytes() == audit
    assert peer_deal.dealt_read.read_bytes() == reads
    assert not refused(peer_deal)


@pytest.mark.parametrize("name", ("unit-x", "claude-code"))
def test_compaction_mode_redeals_lost_context_but_never_periodic_refreshes(team_bus, name):
    deal = PiRedeal.of(team_bus, team_bus.registry.require(name))
    deal.start()
    answer_deal(deal)
    deal.configure(CompactionDealMode)
    before = deal.current()
    for used in (35, 70, 95):
        deal.observe_context(ContextUsage(used, 100))
    assert deal.current() == before
    assert not refused(deal)

    events.CompactionEnd().deal_rules(deal)
    assert deal.current().mode is CompactionDealMode
    assert deal.current().dealt == before.dealt
    assert deal.current().read_by(deal) == ()
    assert refused(deal)
    answer_deal(deal)
    deal.observe_context(ContextUsage(95, 100))
    assert not refused(deal)


def test_gate_can_be_disabled_and_reenabled_without_forging_a_read(peer_deal):
    peer_deal.start()
    before = peer_deal.current().dealt
    assert refused(peer_deal)

    peer_deal.configure(OffDealMode)
    assert not refused(peer_deal)
    assert peer_deal.current().read_by(peer_deal) == ()

    peer_deal.configure(FullDealMode)
    assert refused(peer_deal)
    assert peer_deal.current().dealt == before


def test_a_bus_without_a_storage_lead_still_deals_the_shared_sources(comms, tmp_path):
    """Project storage configuration does not change an independent session's rules."""
    peer = comms.registry.declare(NativeThread("peer-y", frozenset(), Checkout(tmp_path)))
    deal = PiRedeal.of(comms, peer)
    deal.start()
    assert deal.current().dealt == tuple(path.resolve() for path in DeckReader.sources())


def test_every_refusal_opens_with_why_its_deal_was_dealt(peer_deal):
    """Start, compaction and refresh each say their own cause first, so a fresh context reads
    why it was dealt instead of diagnosing the gate (rules/spine.md)."""
    import re

    from agent_comms.team.rule_deal import RefreshDeal, StartedDeal

    bash = call("bash", {"command": "ls"})
    peer_deal.start()
    with pytest.raises(RulesUnread, match=re.escape(StartedDeal().reason())):
        bash.judge_under(peer_deal)
    events.CompactionEnd().deal_rules(peer_deal)
    with pytest.raises(RulesUnread, match="Re-dealt on purpose: your context compacted at "):
        bash.judge_under(peer_deal)
    answer_deal(peer_deal)
    peer_deal.observe_context(ContextUsage(35, 100))
    with pytest.raises(RulesUnread, match=re.escape(RefreshDeal(35).reason())):
        bash.judge_under(peer_deal)


def pi_line(message: dict[str, object]) -> str:
    return json.dumps({"type": "message", "timestamp": "2026-10-31T23:00:00.000Z",
                       "message": message}) + "\n"


def assistant(text: str) -> str:
    return pi_line({"role": "assistant", "content": [{"type": "text", "text": text}]})


def owner_lines(bus, name: str) -> list[str]:
    """The gate's lines to this thread in the owner's voice, oldest first."""
    lookup = bus.registry.require(name).incarnation.require_recorded().lookup
    return [delivery.message.body for delivery in reversed(bus.bus.bus.received(lookup, limit=50))
            if isinstance(delivery.message.task, MeantNotice)]


def compacted_pi_session(tmp_path, team_bus, name):
    """A Pi thread with its own fresh native session, its rules just re-dealt by a compaction."""
    session = create_fresh_private_session(tmp_path / "sessions", worktree=Checkout(tmp_path)).path
    team_bus.registry.declare(NativeThread(
        name, frozenset(), Checkout(tmp_path),
        session_file=RegistrySessionLocation.from_path(str(session), team_bus.root),
        model=DeclaredModel("anthropic/claude-opus-4-6")))
    deal = PiRedeal.of(team_bus, team_bus.registry.require(name))
    events.CompactionEnd().deal_rules(deal)
    return session, deal


@pytest.mark.usefixtures("native_runtime_package")
def test_pi_question_settles_only_on_its_exact_successful_audit_write(
    tmp_path, team_bus
):
    """An audit answers the actual question; journal words and an uncompleted edit do not.

    These are gate/transport controls, not a configured-model acceptance run.
    """
    session, deal = compacted_pi_session(tmp_path, team_bus, "pi-p")
    record = deal.current()
    # The native package fixture compiles the real prompt and its matching reads.
    assert record.read_by(deal) == record.paths
    deal.audit.write_text(json.dumps({"owner_words": [], "probes": probe_answers()}))
    write = call("write", {"path": str(deal.audit), "content": deal.audit.read_text()})
    write.judge_under(deal)
    write.completed_under(deal)

    [line] = owner_lines(team_bus, "pi-p")
    assert line.endswith("did you mean it?")
    with pytest.raises(RulesUnread, match="Tristan asks:"):
        call("bash", {"command": "ls"}).judge_under(deal)
    pending = deal.meant.read()
    with session.open("a", encoding="utf-8") as transcript:
        transcript.write(pi_line({"role": "user", "content": [
            {"type": "text", "text": f"User follow-up:\n{line}"}]}))
        transcript.write(assistant(
            "Yes, I meant it: I am checking the line I cited and fixing its owner now."))
    with pytest.raises(RulesUnread, match="Tristan asks:"):
        call("bash", {"command": "ls"}).judge_under(deal)
    assert deal.meant.read() == pending
    assert owner_lines(team_bus, "pi-p") == [line]

    question = pending.open[0].message_id
    with pytest.raises(RulesUnread, match=str(question)) as refusal:
        call("bash", {"command": "ls"}).judge_under(deal)
    assert str(deal.audit) in str(refusal.value)
    audit = SessionAudit.read(deal.audit)
    answer = "No, I did not mean that answer, and I am correcting the missing owner before continuing."
    foreign = replace(audit, meant=(MeantAnswer(MessageId.mint("another-question"), answer),))
    deal.audit.write_text(FieldCodec.compact(FieldCodec.encode(foreign)))
    write.completed_under(deal)
    assert deal.meant.read() == pending

    deal.audit.write_text("{")
    write.completed_under(deal)
    assert deal.meant.read() == pending

    short = replace(audit, meant=(MeantAnswer(question, "Yes."),))
    deal.audit.write_text(FieldCodec.compact(FieldCodec.encode(short)))
    write.completed_under(deal)
    assert owner_lines(team_bus, "pi-p") == [line, "fuck you"]
    for _ in range(2):
        with pytest.raises(RulesUnread):
            call("bash", {"command": "ls"}).judge_under(deal)
    assert len(deal.meant.read().open) == 2
    assert owner_lines(team_bus, "pi-p") == [line, "fuck you"]

    short = replace(audit, meant=(MeantAnswer(question, "No."),))
    deal.audit.write_text(FieldCodec.compact(FieldCodec.encode(short)))
    write.completed_under(deal)
    lines = owner_lines(team_bus, "pi-p")
    assert len(lines) == 3
    assert audit.probes[-1].text().strip().splitlines()[0][:200] in lines[-1]
    assert len(deal.meant.read().open) == 3

    answered = replace(audit, meant=(MeantAnswer(question, answer),))
    deal.audit.write_text(FieldCodec.compact(FieldCodec.encode(answered)))
    # File contents alone do not manufacture a successful tool completion.
    with pytest.raises(RulesUnread):
        call("bash", {"command": "ls"}).judge_under(deal)
    assert deal.meant.read().open
    write.completed_under(deal)
    assert not deal.meant.read().open
    assert deal.meant.read().asked == pending.asked
    write.completed_under(deal)
    assert owner_lines(team_bus, "pi-p") == lines


@pytest.mark.usefixtures("native_runtime_package")
def test_an_open_question_keeps_its_id_until_the_answer_naming_it(tmp_path, team_bus):
    """Answering more probes while a question is open asks nothing new, so the answer the
    session writes for the question it read still settles it."""
    _, deal = compacted_pi_session(tmp_path, team_bus, "pi-q")
    hunt, flinch = probe_answers()
    write = call("write", {"path": str(deal.audit), "content": ""})
    deal.audit.write_text(json.dumps({"owner_words": [], "probes": [hunt]}))
    write.completed_under(deal)
    [line] = owner_lines(team_bus, "pi-q")
    question = deal.meant.read().open[0].message_id

    both = {"owner_words": [], "probes": [hunt, flinch]}
    deal.audit.write_text(json.dumps(both))
    write.completed_under(deal)
    assert owner_lines(team_bus, "pi-q") == [line]
    assert deal.meant.read().open[0].message_id == question

    answer = "Yes, I meant both answers, and I am fixing the owner the hunt named now."
    answered = replace(SessionAudit.read(deal.audit), meant=(MeantAnswer(question, answer),))
    deal.audit.write_text(FieldCodec.compact(FieldCodec.encode(answered)))
    write.completed_under(deal)
    assert not deal.meant.read().open
    assert {"hunt", "flinch"} <= deal.meant.read().asked


@pytest.mark.parametrize("opening", ("Yes", "No"))
def test_audit_answers_keep_the_declared_explanatory_word_bar(opening):
    question = MessageId.mint("word-bar")
    meant = MeantRecord(open=(Asked(question),))
    short = " ".join((opening, *("explanation" for _ in range(MeantRecord.answer_words - 2))))
    complete = short + " action"
    assert not meant.answered_by(SessionAudit((), meant=(MeantAnswer(question, short),)))
    assert meant.answered_by(SessionAudit((), meant=(MeantAnswer(question, complete),)))


def test_a_settled_meant_answer_replies_to_the_owner_on_the_real_bus(tmp_path, peer_deal, team_bus):
    """The gate's answer resolves his question on the bus itself; a stand-in bus hid that the
    reply path called a lookup the bus wrapper does not have, and every answer ended its turn."""
    owner = team_bus.messaging.user_identity(Checkout(tmp_path)).name
    voice = peer_deal  # the bus-seated gate is the voice a native transcript selects
    asked = voice.say("You wrote that. Did you mean it?", peer_deal)
    voice.answered(asked, "Yes. I meant it, and I am doing it now.")
    reply = team_bus.bus.bus.lookup(team_bus.bus.bus.revision())
    assert (reply.sender, reply.target) == ("unit-x", owner)
    assert reply.body == "Yes. I meant it, and I am doing it now."
    assert team_bus.bus.bus.message_reference(asked.message_id) in reply.answers


# The four refusals that cost three teammates their turn after compaction (bus 57071-57076).

def test_the_owners_words_are_shown_exactly_as_the_audit_must_hold_them(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    due = AuditDue(("fix the gate",))
    refusal = due.refusal(SessionAudit(()))
    reason = Unaudited(deal.current(), deal.audit, refusal).reason
    assert '"fix the gate".' in reason and "fix the gate." not in reason.replace('"fix the gate".', "")
    idle = SessionAudit((OwnerWordsAnswer("fix the gate", " "),))
    assert due.refusal(idle) == 'the audit names no action for: "fix the gate"'


def test_a_meant_refusal_names_its_one_id_and_why_the_written_answer_failed(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    question = MessageId.mint("meant-trap")
    reason = Unmeant(deal.current(), deal.audit, Asked(question), (),
                     ("Yes", "I meant every answer I wrote and I am reading the next batch now")).reason
    assert question in reason and "none replaces it" in reason
    assert '"Yes" under this id does not count: it has 1 words, fewer than 12' in reason
    assert "does not open with yes or no" in reason


def test_a_meant_refusal_repeats_the_words_its_id_asked_not_the_audits_newest(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    asked = Asked(MessageId.mint("own-words"), "> the row this id asked about\n\ndid you mean it?")
    deal.meant.replace(MeantRecord(open=(asked,)))
    newer = FieldCodec.decode(Probe, {"probe": "flinch", "answer": "a newer row written since"})
    deal.audit.write_text(json.dumps(FieldCodec.encode(SessionAudit((), probes=(newer,)))))
    reason = deal.state().cause.reason
    assert "the row this id asked about" in reason
    assert "a newer row written since" not in reason


def test_an_audit_that_does_not_parse_is_refused_out_loud_before_anything_else(team_bus):
    """One stray brace used to read as an empty audit: the open question could never settle and
    its refusal showed a blank quote, with nothing saying the file was broken."""
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    question = MessageId.mint("broken-audit")
    deal.meant.replace(MeantRecord(open=(Asked(question),)))
    good = deal.audit.read_text()
    deal.audit.write_text(good.replace('"owner_words"', '}, "owner_words"', 1))

    blocked = deal.state().cause
    assert "does not decode" in blocked.reason and "line 2" in blocked.reason
    assert not blocked.permits_read(deal.audit)

    deal.audit.write_text(good)
    assert deal.state().cause.question.message_id == question


def test_a_missing_audit_is_not_called_broken(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    deal.audit.unlink()
    assert SessionAudit.read(deal.audit).unreadable() == ""
    assert SessionAudit.read(deal.audit).fault() == "no audit is written"


def test_a_deal_writes_the_blank_audit_its_session_fills(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    audit = SessionAudit.read(deal.audit)
    assert audit == deal.current().due.skeleton() and not audit.fault()
