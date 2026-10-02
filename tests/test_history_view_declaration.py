"""A new history declaration crosses real current and retained-source boundaries."""

from agent_comms.comms import Comms
from agent_comms.historical_views import HistoryView
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import ChannelDisplayScope
from agent_comms.threads import Thread


class ReviewHistory(HistoryView):
    def capture(self, snapshot):
        return ChannelDisplayScope("#review", frozenset({"#review"}))

    def live_page(self, presentation, **paging):
        return MessagePageRequest.capture(
            self.capture(presentation.registry.snapshot()), **paging
        ).read(presentation.bus.log)


def test_new_history_case_owns_both_source_interpretations_without_reader_dispatch(tmp_path):
    roots = [Comms(tmp_path / name) for name in ("prior", "current")]
    for comms in roots:
        comms.registry.declare(Thread("writer", frozenset({"review", "other"}), str(comms.root)))
        for number in range(3):
            comms.messaging.send("writer", "#review", f"{comms.root.name} {number}")
            comms.messaging.send("writer", "#other", f"excluded {number}")
    prior, current = roots
    original = prior.bus.log.path.read_bytes()
    current.views.attach_history(prior.root)
    view = ReviewHistory()
    backwards = view.full_history(current.views.presentation)
    assert [message.body for message in backwards] == [
        f"{name} {number}" for name in ("prior", "current") for number in range(3)
    ]
    page = view.page(current.views.presentation, limit=2)
    assert [m.body for m in page.messages] == ["current 1", "current 2"]
    forward = []
    cursor = backwards[0].view_cursor
    while True:
        page = view.page(current.views.presentation, after=cursor, limit=2)
        forward.extend(page.messages)
        if not page.has_newer:
            break
        cursor = page.newest_cursor
    assert [m.view_key for m in forward] == [m.view_key for m in backwards[1:]]
    assert page.historical_display is None
    assert prior.bus.log.path.read_bytes() == original
    assert current.bus.log.latest_sequence() == 6
    assert not (prior.root / "read_ledger.json").exists()


def test_ordinary_coordination_snapshot_exposes_actual_active_channel_participant(tmp_path):
    from test_coordinated_runtime import _root

    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    lease = comms.agents.begin_turn("beta", "ordinary-snapshot-active-participant")
    try:
        snapshot = comms.views.coordination_snapshot()
        assert [person.thread.name for person in snapshot.participants("#team")] == ["beta"]
        assert snapshot.participants("#team")[0].presentation.busy
    finally:
        comms.agents.finish_turn(lease)
    assert comms.views.coordination_snapshot().participants("#team") == ()


def test_joined_declarations_decode_one_sql_snapshot_and_reject_foreign_columns():
    import sqlite3
    from dataclasses import dataclass

    import pytest

    from agent_comms.typed_table import TypedRow

    @dataclass(frozen=True)
    class Label(TypedRow):
        label: str

    @dataclass(frozen=True)
    class Ready(TypedRow):
        ready: bool

    with sqlite3.connect(":memory:") as connection:
        assert Label.joined(connection.execute("SELECT 'saved' AS label,1 AS ready"), Ready) == [
            (Label("saved"), Ready(True))
        ]
        for query in (
            "SELECT 'saved' AS label,1 AS label",
            "SELECT 'saved' AS label",
            "SELECT 'saved' AS label,1 AS ready,4 AS foreign_field",
            "SELECT 'saved' AS label,2 AS ready",
        ):
            with pytest.raises((TypeError, ValueError)):
                Label.joined(connection.execute(query), Ready)
