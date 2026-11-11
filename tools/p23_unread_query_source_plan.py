"""Declare the exact unread-query capability the low Bus consumes."""

import argparse
import ast
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path.cwd()
    bus = root / "src/agent_comms/messaging/bus.py"
    record = root / "src/agent_comms/messaging/display_unread_selection.py"
    before = bus.read_text()
    old_import = "from agent_comms.messaging.display_unread_selection import DisplayUnreadSelection"
    if old_import not in before:
        raise ValueError("The expected concrete unread selection import changed")
    after = before.replace(old_import,
        "from agent_comms.messaging.unread_query_source import UnreadQuerySource")
    after = after.replace("selection: DisplayUnreadSelection", "selection: UnreadQuerySource")
    ast.parse(after, filename=str(bus))
    source = ('"""The unread queries consumed by a bus count, without acquiring a registry."""\n\n'
              'from abc import ABC, abstractmethod\n\n'
              'from agent_comms.messaging.read_basis import UnreadQuery\n\n\n'
              'class UnreadQuerySource(ABC):\n'
              '    __slots__ = ()\n\n'
              '    @abstractmethod\n'
              '    def queries(self) -> tuple[UnreadQuery, ...]: ...\n')
    reason = "The complete low Bus use of DisplayUnreadSelection is queries() at DisplayUnread.advanced and BusReader.count_unread. Its concrete data remains the original frozen/slotted record with concrete RegistryProvenance; do not generalize or replace stored fields. Declare that exact query method on UnreadQuerySource and have the actual original record inherit it. Low Bus consumes the nominal capability instead of importing namespace/backend acquisition through the whole record. No proxy, wrapper, field copy, constructor, query/SQL body, default, runtime output or assertion changes."
    args.output.write_text(json.dumps({"stages": [
        {"recipes": [{"recipe_id": "p23-unread-query-contract", "reason": reason,
                      "operations": [{"operation": "create_file",
                          "file_path": str(root / "src/agent_comms/messaging/unread_query_source.py"),
                          "source": source}]}]},
        {"recipes": [{"recipe_id": "p23-original-unread-query-provider", "reason": reason,
                      "operations": [{"operation": "ensure_import", "file_path": str(record),
                          "import_source": "from agent_comms.messaging.unread_query_source import UnreadQuerySource"}]}]},
        {"recipes": [{"recipe_id": "p23-unread-query-inheritance", "reason": reason,
                      "operations": [
                          {"operation": "add_class_base", "file_path": str(record),
                           "target_qualname": "DisplayUnreadSelection", "base_name": "UnreadQuerySource"},
                          {"operation": "patch_target", "file_path": str(bus),
                           "replacements": [{"old_source": before, "new_source": after}]},
                      ]}]},
    ]}, indent=2) + "\n")


if __name__ == "__main__":
    main()
