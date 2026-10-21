"""Move retained request goals into Core rows after the old owners stop.

Delete after every bus has run this step. The retired wire tag is understood
only here; no request-goal member or compatibility decoder is registered.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryStore, RequestHistory
from agent_comms.goals import Goal, UnorderedGoalOrder
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.lifecycle_capability import Complete
from agent_comms.owner_requests import CompletedRequestState, OwnerRequestId, OpenRequestState, RequestDirective
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import OwnerRequest, Todo, TodoStore
from agent_comms.typed_table import Column, JsonStorage, SQLiteSchemaObject, TextStorage, TypedRow, TypedTable


@dataclass(frozen=True)
class StoredRequestGoal(TypedRow):
    """Decode the old directive payload separately from the normalized Goal."""

    goal: Annotated[Goal, Column(storage=JsonStorage)]
    directives: Annotated[tuple[RequestDirective, ...], Column(storage=JsonStorage)]
    owner_created_at: float

    def request(self, commander: RecordedThreadIncarnation, created_at: float) -> OwnerRequest:
        if commander.created_at != self.owner_created_at:
            raise RelationViolationError("Retained request belongs to another commander incarnation.")
        state = (CompletedRequestState(self.goal.progress)
                 if self.goal.state.items_with(Complete, self.goal) else OpenRequestState())
        return OwnerRequest(OwnerRequestId(self.goal.id), commander, self.goal.text,
                            state, self.directives, created_at)


@dataclass(frozen=True)
class CurrentRequestGoal(StoredRequestGoal):
    name: str
    path: str


@dataclass(frozen=True)
class HistoricalRequestGoal(StoredRequestGoal):
    sequence: int
    observed_at: float


@dataclass(frozen=True)
class StoredRequestLink(TypedRow):
    request: Annotated[OwnerRequestId, Column(storage=TextStorage)]
    owner: str
    owner_created: float


# Both the query and the replacement use the new declaration's codec. The old
# optional directives field had the empty-tuple default.
UNORDERED = json.dumps(FieldCodec.encode(UnorderedGoalOrder()))


def memory_copy(lifetime: ExitStack, path: Path) -> sqlite3.Connection:
    db = lifetime.enter_context(closing(sqlite3.connect(":memory:")))
    CoordinationStore.identifiers_only(db)
    if path.exists():
        with closing(CoordinationStore.read_snapshot(path)) as original:
            original.backup(db)
    return db


def historical(db: sqlite3.Connection, column: str, *, where: str = "1") -> list[HistoricalRequestGoal]:
    # column is resolved from GoalHistoryEntry, never supplied by a caller.
    selected = GoalHistoryEntry._column_list((column,))
    return HistoricalRequestGoal.read(db.execute(
        f'SELECT sequence, observed_at, owner_created_at, '
        f"json_set({selected}, '$.order', json(?)) AS goal, "
        f"coalesce(json_extract({selected}, '$.order.directives'), '[]') AS directives "
        f'FROM "{GoalHistoryEntry.declared_name}" '
        f"WHERE json_extract({selected}, '$.order.kind')='request' AND ({where}) "
        'ORDER BY sequence', (UNORDERED,)))


class OwnerRequests(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        registry = RegistryStore.at(root)
        paths = (*registry.written, *SQLiteFile.files(TodoStore.path_in(root)),
                 *SQLiteFile.files(GoalHistoryStore.path_in(root)))
        for path in paths:
            cls.require_confined(root, path)
        return tuple(path for path in paths if path.exists())

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        cls.backup_files(root)
        registry = RegistryStore.at(root)
        todo_path, history_path = TodoStore.path_in(root), GoalHistoryStore.path_in(root)
        if not registry.path.exists():
            if todo_path.exists() or history_path.exists():
                raise RelationViolationError("Request carry needs the retained registry.")
            return Carried(0)
        # The installer holds the wire lock and stops old owners. Use the existing
        # registry lock/guard for its publication, not a second migration lock.
        with registry.locked(), ExitStack() as lifetime:
            registry.private_guard_unlocked()
            raw = registry.path.read_text()
            sql = lifetime.enter_context(closing(sqlite3.connect(":memory:")))
            current = CurrentRequestGoal.read(sql.execute(
                "SELECT key AS name, fullkey || '.goal.order' AS path, "
                "json_extract(value, '$.created_at') AS owner_created_at, "
                "json_set(json_extract(value, '$.goal'), '$.order', json(?)) AS goal, "
                "coalesce(json_extract(value, '$.goal.order.directives'), '[]') AS directives "
                "FROM json_each(?, '$.threads') WHERE json_extract(value, '$.goal.order.kind')='request'",
                (UNORDERED, raw)))
            for source in current:
                (raw,) = sql.execute('SELECT json_set(?, ?, json(?))', (raw, source.path, UNORDERED)).fetchone()
            document = registry._decode(FieldCodec.parse(raw))
            owners = {thread.incarnation.require_recorded().created_at: thread.incarnation.require_recorded()
                      for thread in document.threads.values()}
            todos = memory_copy(lifetime, todo_path)
            history = memory_copy(lifetime, history_path)
            todo_columns = Todo.stored_columns(todos)
            old_links = "goal" in todo_columns
            if old_links:
                for link in StoredRequestLink.read(todos.execute(
                    "SELECT json_extract(goal,'$.goal_id') AS request, "
                    "json_extract(goal,'$.owner') AS owner, json_extract(goal,'$.owner_created') AS owner_created "
                    f'FROM "{Todo.declared_name}" WHERE goal IS NOT NULL GROUP BY goal')):
                    # Deleted commanders still have their original incarnation
                    # on the orders they issued. Do not manufacture a new actor.
                    owners.setdefault(link.owner_created, RecordedThreadIncarnation(link.owner, link.owner_created))
            if OwnerRequest.stored_columns(todos):
                differences = SQLiteSchemaObject.differences(
                    TypedTable.objects_of(todos, (OwnerRequest,)), OwnerRequest.schema_objects(),
                )
                if differences:
                    raise RelationViolationError(f"Request table differs from its declaration: {differences}")
                requests = {request.id: request for request in OwnerRequest.stream(todos)}
            else:
                requests = {}
            existing = dict(requests)
            sources: dict[str, list[HistoricalRequestGoal]] = {}
            first_seen: dict[OwnerRequestId, float] = {}
            if GoalHistoryEntry.stored_columns(history):
                for column in ("before", "after"):
                    sources[column] = historical(history, column)
                committed = sorted((source for column in sources for source in historical(
                    history, column, where="state='committed'")), key=lambda source: source.sequence)
                for source in committed:
                    identity = OwnerRequestId(source.goal.id)
                    first_seen.setdefault(identity, source.observed_at)
                    try:
                        commander = owners[source.owner_created_at]
                    except KeyError:
                        raise RelationViolationError(
                            f"Request {identity.value} has no retained commander incarnation.") from None
                    requests[identity] = source.request(commander, first_seen[identity])
            for source in current:
                identity = OwnerRequestId(source.goal.id)
                first_seen.setdefault(identity, time.time())
                requests[identity] = source.request(document.threads[source.name].incarnation.require_recorded(),
                                                    first_seen[identity])
            for identity, previous in existing.items():
                if identity in requests:
                    proposed = requests[identity]
                    if (previous.commander, previous.text, previous.state, previous.directives) != (
                            proposed.commander, proposed.text, proposed.state, proposed.directives):
                        raise RelationViolationError(f"Request {identity.value} differs while finishing its carry.")
                    requests[identity] = previous
            # Validate every historical endpoint, including pending/aborted/
            # uncertain intents. They cannot become new accepted directives.
            prefixes = {}
            for column, snapshots in sources.items():
                for source in snapshots:
                    identity = OwnerRequestId(source.goal.id)
                    if identity not in requests:
                        raise RelationViolationError(
                            f"Request {identity.value} at history {source.sequence} exists only in an unresolved intent.")
                    prefixes[column, source.sequence] = requests[identity].at_revision(source.directives)
            new_rows = len(requests.keys() - existing.keys())
            todo_changed = old_links or new_rows > 0
            if todo_columns:
                if not old_links and "request" not in todo_columns:
                    raise RelationViolationError("Todo has neither the retired goal nor a request link.")
                if not OwnerRequest.stored_columns(todos):
                    OwnerRequest.create(todos)
            elif requests:
                TodoStore.create_schema(todos)
                todo_changed = True
            for request in requests.values():
                request.upsert(todos)
            linked = 0
            if old_links:
                (linked,) = todos.execute(f'SELECT count(*) FROM "{Todo.declared_name}" WHERE goal IS NOT NULL').fetchone()
                Todo.rebuild(todos, {"request": "json_extract(goal,'$.goal_id')"})
            if todo_columns or requests:
                TodoStore.require_schema(todos)
                for _ in Todo.stream(todos):
                    pass
                if list(todos.execute("PRAGMA foreign_key_check")):
                    raise RelationViolationError("Carried Todo request links violate their foreign keys.")
            history_columns = GoalHistoryEntry.stored_columns(history)
            missing = set(RequestHistory.__dataclass_fields__) - set(history_columns)
            history_changed = bool(history_columns and (missing or prefixes))
            if history_changed:
                GoalHistoryEntry.rebuild(history, {name: "NULL" for name in missing})
                for column, snapshots in sources.items():
                    for source in snapshots:
                        goal_column = GoalHistoryEntry._column_list((column,))
                        prefix_column = GoalHistoryEntry._column_list(("request_" + column,))
                        history.execute(
                            f'UPDATE "{GoalHistoryEntry.declared_name}" '
                            f"SET {goal_column}=json_set({goal_column}, '$.order', json(?)), {prefix_column}=? "
                            "WHERE sequence=?",
                            (UNORDERED, json.dumps(FieldCodec.encode(prefixes[column, source.sequence])), source.sequence),
                        )
            if history_columns:
                GoalHistoryEntry.require_schema(history)
                for entry in GoalHistoryEntry.stream(history):
                    for prefix in (entry.request_before, entry.request_after):
                        if prefix is not None:
                            request = OwnerRequest.require(todos, id=prefix.id)
                            if prefix.directive_count > len(request.directives):
                                raise RelationViolationError("Carried history exceeds its request directives.")
            rewritten: list[str] = []
            if apply:
                for changed, path, prepared in ((todo_changed, todo_path, todos), (history_changed, history_path, history)):
                    if changed:
                        prepared.commit()
                        with closing(CoordinationStore.writer(path)) as destination:
                            prepared.backup(destination)
                        path.chmod(0o600)
                        rewritten.extend(str(file.relative_to(root)) for file in SQLiteFile.files(path))
                if current:
                    registry._write_unlocked(raw)
                    rewritten.extend(str(path.relative_to(root)) for path in registry.written)
            return Carried(new_rows + int(old_links) + int(history_changed) + len(current),
                           detail={"requests": new_rows, "linked_todos": linked,
                                   "directives": sum(len(request.directives) for request in requests.values()),
                                   "history_endpoints": len(prefixes)}, rewritten=tuple(rewritten))
