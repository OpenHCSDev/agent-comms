"""Print the stored form of each versioned record declared in the named modules.

The debt ratchet runs this once per revision with that revision's source on PYTHONPATH,
so each side's stored form comes from its own FieldCodec declarations and no revision's
code is imported into another's process. A record's stored form is FieldCodec's record
schema: each key on disk and its representation's wire spelling. Its version is the
`version` ClassVar the writer stamps, or a single-valued stored `version` field.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import sys

from agent_comms.field_codec import FieldCodec


def stored_form(record: type) -> tuple[int, dict[str, object]] | None:
    """The record's format version and the schema FieldCodec writes for it, if it has one."""
    schema = FieldCodec.record_schema(record)
    stamped = schema["properties"].pop("version", {}).get("enum", [])
    schema["required"] = [key for key in schema["required"] if key != "version"]
    declared = vars(record).get("version")
    if len(stamped) == 1:
        return stamped[0], schema
    return (declared, schema) if isinstance(declared, int) else None


def stored_formats(module_name: str) -> dict[str, tuple[int, dict[str, object]]]:
    module = importlib.import_module(module_name)
    records = [
        record
        for record in vars(module).values()
        if inspect.isclass(record) and record.__module__ == module_name
    ]
    found = {}
    for record in filter(dataclasses.is_dataclass, records):
        stored = {field.name for field in dataclasses.fields(record)} | set(vars(record))
        form = stored_form(record) if "version" in stored else None
        if form is not None:
            found[f"{module_name}.{record.__name__}"] = form
    return found


if __name__ == "__main__":
    print(
        json.dumps(
            {name: form for module in sys.argv[1:] for name, form in stored_formats(module).items()}
        )
    )
