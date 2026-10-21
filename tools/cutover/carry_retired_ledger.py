"""Delete the carry ledger: each step's own dry run now says whether a bus needs it."""

from pathlib import Path

from agent_comms.installer.carry import Carried, Carry


class RetiredLedger(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (root / "carries.json",)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        paths = tuple(path for path in cls.backup_files(root) if path.exists())
        for path in paths:
            cls.require_confined(root, path)
            if apply:
                path.unlink()
        return Carried(len(paths), {}, tuple(path.name for path in paths) if apply else ())
