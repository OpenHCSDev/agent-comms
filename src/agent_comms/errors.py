"""Errors: declaration and persistence owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from agent_comms.coordination_errors import DrainUnavailable
from agent_comms.message_reference import MessageReference

if TYPE_CHECKING:
    from agent_comms.messages import BusRecord
    from agent_comms.todos import Todo


class RejectedEnforcementError(ValueError):
    """A checked standing-rule action was refused before it could apply."""

    reason: ClassVar[str]

    def explanation(self) -> str:
        return self.reason


class ToolRefusedError(ValueError):
    """An intentional refusal safe to return as a tool answer, not a transport failure."""


class TodoError(ToolRefusedError):
    """A todo request is invalid or its persisted state cannot be trusted."""


class TodoConflict(TodoError):
    """The expected revision, assignee or generation is no longer current."""

    def __init__(self, current: Todo) -> None:
        self.current = current
        owner = current.assignment.owner if current.assignment else "unassigned"
        super().__init__(f"Todo {current.id} changed (revision {current.revision}, owner {owner}).")


class MissingSourceLine(ToolRefusedError):  # noqa: N818 - existing source refusal name
    """A cited file:line is not in its declared source revision."""


class UnregisteredThreadError(ToolRefusedError):
    """A reference does not resolve to a registered thread."""


class UnknownChannelError(ToolRefusedError):
    """A target is absent from the catalog and current thread tags."""


class RouteChangedError(ValueError):
    """The selected Comms route was replaced; reopen against the current one."""

    reader_sentence = "On another Comms route; reopen this view"


class RelationViolationError(ValueError, DrainUnavailable):
    """A required relation between declarations cannot be proved."""


class OverdueHeadRefusalError(RejectedEnforcementError, RelationViolationError):
    reason: ClassVar[str] = "a reviewed head is overdue"


class UnclaimedError(RelationViolationError):
    """The requested bus sequence has no committed claim transition."""

    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"Bus row {seq} has no committed claim transition")


class MessageAcknowledgmentError(RelationViolationError):
    """Core could not confirm every requested message; no partial read is committed."""

    def __init__(self, recipient: str, references: tuple[MessageReference, ...]) -> None:
        self.recipient = recipient
        self.references = references
        named = ", ".join(f"{reference.seq}/{reference.message_id}" for reference in references)
        super().__init__(f"Core cannot acknowledge messages for {recipient!r}: {named}. "
                         "Delivery remains unconfirmed; no reads were changed.")


class UnreceivedAnswerError(ToolRefusedError, RelationViolationError):
    """A message answers only messages its sender was delivered."""


class ThreadNotStoppedError(ToolRefusedError, RelationViolationError):
    """A stopped-thread precondition failed, not an unknown write outcome."""


class StoredHistoryUnreadable(RelationViolationError):  # noqa: N818 - nominal read failure
    """Stored wire bytes cannot be decoded as their declared history type."""


class ClaimEnvelopeUnknownError(RelationViolationError):
    """A claim row may be visible after failed durability; never replay automatically."""


class HumanSendError(RelationViolationError):
    """A human send Core did not complete; delivery_failure decodes each member for the sender."""


class HumanSendRefusedError(ToolRefusedError, HumanSendError):
    """The send transaction rolled back; the human's message was not sent."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"Message was not sent: {cause}")


class CommittedMessageError(RuntimeError):
    """The message was sent; signalling or connection retirement then failed."""

    def __init__(self, record: BusRecord) -> None:
        self.record = record
        super().__init__(
            f"Message {record.seq}/{record.message_id} was sent, but write completion failed."
        )


class HumanClaimBlockedError(HumanSendError):
    """A prior private reservation cannot be safely retried."""


class GoalAttemptError(RuntimeError):
    """A goal attempt cannot proceed as requested."""

    diagnostic_text: ClassVar[str] = "Goal launch grant unavailable; explicit Retry required."

    def diagnostic(self) -> str:
        return self.diagnostic_text


class UnresolvedAttemptError(GoalAttemptError):
    """A blocked or in-flight attempt needs an explicit resolution."""

    diagnostic_text = "Goal attempt unresolved; inspect diagnostics before Retry."


class StaleAttemptError(GoalAttemptError):
    """A late outcome cannot modify the current goal generation."""

