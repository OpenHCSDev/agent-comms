"""A context language model acts through one bash tool, as it was trained to.

The CLM reference harness (facebookresearch/context-language-models, clm/clm_harness) gives
the model exactly one action, ``bash``: it mirrors the live context to a file the model edits
with ordinary shell commands, and the harness reads the edit back into the next request.
A checkpoint RL-trained under that harness (context-language-model/rl-ckpt-livectx_dapo-iter44)
cannot manage its context without a shell, and an extra tool schema is context it never saw.
So whatever an input would grant, a clm-local model runs with ``bash`` alone; the input's own
mode still opens the owner transport, so nothing it refuses becomes reachable through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_comms.transport.native_tool_transport import NativeToolMode, UnboundToolTransport


@dataclass(frozen=True)
class ContextLanguageModelMode(NativeToolMode):
    within: NativeToolMode

    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        return ("--no-builtin-tools", "--tools", *self.allowed_tools())

    def allowed_tools(self) -> tuple[str, ...]:
        return ("bash",)

    async def open_transport(self, turn, ownership) -> UnboundToolTransport:
        return await self.within.open_transport(turn, ownership)
