"""Bounded journal reads and exact report checks on authored durable roots."""
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryError, GoalHistoryQuery, GoalHistoryStore
from agent_comms.goals import Goal
from agent_comms.registration import Registration
from agent_comms.runtime_requests import RuntimeRequest
from agent_comms.threads import Thread
from agent_comms.tools import CommsGoalHistoryTool
from goal_history_indexes import install_indexes


def authored_history(root: Path, count: int = 250):
    root.mkdir()
    registry = Registration(root / "registry.json")
    thread = Thread("owner", frozenset(), str(root), goal=Goal("Authored revisions", "goal", reported_turn="old-turn"))
    registry.register(thread)
    journal = GoalHistoryStore(registry.store.path)
    current = thread.goal
    with journal._transaction() as connection:
        for ordinal in range(1, count):
            following = replace(current, revision=current.revision + 1, reported_turn=None)
            GoalHistoryEntry(None, "transition", float(ordinal), current, following,
                             owner_created_at=thread.created_at, state="committed").insert(connection)
            current = following
    status = registry.status(thread.name)
    with registry.store.editing() as edit:
        change = edit.document.prepare_registration(replace(thread, goal=current), status, new_owner=False)
        change.apply(edit.document)
        edit.commit()
    return registry


def test_pages_are_newest_first_bounded_and_do_not_follow_a_later_append(tmp_path):
    registry = authored_history(tmp_path / "journal")
    captured = registry.goal_history("owner")
    first = captured.entries()
    second = captured.entries(GoalHistoryQuery(before_sequence=first[-1].sequence))
    third = captured.entries(GoalHistoryQuery(before_sequence=second[-1].sequence))
    assert [len(first), len(second), len(third)] == [100, 100, 50]
    assert [row.sequence for row in (*first, *second, *third)] == list(range(250, 0, -1))
    current = registry.require("owner")
    following = replace(current.goal, revision=current.goal.revision + 1)
    registry.register(replace(current, goal=following))
    assert captured.entries() == first
    assert registry.goal_history("owner").entries(GoalHistoryQuery(limit=1))[0].after == following
    assert captured.entries(GoalHistoryQuery(before_sequence=1)) == ()


def test_exact_report_query_is_not_limited_to_the_display_page(tmp_path):
    registry = authored_history(tmp_path / "journal")
    captured = registry.goal_history("owner")
    assert not any(row.reports_turn("old-turn") for row in captured.entries())
    assert captured.reports_turn("old-turn")
    assert not captured.reports_turn("never-reported")


