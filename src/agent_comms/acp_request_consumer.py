"""Declared owner commands at the ACP boundary; shared behavior is inherited."""

from acp import RequestError
from acp.schema import PromptResponse

from .acp_extension import (
    ClearQueueRequest,
    CompactRequest,
    PromptRequest,
    SelectedWriteRequest,
    SendNowRequest,
    encode_request,
)
from .mro_dispatch import MroDispatch, handles


class AcpRequestConsumer(MroDispatch):
    def __init__(self, agent, session_id, prompt):
        self.agent, self.session_id, self.prompt = agent, session_id, prompt
        self.response = None

    async def run(self, request):
        await self.dispatch(request)
        if self.response is None:
            raise RuntimeError("Declared ACP request has no response handler")
        return self.response

    async def forward(self, request):
        result = await self.agent.sessions.proxies[self.session_id].request(
            "prompt",
            meta=encode_request(request),
            prompt=[
                block
                if isinstance(block, dict)
                else block.model_dump(by_alias=True, exclude_none=True)
                for block in self.prompt
            ],
        )
        return PromptResponse.model_validate(result)

    @handles(PromptRequest)
    async def prompt_request(self, request):
        if self.session_id in self.agent.sessions.proxies:
            self.response = await self.forward(request)
            return
        self.response = await self.agent._prompt_request(self.session_id, self.prompt, request)

    @handles(CompactRequest)
    async def compact(self, request):
        self.agent._require_compaction_text(self.prompt)
        if self.agent._prompt_text(self.prompt).strip() and not self.agent._prompt_text(
            self.prompt
        ).strip().startswith("/compact"):
            raise RequestError.invalid_params({"reason": "Compaction metadata requires blank text"})
        self.response = await self.agent._compact_request(self.session_id, request.instructions)

    @handles(SelectedWriteRequest)
    async def selected_write(self, request):
        if self.prompt:
            raise RequestError.invalid_params({"reason": "Selected write requires no prompt"})
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
        self.agent.inputs.send_now(self.session_id)
        self.response = PromptResponse(stop_reason="end_turn")
