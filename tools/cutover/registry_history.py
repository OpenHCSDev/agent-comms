"""Stage retained registry and goal history once; delete after quiet installation."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, fields
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_pauses import GoalPauseEvent
from agent_comms.goal_states import ActiveGoal, GoalState, PausedGoal, PauseSource
from agent_comms.goals import Goal
from agent_comms.registry_document import RegistryDocument
from agent_comms.store_files import _atomic_write_text, file_revision
from agent_comms.thread_identity import GenerationCounter
from agent_comms.thread_status import StoppedThreadStatus, ThreadStatus
from agent_comms.threads import Thread


@dataclass(frozen=True)
class StoredGoal(Goal):
    """The flat saved format, restricted to this disposable conversion tool."""

    state: GoalState = field(init=False, default_factory=ActiveGoal)
    status: type[GoalState] = field(default=ActiveGoal, metadata={"wire_required": True})
    block_reason: str | None = None
    pause_source: type[PauseSource] | None = None

    def current(self, pauses: dict[str, GoalPauseEvent]) -> Goal:
        event = pauses.get(f"{self.id}:{self.revision}")
        source = self.pause_source or (type(event.source) if event else None)
        state = FieldCodec.decode(
            GoalState,
            self.status.wire_payload(
                self.block_reason, source.declared_name if source is not None else None
            ),
        )
        return Goal(
            **{
                item.name: state if item.name == "state" else getattr(self, item.name)
                for item in fields(Goal)
                if item.init
            }
        )


@dataclass(frozen=True)
class StoredThread(Thread):
    pid: int = 0
    goal: StoredGoal | None = None
    status: type[ThreadStatus] = StoppedThreadStatus
    last_seen: float = 0.0

    def current(self, pauses: dict[str, GoalPauseEvent]) -> Thread:
        values = {item.name: getattr(self, item.name) for item in fields(Thread) if item.init}
        values.update(
            process_identity=None,
            active_turn=None,
            goal=self.goal.current(pauses) if self.goal else None,
        )
        return Thread(**values)


@dataclass(frozen=True)
class StoredRegistry:
    threads: dict[str, StoredThread]
    aliases: dict[str, str]
    owner_epoch_counter: int
    owner_epochs: dict[str, int]
    admission_generation_counter: int
    admission_generations: dict[str, int]
    turn_epochs: dict[str, int] = field(default_factory=dict)

    def current(self, pauses: dict[str, GoalPauseEvent]) -> RegistryDocument:
        return RegistryDocument(
            threads={name: thread.current(pauses) for name, thread in self.threads.items()},
            statuses={name: thread.status() for name, thread in self.threads.items()},
            last_seen={name: thread.last_seen for name, thread in self.threads.items()},
            aliases=self.aliases,
            owners=GenerationCounter(self.owner_epoch_counter, self.owner_epochs),
            admissions=GenerationCounter(
                self.admission_generation_counter, self.admission_generations
            ),
        )


@dataclass(frozen=True)
class RegistryRewrite:
    source: str
    staged: str
    threads: int
    goals: int
    history_entries: int
    paused_without_attribution: tuple[str, ...]


def stage(source: Path, destination: Path) -> RegistryRewrite:
    """Copy before rewriting; source stores are opened read-only and never initialized.

    Original registry bytes and a SQLite backup retain process/turn evidence and
    pending/uncertain journal rows. Reset only copied process bindings, retaining
    thread incarnations, aliases and monotonic generation domains. No input runs.
    """
    source, destination = source.resolve(), destination.absolute()
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Stage must be separate from retained source history")
    registry_path = source / "registry.json"
    pauses_path = source / "goal_pause_events.json"
    history_path = source / "goal_history.sqlite3"
    before = tuple(file_revision(path) for path in (registry_path, pauses_path, history_path))
    original = registry_path.read_text()
    raw = json.loads(original)
    if any("created_at" not in thread for thread in raw["threads"].values()):
        raise ValueError("Stored creation identity is missing; do not synthesize one")
    stored = FieldCodec.decode(StoredRegistry, raw)
    pauses = (
        {
            key: GoalPauseEvent.from_wire(row)
            for key, row in json.loads(pauses_path.read_text()).items()
        }
        if pauses_path.exists()
        else {}
    )
    current = stored.current(pauses)
    document = FieldCodec.encode(current)
    RegistryDocument.from_wire(document)
    missing: set[str] = set()

    def convert_goal(goal: StoredGoal) -> Goal:
        key = f"{goal.id}:{goal.revision}"
        if goal.status is PausedGoal and goal.pause_source is None and key not in pauses:
            missing.add(key)
        return goal.current(pauses)

    for thread in stored.threads.values():
        if thread.goal is not None:
            convert_goal(thread.goal)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    _atomic_write_text(destination / "original-registry.json", original)
    _atomic_write_text(destination / "registry.json", json.dumps(document))
    if pauses_path.exists():
        _atomic_write_text(destination / pauses_path.name, pauses_path.read_text())
    count = 0
    if history_path.exists():
        saved = destination / "original-goal-history.sqlite3"
        rewritten = destination / history_path.name
        with (
            closing(sqlite3.connect(history_path.as_uri() + "?mode=ro", uri=True)) as origin,
            closing(sqlite3.connect(saved)) as archive,
            closing(sqlite3.connect(rewritten)) as output,
        ):
            origin.backup(archive)
            archive.backup(output)
            os.chmod(saved, 0o600)
            os.chmod(rewritten, 0o600)
            rows = output.execute(
                "SELECT sequence, before_goal, after_goal FROM entries"
            ).fetchall()
            with output:
                for sequence, before_goal, after_goal in rows:
                    goals = [
                        json.dumps(
                            convert_goal(FieldCodec.decode(StoredGoal, json.loads(value))).to_wire()
                        )
                        if value is not None
                        else None
                        for value in (before_goal, after_goal)
                    ]
                    output.execute(
                        "UPDATE entries SET before_goal=?, after_goal=? WHERE sequence=?",
                        (*goals, sequence),
                    )
            count = len(rows)
    if before != tuple(file_revision(path) for path in (registry_path, pauses_path, history_path)):
        raise ValueError("Source changed during staging; candidate is not installable")
    receipt = RegistryRewrite(
        str(source),
        str(destination),
        len(current.threads),
        sum(thread.goal is not None for thread in current.threads.values()),
        count,
        tuple(sorted(missing)),
    )
    _atomic_write_text(
        destination / "registry-rewrite-receipt.json",
        json.dumps(FieldCodec.encode(receipt), indent=2),
    )
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(FieldCodec.encode(stage(arguments.source, arguments.destination)), indent=2))
