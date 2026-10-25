"""History is durable, append-only, and never decoded for a board read."""
from agent_comms.repo import Checkout
from dataclasses import replace
import sqlite3
import sys
import time

import pytest

from agent_comms.commander.order import Commander
from agent_comms.field_codec import FieldCodec, GoalId, MessageId, OrderId
from agent_comms.native_tool_call import ToolCallId
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.order_story import StoryEvent
from agent_comms.order_tools import CommsTodoListTool
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import (
    AcceptedVerdict, Assignment, CheckedProblemOrder, DroppedVerdict, EditSendBackVerdict, FailedCheckStrike,
    LimitReleaseTodo, OrderMessage, PeerAcceptedVerdict, ReleaseTodo, RewrittenVerdict, SendBackStrike,
    SendBackVerdict, StoredOrderStory, StrikeReason, StrikeTodo, SupersededVerdict, Todo, TodoCommand,
    VoidStrikeTodo, WordingCorrection,
)
from agent_comms.tool_caller import NoThreadCaller
from cutover.carry_todo_stories import INLINE_FIELDS, TodoStories
from test_todos import order


@pytest.fixture
def recorded_problem(comms, source_repository, tmp_path):
    for name in ("lead", "worker"):
        comms.registry.declare(Thread(name, frozenset({"comms"}), Checkout(tmp_path / name)))
    actor = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation
    store = comms.goals.todos
    request = store.add_request(actor, "Repair the existing record owner")
    todo = Todo.ordered("P1", source_repository.github, "Repair the existing record owner", actor,
        Assignment.of(worker, actor, GoalId.mint()), RecordedEventTime(1000),
        replace(order(source_repository), edits=("Repair the existing record owner",)), request.id)
    with store.writing() as db:
        todo.insert(db)
    return comms


def story_members(comms):
    todo = comms.goals.todos.get("P1")
    actor = comms.registry.require("lead").incarnation
    strike = StrikeTodo(todo.revision, actor, todo.held(),
                        "The worker left the required owner duplicated across two source paths.")
    return (
        strike,
        VoidStrikeTodo(todo.revision, actor, strike,
                       "The original source trace shows this strike was issued in error."),
        ReleaseTodo(todo.revision, actor, todo.held(), StrikeReason(
            "The commander transfers this order so its original events remain visible.")),
        AcceptedVerdict(todo.revision, actor, todo.held(), ToolCallId("accepted-call"),
                        quote="Repair the existing record owner"),
        WordingCorrection(todo.revision, MessageId.mint("wording correction"), ("custody",),
                          at=RecordedEventTime(2000)),
        OrderMessage(todo.revision, actor, MessageId.mint("order message"),
                     at=RecordedEventTime(3000)),
        replace(todo.order.checked(), id=OrderId.mint()),
    )


