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

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryStore
from agent_comms.goals import Goal, UnorderedGoalOrder
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.carry import Carried, Carry
from agent_comms.lifecycle_capability import Complete
from agent_comms.locked_store import RegistryFile, SQLiteFile
from agent_comms.owner_requests import (
    CompletedRequestState,
    OpenRequestState,
    OwnerRequestId,
    RequestDirective,
)
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import AtomicFile, _store_lock
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import OwnerRequest, Todo, TodoStore
from agent_comms.typed_table import (
    Column,
    JsonStorage,
    SQLiteSchemaObject,
    TextStorage,
    TypedRow,
    TypedTable,
    WriterOwnedFile,
)

from .carry_bus_history import ArchivedCarry, BusHistory, CarryCatalogSource


@dataclass(frozen=True)
class StoredRequestGoal(TypedRow):
    """Decode the old directive payload separately from the normalized Goal."""

    goal: Annotated[Goal, Column(storage=JsonStorage)]
    directives: Annotated[tuple[RequestDirective, ...], Column(storage=JsonStorage)]
    owner_created_at: float

    def request(self, commander: RecordedThreadIncarnation, created_at: float) -> OwnerRequest:
        if commander.created_at != self.owner_created_at:
            raise RelationViolationError(
                "Retained request belongs to another commander incarnation."
            )
        state = (
            CompletedRequestState(self.goal.progress)
            if self.goal.state.items_with(Complete, self.goal)
            else OpenRequestState()
        )
        return OwnerRequest(
            OwnerRequestId(self.goal.id),
            commander,
            self.goal.text,
            state,
            self.directives,
            created_at,
        )


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
    db = lifetime.enter_context(closing(SQLiteFile.writer(":memory:")))
    if path.exists():
        with closing(SQLiteFile.read_snapshot(path)) as original:
            original.backup(db)
    return db


def historical(
    db: sqlite3.Connection, column: str, *, where: str = "1"
) -> list[HistoricalRequestGoal]:
    return HistoricalRequestGoal.read(
        GoalHistoryEntry.orders_with_replacement(
            db, column, UNORDERED, order_kind="request", where=where
        )
    )


