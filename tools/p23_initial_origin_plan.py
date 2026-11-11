"""Move the original origin bases and record-builder alias, without copies or shims."""

import json

from p23_revision_owner_plan import SRC, stage

moves = (
    ("p23-lower-original-record-builder", "bus.py", ["RecordBuilder"], "bus_record_builder.py"),
    ("p23-lower-original-owner-origins", "bus_publication.py",
     ["InitialOrigin", "OwnerVoiceOrigin", "HumanOrigin"], "initial_origin.py"),
)
print(json.dumps({"stages": [stage(
    identity,
    "The retained 149-class trace identifies the actual eager BusRecord/table barrier in bus_publication, not eager Message/ClaimMessage/StoredInput fields. Relocate these original declarations through NRA and migrate every selected direct/module-qualified consumer; keep actual fields, defaults, inheritance, exceptions and method bodies. The original NoTaskAttachment default and concrete fact source records do not change. Subsequent integrated stages replace upper live annotations with actual nominal provider capabilities and retain the original sender AND message_id database query under BusReader. This relocation alone is not runtime closure or a completion claim.",
    [{"operation": "relocate_symbols_to_new_module", "file_path": str(SRC / source),
      "symbol_qualnames": symbols, "destination_path": str(SRC / destination)}],
) for identity, source, symbols, destination in moves]}, indent=2))
