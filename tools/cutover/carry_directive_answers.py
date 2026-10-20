"""P285: remove obsolete directive counters from the registry and goal journal."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryStore
from agent_comms.goals import GoalOrder, RequestGoalOrder
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.typed_table import Column, JsonStorage, TypedRow

from agent_comms.installer.carry import Carried, Carry


@dataclass(frozen=True)
class SavedGoalJson(TypedRow):
    sequence: int
    payload: JsonValue = field(metadata={"sql": Column(storage=JsonStorage)})


class DirectiveAnswers(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        store = RegistryStore.at(root)
        return store.path, store.guard_path, GoalHistoryStore.path_for(store.path)

    @classmethod
    def convert(cls, value: JsonValue) -> int:
        changed = 0
        match value:
            case dict():
                directives = value.get("directives")
                if value.get(GoalOrder.family_discriminator) == FieldCodec.encode(
                    RequestGoalOrder
                ) and isinstance(directives, list):
                    for directive in directives:
                        if isinstance(directive, dict) and "served" in directive:
                            directive.pop("served")
                            changed += 1
                changed += sum(cls.convert(member) for member in value.values())
            case list():
                changed += sum(cls.convert(member) for member in value)
        return changed

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore.at(root)
        changed = 0
        rewritten: list[str] = []
        history = GoalHistoryStore.path_for(store.path)
        if history.exists():
            connection = (
                CoordinationStore.writer(history)
                if apply
                else CoordinationStore.read_snapshot(history)
            )
            with closing(connection) as db, db:
                for column in GoalHistoryEntry._fields():
                    if column.storage is not JsonStorage:
                        continue
                    name = GoalHistoryEntry._column_list((column.name,))
                    rows = SavedGoalJson.read(
                        db.execute(
                            f"SELECT sequence, {name} AS payload "
                            f"FROM {GoalHistoryEntry.declared_name}"
                        )
                    )
                    for row in rows:
                        found = cls.convert(row.payload)
                        changed += found
                        if found and apply:
                            db.execute(
                                f"UPDATE {GoalHistoryEntry.declared_name} SET {name}=? "
                                "WHERE sequence=?",
                                (json.dumps(row.payload), row.sequence),
                            )
            if changed and apply:
                rewritten.extend(path.name for path in root.glob(f"{history.name}*"))
        if store.path.exists():
            with store.locked():
                store.private_guard_unlocked()
                raw = FieldCodec.decode(
                    dict[str, JsonValue], FieldCodec.parse(store.path.read_bytes())
                )
                found = cls.convert(raw)
                document = RegistryDocument.from_wire(raw)
                changed += found
                if found and apply:
                    store.save_unlocked(document)
                    rewritten.extend((store.path.name, store.guard_path.name))
        return Carried(changed, rewritten=tuple(rewritten))
