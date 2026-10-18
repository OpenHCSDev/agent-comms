"""The one contract every declared tool's apply returns."""


class DeclaredReply:
    """A declared reply record; the one sender that owns its transport encodes it."""

    __slots__ = ()


class ToolResult(DeclaredReply):
    """A tool's declared result record; invoke alone encodes it for every adapter."""

    __slots__ = ()
