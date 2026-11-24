"""ACP tool-kind translation for declared tool actions."""

from __future__ import annotations

from acp.schema import ToolKind
from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.transport.tool_declaration import (
    EditAction,
    ExecuteAction,
    OtherAction,
    ReadAction,
    SearchAction,
    ToolAction,
)


class AcpToolAction(MroDispatch):
    """Translate the tool's capability only when building an ACP message."""

    @handles(SearchAction)
    @staticmethod
    def search(action: type[SearchAction]) -> ToolKind:
        return "search"

    @handles(ReadAction)
    @staticmethod
    def read(action: type[ReadAction]) -> ToolKind:
        return "read"

    @handles(ExecuteAction)
    @staticmethod
    def execute(action: type[ExecuteAction]) -> ToolKind:
        return "execute"

    @handles(EditAction)
    @staticmethod
    def edit(action: type[EditAction]) -> ToolKind:
        return "edit"

    @handles(OtherAction)
    @staticmethod
    def other(action: type[OtherAction]) -> ToolKind:
        return "other"

    @classmethod
    def kind(cls, action: type[ToolAction]) -> ToolKind:
        kind: ToolKind = cls.handler_for(action)(action)
        return kind
