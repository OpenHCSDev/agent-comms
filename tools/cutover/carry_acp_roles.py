"""P656: move saved ACP membership from its old tag into its declared role once."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_identity import AcpThreadRole
from .carry import Carried, Carry


class AcpRoles(Carry):
    after = "carry_starting_activity"

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        store = RegistryStore(root / "registry.json")
        return store.path, store.guard_path

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / "registry.json")
        if not store.path.exists():
            return Carried(0)
        with store.locked():
            document = RegistryDocument.from_wire(json.loads(store.path.read_text()))
            old = [thread for thread in document.threads.values()
                   if "acp" in thread.tags and not thread.role.acp_thread(thread)]
            for thread in old:
                thread.role.require_executable(ValueError(
                    f"Refusing to turn human {thread.name!r} into an ACP executor"))
                document.threads[thread.name] = replace(thread, role=AcpThreadRole)
            if apply and old:
                store.save_unlocked(document)
        files = (store.path.name, store.guard_path.name) if old else ()
        return Carried(len(old), {"threads": len(document.threads)}, files)


if __name__ == "__main__":
    AcpRoles.main()
