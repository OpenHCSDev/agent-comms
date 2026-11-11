"""Report the stored form of each versioned record declared in the named modules.

The NRA gate runs this once per revision with that revision's source first on sys.path,
so each side's stored form comes from its own FieldCodec declarations and no revision's
code is imported into another's process. A record's stored form is FieldCodec's record
schema: each key on disk and its representation's wire spelling, with every shared
definition inlined. Its version is the `version` ClassVar the writer stamps, or a
single-valued stored `version` field.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
from collections.abc import Iterator, Mapping

from nominal_refactor_advisor import audit
from nominal_refactor_advisor.json_reports import json_report_object

from agent_comms.nominal.field_codec import FieldCodec


def inlined(schema: object, definitions: Mapping[str, object],
            expanding: tuple[str, ...] = ()) -> object:
    """The schema with each `#/$defs/NAME` reference replaced by that definition. FieldCodec
    names definitions by the order a schema build first meets them (`d0`, `d1`, ...), so a
    new type anywhere in a record renumbers every later reference while nothing on disk
    changes (#3999: SelectedCheckout turned `offered`'s d69 into d72). A reference inside
    its own definition stays one, named by the declaration its `$comment` records."""
    match schema:
        case {"$ref": str() as reference, **rest} if reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            definition = definitions[name]
            if name in expanding:
                match definition:
                    case {"$comment": str() as declared}:
                        return {"$ref": declared, **rest}
                raise ValueError(f"recursive definition {name} names no declaration")
            match definition:
                case dict():
                    return inlined({**definition, **rest}, definitions, (*expanding, name))
            raise ValueError(f"definition {name} is not a schema")
        case dict():
            return {key: inlined(value, definitions, expanding) for key, value in schema.items()}
        case list():
            return [inlined(item, definitions, expanding) for item in schema]
    return schema


def record_schema(record: type) -> object:
    """The record's schema with no document-local reference names left in it."""
    schema = FieldCodec.record_schema(record)
    match schema:
        case {"$defs": dict() as definitions, **stored}:
            return inlined(stored, definitions)
    return schema


def stored_format(record: type) -> Iterator[audit.project.StoredFormat]:
    """The record's stored format, when the class itself declares a version: a single-valued
    stored field, or the ClassVar the writer stamps beside the fields. A record declaring none
    yields nothing, and its schema is never read."""
    match vars(record):
        case {"version": declared}:
            match declared, record_schema(record):
                case _, {"properties": {"version": {"enum": [int() as stamped]}, **stored},
                         "required": list() as required}:
                    fields: dict[object, object] = {**stored}
                    yield audit.project.StoredFormat.declared(
                        stamped, fields, (key for key in required
                                          if isinstance(key, str) and key in stored))
                case int() as version, {"properties": dict() as properties,
                                        "required": list() as required}:
                    yield audit.project.StoredFormat.declared(
                        version, {**properties},
                        (key for key in required if isinstance(key, str)))


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
                name: json_report_object(form)
                for module in modules
                for name, form in stored_formats(module).items()
            }
        )
    )