def test_story_members_round_trip_in_one_append_only_table(recorded_problem):
    store = recorded_problem.goals.todos
    events = story_members(recorded_problem)
    todo = store.get("P1")
    actor = recorded_problem.registry.require("lead").incarnation
    events += (
        FailedCheckStrike.make(todo, 4000, "The declared acceptance command failed at its source owner."),
        PeerAcceptedVerdict(todo.revision, actor, todo.held(), ToolCallId("peer-call"),
            quote="Repair the existing record owner", peer=MessageId.mint("peer review")),
        SupersededVerdict(todo.revision, actor, todo.held(), ToolCallId("superseded-call"), "P2",
                          "The replacement order removes this original implementation entirely."),
        DroppedVerdict(todo.revision, actor, todo.held(), ToolCallId("dropped-call"),
                       path="owner.py", added=("discarded text",)),
        SendBackVerdict(todo.revision, actor, todo.held(), ToolCallId("sent-back-call"),
                        StrikeReason("The repair still copies the record shape outside its declaration.")),
        EditSendBackVerdict(todo.revision, actor, todo.held(), ToolCallId("edit-sent-back-call"),
                            StrikeReason("The edit restates the record shape the owner already declares."),
                            path="owner.py"),
        RewrittenVerdict(todo.revision, actor, todo.held(), ToolCallId("rewritten-call"),
                         path="owner.py", added=("rewritten text",), commit="0" * 40,
                         reason="A later commit rewrote this edit and carries its reviewed lines."),
        SendBackStrike(todo.revision, actor, todo.held(),
                       "The send-back records the miss the repair repeated in its owner."),
        LimitReleaseTodo(todo.revision, actor, todo.held(), StrikeReason(
            "The worker took the strike limit on this order, so the task goes to another unit.")),
    )
    assert {type(event) for event in events} >= set(TodoCommand.members_with(StoryEvent))
    with store.writing() as db:
        for event in events:
            StoredOrderStory.append(db, "P1", event)
        assert tuple(row.event for row in StoredOrderStory.select(db, order_by=("seq",))) == events
        with pytest.raises(sqlite3.IntegrityError, match="cannot be updated"):
            db.execute(f'UPDATE "{StoredOrderStory.declared_name}" SET event=event')
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            db.execute(f'DELETE FROM "{StoredOrderStory.declared_name}"')


def test_voided_strike_uses_its_stored_sequence_and_count_returns_to_zero(recorded_problem):
    comms = recorded_problem
    store = comms.goals.todos
    actor = comms.registry.require("lead").incarnation
    original = store.get("P1")
    strike = story_members(comms)[0]
    struck = store.apply(original.id, strike)
    assert struck.strike_count == 1
    cleared = store.apply(original.id, VoidStrikeTodo(struck.revision, actor, strike,
        "The original source trace shows this strike was issued in error."))
    assert cleared.strike_count == 0
    assert cleared.live_strikes() == ()
    assert struck.strike_count == 1
    assert len(struck.live_strikes()) == 1
    assert original.strike_count == 0
    assert original.history.events(original.id, StrikeTodo) == ()
    with store.snapshot() as db:
        first, void = tuple(StoredOrderStory.select(db, problem=original.id, order_by=("seq",)))
        assert void.voids == first.seq


def test_undated_strike_counts_without_inventing_an_order_time(recorded_problem):
    store = recorded_problem.goals.todos
    strike = replace(story_members(recorded_problem)[0], at=UnrecordedEventTime())
    saved = store.apply("P1", strike)
    assert saved.strike_count == 1
    assert not saved.struck_on_order(strike.previous.owner_identity)
    assert saved.history.events(saved.id, StrikeTodo) == (strike,)


def test_commander_unstrike_selects_the_newest_live_record(recorded_problem, source_repository):
    comms = recorded_problem
    store = comms.goals.todos
    first = story_members(comms)[0]
    struck = store.apply("P1", first)
    second = replace(first, expected_revision=struck.revision,
        reason="The worker omitted the installed application check for its changed owner.")
    store.apply("P1", second)
    commander = Commander.on_root("lead", (source_repository,), comms.root)
    commander.unstrike("P1", "The second strike was issued in error after the required owner was repaired.")
    assert store.get("P1").live_strikes() == (first,)


def test_strike_count_distinguishes_a_reused_thread_name(recorded_problem):
    comms = recorded_problem
    store = comms.goals.todos
    original = store.get("P1")
    struck = store.apply(original.id, story_members(comms)[0])
    worker = comms.registry.require("worker").incarnation
    reused = RecordedThreadIncarnation(worker.name, worker.wire_birth + 1)
    assert struck.strikes_against(worker) == 1
    assert struck.strikes_against(reused) == 0


def test_wording_writer_records_its_actual_time(recorded_problem):
    comms = recorded_problem
    before = time.time()
    (corrected,) = comms.goals.todos.correct(comms.registry.require("worker").incarnation,
        MessageId.mint("wording correction"), "The custody is ready.")
    after = time.time()
    assert before <= corrected.history.events(corrected.id, WordingCorrection, limit=1, descending=True)[0].at.recorded_at("A wording correction") <= after


