"""Carry machine-local parking once into the canonical Core thread declarations."""

from pathlib import Path

from agent_comms.commander.config import CommanderConfig
from agent_comms.registration import Registration

from .carry import Carried, Carry


class CommanderParking(CommanderConfig):
    """Only the retired field read at this conversion boundary."""

    parked: frozenset[str] = frozenset()


class ParkedThreadsCarry(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (root / "registry.json",)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = CommanderConfig.root_path(root)
        if not path.exists():
            return Carried(0)
        legacy = CommanderParking.model_validate_json(path.read_text())
        registry = Registration(root / "registry.json")
        changed = tuple(
            thread.name
            for name in sorted(legacy.parked)
            if not (thread := registry.require(name)).parked
        )
        if apply:
            for name in changed:
                registry.set_parked(name, True)
        return Carried(len(changed), rewritten=("registry.json",) if apply and changed else ())


if __name__ == "__main__":
    ParkedThreadsCarry.main()
