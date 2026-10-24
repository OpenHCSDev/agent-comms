"""Declared owner commands at the ACP boundary; shared behavior is inherited."""

from acp.exceptions import RequestError
from acp.schema import PromptRequest, PromptResponse

from . import acp_extension
from .acp_extension import (
    ClearQueueRequest,
    CompactRequest,
    SelectedWriteRequest,
    SendNowRequest,
    encode_request,
)
from .mro_dispatch import MroDispatch, handles


class AcpRequestConsumer(MroDispatch):

    def __init__(self, agent, request: PromptRequest):
        self.agent, self.request = agent, request
        self.response = None

    async def run(self, request):
        await self.dispatch(request)
        if self.response is None:
            raise RuntimeError("Declared ACP request has no response handler")
        return self.response

    async def forward(self, request):
        forwarded = self.request.model_copy(
            update={"field_meta": {**(self.request.field_meta or {}), **encode_request(request)}}
        )
        result = await self.agent.sessions.proxies[self.request.session_id].request(
            "prompt", request=forwarded
        )
        return PromptResponse.model_validate(result)

    @handles(acp_extension.PromptRequest)
    async def prompt_request(self, request):
        if self.request.session_id in self.agent.sessions.proxies:
            self.response = await self.forward(request)
            return
        self.response = await self.agent._prompt_request(self.request, request)

    @handles(CompactRequest)
    async def compact(self, request):
        self.agent._require_compaction_text(self.request)
        if self.agent._prompt_text(self.request).strip() and not self.agent._prompt_text(
            self.request
        ).strip().startswith("/compact"):
            raise RequestError.invalid_params({"reason": "Compaction metadata requires blank text"})
        self.response = await self.agent._compact_request(
            self.request.session_id, request.instructions
        )

    @handles(SelectedWriteRequest)
    async def selected_write(self, request):
        if self.request.prompt:
            raise RequestError.invalid_params({"reason": "Selected write requires no prompt"})
        if self.request.session_id in self.agent.sessions.proxies:
            self.response = await self.forward(request)
            return
        self.response = await self.agent._selected_write_request(self.request.session_id, request)

    @handles(ClearQueueRequest)
    async def clear(self, request):
        if self.request.session_id in self.agent.sessions.proxies:
            await self.agent.sessions.proxies[self.request.session_id].request("clear_queue")
        else:
            await self.agent.inputs.clear_queued_inputs(self.request.session_id)
        self.response = PromptResponse(stop_reason="end_turn")

    @handles(SendNowRequest)
    async def send_now(self, request):
        if self.request.session_id in self.agent.sessions.proxies:
            self.response = await self.forward(request)
            return
        await self.agent.inputs.send_now(self.request.session_id)
        self.response = PromptResponse(stop_reason="end_turn")
