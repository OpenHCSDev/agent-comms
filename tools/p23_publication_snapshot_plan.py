"""Put original input-publication I/O on the held snapshots that actually publish."""

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
    publisher = root / "src/agent_comms/messaging/publisher.py"
    bus_before = bus.read_text()
    bus_after = bus_before.replace(
        "from agent_comms.execution.input_publication_source import InputPublicationSource\n", "",
    ).replace("    InputPublicationSource,\n", "")
    if bus_after == bus_before:
        raise ValueError("The low BusReader no longer has the expected input-publication capability")
    before = publisher.read_text()
    if before.count("BusSnapshot.from_connection(db)") != 3:
        raise ValueError("Publication snapshot construction no longer has the three traced sites")
    header = "@dataclass(eq=False, repr=False)\nclass Publisher:"
    if header not in before:
        raise ValueError("Original Publisher declaration changed")
    after = before.replace(
        "from agent_comms.coordination.coordinator import Coordination\n",
        "from agent_comms.coordination.coordinator import Coordination\n"
        "from agent_comms.execution.input_publication_source import InputPublicationSource\n",
    ).replace(header,
        'class PublicationSnapshot(InputPublicationSource, BusSnapshot):\n'
        '    """The original held bus cut with input-publication acquisition."""\n\n\n' + header,
    ).replace("BusSnapshot.from_connection(db)", "PublicationSnapshot.from_connection(db)")
    after = after.replace("original_source: BusReader", "original_source: PublicationSnapshot")
    operations = []
    for path, original, changed in ((bus, bus_before, bus_after), (publisher, before, after)):
        ast.parse(changed, filename=str(path))
        operations.append({"operation": "patch_target", "file_path": str(path),
                           "replacements": [{"old_source": original, "new_source": changed}]})
    reason = "Both actual startup traces show low BusReader importing the high InputPublicationSource store I/O. The complete call map has three Publisher snapshot factories; NativeInputConstraintPin is the only input-publication caller. Move that capability edge from ordinary readers onto empty PublicationSnapshot(InputPublicationSource, BusSnapshot), and use its inherited from_connection constructor at all three actual publication sites. Preserve the exact original BusSnapshot fields, path, physical identity, held SQLite connection, observation context and Self constructor. Keep InputPublicationSource.input_publication unchanged, including privacy acquisition before original input lookup and all subsequent author/root checks. No proxy, wrapper, forwarding method, copied store, cached answer, constructor or wire-field change. Annotation-disclosure grants use the separate message-wording path and retain ordinary readers."
    args.output.write_text(json.dumps({"stages": [
        {"recipes": [{"recipe_id": "p23-publication-snapshot-capability", "reason": reason,
                      "operations": operations}]},
        {"recipes": [{"recipe_id": "p23-delete-unused-publisher-reader-import", "reason": reason,
                      "operations": [{"operation": "remove_import_bindings",
                          "file_path": str(publisher), "bound_names": ["BusReader"]}]}]},
    ]}, indent=2) + "\n")


if __name__ == "__main__":
    main()
