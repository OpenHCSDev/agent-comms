"""Declared CLI and tool replies share their JSON behavior."""

from .field_codec import FieldCodec


class DeclaredReply:
    """A declared reply record; the one sender that owns its transport encodes it."""

    __slots__ = ()

    def to_wire(self) -> object:
        """Declared records use the shared codec; specialized replies own their form."""
        return FieldCodec.encode(self)


class ToolResult(DeclaredReply):
    """A tool's declared result record; invoke alone encodes it for every adapter."""

    __slots__ = ()
