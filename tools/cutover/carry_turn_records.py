"""Registry threads drop the idle active_turn stored before P482 (#1482).

P482 moved a thread's turn into its own record file; the registry now holds only
a reference to it (turn_record). An idle thread stored ``active_turn: null`` and
becomes the declared NoTurnRecord. A thread holding a turn is refused: stop its
owner first, so no live turn is carried as idle. The result is decoded and saved
by RegistryStore, so the registry and its owner guard are written together.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore

from .carry import Carried, Carry


class TurnRecords(Carry):
    """Each registry thread names its turn record instead of storing an idle turn."""

    after = "carry_todo_voids"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / "registry.json")
        if not store.path.exists():
            return Carried(0)
        with store.locked():
            raw = json.loads(store.path.read_text())
            threads = raw["threads"]
            held = sorted(name for name, thread in threads.items()
                          if thread.get("active_turn") is not None)
            if held:
                raise ValueError(f"Threads hold a turn; stop their owners first: {held}")
            idle = [name for name, thread in threads.items() if "active_turn" in thread]
            for name in idle:
                del threads[name]["active_turn"]
            document = RegistryDocument.from_wire(raw)
            if apply and idle:
                store.save_unlocked(document)
        written = (store.path.name, store.guard_path.name) if idle else ()
        return Carried(len(idle), {"threads": len(threads)}, written)


if __name__ == "__main__":
    TurnRecords.main()
