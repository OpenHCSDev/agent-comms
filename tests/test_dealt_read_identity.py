"""A dealt file counts as read by its content, never by where an install put it, and nothing
but lost context takes a read or an answer away (2026-10-30: one session read its 33 files
three times in an hour, and the audit it wrote vanished each time)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_comms.context_reading import ContextUsage
from agent_comms.rule_deal import DealRecord, DealtRead, DealtSource, Open, ReadSpan, Unread
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_rule_gate import PiRedeal
from agent_comms_hooks.skill import FleetReader
from conftest import answer_deal


def test_identical_content_under_a_new_runtime_needs_no_reread(commander_bus):
    deal = PiRedeal.of(commander_bus, commander_bus.registry.require("unit-x"))
    deal.redeal()
    answer_deal(deal)
    record = deal.current()
    moved = tuple(
        Path("/old/runtimes/core-0/lib/python3.14/site-packages", source.package, source.relative)
        for source in record.sources()
    )
    deal.publish(DealRecord(moved, record.due))
    assert deal.current().paths == record.paths
    assert isinstance(deal.state(), Open)


def test_answers_and_reads_outlive_everything_but_lost_context(commander_bus):
    deal = PiRedeal.of(commander_bus, commander_bus.registry.require("unit-x"))
    deal.redeal()
    answer_deal(deal)
    read = deal.current().read_by(deal)
    deal.audit.write_text(deal.audit.read_text(encoding="utf-8"), encoding="utf-8")
    assert deal.current().read_by(deal) == read
    deal.deal(FleetReader, ())
    assert deal.audit.exists() and deal.current().read_by(deal) == ()


def test_a_changed_file_is_owed_again_and_only_that_file(commander_bus):
    deal = PiRedeal.of(commander_bus, commander_bus.registry.require("unit-x"))
    deal.redeal()
    record = deal.current()
    changed, *kept = record.paths
    for path in kept:
        deal.record_read(path, ReadSpan(1, sys.maxsize, sys.maxsize))
    stale = DealtRead(DealtSource.of(changed), "0" * 64)
    with deal.dealt_read.open("a", encoding="utf-8") as log:
        log.write(json.dumps(FieldCodec.encode(stale)) + "\n")
    assert next(Unread.owed_by(deal, record)).remaining == (changed,)


def test_a_refresh_passed_while_reading_the_deal_is_answered_by_it(commander_bus):
    """bus 45261: the first tool call after a Pi unit finished its deal (a comms_send) dealt 16
    files again, because reading them had filled the context past the 66%-left refresh."""
    deal = PiRedeal.of(commander_bus, commander_bus.registry.require("unit-x"))
    deal.redeal()
    deal.observe_context(ContextUsage(40, 100))
    answer_deal(deal)
    dealt = deal.current().dealt
    deal.observe_context(ContextUsage(41, 100))
    assert isinstance(deal.state(), Open) and deal.current().dealt == dealt
    assert deal.audit.exists()
    deal.observe_context(ContextUsage(70, 100))
    assert not isinstance(deal.state(), Open) and deal.audit.exists()
