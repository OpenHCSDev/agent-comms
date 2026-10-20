"""Carry machine-local parking once into the canonical Core thread declarations."""

from dataclasses import dataclass
from pathlib import Path

from agent_comms.commander.config import CommanderConfig
from agent_comms.field_codec import FieldCodec
from agent_comms.registration import Registration

from .carry import Carried, Carry


@dataclass(frozen=True)
class CommanderParking:
    """Only the retired field read at this conversion boundary."""

    parked: frozenset[str] = frozenset()


class ParkedThreadsCarry(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (root / "registry.json",)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = CommanderConfig.path()
        if not path.exists():
            return Carried(0)
        legacy = FieldCodec.decode(CommanderParking, FieldCodec.parse(path.read_bytes()))
        registry = Registration(root / "registry.json")
        changed = tuple(thread.name for name in sorted(legacy.parked)
                        if not (thread := registry.require(name)).parked)
        if apply:
            for name in changed:
                registry.set_parked(name, True)
        return Carried(len(changed), rewritten=("registry.json",) if apply and changed else ())


if __name__ == "__main__":
    ParkedThreadsCarry.main()
