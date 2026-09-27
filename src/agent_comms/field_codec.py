"""Strict JSON boundary conversion derived from dataclass declarations."""

from __future__ import annotations

import math
import types
from dataclasses import Field, fields, is_dataclass
from enum import Enum
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints, overload

from .declared_family import DeclaredFamily

T = TypeVar("T")


class FieldCodec:
    """Family tags use ``kind``; aliases use field(metadata={"wire_name": ...}).

    Decoding rejects unknown fields and primitive coercions (including bool as
    int). Missing fields use declared defaults. Tuple fields round-trip as JSON
    arrays. Only init fields participate; ClassVars and derived fields do not.
    """

    @staticmethod
    def _fields(cls: Any) -> list[tuple[Field[Any], str]]:
        declared = [
            (field, field.metadata.get("wire_name", field.name))
            for field in fields(cls)
            if field.init
        ]
        keys = [key for _, key in declared]
        if any(not isinstance(key, str) or not key for key in keys):
            raise TypeError("Wire field names must be nonempty strings.")
        if len(set(keys)) != len(keys) or (issubclass(cls, DeclaredFamily) and "kind" in keys):
            raise TypeError("Conflicting wire field names.")
        return declared

    @overload
    @classmethod
    def encode(cls, value: DeclaredFamily) -> dict[str, Any]: ...

    @overload
    @classmethod
    def encode(cls, value: object) -> Any: ...

    @classmethod
    def encode(cls, value: object) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            result = {"kind": value.declared_name} if isinstance(value, DeclaredFamily) else {}
            result.update(
                (key, cls.encode(getattr(value, field.name)))
                for field, key in cls._fields(type(value))
            )
            return result
        if isinstance(value, Enum):
            return cls.encode(value.value)
        if value is None or type(value) in (str, int, bool):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if isinstance(value, (list, tuple)):
            return [cls.encode(item) for item in value]
        if isinstance(value, dict) and all(type(key) is str for key in value):
            return {key: cls.encode(item) for key, item in value.items()}
        raise TypeError(f"Unsupported JSON value: {type(value).__name__}")

    @classmethod
    def decode(cls, target: type[T], data: object) -> T:
        return cast(T, cls._decode(target, data))

    @classmethod
    def _decode(cls, target: Any, data: Any) -> Any:
        if target is Any:
            cls.encode(data)  # still require JSON-compatible data
            return data
        origin, args = get_origin(target), get_args(target)
        if origin in (Union, types.UnionType):
            for alternative in args:
                try:
                    return cls.decode(alternative, data)
                except (TypeError, ValueError):
                    pass
            raise ValueError(f"Value does not match {target}")
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
            name = data.get("kind")
            if not isinstance(name, str):
                raise ValueError("Expected a string family kind.")
            target = target.decode(name)
            data = {key: value for key, value in data.items() if key != "kind"}
        if isinstance(target, type) and is_dataclass(target):
            if not isinstance(data, dict):
                raise ValueError("Expected a record object.")
            declared = cls._fields(target)
            unknown = set(data) - {key for _, key in declared}
            if unknown:
                raise ValueError(f"Unknown fields for {target.__name__}: {sorted(unknown)}")
            hints = get_type_hints(target)
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
