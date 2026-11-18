"""Where a UI's Core requests are answered.

A UI asks Core through the same ToolRequest family the connector serves. A read is answered by
the observation child already serving this root, so the asking interpreter is free while it
runs, the way a remote UI's read is answered out of its own interpreter; every other request is
applied here. On a remote bus the request is sent as tools/call
(remote_core.RemoteCoreAccess). Either way the UI gets the request's declared result, so
nothing in the UI knows which side answered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.comms import Comms
from agent_comms.transport.request import CoreAccess, ReadRequest, ToolRequest
from agent_comms.transport.tool_caller import HumanCaller
from agent_comms.transport.tool_request import ServedRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.ui_model.observation import CoreReadOperation, ObservationProcess


@dataclass(frozen=True)
class LocalCoreAccess(CoreAccess, MroDispatch):
    """Core in this process. The UI is the owner's, so it asks as the human caller, the same
    caller a remote UI grant composes on the server.

    Which requests leave this interpreter is the request's own declaration, as it is for a
    remote UI: a read goes to the observation child, everything else is applied here.
    """

    comms: Comms
    reads: ObservationProcess

    def call[R: ToolResult](self, request: ToolRequest[R]) -> R:
        return cast(R, type(self).handler_for(type(request))(self, request))

    @handles(ReadRequest)
    def read_in_child[R: ToolResult](self, request: ReadRequest[R]) -> R:
        """The child already serving this root's reads answers this one, as the same caller."""
        return cast(R, self.reads.read_from_thread(
            CoreReadOperation(str(self.comms.root), request)))

    @handles(ToolRequest)
    def apply_here[R: ToolResult](self, request: ToolRequest[R]) -> R:
        """Writes and everything else keep the path and the progress owner they have."""
        return cast(R, ServedRequest.serving(request).apply_without_thread(self.comms, HumanCaller()))
