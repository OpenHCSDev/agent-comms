"""Finish the admitted import bindings and whitespace of the revision-owner plan."""

import json

from p23_revision_owner_plan import SRC, imported, patch, stage

presentation = SRC / "presentation.py"
history = SRC / "history_views.py"
text = presentation.read_text()
start = text.index("if TYPE_CHECKING:\n")
end = text.index("\n\n\n@dataclass", start)
guard = text[start:end]
blank_start = text.index("    __slots__ = ()\n", text.index("class NoRevision")) + len("    __slots__ = ()\n")
blank_end = text.index("def expiry_period_changed", blank_start)
blank = text[blank_start:blank_end]
if blank.strip():
    raise ValueError("Unexpected declaration between NoRevision and expiry predicate")
operations = [
    patch(presentation, [
        (guard, "if TYPE_CHECKING:\n    from agent_comms.messaging.message_bus import MessageBus\n    from agent_comms.planning.goal_management import Goals"),
        ("class NoRevision(ReadState):\n    __slots__ = ()\n" + blank,
         "class NoRevision(ReadState):\n    __slots__ = ()\n\n\n"),
        ("        }\n\n\n    def selection_reusable_from", "        }\n\n    def selection_reusable_from"),
        ("earlier))\n\n\n    @classmethod\n    def period_for", "earlier))\n\n    @classmethod\n    def period_for"),
    ]),
    {"operation": "remove_import_names", "file_path": str(presentation),
     "module_name": "agent_comms.coordination.errors", "import_names": ["StaleRevision"]},
    {"operation": "remove_import_names", "file_path": str(presentation),
     "module_name": "agent_comms.message_notification", "import_names": ["NotificationPass"]},
    {"operation": "remove_import_names", "file_path": str(history),
     "module_name": "agent_comms.watched", "import_names": ["FileRevision"]},
    imported(history, "from agent_comms.storage.private_path import FileRevision"),
    patch(history, [("        return WireRevision(**captured, bus_identity=RecordedBusIdentity.capture(self.bus.bus.path),\n                   bus_seq=self.bus.bus.revision(), expiry_tick=expiry_tick)",
                     "        return WireRevision(\n            **captured, bus_identity=RecordedBusIdentity.capture(self.bus.bus.path),\n            bus_seq=self.bus.bus.revision(), expiry_tick=expiry_tick,\n        )")]),
]
for statement in (
    "from agent_comms.bus import BusSnapshot",
    "from agent_comms.catalog_document import CatalogDocument",
    "from agent_comms.catalog_store import ChannelCatalog",
    "from agent_comms.pi_counts import PiCount",
    "from agent_comms.registry.registration import Registration",
    "from agent_comms.registry.registry_document import RegistrySnapshot",
    "from agent_comms.registry.registry_names import DeletedThread",
):
    operations.append(imported(presentation, statement))
print(json.dumps({"stages": [stage(
    "p23-revision-original-import-owners",
    "The retained complete guarded-edge graph proves no runtime return path for BusSnapshot, CatalogDocument, ChannelCatalog, PiCount, Registration, RegistrySnapshot or DeletedThread. Bind those actual declarations, retaining only the separately disclosed MessageBus/Goals cycles for the next cut. Import FileRevision from its actual declaration module, not an incidental importing module. Delete imports made unused by moving attribution; normalize only whitespace introduced by the move.",
    operations,
)]}, indent=2))
