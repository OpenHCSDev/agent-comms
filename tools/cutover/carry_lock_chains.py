"""Delete the locks of locks that ``Thread`` once created in native session directories.

``Thread`` declared ``native-sessions/{lookup}/{name}``, so every file in a session
directory matched it, its lock files included; each copy then locked the lock it
matched, one level deeper per walk (962 files on the live bus, 26 deep).
``ThreadCatalog`` now declares the one file there, and a lock is never itself
lockable, so no new chain can start. A first-level lock still belongs to its store.

Delete after every bus has run this step.
"""

from __future__ import annotations

from pathlib import Path

from agent_comms.installer.carry import Carried, Carry
from agent_comms.store_files import is_store_lock, locked_store_path
from agent_comms.threads import ThreadCatalog


class LockChains(Carry):
    opened_files = ()
    """No declared file owns a lock of a lock; its inputs are the chains it finds."""

    def chained(self) -> tuple[Path, ...]:
        paths = tuple(sorted(
            path for path in self.root.glob(str(ThreadCatalog.pattern().parent / "*"))
            if is_store_lock(path) and is_store_lock(locked_store_path(path))
        ))
        for path in paths:
            self.require_confined(self.root, path)
        return paths

    def input_files(self) -> tuple[Path, ...]:
        return self.chained()

    def carry(self, *, apply: bool) -> Carried:
        chained = self.chained()
        if apply:
            for path in chained:
                path.unlink()
        return Carried(len(chained), detail={"deleted": len(chained)},
                       rewritten=tuple(path.relative_to(self.root) for path in chained)
                       if apply else ())
