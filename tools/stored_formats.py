"""Report the stored form of each versioned record declared in the named modules.

The debt ratchet runs this once per revision with that revision's source first on sys.path,
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
from dataclasses import dataclass

from agent_comms.field_codec import FieldCodec


@dataclass(frozen=True)
class StoredForm:
    """A versioned record as FieldCodec writes it: its format version, each stored key with
    that field's wire schema, and the keys every record carries."""

    version: int
    fields: dict[object, object]
    required: frozenset[str]
    """The keys every record carries: a set, because declaring a field in a parent class
    reorders the schema's list without changing what is written."""

    @classmethod
    def of(cls, record: type) -> StoredForm | None:
        """Decode the record schema once. The version is a single-valued stored field, or the
        ClassVar the writer stamps beside the fields."""
        declared = vars(record).get("version")
        match FieldCodec.record_schema(record):
            case {"properties": {"version": {"enum": [stamped]}, **stored}, "required": required}:
                return cls(stamped, stored, frozenset(key for key in required if key in stored))
            case {"properties": properties, "required": required} if isinstance(declared, int):
                return cls(declared, properties, frozenset(required))
        return None

    def wire(self) -> dict[str, object]:
        """The JSON the ratchet decodes. JSON has no set, so the required keys travel as a
        sorted list and the decoder makes them a set again."""
        return {"version": self.version, "fields": self.fields, "required": sorted(self.required)}


def stored_formats(module_name: str) -> dict[str, StoredForm]:
    module = importlib.import_module(module_name)
    records = [
        record
        for record in vars(module).values()
        if inspect.isclass(record) and record.__module__ == module_name
    ]
    found = {}
    for record in filter(dataclasses.is_dataclass, records):
        form = StoredForm.of(record) if vars(record).get("version") is not None else None
        if form is not None:
            found[f"{module_name}.{record.__name__}"] = form
    return found


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
