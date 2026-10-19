"""Carry known tag declarations and their recorded creation times into the catalog."""
from pathlib import Path
import json

from agent_comms.catalog_document import CatalogDocument
from agent_comms.catalog_store import ChannelCatalog
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_store import RegistryStore
from cutover.carry import Carried, Carry


class CatalogTags(Carry):
    after = "carry_todo_messages"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        catalog = ChannelCatalog.at(root)
        with RegistryStore(root / "registry.json").reading() as registry, catalog.locked():
            wire = json.loads(catalog.path.read_bytes())
            declared = set(wire["tags"])
            first_seen = {}
            for thread in registry.threads.values():
                for tag in thread.tags:
                    first_seen[tag] = min(first_seen.get(tag, thread.created_at), thread.created_at)
            known = declared | first_seen.keys()
            preferences = wire.setdefault("preferences", {})
            filled = 0
            for tag in known:
                preference = preferences.setdefault(f"#{tag}", {})
                if preference.get("created_at", 0) <= 0:
                    if tag not in first_seen or first_seen[tag] <= 0:
                        raise ValueError(f"No retained creation time for #{tag}")
                    preference["created_at"] = first_seen[tag]
                    filled += 1
            wire["tags"] = sorted(known)
            document = FieldCodec.decode(CatalogDocument, wire)
            changed = len(known - declared) + filled
            if apply and changed:
                catalog._publish_unlocked(document)
        return Carried(changed, {"added_tags": len(known - declared), "filled_times": filled},
                       (catalog.filename,) if apply and changed else ())


if __name__ == "__main__":
    CatalogTags.main()
