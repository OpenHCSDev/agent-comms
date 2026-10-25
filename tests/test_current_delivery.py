"""Retained Bus snapshots preserve current-incarnation routing and displayed reads."""

from agent_comms.repo import Checkout
from collections import Counter

import pytest

from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import CommandTarget
from agent_comms.comms import wire
from agent_comms.messages import InfoKind, Message
from agent_comms.page_traversal import LiveCursor
from agent_comms.read_basis import ConversationCursor, NamedViewer, ReadCursor
from agent_comms.read_ledger import ReadLedger
from agent_comms.routing import DeliveryScope
from agent_comms.threads import Thread


@pytest.fixture
def routed(comms, tmp_path):
    comms.channels.create_tag("team")
    for name in ("sender", "recipient"):
        comms.registry.declare(Thread(name, frozenset({"team"}), Checkout.from_wire(str(tmp_path))))
    return comms


def consistent(comms, *names):
    bulk = comms.bus.pending_counts_all(names)
    for name in names:
        inbox = comms.bus.inbox(name).page.messages
        assert bulk[name] == comms.bus.pending_count(name) == len(inbox)
        with comms.bus.snapshot(viewer=NamedViewer(name)) as (basis, source):
            scope = DeliveryScope.capture(name, basis.registry, basis.catalog)
            assert comms.bus.pending_counts_opened(source, basis) == Counter(
                scope.conversation(row.sender, row.target) for row in inbox
            )


@pytest.mark.parametrize("rebound", ["sender", "recipient"])
def test_current_inbox_bulk_and_index_exclude_rebound_history(routed, rebound):
    comms = routed
    old = comms.messaging.send("sender", "recipient", "retained history").message
    comms.messaging.send("sender", "#team", "old channel row")
    original = comms.registry.require(rebound)
    comms.registry.unregister(rebound)
    comms.registry.remove(rebound)
    comms.registry.declare(Thread(
        rebound, original.tags, original.worktree,
    ))
    new = comms.messaging.send("sender", "recipient", "current input").message
    comms.messaging.send("sender", "#team", "current channel input")
    current = wire(comms.root)
    consistent(current, "sender", "recipient")
    with current.bus.snapshot(viewer=NamedViewer("recipient")) as (basis, source):
        assert current.bus.pending_counts_opened(source, basis) == {"sender": 1, "#team": 1}
    assert [row.seq for row in current.bus.inbox("recipient", CommandTarget.named("sender")).page.messages] == [new.seq]
    assert old in current.views.channel_display_page("#any").messages
    assert [row.body for row in current.views.history_page(
        CommandTarget.named("recipient"), viewer=NamedViewer("sender"),
    ).messages] == ["retained history", "current input"]


def test_unordered_times_and_sparse_seen_rows(routed):
    comms = routed
    birth = comms.registry.require("recipient").created_at
    messages = [
        comms.bus.publisher.publish_ordinary(
            Message("sender", "recipient", f"row {index}", InfoKind, timestamp=timestamp),
            destination=CommandTarget.named("recipient"), origin=AgentOrigin(),
        )
        for index, timestamp in enumerate((birth + 2, birth - 1, birth + 1, birth - 2))
    ]
    displayed = comms.bus.reads.capture(
        "recipient", messages[:2], comms.registry.snapshot(), comms.bus.bus,
        document=comms.bus.reads.read(),
    )
    comms.bus.reads.mark_displayed("recipient", displayed)
    consistent(comms, "sender", "recipient")
    assert [row.seq for row in comms.bus.inbox("recipient").page.messages] == [messages[2].seq]


@pytest.mark.parametrize("birth_crossing", [False, True])
def test_retained_cut_clips_future_paint_in_counts_and_listings(routed, tmp_path, birth_crossing):
    comms = routed
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    birth = comms.registry.require(viewer).created_at
    first = comms.bus.publisher.publish_ordinary(
        Message("sender", viewer, "inside cut", InfoKind, timestamp=birth + 2), destination=CommandTarget.named(viewer), origin=AgentOrigin(),
    )
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        later = comms.bus.publisher.publish_ordinary(Message(
            "sender", viewer, "outside cut", InfoKind,
            timestamp=birth - 1 if birth_crossing else birth + 3,
        ), destination=CommandTarget.named(viewer), origin=AgentOrigin())
        displayed = comms.bus.reads.capture(
            viewer, (later,), basis.registry, comms.bus.bus, document=comms.bus.reads.read(),
        )
        comms.bus.reads.mark_displayed(viewer, displayed)
        with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
            assert comms.bus.pending_counts_opened(pending_source, pending_basis) == {"sender": 1}
        later_reads = comms.bus.reads.read().cursor(viewer, basis.registry, source)
        assert later_reads == ReadCursor({
            ReadLedger.conversation(later, basis.registry): ConversationCursor().add((later.seq,)),
        })
        delivery = DeliveryScope.capture(viewer, basis.registry, basis.catalog)
        assert source.unseen_counts(basis.registry, {viewer: delivery}, {viewer: later_reads}) == {
            viewer: {"sender": 1},
        }
        assert [row.seq for row in source.unseen_deliveries(
            basis.registry, delivery, later_reads, traversal=LiveCursor(0).newer(),
        )] == [first.seq]


def test_retained_source_uses_its_ledger_identity_after_path_replacement(routed, tmp_path):
    comms = routed
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send("sender", viewer, "already read").message
    displayed = comms.bus.reads.capture(
        viewer, (message,), comms.registry.snapshot(), comms.bus.bus,
        document=comms.bus.reads.read(),
    )
    comms.bus.reads.mark_displayed(viewer, displayed)
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (pending_basis, pending_source):
        assert comms.bus.pending_counts_opened(pending_source, pending_basis) == {}
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        replacement = tmp_path / "replacement.sqlite3"
        comms.bus.bus.backup(replacement)
        replacement.replace(comms.bus.bus.path)
        assert comms.bus._pending_projection(source, basis, (viewer,)) == {viewer: {}}
        assert comms.bus.pending_counts_opened(source, basis) == {}
