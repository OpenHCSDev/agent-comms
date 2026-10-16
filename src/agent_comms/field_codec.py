"""Strict JSON boundary conversion derived from dataclass store_files."""

from __future__ import annotations

import math
import types
from abc import ABC, abstractmethod
from dataclasses import MISSING, Field, dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing_extensions import TypeAliasType
from typing import (
    Annotated,
    Any,
    Literal,
    Self,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from .declared_family import DeclaredFamily
from .sealed import Sealed

T = TypeVar("T")


class FieldRepresentation(ABC):
    """A field capability, declared as ``Annotated[ValueType, Representation]``.

    Private transports can declare captures without permitting those objects in
    ordinary JSON. All record structure remains owned by FieldCodec.
    """

    accepts_null = False
    # True when the wire value is one external JSON shape decoded by from_wire
    # rather than the representation's own validated decode.
    external_json_shape = False

    @classmethod
    @abstractmethod
    def encode(cls, value: object) -> object: ...

    @classmethod
    @abstractmethod
    def decode(cls, value: object) -> object: ...

    @classmethod
    def schema(cls) -> dict[str, Any]:
        raise TypeError(f"{cls.__name__} has no JSON schema")


class WireValue(FieldRepresentation):
    """A value supplies its representation to the single field boundary."""

    __slots__ = ()

    @abstractmethod
    def to_wire(self) -> Any: ...

    @classmethod
    @abstractmethod
    def from_wire(cls, data: Any) -> Self: ...

    @classmethod
    def encode(cls, value: WireValue) -> Any:
        return FieldCodec.encode(value.to_wire())

    @classmethod
    def decode(cls, value: object) -> Self:
        FieldCodec.encode(value)  # Custom forms still cross the JSON boundary.
        return cls.from_wire(value)


class JsonShapeMember:
    """A declared external JSON shape with one typed ``value`` field.

    Concrete members opt in to shape selection. Other members of their nominal
    family can represent decoded meanings without becoming ingress candidates.
    """

    @classmethod
    def from_json_value(cls, value):
        return cls(value=value)


class JsonShapeFamily(WireValue):
    """Untagged external JSON enters one declaration-owned value family.

    Put DeclaredFamily before this capability in the root's bases, preserving
    its name-decoding contract. Each JsonShapeMember declares
    its external shape through its value annotation; this codec owns primitive
    selection and recursive conversion. Rendering stays with the value owners.
    Its wire value is the declared shape itself, so it answers
    ``external_json_shape`` True and decodes through ``from_wire``.
    """

    external_json_shape = True

    accepts_null = True

    @classmethod
    def _value_annotation(cls, member):
        if tuple(item.name for item, _ in FieldCodec.record_fields(member)) != ("value",):
            raise TypeError("A JSON shape must declare one value field")
        return FieldCodec._types(member)["value"]

    @classmethod
    def from_wire(cls, data):
        if not issubclass(cls, DeclaredFamily):
            raise TypeError("A JSON shape family requires a declared family")
        owners = []
        for member in cls.members_with(JsonShapeMember):
            annotation = cls._value_annotation(member)
            shape = get_origin(annotation) or annotation
            if shape in (tuple, frozenset):
                shape = list
            if shape not in (dict, list, str, int, float, bool, type(None)):
                raise TypeError(f"Unsupported JSON shape declaration: {member.__name__}")
            if type(data) is shape:
                owners.append((member, annotation))
        if len(owners) != 1:
            raise ValueError(f"Expected one {cls.__name__} owner for {type(data).__name__}")
        member, annotation = owners[0]
        result = member.from_json_value(FieldCodec.decode(annotation, data))
        if not isinstance(result, cls):
            raise TypeError("A JSON shape factory must return its own family")
        return result

    def to_wire(self):
        return self.value


class TextRepresentation(FieldRepresentation):
    """External text scalars share strict decoding at the canonical boundary."""

    @classmethod
    def decode(cls, value: object) -> object:
        return cls.from_text(FieldCodec.decode(str, value))

    @classmethod
    @abstractmethod
    def from_text(cls, value: str) -> object: ...

    @classmethod
    def schema(cls) -> dict[str, Any]:
        return FieldCodec.value_schema(str)


class PathText(TextRepresentation):
    @classmethod
    def encode(cls, value: object) -> str:
        if not isinstance(value, Path):
            raise TypeError("Expected a filesystem path")
        return str(value)

    @classmethod
    def from_text(cls, value: str) -> Path:
        return Path(value)


class TimestampText(TextRepresentation):
    @classmethod
    def encode(cls, value: object) -> str:
        if not isinstance(value, datetime):
            raise TypeError("Expected a datetime")
        return value.isoformat()

    @classmethod
    def from_text(cls, value: str) -> datetime:
        return datetime.fromisoformat(value)


class Projected(property):
    """A computed field declared by its owner for a named read-only view."""

    def __init__(self, getter, *, view: str, name: str | None = None):
        super().__init__(getter)
        self.view = view
        self.wire_name = name or getter.__name__


def projected(*, view: str, name: str | None = None):
    return lambda getter: Projected(getter, view=view, name=name)


class FieldCodec(Sealed):
    """Family tags use their declared ``family_discriminator`` (default ``kind``); aliases
    use field(metadata={"wire_name": ...}).

    Decoding rejects unknown fields and primitive coercions (including bool as
    int). Missing fields use declared defaults. Tuple fields round-trip as JSON
    arrays. Init fields and explicitly projected wire properties participate;
    projected values are derived again on decode. ClassVars are not stored.
    ``wire_nonnull`` rejects an explicit null while allowing an omitted default.
    Annotated field representations add scalar/capture behavior while retaining
    the single record codec and declaration-derived schema.
    """

    _declaration_cache_capacity = 256

    @staticmethod
    def _representation(annotation: object) -> tuple[object, type[FieldRepresentation] | None]:
        if annotation is None:
            return None, None
        if isinstance(annotation, type):
            # A class has no Annotated metadata. Its live capability still
            # determines representation, including inherited/virtual members.
            return annotation, annotation if issubclass(annotation, WireValue) else None
        if get_origin(annotation) is Annotated:
            target, *metadata = get_args(annotation)
        else:
            target, metadata = annotation, ()
        representations = [
            item for item in metadata
            if isinstance(item, type) and issubclass(item, FieldRepresentation)
        ]
        if len(representations) > 1:
            raise TypeError("A field must have one representation")
        if isinstance(target, TypeAliasType):
            from pydantic import JsonValue

            if target is JsonValue:
                return Any, representations[0] if representations else _AnyDecoder
        if representations:
            return target, representations[0]
        if isinstance(target, type) and issubclass(target, WireValue):
            return target, target
        return target, None

    @classmethod
    def _value_representation(cls, value: object, annotation: object):
        if annotation is None:
            return cls._representation(type(value))[1]
        _, representation = cls._representation(annotation)
        if representation is None:
            _, representation = cls._representation(type(value))
        return representation

    @staticmethod
    @lru_cache(maxsize=_declaration_cache_capacity)
    def record_fields(cls: Any) -> tuple[tuple[Field[Any], str], ...]:
        """The codec's record declarations and wire keys, in wire order."""
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
    @lru_cache(maxsize=_declaration_cache_capacity)
    def _types(declaration: type) -> dict[str, Any]:
        return get_type_hints(declaration, include_extras=True)

    @overload
    @classmethod
    def encode(cls, value: DeclaredFamily) -> dict[str, Any]: ...

    @overload
    @classmethod
    def encode(cls, value: object, annotation: object = None) -> Any: ...

    @classmethod
    def encode(cls, value: object, annotation: object = None) -> Any:
        representation = cls._value_representation(value, annotation)
        if representation is not None and value is not None:
            return representation.encode(value)
        # Exact JSON scalars cannot also be records, enums or family classes.
        # Representation selection above retains custom scalar declarations.
        kind = type(value)
        if value is None or kind is str or kind is int or kind is bool:
            return value
        if kind is float and math.isfinite(value):
            return value
        if is_dataclass(value) and not isinstance(value, type):
            hints = cls._types(type(value))
            result = (
                {value.family_discriminator: value.declared_name}
                if isinstance(value, DeclaredFamily)
                else {}
            )
            result.update(
                (key, cls.encode(getattr(value, field.name), hints[field.name]))
                for field, key in cls.record_fields(type(value))
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
        if isinstance(value, frozenset):
            return [cls.encode(item) for item in sorted(value)]
        if isinstance(value, (list, tuple)):
            return [cls.encode(item) for item in value]
        if isinstance(value, dict) and all(type(key) is str for key in value):
            return {key: cls.encode(item) for key, item in value.items()}
        raise TypeError(f"Unsupported JSON value: {type(value).__name__}")

    @classmethod
    def decode(cls, target: type[T], data: object) -> T:
        return cast(T, compile_decoder(target).decode(data))

    @classmethod
    def _family_schema(cls, target: type[DeclaredFamily]) -> dict[str, Any]:
        """Instance values carry their selector inside the encoded object."""
        alternatives = []
        for member in target.members_with(target):
            tag = member.family_discriminator
            alternatives.append(cls._record_schema(
                member, {tag: {"type": "string", "const": member.declared_name}}, [tag]
            ))
        return {"oneOf": alternatives} if alternatives else {"not": {}}

    @classmethod
    def record_schema(cls, target: type) -> dict[str, Any]:
        """Named request fields; external tool/CLI selection remains separate."""
        return cls._record_schema(target, {}, [])

    @classmethod
    def _record_schema(
        cls, target: type, properties: dict[str, Any], required: list[str]
    ) -> dict[str, Any]:
        """Build fields once, including an internally supplied family selector."""
        hints = cls._types(target)
        for declared, key in cls.record_fields(target):
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
                schema["default"] = cls.encode(declared.default, annotation)
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
        annotation, representation = cls._representation(annotation)
        if representation is not None:
            schema = representation.schema()
            if type(None) in get_args(annotation):
                return {"anyOf": [schema, {"type": "null"}]}
            return schema
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
        if origin is dict and args[0] is str:
            return {"type": "object", "additionalProperties": cls.value_schema(args[1])}
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
        if isinstance(annotation, type) and issubclass(annotation, DeclaredFamily):
            return cls._family_schema(annotation)
        if isinstance(annotation, type) and is_dataclass(annotation):
            return cls.record_schema(annotation)
        raise TypeError(f"No declared JSON schema for {annotation}")

    @classmethod
    def project(cls, value: object, view: str, annotation: object = None) -> Any:
        """Encode a redacted view using field exclusions and owned properties.

        Projection is deliberately one way. Excluded secrets cannot be rebuilt
        from a read-only view, and a view never serves as a persistence record.
        """
        representation = cls._value_representation(value, annotation)
        if representation is not None:
            return cls.encode(value, annotation)
        if is_dataclass(value) and not isinstance(value, type):
            hints = cls._types(type(value))
            result = {
                field.metadata.get(f"{view}_name", key): cls.project(
                    getattr(value, field.name), view, hints[field.name]
                )
                for field, key in cls.record_fields(type(value))
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

    @dataclass(frozen=True)
    class _FieldPlan:
        """One field's wire key, checks and compiled value decoder."""

        name: str
        key: str
        required: bool
        reject_null: bool
        choices: object
        value: ValueDecoder

    @dataclass(frozen=True)
    class _RecordPlan:
        """A record's whole decode plan, derived once from its declaration."""

        fields: tuple[FieldCodec._FieldPlan, ...]
        required_keys: tuple[str, ...]
        init_required_keys: tuple[str, ...]
        reject_null_keys: frozenset[str]
        accepted_keys: frozenset[str]

    @classmethod
    @lru_cache(maxsize=_declaration_cache_capacity)
    def _record_plan(cls, record: type) -> FieldCodec._RecordPlan:
        """Resolve every per-row type decision for one record declaration once."""
        declared = cls.record_fields(record)
        hints = cls._types(record)
        computed = {member.wire_name for member in cls._projections(record, "wire").values()}
        entries = tuple(
            cls._FieldPlan(
                name=field.name,
                key=key,
                required=bool(field.metadata.get("wire_required")),
                reject_null=bool(field.metadata.get("wire_nonnull")),
                choices=field.metadata.get("wire_choices"),
                value=compile_decoder(hints[field.name]),
            )
            for field, key in declared
        )
        return cls._RecordPlan(
            fields=entries,
            required_keys=tuple(entry.key for entry in entries if entry.required),
            init_required_keys=tuple(
                entry.key for entry, (field, _) in zip(entries, declared)
                if field.default is MISSING and field.default_factory is MISSING
            ),
            reject_null_keys=frozenset(entry.key for entry in entries if entry.reject_null),
            accepted_keys=frozenset(entry.key for entry in entries) | computed,
        )

    @staticmethod
    @lru_cache(maxsize=_declaration_cache_capacity)
    def _projections(declaration: type, view: str) -> types.MappingProxyType[str, Projected]:
        # Cache immutable declarations only. Each consumer still invokes the
        # original property on the current record; projected values are never cached.
        return types.MappingProxyType({
            name: member
            for base in reversed(declaration.__mro__)
            for name, member in vars(base).items()
            if isinstance(member, Projected) and member.view == view
        })

    @staticmethod
    def _decode_scalar(target: Any, data: Any) -> Any:
        """The original strict scalar boundary, after representation selection.

        Exact primitive declarations skip structural dispatch. Unknown targets
        reach this only after that dispatch, preserving custom annotation
        equality and record/enum construction precedence.
        """
        if target in (str, int, bool, type(None)) and type(data) is target:
            return data
        if target is float and type(data) in (int, float) and math.isfinite(data):
            return data
        raise ValueError(f"Expected {target}, received {type(data).__name__}")


class ValueDecoder(ABC):
    """One annotation's decode decisions, resolved once from its declaration.

    Each way a value decodes is one subclass. ``compile_decoder`` picks the class
    from the annotation once; decoding a value is that object's own
    ``decode``, with no per-value re-classification.
    """

    __slots__ = ()

    @abstractmethod
    def decode(self, data: object) -> object: ...

@dataclass(frozen=True, slots=True)
class _ScalarDecoder(ValueDecoder):
    target: object

    def decode(self, data):
        return FieldCodec._decode_scalar(self.target, data)

@dataclass(frozen=True, slots=True)
class _RepresentedDecoder(ValueDecoder):
    """A declared representation capability; null without one decodes structurally."""

    representation: type[FieldRepresentation]
    structural: ValueDecoder
    accepts_null: bool
    external_json_shape: bool

    def decode(self, data):
        representation = self.representation
        if data is None and not self.accepts_null:
            return self.structural.decode(data)
        if self.external_json_shape:
            # Decode through the representation capability, independently
            # of the family's public name decoder. Children validate once;
            # WireValue's pre-validation would traverse each subtree again.
            return representation.from_wire(data)
        return representation.decode(data)

@dataclass(frozen=True, slots=True)
class _AnyDecoder(ValueDecoder, FieldRepresentation):
    @staticmethod
    @lru_cache(maxsize=1)
    def validator():
        from pydantic import ConfigDict, JsonValue, TypeAdapter

        return TypeAdapter(JsonValue, config=ConfigDict(allow_inf_nan=False))

    @classmethod
    def decode(cls, data):
        try:
            return cls.validator().validate_python(data, strict=True)
        except ValueError as error:
            raise TypeError("Expected a JSON value with string keys and finite numbers") from error

    @classmethod
    def encode(cls, value):
        return cls.decode(value)

    @classmethod
    def schema(cls):
        return {}

@dataclass(frozen=True, slots=True)
class _FamilyNameDecoder(ValueDecoder):
    family: type[DeclaredFamily]

    def decode(self, data):
        if type(data) is not str:
            raise ValueError("Expected a declared family name.")
        return self.family.decode(data)

@dataclass(frozen=True, slots=True)
class _LiteralDecoder(ValueDecoder):
    target: object
    values: tuple[object, ...]

    def decode(self, data):
        if any(type(data) is type(value) and data == value for value in self.values):
            return data
        raise ValueError(f"Value does not match {self.target}")

@dataclass(frozen=True, slots=True)
class _UnionDecoder(ValueDecoder):
    target: object
    alternatives: tuple[ValueDecoder, ...]

    def decode(self, data):
        first_error = None
        try:
            for alternative in self.alternatives:
                try:
                    return alternative.decode(data)
                except (TypeError, ValueError) as error:
                    if first_error is None:
                        first_error = error
            # Keep the original declaration-owned reason on total failure.
            raise ValueError(
                f"Value does not match {self.target}: {first_error}"
            ) from first_error
        finally:
            # A swallowed traceback points back to this frame. End that
            # loan even on success, rather than leaving a cyclic payload
            # graph for a later UI-thread garbage collection.
            first_error = None

@dataclass(frozen=True, slots=True)
class _FrozenSetDecoder(ValueDecoder):
    item: ValueDecoder

    def decode(self, data):
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array.")
        return frozenset(self.item.decode(value) for value in data)

@dataclass(frozen=True, slots=True)
class _ListDecoder(ValueDecoder):
    item: ValueDecoder

    def decode(self, data):
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array.")
        return [self.item.decode(value) for value in data]

@dataclass(frozen=True, slots=True)
class _VariadicTupleDecoder(ValueDecoder):
    item: ValueDecoder

    def decode(self, data):
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array.")
        return tuple(self.item.decode(value) for value in data)

@dataclass(frozen=True, slots=True)
class _FixedTupleDecoder(ValueDecoder):
    items: tuple[ValueDecoder, ...]

    def decode(self, data):
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array.")
        if len(data) != len(self.items):
            raise ValueError("Tuple length does not match declaration.")
        return tuple(item.decode(value) for item, value in zip(self.items, data, strict=True))

@dataclass(frozen=True, slots=True)
class _DictDecoder(ValueDecoder):
    string_keys: bool
    value: ValueDecoder

    def decode(self, data):
        if not isinstance(data, dict) or not self.string_keys:
            raise ValueError("Expected a JSON object with string keys.")
        return {
            FieldCodec._decode_scalar(str, key): self.value.decode(value)
            for key, value in data.items()
        }

@dataclass(frozen=True, slots=True)
class _FamilyRecordDecoder(ValueDecoder):
    """The encoded object carries its member's name under the family tag."""

    family: type[DeclaredFamily]

    def decode(self, data):
        if not isinstance(data, dict):
            raise ValueError("Expected a family object.")
        tag = self.family.family_discriminator
        name = data.get(tag)
        if not isinstance(name, str):
            raise ValueError(f"Expected a string family {tag}.")
        member = self.family.decode(name)
        return _member_decoder(member).decode(
            {key: value for key, value in data.items() if key != tag}
        )

@dataclass(frozen=True, slots=True)
class _RecordDecoder(ValueDecoder):
    """A dataclass record. Its plan is looked up at decode time, so a
    record that contains itself compiles without recursion."""

    record: type

    def decode(self, data):
        target = self.record
        plan = FieldCodec._record_plan(target)
        if not isinstance(data, dict):
            raise ValueError("Expected a record object.")
        missing = [key for key in plan.required_keys if key not in data]
        if missing:
            raise ValueError(
                f"Missing required fields for {target.__name__}: {', '.join(missing)}"
            )
        init_missing = [key for key in plan.init_required_keys if key not in data]
        if init_missing:
            raise TypeError(
                f"Missing required fields for {target.__name__}: {', '.join(init_missing)}"
            )
        unknown = data.keys() - plan.accepted_keys
        if unknown:
            raise ValueError(f"Unknown fields for {target.__name__}: {sorted(unknown)}")
        null = plan.reject_null_keys & data.keys()
        if any(data[key] is None for key in null):
            raise ValueError(f"Null field for {target.__name__}")
        values = {}
        for entry in plan.fields:
            if entry.key not in data:
                continue
            value = data[entry.key]
            if entry.choices is not None and value not in entry.choices():
                raise ValueError(
                    f"Field {entry.key} must be one of: {', '.join(entry.choices())}"
                )
            values[entry.name] = entry.value.decode(value)
        return target(**values)

@dataclass(frozen=True, slots=True)
class _EnumDecoder(ValueDecoder):
    target: object

    def decode(self, data):
        return self.target(data)

@lru_cache(maxsize=None)
def compile_decoder(annotation: object) -> ValueDecoder:
    """Compile one annotation's decoder. Declarations are finite and
    immutable for this process, so every decoder is built once."""
    target, representation = FieldCodec._representation(annotation)
    structural = _structural_decoder(target)
    if representation is None:
        return structural
    return _RepresentedDecoder(
        representation, structural,
        representation.accepts_null, representation.external_json_shape,
    )

def _structural_decoder(target: object) -> ValueDecoder:
    if (
        target is str or target is int or target is bool
        or target is type(None) or target is float
    ):
        return _ScalarDecoder(target)
    if target is Any:
        return _AnyDecoder()
    origin, args = get_origin(target), get_args(target)
    if origin is type and args and issubclass(args[0], DeclaredFamily):
        return _FamilyNameDecoder(args[0])
    if origin is Literal:
        return _LiteralDecoder(target, args)
    if origin in (Union, types.UnionType):
        return _UnionDecoder(target, tuple(compile_decoder(item) for item in args))
    if origin is frozenset:
        return _FrozenSetDecoder(compile_decoder(args[0]))
    if origin is list:
        return _ListDecoder(compile_decoder(args[0]))
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return _VariadicTupleDecoder(compile_decoder(args[0]))
        return _FixedTupleDecoder(tuple(compile_decoder(item) for item in args))
    if origin is dict:
        return _DictDecoder(args[0] is str, compile_decoder(args[1]))
    if isinstance(target, type) and issubclass(target, DeclaredFamily):
        return _FamilyRecordDecoder(target)
    return _member_decoder(target)

@lru_cache(maxsize=None)
def _member_decoder(target: object) -> ValueDecoder:
    """A concrete record, enum or scalar, after any family tag is removed."""
    if isinstance(target, type) and is_dataclass(target):
        return _RecordDecoder(target)
    if isinstance(target, type) and issubclass(target, Enum):
        return _EnumDecoder(target)
    return _ScalarDecoder(target)
