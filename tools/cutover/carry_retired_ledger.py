"""Delete the carry ledger: each step's own dry run now says whether a bus needs it."""

from pathlib import Path

from agent_comms.installer.carry import Carried, Carry


class RetiredLedger(Carry):
    def backup_files(self) -> tuple[Path, ...]:
        return (self.root / "carries.json",)

    def carry(self) -> Carried:
        paths = tuple(path for path in self.backup_files() if path.exists())
        for path in paths:
            self.require_confined(self.root, path)
            if self.apply:
                path.unlink()
        return Carried(len(paths), {}, tuple(path.name for path in paths) if self.apply else ())
