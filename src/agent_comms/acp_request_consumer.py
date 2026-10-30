"""Declared owner commands at the ACP boundary; shared behavior is inherited."""

import re

from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)

from .coordination_errors import Refused
from acp.schema import PromptRequest, PromptResponse

from . import acp_extension
from .acp_extension import (
    ClearQueueRequest,
    CompactRequest,
    SelectedWriteRequest,
    SendNowRequest,
    decode_request,
    encode_request,
)
from .field_codec import AcpSessionId, MalformedRecord


class AcpRequestConsumer(MroDispatch):
    def __init__(self, agent, request: PromptRequest, session_id: AcpSessionId):
        self.agent, self.request, self.session_id = agent, request, session_id
        self.response = None

    async def run(self):
        try:
            request = decode_request(self.request.field_meta)
        except MalformedRecord as error:
            raise Refused.invalid_params({"reason": str(error)}) from error
        # Literal /compact is the external ACP text command; ours uses a record.
        text = self.agent._prompt_text(self.request).strip()
        if re.match(r"^/compact(?:\s|$)", text):
            self.agent._require_compaction_text(self.request)
            request = CompactRequest(text[len("/compact") :].strip())
        await self.dispatch(request)
        if self.response is None:
            raise RuntimeError("Declared ACP request has no response handler")
        return self.response

    async def forward(self, request):
        forwarded = self.request.model_copy(
            update={
                "session_id": self.session_id,
                "field_meta": {**(self.request.field_meta or {}), **encode_request(request)},
            }
        )
        result = await self.agent.sessions.proxies[self.session_id].request(
            "prompt", request=forwarded
        )
        return PromptResponse.model_validate(result)

    @handles(acp_extension.PromptRequest)
    async def prompt_request(self, request):
        if self.session_id in self.agent.sessions.proxies:
            self.response = await self.forward(request)
            return
        self.response = await self.agent._prompt_request(self, request)

    @handles(CompactRequest)
    async def compact(self, request):
        self.agent._require_compaction_text(self.request)
        if (
            self.agent._prompt_text(self.request).strip()
            and not self.agent._prompt_text(self.request).strip().startswith("/compact")
        ):
            raise Refused.invalid_params({"reason": "Compaction metadata requires blank text"})
        self.response = await self.agent._compact_request(self.session_id, request.instructions)

    @handles(SelectedWriteRequest)
    async def selected_write(self, request):
        if self.request.prompt:
            raise Refused.invalid_params({"reason": "Selected write requires no prompt"})
        if self.session_id in self.agent.sessions.proxies:
            self.response = await self.forward(request)
            return
        self.response = await self.agent._selected_write_request(self.session_id, request)

    @handles(ClearQueueRequest)
    async def clear(self, request):
        if self.session_id in self.agent.sessions.proxies:
            await self.agent.sessions.proxies[self.session_id].request("clear_queue")
        else:
            await self.agent.inputs.clear_queued_inputs(self.session_id)
        self.response = PromptResponse(stop_reason="end_turn")

    @handles(SendNowRequest)
    async def send_now(self, request):
        if self.session_id in self.agent.sessions.proxies:
            self.response = await self.forward(request)
            return
        await self.agent.inputs.send_now(self.session_id)
        self.response = PromptResponse(stop_reason="end_turn")
