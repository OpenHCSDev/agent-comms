"""Declared CLI and tool replies share their JSON behavior."""

from .field_codec import FieldCodec, projected


class DeclaredReply:
    """A declared reply record; the one sender that owns its transport encodes it."""

    __slots__ = ()

    def to_wire(self) -> object:
        """Declared records use the shared codec; specialized replies own their form."""
        return FieldCodec.encode(self)


class ToolResult(DeclaredReply):
    """A tool's declared result record; invoke alone encodes it for every adapter."""

    __slots__ = ()


class OwnGoalPublication(ToolResult):
    """A self-relative goal writer returns the exact declaration it published."""

    __slots__ = ()

    @projected(view="wire", name="agentCommsKind")
    def publication_kind(self) -> str:
        from .pi_payloads import PublishedGoalToolDetails

        return PublishedGoalToolDetails.declared_name