class OwnerRequests(ArchivedCarry):
    opened_files = (HistoryArchive, RegistryFile, TodoStore, GoalHistoryStore)
    precedes = (BusHistory,)


    def carry(self, *, apply: bool) -> Carried:
        with _store_lock(HistoryArchive.path_in(self.root)):
            self.input_files()
            sources = CarryCatalogSource.read(self.root, self.original_root)
            results = [(self.root, self.carry_root(self.root, apply=apply))]
            rebound = []
            resealed = False
            for source in sources:
                mapped = source.mapped_root(self.root, self.original_root)
                result = self.carry_root(mapped, apply=apply)
                results.append((mapped, result))
                if apply and RegistryStore.at(mapped).path.relative_to(mapped) in result.rewritten:
                    source = source.carried_registry(mapped)
                    resealed = True
                rebound.append(source)
            rewritten = [
                root.relative_to(self.root) / name
                for root, result in results
                for name in result.rewritten
            ]
            if resealed:
                AtomicFile(HistoryArchive.path_in(self.root)).write_text(json.dumps(FieldCodec.encode(tuple(rebound))))
                rewritten.append(HistoryArchive.path_in(self.root).relative_to(self.root))
            detail: dict[str, int] = {}
            for _, result in results:
                for key, value in result.detail.items():
                    detail[key] = detail.get(key, 0) + value
            return Carried(sum(result.changed for _, result in results), detail, tuple(rewritten))

    def carry_root(self, root: Path, *, apply: bool) -> Carried:
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
            sql = lifetime.enter_context(closing(SQLiteFile.writer(":memory:")))
            current = CurrentRequestGoal.read(
                sql.execute(
                    "SELECT key AS name, fullkey || '.goal.order' AS path, "
                    "json_extract(value, '$.created_at') AS owner_created_at, "
                    "json_set(json_extract(value, '$.goal'), '$.order', json(?)) AS goal, "
                    "coalesce(json_extract(value, '$.goal.order.directives'), '[]') AS directives "
                    "FROM json_each(?, '$.threads') "
                    "WHERE json_extract(value, '$.goal.order.kind')='request'",
                    (UNORDERED, raw),
                )
            )
            for current_source in current:
                (raw,) = sql.execute(
                    "SELECT json_set(?, ?, json(?))", (raw, current_source.path, UNORDERED)
                ).fetchone()
            document = registry._decode(FieldCodec.parse(raw))
            owners = {}
            for thread in document.threads.values():
                recorded_commander = thread.incarnation.require_recorded()
                owners[recorded_commander.created_at] = recorded_commander
            todos = memory_copy(lifetime, todo_path)
            history = memory_copy(lifetime, history_path)
            todo_columns = Todo.stored_columns(todos)
            old_links = "goal" in todo_columns
            if old_links:
                for link in StoredRequestLink.read(
                    todos.execute(
                        "SELECT json_extract(goal,'$.goal_id') AS request, "
                        "json_extract(goal,'$.owner') AS owner, "
                        "json_extract(goal,'$.owner_created') AS owner_created "
                        f'FROM "{Todo.declared_name}" WHERE goal IS NOT NULL GROUP BY goal'
                    )
                ):
                    # Deleted commanders still have their original incarnation
                    # on the orders they issued. Do not manufacture a new actor.
                    owners.setdefault(
                        link.owner_created,
                        RecordedThreadIncarnation(link.owner, link.owner_created),
                    )
            if OwnerRequest.stored_columns(todos):
                differences = SQLiteSchemaObject.differences(
                    TypedTable.objects_of(todos, (OwnerRequest,)),
                    OwnerRequest.schema_objects(),
                )
                if differences:
                    raise RelationViolationError(
                        f"Request table differs from its declaration: {differences}"
                    )
                requests = {request.id: request for request in OwnerRequest.stream(todos)}
            else:
                requests = {}
            existing = dict(requests)
            sources: dict[str, list[HistoricalRequestGoal]] = {}
            first_seen: dict[OwnerRequestId, float] = {}
            if GoalHistoryEntry.stored_columns(history):
                for column in GoalHistoryEntry.endpoint_columns():
                    sources[column] = historical(history, column)
                committed = sorted(
                    (
                        source
                        for column in sources
                        for source in historical(history, column, where="state='committed'")
                    ),
                    key=lambda source: source.sequence,
                )
                for source in committed:
                    identity = OwnerRequestId(source.goal.id)
                    first_seen.setdefault(identity, source.observed_at)
                    try:
                        commander = owners[source.owner_created_at]
                    except KeyError:
                        raise RelationViolationError(
                            f"Request {identity.value} has no retained commander incarnation."
                        ) from None
                    requests[identity] = source.request(commander, first_seen[identity])
            for current_source in current:
                identity = OwnerRequestId(current_source.goal.id)
                first_seen.setdefault(identity, time.time())
                requests[identity] = current_source.request(
                    document.threads[current_source.name].incarnation.require_recorded(),
                    first_seen[identity],
                )
            for identity, previous in existing.items():
                if identity in requests:
                    proposed = requests[identity]
                    if (previous.commander, previous.text, previous.state, previous.directives) != (
                        proposed.commander,
                        proposed.text,
                        proposed.state,
                        proposed.directives,
                    ):
                        raise RelationViolationError(
                            f"Request {identity.value} differs while finishing its carry."
                        )
                    requests[identity] = previous
            # Validate every historical endpoint, including pending/aborted/
            # uncertain intents. They cannot become new accepted directives.
            prefixes = {}
            for column, snapshots in sources.items():
                for source in snapshots:
                    identity = OwnerRequestId(source.goal.id)
                    if identity not in requests:
                        raise RelationViolationError(
                            f"Request {identity.value} at history {source.sequence} "
                            "exists only in an unresolved intent."
                        )
                    prefixes[column, source.sequence] = requests[identity].at_revision(
                        source.directives
                    )
            new_rows = len(requests.keys() - existing.keys())
            todo_changed = old_links or new_rows > 0
            if todo_columns:
                if not old_links and "request" not in todo_columns:
                    raise RelationViolationError(
                        "Todo has neither the retired goal nor a request link."
                    )
                if not OwnerRequest.stored_columns(todos):
                    OwnerRequest.reconcile_schema(todos, WriterOwnedFile())
            elif requests:
                TodoStore.reconcile_schema(todos, WriterOwnedFile())
                todo_changed = True
            for request in requests.values():
                request.upsert(todos)
            linked = 0
            if old_links:
                (linked,) = todos.execute(
                    f'SELECT count(*) FROM "{Todo.declared_name}" WHERE goal IS NOT NULL'
                ).fetchone()
                Todo.rebuild(todos, {"request": "json_extract(goal,'$.goal_id')"})
            if todo_columns or requests:
                TodoStore.require_schema(todos)
                for _ in Todo.stream(todos):
                    pass
                if list(todos.execute("PRAGMA foreign_key_check")):
                    raise RelationViolationError(
                        "Carried Todo request links violate their foreign keys."
                    )
            history_columns = GoalHistoryEntry.stored_columns(history)
            history_changed = bool(
                history_columns and (prefixes or GoalHistoryEntry.schema_differs(history))
            )
            if history_changed:
                GoalHistoryEntry.reconcile_schema(history, WriterOwnedFile())
                for column, snapshots in sources.items():
                    for source in snapshots:
                        GoalHistoryEntry.move_request_order(
                            history,
                            column,
                            source.sequence,
                            prefixes[column, source.sequence],
                            UNORDERED,
                        )
            if history_columns:
                GoalHistoryEntry.require_schema(history)
                for entry in GoalHistoryEntry.stream(history):
                    for prefix in entry.request_revisions:
                        prefix.validate(requests)
            rewritten: list[Path] = []
            if apply:
                # Prepared in-memory files are private to this carry. Replacing a
                # retained file requires the installer's actual maintenance hold.
                if todo_changed:
                    TodoStore.schema_change(TodoStore.path_in(self.root)).require()
                if history_changed:
                    GoalHistoryStore.schema_change(GoalHistoryStore.path_in(self.root)).require()
                for changed, path, prepared in (
                    (todo_changed, todo_path, todos),
                    (history_changed, history_path, history),
                ):
                    if changed:
                        prepared.commit()
                        with closing(SQLiteFile.writer(path)) as destination:
                            prepared.backup(destination)
                        path.chmod(0o600)
                        rewritten.append(path.relative_to(root))
                if todo_changed:
                    TodoStore.publish_change(todo_path)
                    rewritten.extend(
                        path.relative_to(root) for path in TodoStore.source_paths(root)
                    )
                if current:
                    registry._write_unlocked(raw)
                    rewritten.extend(path.relative_to(root) for path in registry.written)
            return Carried(
                new_rows + int(old_links) + int(history_changed) + len(current),
                detail={
                    "requests": new_rows,
                    "linked_todos": linked,
                    "directives": sum(len(request.directives) for request in requests.values()),
                    "history_endpoints": len(prefixes),
                },
                rewritten=tuple(rewritten),
            )
