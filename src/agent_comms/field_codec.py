"""Strict JSON boundary conversion derived from dataclass store_files."""

from __future__ import annotations

import math
import types
from dataclasses import MISSING, Field, fields, is_dataclass
from enum import Enum
from functools import lru_cache
from typing import (
    Any,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from .declared_family import DeclaredFamily

T = TypeVar("T")


class Projected(property):
    """A computed field declared by its owner for a named read-only view."""

    def __init__(self, getter, *, view: str, name: str | None = None):
        super().__init__(getter)
        self.view = view
        self.wire_name = name or getter.__name__


def projected(*, view: str, name: str | None = None):
    return lambda getter: Projected(getter, view=view, name=name)


class FieldCodec:
    """Family tags use their declared ``family_discriminator`` (default ``kind``); aliases
    use field(metadata={"wire_name": ...}).

    Decoding rejects unknown fields and primitive coercions (including bool as
    int). Missing fields use declared defaults. Tuple fields round-trip as JSON
    arrays. Init fields and explicitly projected wire properties participate;
    projected values are derived again on decode. ClassVars are not stored.
    ``wire_nonnull`` rejects an explicit null while allowing an omitted default.
    """

    @staticmethod
    @lru_cache(maxsize=256)
    def _fields(cls: Any) -> tuple[tuple[Field[Any], str], ...]:
        # Declarations are immutable for this process; cache only their derived
        # schema, never decoded rows, registry membership or document revisions.
        declared = [
            (field, field.metadata.get("wire_name", field.name))
            for field in fields(cls)
            if field.init
        ]
        keys = [key for _, key in declared]
        if any(not isinstance(key, str) or not key for key in keys):
            raise TypeError("Wire field names must be nonempty strings.")
        if len(set(keys)) != len(keys) or (
            issubclass(cls, DeclaredFamily) and cls.family_discriminator in keys
        ):
            raise TypeError("Conflicting wire field names.")
        return tuple(
            item
            for _, item in sorted(
                enumerate(declared),
                key=lambda row: (row[1][0].metadata.get("wire_order", row[0]), row[0]),
            )
        )

    @staticmethod
    @lru_cache(maxsize=256)
    def _types(declaration: type) -> dict[str, Any]:
        return get_type_hints(declaration)

    @overload
    @classmethod
    def encode(cls, value: DeclaredFamily) -> dict[str, Any]: ...

    @overload
    @classmethod
    def encode(cls, value: object) -> Any: ...

    @classmethod
    def encode(cls, value: object) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            result = (
                {value.family_discriminator: type(value).wire_tag()}
                if isinstance(value, DeclaredFamily)
                else {}
            )
            result.update(
                (key, cls.encode(getattr(value, field.name)))
                for field, key in cls._fields(type(value))
                if not (
                    field.metadata.get("wire_omit_default")
                    and getattr(value, field.name)
                    == (
                        field.default
                        if field.default is not MISSING
                        else (
                            field.default_factory()
                            if field.default_factory is not MISSING
                            else MISSING
                        )
                    )
                )
            )
            result.update(
                (member.wire_name, cls.encode(getattr(value, name)))
                for name, member in cls._projections(type(value), "wire").items()
            )
            return result
        if isinstance(value, type) and issubclass(value, DeclaredFamily):
            return value.declared_name
        if isinstance(value, Enum):
            return cls.encode(value.value)
        if value is None or type(value) in (str, int, bool):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if isinstance(value, frozenset):
            return [cls.encode(item) for item in sorted(value)]
        if isinstance(value, (list, tuple)):
            return [cls.encode(item) for item in value]
        if isinstance(value, dict) and all(type(key) is str for key in value):
            return {key: cls.encode(item) for key, item in value.items()}
        raise TypeError(f"Unsupported JSON value: {type(value).__name__}")

    @classmethod
    def decode(cls, target: type[T], data: object) -> T:
        return cast(T, cls._decode(target, data))

    @classmethod
    def record_schema(cls, target: type) -> dict[str, Any]:
        """JSON Schema for named external request fields; family selection is separate."""
        properties = {}
        required = []
        hints = cls._types(target)
        for declared, key in cls._fields(target):
            annotation = hints[declared.name]
            metadata = declared.metadata
            if metadata.get("wire_nonnull") and get_origin(annotation) in (Union, types.UnionType):
                members = tuple(item for item in get_args(annotation) if item is not type(None))
                annotation = members[0] if len(members) == 1 else Union[members]  # noqa: UP007 - runtime tuple
            schema = cls.value_schema(annotation)
            if "description" in metadata:
                schema["description"] = metadata["description"]
            if "wire_choices" in metadata:
                schema["enum"] = list(metadata["wire_choices"]())
            if declared.default is MISSING and declared.default_factory is MISSING:
                required.append(key)
            elif declared.default is not MISSING and declared.default is not None:
                schema["default"] = cls.encode(declared.default)
            properties[key] = schema
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @classmethod
    def value_schema(cls, annotation: object) -> dict[str, Any]:
        """Derive external scalar/array choices from the same decode declarations."""
        origin, args = get_origin(annotation), get_args(annotation)
        if origin in (Union, types.UnionType):
            members = list(args)
            if len(members) == 1:
                return cls.value_schema(members[0])
            return {"anyOf": [cls.value_schema(item) for item in members]}
        if origin is Literal:
            return {**cls.value_schema(type(args[0])), "enum": list(args)}
        if origin in (list, tuple, frozenset):
            return {"type": "array", "items": cls.value_schema(args[0])}
        if origin is type and args and issubclass(args[0], DeclaredFamily):
            return {"type": "string", "enum": list(args[0].names())}
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            values = [item.value for item in annotation]
            return {**cls.value_schema(type(values[0])), "enum": values}
        primitive = {
            str: "string",
            bool: "boolean",
            int: "integer",
            float: "number",
            type(None): "null",
        }
        if annotation in primitive:
            return {"type": primitive[annotation]}
        if isinstance(annotation, type) and is_dataclass(annotation):
            return cls.record_schema(annotation)
        raise TypeError(f"No declared JSON schema for {annotation}")

    @classmethod
    def project(cls, value: object, view: str) -> Any:
        """Encode a redacted view using field exclusions and owned properties.

        Projection is deliberately one way. Excluded secrets cannot be rebuilt
        from a read-only view, and a view never serves as a persistence record.
        """
        if is_dataclass(value) and not isinstance(value, type):
            result = {
                field.metadata.get(f"{view}_name", key): cls.project(
                    getattr(value, field.name), view
                )
                for field, key in cls._fields(type(value))
                if not field.metadata.get(f"{view}_exclude")
            }
            properties = cls._projections(type(value), view)
            result.update(
                (member.wire_name, cls.project(getattr(value, name), view))
                for name, member in properties.items()
            )
            return result
        if isinstance(value, (tuple, list)):
            return [cls.project(item, view) for item in value]
        return cls.encode(value)

    @staticmethod
    def _projections(declaration: type, view: str) -> dict[str, Projected]:
        return {
            name: member
            for base in reversed(declaration.__mro__)
            for name, member in vars(base).items()
            if isinstance(member, Projected) and member.view == view
        }

    @classmethod
    def _decode(cls, target: Any, data: Any) -> Any:
        if target is Any:
            cls.encode(data)  # still require valid JSON data
            return data
        origin, args = get_origin(target), get_args(target)
        if origin is type and args and issubclass(args[0], DeclaredFamily):
            if type(data) is not str:
                raise ValueError("Expected a declared family name.")
            return args[0].decode(data)
        if origin is Literal:
            if any(type(data) is type(value) and data == value for value in args):
                return data
            raise ValueError(f"Value does not match {target}")
        if origin in (Union, types.UnionType):
            errors = []
            for alternative in args:
                try:
                    return cls.decode(alternative, data)
                except (TypeError, ValueError) as error:
                    errors.append(error)
            # Keep declaration-owned failure detail through an optional/union
            # boundary (e.g. a native UNKNOWN reason that fails validation).
            raise ValueError(f"Value does not match {target}: {errors[0]}") from errors[0]
        if origin is frozenset:
            if not isinstance(data, list):
                raise ValueError("Expected a JSON array.")
            return frozenset(cls.decode(args[0], item) for item in data)
        if origin in (list, tuple):
            if not isinstance(data, list):
                raise ValueError("Expected a JSON array.")
            if origin is list:
                return [cls.decode(args[0], item) for item in data]
            if len(args) == 2 and args[1] is Ellipsis:
                return tuple(cls.decode(args[0], item) for item in data)
            if len(data) != len(args):
                raise ValueError("Tuple length does not match declaration.")
            return tuple(cls.decode(kind, item) for kind, item in zip(args, data, strict=True))
        if origin is dict:
            if not isinstance(data, dict) or args[0] is not str:
                raise ValueError("Expected a JSON object with string keys.")
            return {cls.decode(str, key): cls.decode(args[1], value) for key, value in data.items()}
        if isinstance(target, type) and issubclass(target, DeclaredFamily):
            if not isinstance(data, dict):
                raise ValueError("Expected a family object.")
            tag = target.family_discriminator
            name = data.get(tag)
            target = target.decode_wire_tag(name)
            data = {key: value for key, value in data.items() if key != tag}
        if isinstance(target, type) and is_dataclass(target):
            if not isinstance(data, dict):
                raise ValueError("Expected a record object.")
            declared = cls._fields(target)
            if any(
                field.metadata.get("wire_required") and key not in data for field, key in declared
            ):
                raise ValueError(f"Missing required fields for {target.__name__}")
            computed = {member.wire_name for member in cls._projections(target, "wire").values()}
            unknown = set(data) - {key for _, key in declared} - computed
            if unknown:
                raise ValueError(f"Unknown fields for {target.__name__}: {sorted(unknown)}")
            if any(
                field.metadata.get("wire_nonnull") and key in data and data[key] is None
                for field, key in declared
            ):
                raise ValueError(f"Null field for {target.__name__}")
            for field, key in declared:
                choices = field.metadata.get("wire_choices")
                if choices is not None and key in data and data[key] not in choices():
                    raise ValueError(f"Field {key} must be one of: {', '.join(choices())}")
            hints = cls._types(target)
            return target(
                **{
                    field.name: cls.decode(hints[field.name], data[key])
                    for field, key in declared
                    if key in data
                }
            )
        if isinstance(target, type) and issubclass(target, Enum):
            return target(data)
        if target in (str, int, bool, type(None)) and type(data) is target:
            return data
        if target is float and type(data) in (int, float) and math.isfinite(data):
            return data
        raise ValueError(f"Expected {target}, received {type(data).__name__}")
