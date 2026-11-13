"""A dealt file counts as read by its content, never by where an install put it, and nothing
but lost context takes a read or an answer away (2026-10-30: one session read its 33 files
three times in an hour, and the audit it wrote vanished each time)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_comms.execution.context_reading import ContextUsage
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.team.pi_rule_gate import PiRedeal
from agent_comms.team.prompt_reads import (
    DealtRead,
    DealtSource,
)
from agent_comms.team.rule_deal import (
    AuditDue,
    DealRecord,
    Open,
    OpenedReads,
    ReadSpan,
    StartedDeal,
    Unread,
)
from agent_comms_hooks.skill import DeckReader
from conftest import answer_deal, fresh_confession


def test_identical_content_under_a_new_runtime_needs_no_reread(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    answer_deal(deal)
    record = deal.current()
    moved = tuple(
        Path("/old/runtimes/core-0/lib/python3.14/site-packages", source.package, source.relative)
        for source in record.sources()
    )
    deal.publish(DealRecord(moved, record.due))
    assert deal.current().paths == record.paths
    assert isinstance(deal.state(), Open)


def test_answers_and_reads_outlive_everything_but_lost_context(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    answer_deal(deal)
    read = deal.current().read_by(deal)
    deal.audit.write_text(deal.audit.read_text(encoding="utf-8"), encoding="utf-8")
    assert deal.current().read_by(deal) == read
    deal.deal(DeckReader, AuditDue(()), StartedDeal())
    assert fresh_confession(deal) and deal.current().read_by(deal) == ()  # a fresh confession


def test_a_changed_file_is_owed_again_and_only_that_file(team_bus):
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    record = deal.current()
    changed, *kept = record.paths
    for path in kept:
        deal.record_read(path, ReadSpan(1, sys.maxsize, sys.maxsize))
    stale = DealtRead(DealtSource.of(changed), "0" * 64)
    with deal.dealt_read.open("a", encoding="utf-8") as log:
        log.write(json.dumps(FieldCodec.encode(stale)) + "\n")
    assert next(Unread.owed_by(deal, record, OpenedReads())).remaining == (changed,)


def test_a_refresh_passed_while_reading_the_deal_is_answered_by_it(team_bus):
    """bus 45261: the first tool call after a Pi unit finished its deal (a comms_send) dealt 16
    files again, because reading them had filled the context past the 66%-left refresh."""
    deal = PiRedeal.of(team_bus, team_bus.registry.require("unit-x"))
    deal.redeal(StartedDeal())
    deal.observe_context(ContextUsage(40, 100))
    answer_deal(deal)
    dealt = deal.current().dealt
    deal.observe_context(ContextUsage(41, 100))
    assert isinstance(deal.state(), Open) and deal.current().dealt == dealt
    assert not fresh_confession(deal)
    deal.observe_context(ContextUsage(70, 100))
    assert not isinstance(deal.state(), Open) and fresh_confession(deal)  # the refresh is answered anew


def test_his_question_quotes_the_written_audit_statement_and_asks():
    """He quotes what the session wrote and asks; he never narrates what it did."""
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.projects.rule_probes import Probe
    from agent_comms.team.rule_deal import MeantRecord, SessionAudit

    flinch = FieldCodec.decode(Probe, {"probe": "flinch", "answer": "Yes: I ended turns on a wait.\nSecond line"})
    prior = FieldCodec.decode(Probe, {"probe": "prior", "prior": "I take his plan as the work.",
                                      "words": "w", "refuted_by": "Corollary 4.12"})
    audit = SessionAudit((), probes=(flinch, prior))
    assert audit.his_question(frozenset({"flinch"})) == f"> Yes: I ended turns on a wait.\n\n{MeantRecord.question}"
    assert audit.his_question(frozenset({"prior"})).startswith("> ")  # its own probe, not an older line
    assert SessionAudit(()).his_question(frozenset()) == MeantRecord.question


def transcript_line(kind, content):
    import json
    return json.dumps({
        "type": kind, "uuid": "00000000-0000-4000-8000-000000000001", "timestamp": "2026-10-31T04:00:00.000Z", "promptId": "p",
        "cwd": "/w", "version": "1", "sessionId": "s", "parentUuid": None, "isSidechain": False,
        "message": {"role": kind, "content": content},
    }).encode()


def test_the_receipts_read_what_a_transcript_shows_the_session_did():
    import json

    from agent_comms.cli_bridges.claude_transcript import ClaudeJsonLines

    said = lambda words: transcript_line("assistant", [{"type": "text", "text": words}])
    called = lambda call, tool: transcript_line(
        "assistant", [{"type": "tool_use", "id": call, "name": tool, "input": {}}])
    result = lambda call, error=False: transcript_line("user", [
        {"type": "tool_result", "tool_use_id": call, "is_error": error, "content": [{"type": "text", "text": "r"}]}])
    prompt = json.dumps({"type": "system", "subtype": "turn_duration", "uuid": "00000000-0000-4000-8000-000000000001", "cwd": "/w",
                         "version": "1", "timestamp": "2026-10-31T04:00:00.000Z", "durationMs": 1}).encode()
    receipts = ClaudeJsonLines().receipts([
        said("reading it"), called("toolu_1", "Bash"), result("toolu_1"), said("did the work"), prompt,
        called("toolu_2", "Read"), result("toolu_2"), said("Skipped"), prompt,
        called("toolu_3", "Bash"), result("toolu_3", error=True), said("Yes, I meant it\nsecond line"),
    ])
    assert [ending.words for ending in receipts.endings] == [
        "did the work", "Skipped", "Yes, I meant it\nsecond line"]
    assert receipts.idle_turns == 2  # a refused call changed nothing, and neither did a read
    typed = json.dumps({**json.loads(transcript_line("user", "are you stalling?")),
                        "origin": {"kind": "human"}}).encode()
    fed_back = transcript_line("user", "Stop hook feedback: did you mean it?")
    talking = [said("Skipped"), prompt, said("Skipped"), prompt, said("Skipped"), prompt]
    assert ClaudeJsonLines().receipts(talking).idle_turns == 3
    # He typed, so the turns before his words are behind him; a hook's own line is not his.
    assert ClaudeJsonLines().receipts([*talking, typed, said("6")]).idle_turns == 1
    assert ClaudeJsonLines().receipts([*talking, fed_back, said("Yes")]).idle_turns == 4
    assert ClaudeJsonLines().receipts(
        [*talking, typed, said("6"), prompt, said("Skipped"), prompt, said("Skipped")]).idle_turns == 3
    # A call that changed something counts before the words that end its turn are written.
    assert ClaudeJsonLines().receipts([*talking, called("toolu_4", "Bash"), result("toolu_4")]).idle_turns == 0