def test_page_decoding_runs_after_another_process_can_take_the_registry_lock(tmp_path, monkeypatch):
    registry = authored_history(tmp_path / "journal", 12)
    original = GoalHistoryQuery.read

    def read(query, connection, owner_created_at, through_sequence):
        result = subprocess.run([
            sys.executable, "-c",
            "from pathlib import Path; import sys; from agent_comms.registry_store import RegistryStore; "
            "store=RegistryStore(Path(sys.argv[1])); "
            "lock=store.locked(); lock.__enter__(); lock.__exit__(None,None,None)",
            str(registry.store.path),
        ], env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src")),
           capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return original(query, connection, owner_created_at, through_sequence)

    monkeypatch.setattr(GoalHistoryQuery, "read", read)
    assert len(registry.goal_history("owner").entries(GoalHistoryQuery(limit=7))) == 7


def test_observation_decodes_only_tail_and_bounded_page(tmp_path, monkeypatch):
    registry = authored_history(tmp_path / "journal")
    original = GoalHistoryEntry._decode_row.__func__
    decoded = []

    def read(cls, row, positions):
        result = original(cls, row, positions)
        decoded.append(result.sequence)
        return result

    monkeypatch.setattr(GoalHistoryEntry, "_decode_row", classmethod(read))
    captured = registry.goal_history("owner")
    assert decoded == [250]
    assert len(captured.entries(GoalHistoryQuery(limit=7))) == 7
    assert decoded == [250, *range(250, 243, -1)]


def test_aborted_tail_uses_indexed_last_committed_entry(tmp_path, monkeypatch):
    registry = authored_history(tmp_path / "journal", 10)
    thread = registry.require("owner")
    journal = GoalHistoryStore(registry.store.path)
    with journal._transaction() as connection:
        for ordinal in range(500):
            GoalHistoryEntry(None, "transition", float(ordinal), thread.goal,
                             replace(thread.goal, revision=thread.goal.revision + 1),
                             owner_created_at=thread.created_at, state="aborted").insert(connection)
    original = GoalHistoryEntry._decode_row.__func__
    decoded = []

    def read(cls, row, positions):
        value = original(cls, row, positions)
        decoded.append(value.sequence)
        return value

    monkeypatch.setattr(GoalHistoryEntry, "_decode_row", classmethod(read))
    captured = registry.goal_history("owner")
    assert decoded == [10]
    assert captured.through_sequence == 10


def test_observation_seeks_pending_and_committed_through_the_declared_state_index(tmp_path, monkeypatch):
    registry = authored_history(tmp_path / "journal", 250)
    thread = registry.require("owner")
    journal = GoalHistoryStore(registry.store.path)
    statements = []
    connect = GoalHistoryStore._connect

    def traced(self):
        connection = connect(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(GoalHistoryStore, "_connect", traced)
    journal.observe(thread.created_at, thread.goal)
    selected = [sql for sql in statements if sql.startswith("SELECT") and "goal_history_entry" in sql]
    assert len(selected) == 2
    state_index = next(name for name, sql in GoalHistoryEntry.index_schema().items() if "state" in sql)
    with closing(CoordinationStore.read_snapshot(journal.path)) as connection:
        for sql in selected:
            plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]
            assert any("SEARCH" in detail and state_index in detail for detail in plan)
            assert not any("SCAN goal_history_entry" in detail for detail in plan)


@pytest.mark.parametrize("pending_count", [3, 105])
def test_every_pending_decision_is_reconciled_without_a_page_or_cardinality_cap(tmp_path, pending_count):
    registry = authored_history(tmp_path / "journal", 1)
    thread = registry.require("owner")
    journal = GoalHistoryStore(registry.store.path)
    other = replace(thread.goal, text="Another endpoint")
    distinct = replace(thread.goal, text="An unrelated endpoint")
    cases = ((other, thread.goal, "committed"), (thread.goal, other, "aborted"),
             (other, distinct, "uncertain"))
    with journal._transaction() as connection:
        for ordinal in range(pending_count):
            before, after, _ = cases[ordinal % len(cases)]
            GoalHistoryEntry(None, "transition", float(ordinal), before, after,
                             owner_created_at=thread.created_at, state="pending").insert(connection)
    original = GoalHistoryStore.acquire_read_only(registry.store.path)
    registry.goal_history("owner")
    reconciled = GoalHistoryStore.acquire_read_only(registry.store.path)
    assert len(reconciled) == len(original) == pending_count + 1
    assert [row.state for row in reconciled[1:]] == [
        cases[ordinal % len(cases)][2] for ordinal in range(pending_count)
    ]
    assert all((old.sequence, old.kind, old.before, old.after) ==
               (new.sequence, new.kind, new.before, new.after)
               for old, new in zip(original, reconciled, strict=True))


@pytest.mark.parametrize("committed_after_none", [False, True])
def test_absent_committed_history_is_distinct_from_committed_after_none(tmp_path, committed_after_none):
    root = tmp_path / "journal"
    root.mkdir()
    registry = Registration(root / "registry.json")
    thread = Thread("owner", frozenset(), str(root))
    registry.register(thread)
    journal = GoalHistoryStore(registry.store.path)
    with journal._transaction() as connection:
        GoalHistoryEntry(None, "transition", 1.0, Goal("Prior", "prior"),
                         None if committed_after_none else Goal("Other", "other"),
                         owner_created_at=thread.created_at,
                         state="committed" if committed_after_none else "pending").insert(connection)
    captured = registry.goal_history("owner")
    rows = GoalHistoryStore.acquire_read_only(registry.store.path)
    assert len(rows) == 1
    if committed_after_none:
        assert captured.through_sequence == rows[0].sequence
        assert captured.entries() == rows and rows[0].after is None
    else:
        assert captured.through_sequence is None and captured.entries() == ()
        assert rows[0].state == "uncertain"


def test_query_filters_both_replacement_endpoints_without_duplicates(tmp_path):
    registry = authored_history(tmp_path / "journal", 2)
    thread = registry.require("owner")
    replacement = replace(thread.goal, id="replacement")
    registry.register(replace(thread, goal=replacement))
    captured = registry.goal_history("owner")
    assert [row.sequence for row in captured.entries(GoalHistoryQuery(goal_id="goal"))] == [3, 2, 1]
    assert [row.sequence for row in captured.entries(GoalHistoryQuery(goal_id="replacement"))] == [3]
    assert captured.entries(GoalHistoryQuery(goal_id="absent")) == ()


def test_goal_filtered_sql_seeks_each_endpoint_index(tmp_path):
    registry = authored_history(tmp_path / "journal", 250)
    captured = registry.goal_history("owner")
    with closing(CoordinationStore.read_snapshot(captured.path)) as connection:
        statements = []
        connection.set_trace_callback(statements.append)
        rows = GoalHistoryQuery(goal_id="goal", limit=7).read(
            connection, captured.owner_created_at, captured.through_sequence,
        )
        connection.set_trace_callback(None)
        assert len(rows) == 7
        assert len(statements) == 1
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statements[0])]
        # Scanning the at-most-seven-row subqueries is fine; the journal itself
        # must be searched through its indexed endpoint and sequence bounds.
        assert not any("SCAN goal_history_entry" in detail for detail in plan)
        endpoint_indexes = {
            name for name, sql in GoalHistoryEntry.index_schema().items()
            if GoalHistoryEntry.BEFORE_GOAL_ID in sql or GoalHistoryEntry.AFTER_GOAL_ID in sql
        }
        assert all(any("SEARCH" in detail and name in detail for detail in plan)
                   for name in endpoint_indexes)


