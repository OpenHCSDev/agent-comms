from dataclasses import replace

import pytest

from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.bus import Bus, DisplayUnread
from agent_comms.field_codec import FieldCodec, MalformedRecord, ThreadLookup
from agent_comms.messages import InfoKind, Message, PublicRecord
from agent_comms.notification_progress import UnrecordedProgress
from agent_comms.presentation import MessageNotification
from agent_comms.read_basis import (
    ChannelConversation,
    ChannelDisplayScope,
    DisplayedConversation,
    DisplayUnreadSelection,
    MemberActivityDisplayScope,
    ReadCursor,
)
from agent_comms.registry_document import ThreadRows
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.repo import Checkout
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread
from agent_comms.thread_presentation import (
    ThreadPresentation,
    WorkingThreadPresentationStatus,
)
from agent_comms.ui_model.status import ThreadStatusModel


def test_unread_advances_over_new_rows_and_recounts_after_a_read_mark(bus):
    Bus.install(bus.path, history=(
        PublicRecord.from_message(Message("author", "#team", f"backlog {seq}", InfoKind, seq=seq))
        for seq in range(1, 501)
    ))
    namespace = RegistryProvenance(threads={}, aliases={})
    scopes = (ChannelDisplayScope("#team", frozenset({"#team"})),
              ChannelDisplayScope("#other", frozenset({"#other"})))
    selection = DisplayUnreadSelection(ReadCursor(), scopes, ("viewer",), namespace)
    queries = []
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(queries.append)
        first = DisplayUnread().advanced(source, selection)
        assert first.counts == {"#team": 500, "#other": 0}
        assert sum(query.startswith("WITH read_cursors") for query in queries) == 2
        queries.clear()
        assert first.advanced(source, selection) is first
        assert not any(query.startswith("WITH read_cursors") for query in queries)

    for index in range(3):
        bus.append(lambda seq, db, index=index: PublicRecord.from_message(
            Message("author", "#team", f"new {index}", InfoKind, seq=seq)))

    queries.clear()
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(queries.append)
        second = first.advanced(source, selection)
        assert sum(query.startswith("WITH read_cursors") for query in queries) == 2
        assert second.counts == {"#team": 503, "#other": 0}
        full = source.display_unread(selection, through=source.revision())
        assert second == full

    selection = replace(selection, seen=selection.seen.add((
        DisplayedConversation(ChannelConversation("#team"), (), prefix_through=250),
    )))
    with bus.reading() as source:
        after_mark = second.advanced(source, selection)
        full = source.display_unread(selection, through=source.revision())
        assert after_mark == full
        assert after_mark.counts == {"#team": 253, "#other": 0}
        excluded = after_mark.advanced(source, replace(selection, viewer_names=("author",)))
        assert excluded.counts == {"#team": 0, "#other": 0}
        expanded = excluded.advanced(source, replace(selection, scopes=selection.scopes[:1]))
        assert expanded.counts == {"#team": 253}


def test_a_registry_revision_that_changes_no_query_counts_only_appended_rows(bus):
    """The registry changes on every fleet turn; a count that read none of what changed
    must not rescan history, and a read mark on one channel recounts only that channel."""
    Bus.install(bus.path, history=(
        PublicRecord.from_message(Message("author", "#team", f"backlog {seq}", InfoKind, seq=seq))
        for seq in range(1, 201)
    ))
    scopes = (ChannelDisplayScope("#team", frozenset({"#team"})),
              ChannelDisplayScope("#other", frozenset({"#other"})))
    selection = DisplayUnreadSelection(
        ReadCursor(), scopes, ("viewer",), RegistryProvenance(threads={}, aliases={}))
    with bus.reading() as source:
        first = DisplayUnread().advanced(source, selection)
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "#team", "new", InfoKind, seq=seq)))
    class RevisedRegistry(RegistryProvenance):
        """Another registry revision that resolves every name the same way."""

    revised = replace(selection, registry=RevisedRegistry(threads={}, aliases={}))
    assert revised != selection
    queries = []
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(queries.append)
        second = first.advanced(source, revised)
        assert second.counts == {"#team": 201, "#other": 0}
        counted = [query for query in queries if query.startswith("WITH read_cursors")]
        assert len(counted) == 2 and all(f"seq > {first.through}" in query for query in counted)
    marked = replace(revised, seen=revised.seen.add((
        DisplayedConversation(ChannelConversation("#team"), (), prefix_through=150),
    )))
    queries.clear()
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(queries.append)
        third = second.advanced(source, marked)
        recounted = [query for query in queries if query.startswith("WITH read_cursors")]
        assert len(recounted) == 2 and all("target IN ('#team')" in query for query in recounted)
        assert third.counts == {"#team": 51, "#other": 0}
        assert third == source.display_unread(marked, through=source.revision())
    reread = replace(marked, seen=marked.seen.add((
        DisplayedConversation(ChannelConversation("#team"), (), prefix_through=180),
    )))
    queries.clear()
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(queries.append)
        fourth = third.advanced(source, reread)
        recounted = [query for query in queries if query.startswith("WITH read_cursors")]
        assert len(recounted) == 2
        assert sorted("seq > 150" in query for query in recounted) == [False, True]
        assert sorted("seq > 180" in query for query in recounted) == [False, True]
        assert fourth.counts == {"#team": 21, "#other": 0}
        assert fourth == source.display_unread(reread, through=source.revision())


