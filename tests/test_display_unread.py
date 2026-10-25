from dataclasses import replace

from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.bus import Bus, DisplayUnread
from agent_comms.field_codec import ThreadLookup
from agent_comms.messages import InfoKind, Message, PublicRecord
from agent_comms.notification_progress import UnrecordedProgress
from agent_comms.presentation import MessageNotification
from agent_comms.read_basis import (
    ChannelConversation,
    ChannelDisplayScope,
    DisplayedConversation,
    ReadCursor,
)
from agent_comms.registry_provenance import RegistryProvenance
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
    viewer = ("viewer",)
    seen = ReadCursor()
    queries = []
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(queries.append)
        first = DisplayUnread().advanced(source, scopes, viewer, seen, namespace)
        assert first.counts == {"#team": 500, "#other": 0}
        assert sum(query.startswith("WITH read_cursors") for query in queries) == 2
        queries.clear()
        assert first.advanced(source, scopes, viewer, seen, namespace) is first
        assert not any(query.startswith("WITH read_cursors") for query in queries)

    for index in range(3):
        bus.append(lambda seq, db, index=index: PublicRecord.from_message(
            Message("author", "#team", f"new {index}", InfoKind, seq=seq)))

    queries.clear()
    with bus.reading() as source, source.observing() as db:
        db.set_trace_callback(queries.append)
        second = first.advanced(source, scopes, viewer, seen, namespace)
        assert sum(query.startswith("WITH read_cursors") for query in queries) == 2
        assert second.counts == {"#team": 503, "#other": 0}
        full = source.display_unread(scopes, viewer, seen, namespace, through=source.revision())
        assert second == full
        print("500-row sidebar: full=2 COUNT queries; advance=2 bounded COUNT queries; unchanged=0")

    seen = seen.add((DisplayedConversation(ChannelConversation("#team"), (), prefix_through=250),))
    with bus.reading() as source:
        after_mark = second.advanced(source, scopes, viewer, seen, namespace)
        full = source.display_unread(scopes, viewer, seen, namespace, through=source.revision())
        assert after_mark == full
        assert after_mark.counts == {"#team": 253, "#other": 0}
        excluded = after_mark.advanced(source, scopes, ("author",), seen, namespace)
        assert excluded.counts == {"#team": 0, "#other": 0}
        expanded = excluded.advanced(source, scopes[:1], viewer, seen, namespace)
        assert expanded.counts == {"#team": 253}


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
