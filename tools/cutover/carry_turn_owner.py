"""Record each referenced turn's owner as the process identity it was leased to.

A turn file stored its owner as a bare PID, so a reused PID owned a stale turn.
The owner is now the thread's ``ProcessIdentity``. Only turn files a thread still
references are ever read; each is rewritten from the identity its thread recorded.
A referenced turn whose PID differs from its thread's process has no recorded
owner, and the carry refuses it instead of inventing a start time.

Delete after every bus has run this step.
"""

from __future__ import annotations

import json

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import Carried, Carry
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _atomic_write_text
from agent_comms.thread_identity import TurnIdentity
from agent_comms.turn_lease import ActiveTurn, StoredTurnRecord


class TurnOwner(Carry):
    def carry(self, *, apply: bool) -> Carried:
        registry = RegistryStore.at(self.root)
        if not registry.path.exists():
            return Carried(0)
        rewritten: list[str] = []
        for thread in registry.read_snapshot_unlocked().threads.values():
            if not thread.executing:
                continue
            path = StoredTurnRecord.for_identity(
                registry.path, TurnIdentity(thread.incarnation, thread.turn_generation)).path
            stored = json.loads(path.read_text())
            if "owner_pid" not in stored:
                continue
            owner = thread.require_process()
            if owner.pid != stored["owner_pid"]:
                raise RelationViolationError(
                    f"{path} names PID {stored['owner_pid']}, which {thread.name!r} "
                    "never recorded as its process."
                )
            carried = {
                ("owner" if key == "owner_pid" else key):
                    (FieldCodec.encode(owner, ProcessIdentity) if key == "owner_pid" else value)
                for key, value in stored.items()
            }
            FieldCodec.decode(ActiveTurn, carried)
            if apply:
                _atomic_write_text(path, json.dumps(carried))
            rewritten.append(str(path.relative_to(self.root)))
        return Carried(len(rewritten), rewritten=tuple(rewritten) if apply else ())
