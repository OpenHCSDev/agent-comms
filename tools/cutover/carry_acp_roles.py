"""P656: move saved ACP membership from its old tag into its declared role once."""
from __future__ import annotations

import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_identity import AcpThreadRole
from .carry import Carried, Carry


class AcpRoles(Carry):
    after = "carry_starting_activity"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / "registry.json")
        if not store.path.exists():
            return Carried(0)
        with store.locked():
            raw = json.loads(store.path.read_text())
            role = FieldCodec.encode(AcpThreadRole)
            old = [thread for thread in raw["threads"].values()
                   if "acp" in thread["tags"] and thread["role"] != role]
            for thread in old:
                thread["role"] = role
            document = RegistryDocument.from_wire(raw)
            if apply and old:
                store.save_unlocked(document)
        files = (store.path.name, store.guard_path.name) if old else ()
        return Carried(len(old), {"threads": len(raw["threads"])}, files)


if __name__ == "__main__":
    AcpRoles.main()
