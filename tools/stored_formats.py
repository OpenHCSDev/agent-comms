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

from agent_comms.field_codec import BoundedText, FieldCodec


@dataclass(frozen=True)
class StoredForm:
    """A versioned record as FieldCodec writes it: its format version, each stored key with
    that field's wire schema, and the keys every record carries."""

    version: int
    fields: dict[object, object]
    required: list[str]

    @classmethod
    def of(cls, record: type) -> StoredForm | None:
        """Decode the record schema once. The version is a single-valued stored field, or the
        ClassVar the writer stamps beside the fields."""
        declared = vars(record).get("version")
        schema = FieldCodec.record_schema(record)
        for entry in FieldCodec._record_plan(record).fields:
            schema["properties"][entry.key] = wire_spelling(
                schema["properties"][entry.key], FieldCodec.representation(entry.annotation)[1])
        match schema:
            case {"properties": {"version": {"enum": [stamped]}, **stored}, "required": required}:
                return cls(stamped, stored, [key for key in required if key in stored])
            case {"properties": properties, "required": required} if isinstance(declared, int):
                return cls(declared, properties, required)
        return None


def wire_spelling(schema: object, representation: type) -> object:
    """A field's stored form is the text it accepts. A description documents a schema and is
    never part of it, and BoundedText spells what it accepts by its bound, so it shares a form
    with any spelled text accepting the same set."""
    match schema:
        case dict():
            spelled = {key: wire_spelling(value, object) for key, value in schema.items()
                       if key != "description"}
            if issubclass(representation, BoundedText):
                spelled["pattern"] = rf"^(?:[\s\S]{{1,{representation.max_length}}})$"
            return spelled
        case list():
            return [wire_spelling(item, object) for item in schema]
    return schema


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
                name: dataclasses.asdict(form)
                for module in modules
                for name, form in stored_formats(module).items()
            }
        )
    )
