"""Remove obsolete schema-named derived files; their sources rebuild the named stores.

Run only by the installer under its maintenance hold. Delete this step after the
machine's live installation has recorded it in CarryLedger.
"""

from pathlib import Path
import re

from agent_comms.installer.carry import Carried, Carry
from agent_comms.typed_table import DerivedStore


class DerivedStoreNames(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return ()

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        obsolete = re.compile(r"[a-z0-9_]+\.[0-9a-f]{12}\.sqlite3")
        paths = tuple(sorted({
            member
            for path in root.iterdir() if obsolete.fullmatch(path.name)
            for member in DerivedStore.member_paths(path) if member.exists()
        }))
        for path in paths:
            cls.require_confined(root, path)
            if apply:
                path.unlink()
        return Carried(len(paths), rewritten=tuple(path.name for path in paths))
