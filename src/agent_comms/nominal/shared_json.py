"""A JSON document retaining shared subtrees, without equating their values."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from pydantic import JsonValue
from typing_extensions import TypeForm

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import CapturedJson, FieldCodec, MalformedRecord


@dataclass(frozen=True)
class JsonNode(DeclaredFamily, declared_name=None):
    family_discriminator = "node"

    @abstractmethod
    def expand(self, previous: list[JsonValue]) -> JsonValue: ...

    @abstractmethod
    def content_key(self) -> tuple[object, ...]: ...

    @staticmethod
    def require(previous: list[JsonValue], index: int) -> JsonValue:
        if not 0 <= index < len(previous):
            raise MalformedRecord(f"JSON reference {index} is not an earlier node")
        return previous[index]


@dataclass(frozen=True)
class JsonLiteral(JsonNode, declared_name="literal"):
    value: JsonValue

    def content_key(self) -> tuple[object, ...]:
        return (type(self), FieldCodec.compact(self.value))

    def expand(self, previous: list[JsonValue]) -> JsonValue:
        return self.value


@dataclass(frozen=True)
class JsonArray(JsonNode, declared_name="array"):
    items: tuple[int, ...]

    def content_key(self) -> tuple[object, ...]:
        return (type(self), self.items)

    def expand(self, previous: list[JsonValue]) -> JsonValue:
        return [self.require(previous, index) for index in self.items]


@dataclass(frozen=True)
class JsonObject(JsonNode, declared_name="object"):
    entries: dict[str, int]

    def content_key(self) -> tuple[object, ...]:
        return (type(self), tuple(self.entries.items()))

    def expand(self, previous: list[JsonValue]) -> JsonValue:
        return {key: self.require(previous, index) for key, index in self.entries.items()}


@dataclass
class JsonCapture:
    """Decode the raw JSON boundary once; aliases refer to earlier completed nodes."""

    nodes: list[JsonNode] = field(default_factory=list)
    sources: dict[int, tuple[object, int]] = field(default_factory=dict)
    active: set[int] = field(default_factory=set)
    interned: dict[tuple[object, ...], int] = field(default_factory=dict)

    def capture(self, value: JsonValue) -> int:
        identity = id(value)
        if identity in self.sources:
            return self.sources[identity][1]
        if identity in self.active:
            raise MalformedRecord("A JSON document cannot contain a cycle")
        self.active.add(identity)
        try:
            if isinstance(value, CapturedJson):
                node = JsonLiteral(value)
            elif isinstance(value, dict):
                node = JsonObject({key: self.capture(item) for key, item in value.items()})
            elif isinstance(value, list):
                node = JsonArray(tuple(self.capture(item) for item in value))
            else:
                node = JsonLiteral(value)
            key = node.content_key()
            if key in self.interned:
                index = self.interned[key]
            else:
                index = len(self.nodes)
                self.nodes.append(node)
                self.interned[key] = index
            self.sources[identity] = (value, index)
            return index
        finally:
            self.active.remove(identity)


@dataclass(frozen=True)
class SharedJson:
    """One captured publication, expanded only through the requested record declaration."""

    nodes: tuple[JsonNode, ...] = ()
    roots: tuple[int, ...] = ()

    @classmethod
    def of(cls, values: object, declaration: object) -> SharedJson:
        encoded = FieldCodec.encode_shared(values, declaration)
        if not isinstance(encoded, list):
            raise MalformedRecord("Shared JSON roots must be a declared array")
        capture = JsonCapture()
        roots = tuple(capture.capture(value) for value in encoded)
        return cls(tuple(capture.nodes), roots)

    def decode[T](self, declaration: TypeForm[T]) -> T:
        expanded: list[JsonValue] = []
        for node in self.nodes:
            expanded.append(node.expand(expanded))
        return FieldCodec.decode(declaration, [JsonNode.require(expanded, root) for root in self.roots])
