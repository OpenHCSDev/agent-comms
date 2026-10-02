"""Manual/worker compaction reply ownership, distinct from durable journal state."""

from abc import abstractmethod
from dataclasses import dataclass

from acp.exceptions import RequestError
from acp.schema import AgentMessageChunk, PromptResponse, TextContentBlock

from .acp_extension import CompactionCommittedUpdate, TranscriptChangedUpdate, encode_updates
from .agent_events import ManualCompactionEnd, CompactionSkipped
from .declared_family import DeclaredFamily
from .owner_compaction_provider import OwnerSummaryOutcome


class CompactionResult(DeclaredFamily, affix="CompactionResult"):
    @abstractmethod
    def terminal_event(self) -> ManualCompactionEnd: ...

    @abstractmethod
    def prompt_response(self) -> PromptResponse: ...

    @abstractmethod
    def adaptive_result(self) -> bool:
        """Derive whether this original result committed a compaction."""

    @abstractmethod
    def require_prepared(self) -> None:
        """Require a committed saved context before the original raw prompt write."""

    async def after_terminal(self, runner, session_id: str) -> None:
        """A refused result publishes no committed transcript invalidation."""


@dataclass(frozen=True)
class CommittedCompactionResult(CompactionResult):
    summary: str
    commit_id: str

    def adaptive_result(self) -> bool:
        return True

    def require_prepared(self) -> None:
        pass

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
class RefusedCompactionResult(CompactionResult, OwnerSummaryOutcome):
    error: str

    async def commit_with(self, writer):
        return None

    @property
    def completion_event(self):
        return CompactionSkipped(reason="adaptive", explanation=self.error)

    def compaction_result(self, operation):
        return self

    def adaptive_result(self) -> bool:
        return False

    def require_prepared(self) -> None:
        from .owner_compaction_settings import PiSettingsEvidenceError

        raise PiSettingsEvidenceError(self.error)

    def terminal_event(self):
        return ManualCompactionEnd(aborted=True, summary=self.error)

    def prompt_response(self):
        raise RequestError(-32603, self.error, {"reason": self.error})
