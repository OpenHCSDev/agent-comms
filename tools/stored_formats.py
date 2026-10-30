"""Report the stored form of each versioned record declared in the named modules.

The NRA gate runs this once per revision with that revision's source first on sys.path,
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
from collections.abc import Iterator

from agent_comms.field_codec import FieldCodec
from nominal_refactor_advisor import audit


def stored_format(record: type) -> Iterator[audit.project.StoredFormat]:
    """The record's stored format, when the class itself declares a version: a single-valued
    stored field, or the ClassVar the writer stamps beside the fields. A record declaring none
    yields nothing, and its schema is never read."""
    match vars(record):
        case {"version": declared}:
            match declared, FieldCodec.record_schema(record):
                case _, {"properties": {"version": {"enum": [stamped]}, **stored},
                         "required": required}:
                    yield audit.project.StoredFormat.declared(
                        stamped, stored, (key for key in required if key in stored))
                case int(), {"properties": properties, "required": required}:
                    yield audit.project.StoredFormat.declared(declared, properties, required)


def stored_formats(module_name: str) -> dict[str, audit.project.StoredFormat]:
    module = importlib.import_module(module_name)
    return {
        f"{module_name}.{record.__name__}": form
        for record in vars(module).values()
        if inspect.isclass(record) and record.__module__ == module_name
        and dataclasses.is_dataclass(record)
        for form in stored_format(record)
    }


def main(modules: list[str]) -> None:
    print(
        json.dumps(
            {
                name: form.wire()
                for module in modules
                for name, form in stored_formats(module).items()
            }
        )
    )
