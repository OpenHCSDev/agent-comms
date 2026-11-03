"""A UI's Core on another machine, reached through the connector's tools/call."""

from __future__ import annotations

import json
import urllib.request

from .connector import ToolsCallMessage, ToolsCallReply
from .connector_profile import UiConnectionProfile
from .core_access import CoreAccess
from .errors import RelationViolationError
from .nominal.field_codec import FieldCodec
from .tool_request import ToolRequest
from .tool_result import ToolResult


class RemoteCoreAccess(UiConnectionProfile, CoreAccess):
    """The owner's UI profile is the remote access: its endpoint and credential are all a
    call needs, and the server still decides scope, expiry and revocation."""

    timeout_seconds = 30

    def call(self, request: ToolRequest) -> ToolResult:
        envelope = ToolsCallMessage(jsonrpc="2.0", id=1, params=request.called())
        posted = urllib.request.Request(
            str(self.endpoint.url),
            data=FieldCodec.compact(FieldCodec.encode(envelope)).encode(),
            headers={"Authorization": f"Bearer {self.credential.token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(posted, timeout=self.timeout_seconds) as response:
            reply = FieldCodec.decode(ToolsCallReply, json.load(response))
        if reply.result.is_error:
            raise RelationViolationError(
                " ".join(content.text for content in reply.result.content))
        return FieldCodec.decode(request.result_type(), reply.result.structured_content)
