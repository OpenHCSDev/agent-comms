"""Carry a copy of the actual board and re-read one known-refused native tool call.

No tool executes: tool-watch reads the original native frame through its production
parser and storage guard. Only the copied stores can receive board writes.
"""

from __future__ import annotations

import collections
import io
import json
import shutil
import sys
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path

from agent_comms.commander import order, tool_watch
from agent_comms.comms import wire
from agent_comms.coordination_database import CoordinationStore
from agent_comms.registry_store import RegistryStore
from agent_comms.todos import TodoStore
from cutover.carry_storage_refusals import StorageRefusals


def copy_store(source: Path, destination: Path) -> None:
    with source.open("rb") as stream:
        sqlite = stream.read(16) == b"SQLite format 3\0"
    if sqlite:
        with closing(CoordinationStore.read_snapshot(source)) as reader:
            with closing(CoordinationStore.writer(destination)) as writer:
                reader.backup(writer)
    else:
        shutil.copy2(source, destination)


def main() -> None:
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=False)
    original = order.COMMS
    unit = "pi-cmd-tools"
    session = original.registry.require(unit).session_file
    assert session, "The registered native source must exist"
    for source in (TodoStore.path_in(original.root), RegistryStore.at(original.root).path):
        copy_store(source, root / source.name)
    first = StorageRefusals.carry(root, apply=True)
    assert StorageRefusals.carry(root, apply=True).changed == 0
    print(json.dumps(first.report("carry_storage_refusals", True)), flush=True)
    copied = wire(root)
    order.COMMANDER = replace(order.COMMANDER, comms=copied)
    order.COMMS = copied
    board = copied.goals.todos
    before = board.for_commander(copied.registry.require(order.ME).incarnation)
    output = io.StringIO()
    # This command was refused, with a recorded result, during P265. It is read
    # as watcher history only, never sent to a tool runner or provider again.
    marker = b"p265-${monitor}-initial.log"
    with Path(session).open("rb") as source, redirect_stdout(output):
        for line in source:
            if marker in line:
                tool_watch.follow(copied.registry, collections.defaultdict(list), unit, line)
    after = board.for_commander(copied.registry.require(order.ME).incarnation)
    assert before == after, "Watching the refusal changed the copied board"
    assert "VIOLATION storage pi-cmd-tools:" in output.getvalue(), output.getvalue()
    print(output.getvalue().strip())
    print("copied board unchanged by refusal; all assignments and strike counts preserved")
    print("original stores opened read-only; no provider input or tool command replayed")


if __name__ == "__main__":
    main()
