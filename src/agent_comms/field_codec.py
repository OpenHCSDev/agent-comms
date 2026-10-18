"""Strict JSON boundary conversion derived from dataclass store_files."""

from __future__ import annotations

import math
import json
import types
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import MISSING, Field, dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from functools import cached_property, lru_cache, reduce
from operator import or_
from pathlib import Path
from typing_extensions import TypeAliasType
from typing import (
    Annotated,
    Any,
    ClassVar,
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


class MalformedRecord(ValueError):  # noqa: N818 - names the refused data
    """Data that does not satisfy its declaration.

    Every data refusal at the JSON and record boundary raises this. A broken
    declaration stays a TypeError and is never caught at a data boundary.
    """

    @classmethod
    @contextmanager
    def refusing(cls, subject: str) -> Iterator[None]:
        """Any check that refuses decoded data with ValueError refuses a malformed record.

        A domain error raised by the record's own checks stays the cause.
        """
        try:
            yield
        except cls:
            raise
        except ValueError as error:
            raise cls(f"{subject}: {error}") from error


@dataclass(frozen=True)
class FieldPlan:
    """One field's wire keys, checks and compiled value decoder."""

    name: str
    key: str
    required: bool
    reject_null: bool
    choices: object
    value: ValueDecoder[object]
    annotation: object
    declared: Field

    def contribute(self, fields):
        fields.append(self)
        return (self.key,)

    def encoded(self, value, *, key=None):
        return {key or self.key: value}

    def schema_fields(self):
        annotation, declared = self.annotation, self.declared
        metadata = declared.metadata
        if metadata.get("wire_nonnull") and get_origin(annotation) in (Union, types.UnionType):
            members = tuple(item for item in get_args(annotation) if item is not type(None))
            annotation = members[0] if len(members) == 1 else Union[members]  # noqa: UP007 - runtime tuple
        schema = FieldCodec.value_schema(annotation)
        if "description" in metadata:
            schema["description"] = metadata["description"]
        if "wire_choices" in metadata:
            schema["enum"] = list(metadata["wire_choices"]())
        if declared.default is not MISSING and declared.default is not None:
            schema["default"] = FieldCodec.encode(declared.default, annotation)
        return {self.key: schema}

    def decoded(self, data):
        if self.key not in data:
            return {}
        value = data[self.key]
        if self.choices is not None and value not in self.choices():
            raise MalformedRecord(f"Field {self.key} must be one of: {', '.join(self.choices())}")
        return {self.name: self.value.decode(value)}


class InlineFieldPlan(FieldPlan):
    @cached_property
    def record(self):
        return FieldCodec._record_plan(self.value.representation.wire_record())

    def contribute(self, fields):
        fields.extend(self.record.fields)
        return self.record.init_required_keys

    def encoded(self, value, *, key=None):
        return value

    def decoded(self, data):
        return {self.name: self.value.decode({entry.key: data[entry.key] for entry in self.record.fields if entry.key in data})}


class FieldRepresentation(ABC):
    """A field capability, declared as ``Annotated[ValueType, Representation]``.

    Private transports can declare captures without permitting those objects in
    ordinary JSON. All record structure remains owned by FieldCodec.
    """

    field_plan: ClassVar[type[FieldPlan]] = FieldPlan
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


class SubclassUnion(WireValue):
    """An untagged wire union derives its alternatives from direct declarations."""

    accepts_null = True
    external_json_shape = True
    _decoder: ClassVar[UnionDecoder[Self]]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        if SubclassUnion in cls.__bases__:
            return
        for base in cls.__bases__:
            if SubclassUnion in base.__bases__:
                base._decoder = ValueDecoder.for_members(tuple(base.__subclasses__()))

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls._decoder.decode(data)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(cls._decoder.target)


class NullMember(WireValue):
    """One declared empty member owns the JSON null representation."""

    accepts_null = True

    @classmethod
    def from_wire(cls, data: object) -> Self:
        FieldCodec.decode(type(None), data)
        return cls()

    def to_wire(self) -> None:
        return None

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(type(None))


class InlineRecord(WireValue):
    """A typed field represented by its declared object's sibling wire fields."""

    field_plan = InlineFieldPlan
    external_json_shape = True

    @classmethod
    def wire_record(cls):
        return cls

    @classmethod
    def schema(cls):
        return FieldCodec.record_schema(cls.wire_record())


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
            raise MalformedRecord(f"Expected one {cls.__name__} owner for {type(data).__name__}")
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
        with MalformedRecord.refusing(f"Invalid {cls.__name__}"):
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
            raise MalformedRecord("Expected a filesystem path")
        return str(value)

    @classmethod
    def from_text(cls, value: str) -> Path:
        return Path(value)


class TimestampText(TextRepresentation):
    @classmethod
    def encode(cls, value: object) -> str:
        if not isinstance(value, datetime):
            raise MalformedRecord("Expected a datetime")
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
        """The record's field declarations, in wire order; plans own expanded keys."""
        # Declarations are immutable for this process; cache only their derived
        # schema, never decoded rows, registry membership or document revisions.
        declared = [(field, FieldCodec.wire_key(field)) for field in fields(cls) if field.init]
        keys = [key for _, key in declared]
        if any(not isinstance(key, str) or not key for key in keys):
            raise TypeError("Wire field names must be nonempty strings.")
        return tuple(
            item
            for _, item in sorted(
                enumerate(declared),
                key=lambda row: (row[1][0].metadata.get("wire_order", row[0]), row[0]),
            )
        )

    @staticmethod
    def wire_key(declaration: Field[object]) -> str:
        """The name a declared field carries on the wire."""
        return cast(str, declaration.metadata.get("wire_name", declaration.name))

    @classmethod
    def record_key(cls, record: type, name: str) -> str:
        return next(key for item, key in cls.record_fields(record) if item.name == name)

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
            return cls.encode_record(value)
        if isinstance(value, type) and issubclass(value, DeclaredFamily):
            return value.declared_name
        if isinstance(value, Enum):
            return cls.encode(value.value)
        if isinstance(value, (list, tuple, frozenset)):
            target, _ = cls._representation(annotation)
            arguments = get_args(target)
            items = sorted(value) if isinstance(value, frozenset) else value
            if get_origin(target) is tuple and arguments and arguments[-1] is not Ellipsis:
                return [cls.encode(item, expected) for item, expected in zip(items, arguments, strict=True)]
            element = arguments[0] if arguments else None
            return [cls.encode(item, element) for item in items]
        if isinstance(value, dict):
            target, _ = cls._representation(annotation)
            key_type, value_type = get_args(target) if get_origin(target) is dict else (None, None)
            if key_type is str or (key_type is None and all(type(key) is str for key in value)):
                return {key: cls.encode(item, value_type) for key, item in value.items()}
            return [[cls.encode(key, key_type), cls.encode(item, value_type)] for key, item in value.items()]
        raise TypeError(f"Unsupported JSON value: {type(value).__name__}")

    @classmethod
    def encode_document(cls, value: object, declared: object) -> object:
        """A document whose declaration names its leaf member stores that member untagged."""
        member = _FamilyRecordDecoder.document_member(declared)
        if member is None:
            return cls.encode(value)
        if get_origin(declared) is dict:
            records = cast(dict[str, object], value)
            return {key: cls.encode_fields(item) for key, item in records.items()}
        return cls.encode_fields(value)

    @classmethod
    def decode_document(cls, declared: type[T], data: object) -> T:
        member = _FamilyRecordDecoder.document_member(declared)
        if member is None:
            return cls.decode(declared, data)
        decoder = _member_decoder(member)
        if get_origin(declared) is not dict:
            return cast(T, decoder.decode(data))
        if not isinstance(data, dict):
            raise MalformedRecord("Expected a JSON object.")
        return cast(T, {key: decoder.decode(item) for key, item in data.items()})

    @classmethod
    def encode_record(cls, value: object) -> dict[str, Any]:
        """Encode a record: its family tag, then its declared fields."""
        tag = {}
        if isinstance(value, DeclaredFamily):
            root = type(value)._family_root
            if root is None:
                raise TypeError("A family value has no family root.")
            tag[root.family_discriminator] = value.declared_name
        return {**tag, **cls.encode_fields(value)}

    @classmethod
    def encode_fields(cls, value: object) -> dict[str, object]:
        """Encode declared fields, including inside a custom wire representation."""
        hints = cls._types(type(value))
        result: dict[str, object] = {}
        plans = {entry.name: entry for entry in cls._record_plan(type(value)).bindings}
        for field, key in cls.record_fields(type(value)):
            current = getattr(value, field.name)
            default = field.default if field.default is not MISSING else (
                field.default_factory() if field.default_factory is not MISSING else MISSING
            )
            if not (field.metadata.get("wire_omit_default") and current == default):
                result.update(plans[field.name].encoded(cls.encode(current, hints[field.name])))
        result.update(
            (member.wire_name, cls.encode(getattr(value, name)))
            for name, member in cls._projections(type(value), "wire").items()
        )
        return result

    @classmethod
    def decode_record(cls, target: type[T], data: object) -> T:
        """Decode declared fields without reentering the record's wire representation."""
        return cast(T, _RecordDecoder(target).decode(data))

    @classmethod
    def decode(cls, target: type[T], data: object) -> T:
        return cast(T, compile_decoder(target).decode(data))

    @staticmethod
    def parse(text: str | bytes) -> object:
        """External JSON text is data: strict UTF-8, syntax and duplicate keys refuse it."""
        with MalformedRecord.refusing("Invalid JSON"):
            return json.loads(
                text.decode("utf-8") if isinstance(text, bytes) else text,
                object_pairs_hook=_unique_object,
            )

    @staticmethod
    def construct(target: Callable[..., T], values: dict[str, object]) -> T:
        """A record built from decoded data refuses its values as a malformed record."""
        with MalformedRecord.refusing(f"Invalid {target.__name__}"):
            return target(**values)

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
        cls._record_plan(target).schema_fields(properties, required)
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
            return {**cls.value_schema(type(args[0])), "enum": [value for _, value in compile_decoder(annotation).editor_choices()]}
        if origin in (list, tuple, frozenset):
            return {"type": "array", "items": cls.value_schema(args[0])}
        if origin is dict:
            if args[0] is str:
                return {"type": "object", "additionalProperties": cls.value_schema(args[1])}
            return {"type": "array", "items": {"type": "array", "prefixItems": [cls.value_schema(arg) for arg in args], "minItems": 2, "maxItems": 2}}
        if origin is type and args and issubclass(args[0], DeclaredFamily):
            return {"type": "string", "enum": [value for _, value in compile_decoder(annotation).editor_choices()]}
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            values = [value for _, value in compile_decoder(annotation).editor_choices()]
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
            result = {}
            plans = {entry.name: entry for entry in cls._record_plan(type(value)).bindings}
            for field, key in cls.record_fields(type(value)):
                if not field.metadata.get(f"{view}_exclude"):
                    encoded = cls.project(getattr(value, field.name), view, hints[field.name])
                    result.update(plans[field.name].encoded(
                        encoded, key=field.metadata.get(f"{view}_name", key)
                    ))
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
    class _RecordPlan:
        """A record's whole decode plan, derived once from its declaration."""

        fields: tuple[FieldPlan, ...]
        bindings: tuple[FieldPlan, ...]
        required_keys: tuple[str, ...]
        init_required_keys: tuple[str, ...]
        reject_null_keys: frozenset[str]
        accepted_keys: frozenset[str]

        def schema_fields(self, properties, required):
            for entry in self.fields:
                properties.update(entry.schema_fields())
            required.extend(self.init_required_keys)
            return properties

    @classmethod
    @lru_cache(maxsize=_declaration_cache_capacity)
    def _record_plan(cls, record: type) -> FieldCodec._RecordPlan:
        """Resolve every per-row type decision for one record declaration once."""
        declared = cls.record_fields(record)
        hints = cls._types(record)
        computed = {member.wire_name for member in cls._projections(record, "wire").values()}
        entries = tuple(
            (cls._representation(hints[field.name])[1] or FieldRepresentation).field_plan(
                name=field.name,
                key=key,
                required=bool(field.metadata.get("wire_required")),
                reject_null=bool(field.metadata.get("wire_nonnull")),
                choices=field.metadata.get("wire_choices"),
                value=compile_decoder(hints[field.name]),
                annotation=hints[field.name],
                declared=field,
            )
            for field, key in declared
        )
        wire_fields, required_keys, init_required_keys = [], [], []
        for entry in entries:
            keys = entry.contribute(wire_fields)
            if entry.required:
                required_keys.extend(keys)
            if entry.declared.default is MISSING and entry.declared.default_factory is MISSING:
                init_required_keys.extend(keys)
        wire_keys = [entry.key for entry in wire_fields]
        if len(set(wire_keys)) != len(wire_keys) or (
            issubclass(record, DeclaredFamily) and record.family_discriminator in wire_keys
        ):
            raise TypeError("Conflicting wire field names.")
        return cls._RecordPlan(
            fields=tuple(wire_fields),
            bindings=entries,
            required_keys=tuple(required_keys),
            init_required_keys=tuple(init_required_keys),
            reject_null_keys=frozenset(entry.key for entry in entries if entry.reject_null),
            accepted_keys=frozenset(wire_keys) | computed,
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

class ValueDecoder[T](ABC):
    """One annotation owns its wire decoding, draft projection and choice domain."""

    __slots__ = ()
    _by_origin: ClassVar[dict[object, type[ValueDecoder[object]]]] = {}
    _by_annotation: ClassVar[dict[object, type[ValueDecoder[object]]]] = {}

    def __init_subclass__(cls):
        super().__init_subclass__()
        for origin in cls.__dict__.get("origins", ()):
            cls._by_origin[origin] = cls
        for annotation in cls.__dict__.get("annotations", ()):
            cls._by_annotation[annotation] = cls

    @classmethod
    def from_annotation(cls, target, args):
        return cls(target)

    @classmethod
    def for_members[M](cls, members: tuple[type[M], ...]) -> UnionDecoder[M]:
        return UnionDecoder(
            reduce(or_, members),
            tuple(compile_decoder(member) for member in members),
        )

    @abstractmethod
    def decode(self, data: object) -> T: ...

    editor_annotation = str

    def encode_editor(self, value):
        encoded = FieldCodec.encode(value)
        return None if encoded is None else json.dumps(encoded)

    def decode_editor(self, value):
        return None if value is None else json.loads(value)

    def editor_choices(self) -> tuple[tuple[str, object], ...]:
        raise TypeError("The declared decoder does not offer finite choices")

    def argument_options(self) -> dict[str, object]:
        """argparse options for this annotation's command-line text."""
        return {}

    def argument_text(self, encoded: object) -> object:
        """The command-line spelling of an encoded value."""
        return encoded

    def argument_payload(self, text: object) -> object:
        """The payload value that command-line text spells."""
        return text


class _ChoiceDecoder[T](ValueDecoder[T]):
    """A finite domain: its command line offers exactly the editor's choices."""

    __slots__ = ()

    def argument_options(self) -> dict[str, object]:
        return {"choices": [self.argument_text(value) for _, value in self.editor_choices()]}

@dataclass(frozen=True, slots=True)
class _ScalarDecoder(ValueDecoder[Any]):
    """A plain target accepts no wire value unless a scalar below declares it."""

    target: object

    def decode(self, data):
        if self.accepts(data):
            return data
        raise MalformedRecord(f"Expected {self.target}, received {type(data).__name__}")

    def accepts(self, data: object) -> bool:
        return False

class _ExactScalarDecoder(_ScalarDecoder):
    """A JSON scalar decoded only from its own type."""

    __slots__ = ()

    def accepts(self, data: object) -> bool:
        return type(data) is self.target

class _TextDecoder(_ExactScalarDecoder):
    __slots__ = ()
    annotations = (str,)

    def encode_editor(self, value):
        return value

    def decode_editor(self, value):
        return value

class _FlagDecoder(_ExactScalarDecoder):
    __slots__ = ()
    annotations = (bool,)

    def argument_options(self) -> dict[str, object]:
        return {"action": "store_true"}

class _NumberDecoder(_ExactScalarDecoder):
    __slots__ = ()

    def argument_options(self) -> dict[str, object]:
        return {"type": self.target}

class _IntegerDecoder(_NumberDecoder):
    __slots__ = ()
    annotations = (int,)

class _RealDecoder(_NumberDecoder):
    """A finite number; an integer is a real number on the wire."""

    __slots__ = ()
    annotations = (float,)

    def accepts(self, data: object) -> bool:
        return type(data) in (int, float) and math.isfinite(cast(float, data))

class _NullDecoder(_ExactScalarDecoder):
    __slots__ = ()
    annotations = (type(None),)
    editor_annotation = type(None)

@dataclass(frozen=True, slots=True)
class _RepresentedDecoder(ValueDecoder[Any]):
    """A declared representation capability; null without one decodes structurally."""

    representation: type[FieldRepresentation]
    structural: ValueDecoder[object]
    def decode(self, data):
        representation = self.representation
        if data is None and not representation.accepts_null:
            return self.structural.decode(data)
        with MalformedRecord.refusing(f"Invalid {representation.__name__}"):
            if representation.external_json_shape:
                # Decode through the representation capability, independently
                # of the family's public name decoder. Children validate once;
                # WireValue's pre-validation would traverse each subtree again.
                return representation.from_wire(data)
            return representation.decode(data)

@dataclass(frozen=True, slots=True)
class _AnyDecoder(ValueDecoder[Any], FieldRepresentation):
    annotations = (Any, object)

    @classmethod
    def from_annotation(cls, target, args):
        return cls()

    @staticmethod
    @lru_cache(maxsize=1)
    def validator():
        from pydantic import ConfigDict, JsonValue, TypeAdapter

        return TypeAdapter(JsonValue, config=ConfigDict(allow_inf_nan=False))

    @classmethod
    def decode(cls, data):
        with MalformedRecord.refusing("Expected a JSON value with string keys and finite numbers"):
            return cls.validator().validate_python(data, strict=True)

    @classmethod
    def encode(cls, value):
        # An in-process value that is not JSON is the caller's bug, not refused data.
        try:
            return cls.validator().validate_python(value, strict=True)
        except ValueError as error:
            raise TypeError("Expected a JSON value with string keys and finite numbers") from error

    @classmethod
    def schema(cls):
        return {}


def _declared_member(family: type[DeclaredFamily], name: str) -> type[DeclaredFamily]:
    """A wire name the family does not declare is malformed data."""
    with MalformedRecord.refusing(f"Invalid {family.__name__} name"):
        return family.decode(name)


@dataclass(frozen=True, slots=True)
class _FamilyNameDecoder(_ChoiceDecoder[Any]):
    origins = (type,)
    family: type[DeclaredFamily]

    @classmethod
    def from_annotation(cls, target, args):
        return cls(args[0]) if args and issubclass(args[0], DeclaredFamily) else _member_decoder(target)

    def editor_choices(self):
        return tuple((name, name) for name in self.family.names())

    def decode(self, data):
        if type(data) is not str:
            raise MalformedRecord("Expected a declared family name.")
        return _declared_member(self.family, data)

@dataclass(frozen=True, slots=True)
class _LiteralDecoder(_ChoiceDecoder[Any]):
    origins = (Literal,)
    target: object
    values: tuple[object, ...]

    @classmethod
    def from_annotation(cls, target, args):
        return cls(target, args)

    def editor_choices(self):
        return tuple((str(value), value) for value in self.values)

    def decode(self, data):
        if any(type(data) is type(value) and data == value for value in self.values):
            return data
        raise MalformedRecord(f"Value does not match {self.target}")

@dataclass(frozen=True, slots=True)
class UnionDecoder[T](ValueDecoder[T]):
    origins = (Union, types.UnionType)
    target: object
    alternatives: tuple[ValueDecoder[T], ...]

    @classmethod
    def from_annotation(cls, target, args):
        return cls(target, tuple(compile_decoder(item) for item in args))

    @property
    def editor_annotation(self):
        return Union[tuple(item.editor_annotation for item in self.alternatives)]

    def argument_options(self) -> dict[str, object]:
        return self.alternatives[0].argument_options()

    def decode(self, data: object) -> T:
        first_error = None
        try:
            for alternative in self.alternatives:
                try:
                    return alternative.decode(data)
                except MalformedRecord as error:
                    if first_error is None:
                        first_error = error
            # Keep the original declaration-owned reason on total failure.
            raise MalformedRecord(
                f"Value does not match {self.target}: {first_error}"
            ) from first_error
        finally:
            # A swallowed traceback points back to this frame. End that
            # loan even on success, rather than leaving a cyclic payload
            # graph for a later UI-thread garbage collection.
            first_error = None

@dataclass(frozen=True, slots=True)
class _ArrayDecoder(ValueDecoder[Any]):
    items: tuple[ValueDecoder[object], ...]

    @classmethod
    def from_annotation(cls, target, args):
        return cls(tuple(compile_decoder(item) for item in args))

    def decode(self, data):
        if not isinstance(data, list):
            raise MalformedRecord("Expected a JSON array.")
        return self.container(self.decode_items(data))

    def decode_items(self, data):
        return (self.items[0].decode(value) for value in data)

class _FrozenSetDecoder(_ArrayDecoder):
    origins = (frozenset,)
    container = frozenset

class _ListDecoder(_ArrayDecoder):
    origins = (list,)
    container = list

class _TupleDecoder(_ArrayDecoder):
    origins = (tuple,)
    container = tuple

    @classmethod
    def from_annotation(cls, target, args):
        if len(args) == 2 and args[1] is Ellipsis:
            return _VariadicTupleDecoder((compile_decoder(args[0]),))
        return _FixedTupleDecoder(tuple(compile_decoder(item) for item in args))

class _VariadicTupleDecoder(_TupleDecoder):
    """The inherited item decoder applies to every tuple element."""

class _FixedTupleDecoder(_TupleDecoder):
    def decode_items(self, data):
        if len(data) != len(self.items):
            raise MalformedRecord("Tuple length does not match declaration.")
        return (item.decode(value) for item, value in zip(self.items, data, strict=True))

def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise MalformedRecord("Duplicate JSON field")
    return result


def _object_entries(data: object) -> Iterable[tuple[object, object]]:
    if not isinstance(data, dict):
        raise MalformedRecord("Expected a JSON object.")
    return data.items()


def _object_or_pair_entries(data: object) -> Iterable[tuple[object, object]]:
    """Keys that are not strings travel as an array of [key, value] pairs."""
    if isinstance(data, dict):
        return data.items()
    if isinstance(data, list) and all(isinstance(pair, list) and len(pair) == 2 for pair in data):
        return data
    raise MalformedRecord("Expected a JSON object or an array of key-value pairs.")


@dataclass(frozen=True, slots=True)
class _DictDecoder(ValueDecoder[Any]):
    key: ValueDecoder[object]
    value: ValueDecoder[object]
    entries: Callable[[object], Iterable[tuple[object, object]]]
    origins = (dict,)

    @classmethod
    def from_annotation(cls, target, args):
        entries = _object_entries if args[0] is str else _object_or_pair_entries
        return cls(compile_decoder(args[0]), compile_decoder(args[1]), entries)

    def decode(self, data):
        return {self.key.decode(key): self.value.decode(value) for key, value in self.entries(data)}

@dataclass(frozen=True, slots=True)
class _FamilyRecordDecoder(ValueDecoder[Any]):
    """The encoded object carries its member's name under the family tag."""

    family: type[DeclaredFamily]

    @staticmethod
    def document_member(declared: object) -> type[DeclaredFamily] | None:
        """The leaf member a document declares directly or as its mapping's values."""
        if get_origin(declared) is dict and get_args(declared)[0] is str:
            declared = get_args(declared)[1]
        if not (isinstance(declared, type) and issubclass(declared, DeclaredFamily)):
            return None
        members = declared._member_declarations()
        if declared not in members or any(
            other is not declared and issubclass(other, declared) for other in members
        ):
            return None
        return declared

    def editor_choices(self):
        return tuple((member.label, {self.family.family_discriminator: member.declared_name})
                     for member in self.family.members_with(self.family))

    def argument_options(self) -> dict[str, object]:
        """Every member is a command-line choice, labelled or not."""
        return {"choices": list(self.family.names())}

    def argument_text(self, encoded: object) -> object:
        return cast(Mapping[str, object], encoded)[self.family.family_discriminator]

    def argument_payload(self, text: object) -> object:
        return {self.family.family_discriminator: text}

    def decode(self, data):
        if not isinstance(data, dict):
            raise MalformedRecord("Expected a family object.")
        tag = self.family.family_discriminator
        name = data.get(tag)
        if not isinstance(name, str):
            raise MalformedRecord(f"Expected a string family {tag}.")
        member = _declared_member(self.family, name)
        return _member_decoder(member).decode(
            {key: value for key, value in data.items() if key != tag}
        )

@dataclass(frozen=True, slots=True)
class _RecordDecoder(ValueDecoder[Any]):
    """A dataclass record. Its plan is looked up at decode time, so a
    record that contains itself compiles without recursion."""

    record: type

    def decode(self, data):
        target = self.record
        plan = FieldCodec._record_plan(target)
        if not isinstance(data, dict):
            raise MalformedRecord("Expected a record object.")
        missing = dict.fromkeys(
            key for key in (*plan.required_keys, *plan.init_required_keys) if key not in data
        )
        if missing:
            raise MalformedRecord(
                f"Missing required fields for {target.__name__}: {', '.join(missing)}"
            )
        unknown = data.keys() - plan.accepted_keys
        if unknown:
            raise MalformedRecord(f"Unknown fields for {target.__name__}: {sorted(unknown)}")
        null = plan.reject_null_keys & data.keys()
        if any(data[key] is None for key in null):
            raise MalformedRecord(f"Null field for {target.__name__}")
        values = {}
        for entry in plan.bindings:
            values.update(entry.decoded(data))
        return FieldCodec.construct(target, values)

@dataclass(frozen=True, slots=True)
class _EnumDecoder(_ChoiceDecoder[Any]):
    target: object

    def editor_choices(self):
        return tuple((str(member.value), member.value) for member in self.target)

    def decode(self, data):
        with MalformedRecord.refusing(f"Invalid {self.target.__name__}"):
            return self.target(data)

@overload
def compile_decoder[T](annotation: type[T]) -> ValueDecoder[T]: ...
@overload
def compile_decoder(annotation: object) -> ValueDecoder[object]: ...
@lru_cache(maxsize=None)
def compile_decoder(annotation: object) -> ValueDecoder[object]:
    """Compile one annotation's decoder. Declarations are finite and
    immutable for this process, so every decoder is built once."""
    target, representation = FieldCodec._representation(annotation)
    structural = _structural_decoder(target)
    if representation is None:
        return structural
    return _RepresentedDecoder(representation, structural)

def _structural_decoder(target: object) -> ValueDecoder[object]:
    decoder = ValueDecoder._by_annotation.get(target) or ValueDecoder._by_origin.get(get_origin(target))
    if decoder is not None:
        return decoder.from_annotation(target, get_args(target))
    if isinstance(target, type) and issubclass(target, DeclaredFamily):
        return _FamilyRecordDecoder(target)
    return _member_decoder(target)

@lru_cache(maxsize=None)
def _member_decoder(target: object) -> ValueDecoder[object]:
    """A concrete record, enum or scalar, after any family tag is removed."""
    if isinstance(target, type) and is_dataclass(target):
        return _RecordDecoder(target)
    if isinstance(target, type) and issubclass(target, Enum):
        return _EnumDecoder(target)
    return _ScalarDecoder(target)
