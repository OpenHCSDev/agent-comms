"""A context language model acts through one bash tool, as it was trained to.

The CLM reference harness (facebookresearch/context-language-models, clm/clm_harness) gives
the model exactly one action, ``bash``: it mirrors the live context to a file the model edits
with ordinary shell commands, and the harness reads the edit back into the next request.
A checkpoint RL-trained under that harness (context-language-model/rl-ckpt-livectx_dapo-iter44)
cannot manage its context without a shell, and an extra tool schema is context it never saw.

So whatever an input would grant, a clm-local model is offered the fleet's declared ``bash``
alone. It is the coding transport's bash, authorized by the thread's owner call by call (its
rule gate, rooms and claims), never Pi's unowned built-in: a selected wake refuses a tool
success that no owner authorized.
"""

from __future__ import annotations

from agent_comms.execution.channel_coding_tools import CodingToolMode


class ContextLanguageModelMode(CodingToolMode):
    def allowed_tools(self) -> tuple[str, ...]:
        return ("bash",)
