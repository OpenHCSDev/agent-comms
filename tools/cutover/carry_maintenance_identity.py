"""Carry an old READY maintenance witness from a name to an exact process record.

A legacy active hold contains no PID or birth time. It cannot honestly be called
abandoned; this one-time converter refuses it rather than fabricating an owner.
There is no legacy reader in the runtime barrier.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, Id128Text, compile_decoder
from agent_comms.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceOperator,
    MaintenanceState,
    ReadyPhase,
)

from .carry import Carried, Carry


@dataclass(frozen=True, kw_only=True)
class OldMarker:
    version: Literal[1]
    root: str
    generation: int


@dataclass(frozen=True, kw_only=True)
class OldReadyState(OldMarker):
    operator: str
    nonce: str
    phase: type[ReadyPhase]

    def carried(self, barrier: MaintenanceBarrier, *, apply: bool) -> Carried:
        marker = FieldCodec.decode(OldMarker, FieldCodec.parse(barrier.marker_path.read_text()))
        expected = OldMarker(version=1, root=self.root, generation=self.generation)
        if marker != expected or self.root != str(barrier.registry_path.parent.resolve()):
            raise RelationViolationError("Legacy maintenance witnesses do not identify this root")
        converted = MaintenanceState(
            version=2,
            root=self.root,
            generation=self.generation + 1,
            operator=MaintenanceOperator.current(),
            nonce=Id128Text.mint(),
            phase=ReadyPhase,
        )
        if apply:
            barrier._write_unlocked(converted)
        return Carried(1, rewritten=tuple(path.name for path in barrier.witness_paths))


@dataclass(frozen=True, kw_only=True)
class IdentifiedState(MaintenanceState):
    def carried(self, barrier: MaintenanceBarrier, *, apply: bool) -> Carried:
        barrier._current_unlocked()
        return Carried(0)


StoredMaintenanceState = IdentifiedState | OldReadyState


class MaintenanceIdentity(Carry):
    @classmethod
    def precedes(cls, later: type[Carry]) -> bool:
        return later is not cls

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return MaintenanceBarrier(root / "registry.json").witness_paths

    @classmethod
    def before_stop(cls, root: Path, *, apply: bool) -> Carried:
        return cls.carry(root, apply=apply)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        barrier = MaintenanceBarrier(root / "registry.json")
        if not barrier.state_path.exists():
            if barrier.marker_path.exists():
                raise RelationViolationError("Maintenance has a marker but no stored holder")
            return Carried(0)
        state: StoredMaintenanceState = compile_decoder(StoredMaintenanceState).decode(
            FieldCodec.parse(barrier.state_path.read_text())
        )
        return state.carried(barrier, apply=apply)


if __name__ == "__main__":
    MaintenanceIdentity.main()
