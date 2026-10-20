"""The one-time conversion deletes retired keys, not first-call evidence."""

import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import MalformedRecord
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import GoalRef, Todo, TodoStore
from agent_comms.installer.carry import CarryChain, CarryLedger
from cutover.carry_first_call import FirstCalls
from test_todos import order, thread


def test_new_row_step_does_not_convert_a_missing_store(tmp_path):
    result = FirstCalls.carry(tmp_path, apply=True)
    assert result.changed == 0 and result.detail == {} and result.rewritten == ()
    assert not TodoStore.path_in(tmp_path).exists()


def test_saved_first_call_keys_are_carried_once_before_strict_order_reads(tmp_path):
    store = TodoStore(tmp_path)
    lead = thread("lead", tmp_path, 1.0)
    current = store.create(
        "P1",
        RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Fix the store",
        creator=lead,
        owner=lead,
        generation="one",
        goal=GoalRef("lead", 1.0, "request"),
        deadline=RecordedEventTime(1000),
        order=order(tmp_path),
    )
    saved = replace(
        current,
        order=current.order.merged("abc123").folded("P2", "same repair"),
        prior_orders=(current.order.reissued(20),),
    )
    with store.writing() as db:
        Todo.update(
            db,
            where="{id} = ?",
            parameters=(saved.id,),
            order=saved.order,
            prior_orders=saved.prior_orders,
        )
        # A folded order nests a checked declaration; history is a separate column.
        for column, path in (("order", "$.original"), ("prior_orders", "$[0]")):
            db.execute(
                f'UPDATE todo SET "{column}"=json_set("{column}", ?, json(?)) WHERE id=?',
                (path + ".first_call", json.dumps({"kind": "late_observed"}), saved.id),
            )
    with pytest.raises(MalformedRecord, match="first_call"):
        store.get(saved.id)
    with closing(CoordinationStore.read_snapshot(store.path)) as db:
        before = tuple(db.execute('SELECT "order", prior_orders FROM todo').fetchone())
    assert FirstCalls.carry(tmp_path, apply=False).changed == 2
    with closing(CoordinationStore.read_snapshot(store.path)) as db:
        assert tuple(db.execute('SELECT "order", prior_orders FROM todo').fetchone()) == before
    declared = next(
        step
        for step in CarryChain.at(Path(__file__).parents[1], "HEAD").steps
        if step.name == FirstCalls.name()
    )
    assert declared.declaration == "FirstCalls"
    chain = CarryChain((declared,))
    ledger = CarryLedger(tmp_path)
    assert (
        ledger.apply(
            chain, FirstCalls.name(), lambda: FirstCalls.carry(tmp_path, apply=True)
        ).changed
        == 2
    )
    assert store.get(saved.id) == saved
    assert FirstCalls.name() in ledger.applied()
    assert (
        ledger.apply(chain, FirstCalls.name(), lambda: pytest.fail("Already carried")).changed == 0
    )
    assert FirstCalls.carry(tmp_path, apply=True).changed == 0
