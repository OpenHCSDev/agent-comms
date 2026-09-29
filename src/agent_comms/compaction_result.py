"""Manual/worker compaction reply ownership, distinct from durable journal state."""

from abc import abstractmethod
from dataclasses import dataclass, field

from acp.exceptions import RequestError
from acp.schema import AgentMessageChunk, PromptResponse, TextContentBlock

from .acp_extension import CompactionCommittedUpdate, TranscriptChangedUpdate, encode_updates
from .agent_events import ManualCompactionEnd
from .declared_family import DeclaredFamily


class CompactionResult(DeclaredFamily, affix="CompactionResult"):
    family_discriminator = "ok"

    @classmethod
    def decode_wire_tag(cls, value: object):
        for member in cls.members_with(cls):
            expected = member.wire_tag()
            if type(value) is type(expected) and value == expected:
                return member
        raise ValueError("Unknown compaction result discriminator")

    @abstractmethod
    def terminal_event(self) -> ManualCompactionEnd: ...

    @abstractmethod
    def prompt_response(self) -> PromptResponse: ...

    async def after_terminal(self, runner, session_id: str) -> None:
        """A refused result publishes no committed transcript invalidation."""


@dataclass(frozen=True)
class CommittedCompactionResult(CompactionResult):
    summary: str
    commit_id: str = field(metadata={"wire_name": "commitId"})

    @classmethod
    def wire_tag(cls):
        return True

    def terminal_event(self):
        return ManualCompactionEnd(aborted=False, summary=self.summary)

    def prompt_response(self):
        return PromptResponse(
            stop_reason="end_turn",
            field_meta=encode_updates(CompactionCommittedUpdate(self.commit_id, self.summary)),
        )

    async def after_terminal(self, runner, session_id):
        await runner.runtime.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(TranscriptChangedUpdate(None)),
            ),
        )


@dataclass(frozen=True)
class RefusedCompactionResult(CompactionResult):
    error: str

    @classmethod
    def wire_tag(cls):
        return False

    def terminal_event(self):
        return ManualCompactionEnd(aborted=True, summary=self.error)

    def prompt_response(self):
        raise RequestError(-32603, self.error, {"reason": self.error})