def test_request_boundaries_share_query_decoding_and_bounds():
    request = RuntimeRequest.from_wire({"action": "goal_history", "thread": "owner",
                                       "goal_id": " goal ", "limit": 7, "before_sequence": 20})
    assert (request.goal_id, request.limit, request.before_sequence) == ("goal", 7, 20)
    tool = FieldCodec.decode(CommsGoalHistoryTool, {"tool": "comms_goal_history", "limit": 7})
    assert tool.limit == request.limit
    for limit in (0, 101):
        with pytest.raises(ValueError, match="limit"):
            RuntimeRequest.from_wire({"action": "goal_history", "thread": "owner", "limit": limit})
    with pytest.raises(ValueError, match="positive"):
        GoalHistoryQuery(before_sequence=0)


def test_index_installation_is_explicit_and_never_rewrites_journal_rows(tmp_path):
    registry = authored_history(tmp_path / "journal", 12)
    original = GoalHistoryStore.acquire_read_only(registry.store.path)
    path = registry.store.path.parent / "goal_history.sqlite3"
    # Author the preceding physical access paths. The row/table declaration is unchanged.
    with sqlite3.connect(path) as connection:
        for name in tuple(GoalHistoryEntry.index_schema())[1:]:
            connection.execute(f'DROP INDEX "{name}"')
    before = path.read_bytes()
    plan = install_indexes(registry.store.path)
    assert len(plan["indexes"]) == 4 and plan["journal_rows_changed"] == 0
    assert path.read_bytes() == before
    assert GoalHistoryStore.acquire_read_only(registry.store.path) == original
    with pytest.raises(GoalHistoryError, match="one-shot index"):
        GoalHistoryStore(registry.store.path)
    result = install_indexes(registry.store.path, apply=True)
    assert result["indexes"] == plan["indexes"]
    assert GoalHistoryStore.acquire_read_only(registry.store.path) == original
    assert install_indexes(registry.store.path)["indexes"] == ()
    assert len(registry.goal_history("owner").entries()) == 12


def test_index_tool_refuses_foreign_schema_without_changes(tmp_path):
    registry = authored_history(tmp_path / "journal", 1)
    path = registry.store.path.parent / "goal_history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    before = path.read_bytes()
    for apply in (False, True):
        with pytest.raises(GoalHistoryError, match="differs"):
            install_indexes(registry.store.path, apply=apply)
        assert path.read_bytes() == before
