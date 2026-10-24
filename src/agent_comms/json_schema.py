"""JSON schema behavior shared by wire representations and value decoders."""

from typing import ClassVar

from pydantic import JsonValue


class JsonSchemaOwner:
    __slots__ = ()

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        raise TypeError(f"{cls.__name__} declares no JSON schema")


class JsonScalar(JsonSchemaOwner):
    __slots__ = ()

    json_type: ClassVar[str]

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return {"type": cls.json_type}
