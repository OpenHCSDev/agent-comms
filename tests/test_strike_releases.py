"""The install carry preserves release facts while retiring their automatic meaning."""
import json
from contextlib import closing

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.todos import ReleaseTodo, StoredOrderStory, StrikeReason, TodoStore
from cutover.carry_strike_releases import StrikeReleases
from conftest import maintenance_held

pytest_plugins = ("test_todo_stories",)


def test_strike_release_carry_preserves_every_fact_and_runs_once(recorded_problem):
    comms = recorded_problem
    store = comms.goals.todos
    original = store.get("P1")
    released = ReleaseTodo(original.revision, original.creator_identity, original.held(),
                          StrikeReason("The recorded automatic release kept its holder and reason in history."))
    wire = FieldCodec.encode(released)
    wire["kind"] = "limit_release_todo"
    with store.writing() as db:
        db.execute(f'INSERT INTO "{StoredOrderStory.declared_name}" (problem,event) VALUES (?,?)',
                   (original.id, json.dumps(wire)))
    step = StrikeReleases(LiveFilesRequest(comms.root), comms.root / "spill")
    assert step.carry(apply=False).changed == 1
    with maintenance_held(comms.root):
        assert step.carry(apply=True).changed == 1
    assert step.carry(apply=False).changed == 0
    with closing(TodoStore.read_snapshot(TodoStore.path_in(comms.root))) as db:
        (row,) = StoredOrderStory.select(db, problem=original.id)
    assert row.event == released
    assert store.get(original.id).held() == original.held()
