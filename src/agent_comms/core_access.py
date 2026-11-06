"""Where a UI's Core requests are answered.

A UI asks Core through the same ToolRequest family the connector serves. In this process
the request is applied directly; on a remote bus it is sent as tools/call
(remote_core.RemoteCoreAccess). Either way the UI gets the request's declared result, so
nothing in the UI knows which side answered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .comms import Comms
from .tool_caller import HumanCaller
from .tool_request import ServedRequest
from .transport.request import CoreAccess, ToolRequest
from .tool_result import ToolResult


@dataclass(frozen=True)
class LocalCoreAccess(CoreAccess):
    """Core in this process. The UI is the owner's, so it asks as the human caller, the same
    authority a remote UI grant composes on the server."""

    comms: Comms

    def call[R: ToolResult](self, request: ToolRequest[R]) -> R:
        return cast(R, ServedRequest.serving(request).apply_without_thread(self.comms, HumanCaller()))
