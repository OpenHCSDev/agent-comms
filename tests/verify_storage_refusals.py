"""Read a real refused native result on a board copy; never run its tool again.

Usage: python tests/verify_storage_refusals.py ORIGINAL_ROOT NEW_COPY_ROOT UNIT
Only the copied registry/Todo stores may be written. Native history is read-only.
"""
from __future__ import annotations

import collections
import io
import json
import shutil
import sys
from contextlib import closing, redirect_stdout
from pathlib import Path

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander
from agent_comms.commander import tool_watch
from agent_comms.coordination_database import CoordinationStore
from agent_comms.native_entries import NativeEntry
from agent_comms.registry_store import RegistryStore
from agent_comms.todos import TodoStore
from cutover.carry_storage_refusals import StorageRefusals


def copy_store(source: Path, destination: Path) -> None:
    with source.open('rb') as stream:
        sqlite = stream.read(16) == b'SQLite format 3\0'
    if sqlite:
        with closing(CoordinationStore.read_snapshot(source)) as reader:
            with closing(CoordinationStore.writer(destination)) as writer:
                reader.backup(writer)
    else:
        shutil.copy2(source, destination)


def main() -> None:
    original, root = Path(sys.argv[1]), Path(sys.argv[2])
    unit = sys.argv[3]
    config = CommanderConfig.read_root(original)
    root.mkdir(parents=True, exist_ok=False)
    for source in (TodoStore.path_in(original), RegistryStore.at(original).path):
        copy_store(source, root / source.name)
    first = StorageRefusals.carry(root, apply=True)
    assert StorageRefusals.carry(root, apply=True).changed == 0
    context = Commander(**config.model_dump(exclude={'root', 'state'}),
                        root=root, state=root/'state')
    assert unit in context.units()
    session = context.comms.registry.require(unit).session_file
    assert session
    before = context.comms.goals.todos.for_commander(context.incarnation)
    # Bound the saved-history read. Missing proof is a refusal, never a success.
    with Path(session).open('rb') as source:
        source.seek(0, 2)
        size = source.tell()
        source.seek(max(0, size - 4 * 1024 * 1024))
        if source.tell():
            source.readline()
        lines = source.readlines()
    frame = next((line for line in reversed(lines)
                  if any(tuple(message.storage_refusals())
                         for message in NativeEntry.read(line).messages)), None)
    assert frame is not None, 'No typed storage refusal in the bounded native history tail'
    output = io.StringIO()
    with redirect_stdout(output):
        for _ in range(3):
            tool_watch.follow(context, collections.defaultdict(list), unit, frame)
    after = context.comms.goals.todos.for_commander(context.incarnation)
    assert before == after
    assert output.getvalue().count(f'VIOLATION storage {unit}:') == 3
    print(json.dumps(first.report('carry_storage_refusals', True)))
    print(output.getvalue().strip())
    print('Copied board unchanged across three reads; original stores/history read-only; no tool replay')


if __name__ == '__main__':
    main()