def test_tagged_unread_reuses_unchanged_membership_and_recounts_actual_tag_changes(comms, bus):
    member = comms.registry.declare(
        Thread("member", frozenset({"team"}), Checkout(comms.root.parent)),
        status=RunningThreadStatus(),
    )
    other = comms.registry.declare(Thread("other", frozenset(), Checkout(comms.root.parent)))
    channel = comms.bus.require_channel("#team")
    for sender, target in ((member.name, other.name), (other.name, member.name)):
        bus.append(lambda seq, db, sender=sender, target=target: PublicRecord.from_message(
            Message(sender, target, "member activity", InfoKind, seq=seq)))

    def selection(snapshot):
        return DisplayUnreadSelection(
            ReadCursor(), (MemberActivityDisplayScope.capture(channel, snapshot),),
            ("viewer",), snapshot,
        )

    original = selection(comms.registry.snapshot())
    with bus.reading() as source:
        first = DisplayUnread().advanced(source, original)
        assert first.counts == {"#team": 2}
    comms.registry.update_thread(
        other.name, lambda thread: replace(thread, task=GivenThreadText("unrelated edit")),
    )
    revised = selection(comms.registry.snapshot())
    assert revised.queries() == original.queries()
    assert revised.queries()[0].same_membership(original.queries()[0])
    statements = []
    with bus.reading() as source, source.observing() as db:
        changes = db.total_changes
        db.set_trace_callback(statements.append)
        assert first.advanced(source, revised).counts == first.counts
        assert not any(query.startswith("WITH read_cursors") for query in statements)
        assert db.total_changes == changes
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message(other.name, member.name, "appended activity", InfoKind, seq=seq)))
    statements.clear()
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(statements.append)
        appended = first.advanced(source, revised)
        counted = [query for query in statements if query.startswith("WITH read_cursors")]
        assert counted and all(f"seq > {first.through}" in query for query in counted)
        assert appended.counts == {"#team": 3}
        assert appended == source.display_unread(revised, through=source.revision())
    comms.registry.update_thread(member.name, lambda thread: replace(thread, tags=frozenset()))
    retagged = selection(comms.registry.snapshot())
    assert not retagged.queries()[0].same_membership(revised.queries()[0])
    with bus.reading() as source:
        recounted = appended.advanced(source, retagged)
        assert recounted.counts == {"#team": 0}
        assert recounted == source.display_unread(retagged, through=source.revision())


def test_tag_membership_keeps_alias_births_and_defers_unselected_full_fields(comms):
    member = comms.registry.declare(
        Thread("member", frozenset({"team"}), Checkout(comms.root.parent)),
        status=RunningThreadStatus(),
    )
    unrelated = comms.registry.declare(Thread("other", frozenset(), Checkout(comms.root.parent)))
    channel = comms.bus.require_channel("#team")
    before = comms.registry.snapshot()
    original = MemberActivityDisplayScope.capture(channel, before)
    comms.registry.rename(member.name, "renamed")
    renamed = comms.registry.snapshot()
    raw = renamed.threads.to_wire()
    raw[unrelated.name][FieldCodec.field_plan(Thread, "model").key] = []
    narrow = replace(renamed, threads=ThreadRows.from_wire(raw))
    scope = MemberActivityDisplayScope.capture(channel, narrow)
    assert scope.includes_participant(member.name)
    assert scope.includes_participant("renamed")
    assert scope.memberships[member.name].incarnation == member.incarnation
    assert scope.memberships["renamed"].incarnation == member.incarnation
    assert not scope.includes_participant(unrelated.name)
    assert all("decoded" not in row.__dict__ for row in narrow.threads._rows.values())
    assert original.includes_participant(member.name)
    comms.registry.unregister("renamed")
    comms.threads.delete("renamed")
    replacement = comms.registry.declare(
        Thread(member.name, frozenset({"team"}), Checkout(comms.root.parent)),
    )
    recreated = MemberActivityDisplayScope.capture(channel, comms.registry.snapshot())
    assert replacement.incarnation != member.incarnation
    assert not recreated.same_membership(original)
    raw["renamed"][FieldCodec.field_plan(Thread, "tags").key] = 7
    malformed = replace(renamed, threads=ThreadRows.from_wire(raw))
    with pytest.raises(MalformedRecord):
        MemberActivityDisplayScope.capture(channel, malformed).memberships


def test_status_row_equality_ignores_an_older_notification_outside_shown_lines():
    lookup = ThreadLookup.mint()
    older = MessageNotification(
        FrozenRecipient(lookup, "worker"), lookup, UnrecordedProgress(),
        message=Message("author", "worker", "older history", InfoKind, seq=1),
    )
    presentation = ThreadPresentation("Worker", "○", "Ready", notifications=(older,))
    row = ThreadStatusModel("worker", True, ("Ready",), presentation, ("Ready",))
    changed_history = replace(row, presentation=replace(
        presentation, notifications=(replace(older, message=replace(older.message, body="changed old history")),),
    ))
    assert row == changed_history
    assert row != replace(row, presentation=replace(presentation, status=WorkingThreadPresentationStatus()))
    assert row != replace(row, summary=("Changed overview",))
    assert row != replace(row, lines=("Changed status line",))
