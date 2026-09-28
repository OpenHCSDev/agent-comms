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
    arrays. Only init fields participate; ClassVars and derived fields do not.
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
                {value.family_discriminator: value.declared_name} if isinstance(value, DeclaredFamily) else {}
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
            properties = {
                name: member
                for base in reversed(type(value).__mro__)
                for name, member in vars(base).items()
                if isinstance(member, Projected) and member.view == view
            }
            result.update(
                (member.wire_name, cls.project(getattr(value, name), view))
                for name, member in properties.items()
            )
            return result
        if isinstance(value, (tuple, list)):
            return [cls.project(item, view) for item in value]
        return cls.encode(value)

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
            if not isinstance(name, str):
                raise ValueError(f"Expected a string family {tag}.")
            target = target.decode(name)
            data = {key: value for key, value in data.items() if key != tag}
        if isinstance(target, type) and is_dataclass(target):
            if not isinstance(data, dict):
                raise ValueError("Expected a record object.")
            declared = cls._fields(target)
            if any(
                field.metadata.get("wire_required") and key not in data for field, key in declared
            ):
                raise ValueError(f"Missing required fields for {target.__name__}")
            unknown = set(data) - {key for _, key in declared}
            if unknown:
                raise ValueError(f"Unknown fields for {target.__name__}: {sorted(unknown)}")
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