def test_board_of_forty_orders_and_two_thousand_verdicts_decodes_no_story_rows(recorded_problem):
    comms = recorded_problem
    store = comms.goals.todos
    actor = comms.registry.require("lead").incarnation
    original = store.get("P1")
    with store.writing() as db:
        for number in range(1, 41):
            todo = replace(original, id=f"P{number}", revision=51,
                           order=replace(original.order.checked(), id=OrderId(f"{number:012x}")))
            todo.upsert(db)
            for verdict in range(50):
                StoredOrderStory.append(db, todo.id, AcceptedVerdict(
                    verdict + 1, actor, todo.held(), ToolCallId(f"call-{number}-{verdict}"),
                    quote="Repair the existing record owner"))
    measured = {"rows": 0, "aggregates": 0}
    read_code = StoredOrderStory.read.__func__.__code__
    aggregate_code = StoredOrderStory.facts_for.__func__.__code__
    def profile(frame, event, arg):
        if event == "call" and frame.f_locals.get("cls") is StoredOrderStory:
            if frame.f_code is read_code:
                measured["rows"] += 1
            if frame.f_code is aggregate_code:
                measured["aggregates"] += 1
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        reply = CommsTodoListTool.invoke(comms, {"thread": "lead"}, caller=NoThreadCaller())
    finally:
        sys.setprofile(previous)
    assert len(reply["todos"]) == 40
    assert measured == {"rows": 0, "aggregates": 1}
    assert len(FieldCodec.compact(reply)) < 100_000


def test_inline_history_is_carried_once_without_losing_void_links(recorded_problem, tmp_path):
    comms = recorded_problem
    store = comms.goals.todos
    strike, void, release, verdict, correction, message, prior = story_members(comms)
    retired = dict.fromkeys(INLINE_FIELDS, ())
    retired.update(strikes=(strike,), voided=(void,), released=(release,), verdicts=(verdict,),
                   corrections=(correction,), messages=(message,), prior_orders=(prior,))
    retired = {column: tuple(FieldCodec.encode_record(event) for event in events)
               for column, events in retired.items()}
    for entry in (*retired["corrections"], *retired["messages"]):
        entry.pop(FieldCodec.record_key(OrderMessage, "at"))
    with store.writing() as db:
        StoredOrderStory.drop(db)
        for column in INLINE_FIELDS:
            db.execute(f'ALTER TABLE "{Todo.declared_name}" ADD COLUMN "{column}" TEXT NOT NULL DEFAULT \'[]\'')
        columns = ", ".join(f'"{column}"=?' for column in INLINE_FIELDS)
        db.execute(f'UPDATE "{Todo.declared_name}" SET {columns} WHERE id=?',
                   (*map(lambda column: FieldCodec.compact(retired[column]), INLINE_FIELDS), "P1"))
    step = TodoStories(LiveFilesRequest(comms.root), tmp_path / "spill")
    assert step.carry(apply=False).changed == 1
    maintenance = MaintenanceBarrier(RegistryStore.at(comms.root).path)
    hold = maintenance.begin(MaintenanceOperator.current())
    try:
        with hold.holding():
            result = step.carry(apply=True)
            assert result.detail == {"todos": 1, "events": 7}
            assert step.carry(apply=False).changed == 0
    finally:
        maintenance.clear(hold)
    saved = store.get("P1")
    assert saved.strike_count == 0
    assert saved.history.events(saved.id, CheckedProblemOrder) == (prior,)
    assert saved.latest_message == replace(message, at=UnrecordedEventTime())
    assert saved.history.events(saved.id, WordingCorrection) == (replace(correction, at=UnrecordedEventTime()),)
    with store.snapshot() as db:
        assert not set(INLINE_FIELDS).intersection(Todo.stored_columns(db))
        assert StoredOrderStory.count(db, problem="P1") == 7
