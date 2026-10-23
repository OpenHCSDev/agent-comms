"""Retire superseded stores after their writers and consumers have been removed."""

from pathlib import Path
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.typed_table import HeldBusFile

from .retired_store_files import RetiredRootFile


class RetiredStores(Carry):
    opened_files: ClassVar[tuple[type[RetiredRootFile], ...]] = (RetiredRootFile,)

    def carry(self, *, apply: bool) -> Carried:
        owners = {
            path: owner
            for family in self.opened_files
            for owner, directory in family.declarations_in(self.root)
            for path in owner.primaries_in(directory)
        }
        rewritten: list[Path] = []
        for path, owner in owners.items():
            self.require_confined(self.root, path)
            if apply:
                HeldBusFile(owner, path).require()
                for member in owner.retirement_paths(path):
                    if member.exists():
                        member.unlink()
                        rewritten.append(member.relative_to(self.root))
        return Carried(len(owners), rewritten=tuple(rewritten))
