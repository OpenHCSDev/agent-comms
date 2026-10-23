"""Move each thread's finished turn ID into its turn record.

Delete after every bus has run this step. The retired ``last_finished_turn_id``
field is understood only here; the thread decoder refuses it.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.carry import Carried, Carry
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _atomic_write_text, _store_lock

from .carry_bus_history import BusHistory, CarryCatalogSource
from .carry_dead_turns import DeadTurns
from .carry_owner_requests import OwnerRequests
from .carry_turn_owner import TurnOwner

RETIRED = "last_finished_turn_id"


class FinishedTurns(Carry):
    precedes = (BusHistory, DeadTurns, OwnerRequests, TurnOwner)
    """Each decodes the registry, which refuses the retired field."""

    def backup_files(self) -> tuple[Path, ...]:
        sources = CarryCatalogSource.read(self.root, self.original_root)
        roots = (
            self.root,
            *(source.mapped_root(self.root, self.original_root) for source in sources),
        )
        paths = (
            HistoryArchive.path_in(self.root),
            *(path for root in roots for path in RegistryStore.at(root).written),
        )
        for path in paths:
            self.require_confined(self.root, path)
        return tuple(path for path in paths if path.exists())

    def carry(self, *, apply: bool) -> Carried:
        with _store_lock(HistoryArchive.path_in(self.root)):
            self.backup_files()
            sources = CarryCatalogSource.read(self.root, self.original_root)
            results = [(self.root, self.carry_root(self.root, apply=apply))]
            rebound = []
            resealed = False
            for source in sources:
                mapped = source.mapped_root(self.root, self.original_root)
                result = self.carry_root(mapped, apply=apply)
                results.append((mapped, result))
                if apply and result.rewritten:
                    source = source.carried_registry(mapped)
                    resealed = True
                rebound.append(source)
            rewritten = [
                str(root.relative_to(self.root) / name)
                for root, result in results
                for name in result.rewritten
            ]
            if resealed:
                _atomic_write_text(
                    HistoryArchive.path_in(self.root), json.dumps(FieldCodec.encode(tuple(rebound)))
                )
                rewritten.append(HistoryArchive.filename)
            finished = sum(int(result.detail.get("finished", 0)) for _, result in results)
            return Carried(
                sum(result.changed for _, result in results),
                {"finished": finished},
                tuple(rewritten),
            )

    def carry_root(self, root: Path, *, apply: bool) -> Carried:
        registry = RegistryStore.at(root)
        if not registry.path.exists():
            return Carried(0)
        with registry.locked():
            registry.private_guard_unlocked()
            document = FieldCodec.parse(registry.path.read_text())
            threads = document["threads"]
            carried = finished = 0
            for name, thread in threads.items():
                if RETIRED not in thread:
                    continue
                turn_id = thread.pop(RETIRED)
                carried += 1
                if turn_id is None:
                    continue
                if thread.get("turn_record") is not None:
                    raise RelationViolationError(
                        f"Thread {name!r} holds a live turn beside a finished one."
                    )
                thread["turn_record"] = {"turn_id": turn_id}
                finished += 1
            if not carried:
                return Carried(0)
            raw = json.dumps(document)
            registry._decode(FieldCodec.parse(raw))
            rewritten: tuple[str, ...] = ()
            if apply:
                registry._write_unlocked(raw)
                rewritten = tuple(str(path.relative_to(root)) for path in registry.written)
            return Carried(carried, {"finished": finished}, rewritten)
