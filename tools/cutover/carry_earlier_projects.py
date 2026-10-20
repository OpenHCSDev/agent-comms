"""P574: remove each thread's current project from its saved earlier projects.

Run only during the install drain. A dry run reads without changing the registry;
RegistryEdit writes the carried document and its existing durability guard together.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_comms.registry_store import RegistryStore

from agent_comms.installer.carry import Carried, Carry


class EarlierProjects(Carry):

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / "registry.json")
        changed = {}
        with store.editing() as edit:
            for name, thread in edit.document.threads.items():
                current = Path(thread.worktree).expanduser().resolve()
                earlier = tuple(path for path in thread.earlier_projects if path != current)
                if earlier != thread.earlier_projects:
                    edit.document.threads[name] = replace(thread, earlier_projects=earlier)
                    changed[name] = len(thread.earlier_projects) - len(earlier)
            if apply:
                edit.commit()
        written = (store.path.name, store.guard_path.name) if apply and changed else ()
        return Carried(len(changed), dict(sorted(changed.items())), written)
