"""Errors: declaration and persistence owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from agent_comms.coordination.errors import DrainUnavailable, Refused
from agent_comms_hooks import ToolRefusedError

if TYPE_CHECKING:
    from agent_comms.messaging.messages import BusRecord


class RejectedEnforcementError(ValueError):
    """A checked standing-rule action was refused before it could apply."""

    reason: ClassVar[str]

    def explanation(self) -> str:
        return self.reason


class BirthChangedError(RejectedEnforcementError):
    """The thread an action was decided for was replaced under its name before it applied."""

    reason: ClassVar[str] = "the thread it was decided for was replaced under its name"


class NobodyOwedError(RejectedEnforcementError):
    """Core would file an order under a running teammate's open request, and none holds one."""

    reason: ClassVar[str] = "no running teammate holds an open owner request"


class TodoError(ToolRefusedError):
    """A todo request is invalid or its persisted state cannot be trusted."""


class MissingSourceLine(ToolRefusedError):  # noqa: N818 - existing source refusal name
    """A cited file:line is not in its declared source revision."""


class UnregisteredThreadError(ToolRefusedError):
    """A reference does not resolve to a registered thread."""


class OutsideRegistrySlice(ToolRefusedError):  # noqa: N818 - a refusal, named for its case
    """A name the registry slice was not asked for: it may well be registered, so it is never
    an unregistered thread, and a handler for gone threads never sees it."""


class UnknownChannelError(ToolRefusedError):
    """A target is absent from the catalog and current thread tags."""


class SourceOutsideImport(ToolRefusedError):  # noqa: N818 - a refusal, named for its case
    """A requested source is not one the imported session recorded. A bare ValueError here
    was no refusal the observer child knew, so one such read killed every UI read with it."""


class MentionOutsideChannelError(ToolRefusedError):
    """A channel post @-names a live thread the channel does not reach. On 2026-11-10 two
    review send-backs to an untagged cloud session went to 22 other threads and never to it."""


class ExportDestinationExists(ToolRefusedError, FileExistsError):
    """A wire export was pointed at a path that already exists and was not told to replace it:
    the caller's choice to refuse, and still the file-exists condition it is."""


class RouteChangedError(ValueError):
    """The selected Comms route was replaced; reopen against the current one."""

    reader_sentence = "On another Comms route; reopen this view"


class RelationViolationError(ValueError, DrainUnavailable):
    """A required relation between declarations cannot be proved."""


class ThreadNotVisibleError(RelationViolationError, RejectedEnforcementError):
    """A direct target refused publication before a standing-rule action could apply."""

    def explanation(self) -> str:
        return str(self)


class GoalRefused(Refused, ToolRefusedError, RelationViolationError):
    """A goal relation refused the action; preserve that refusal across owner RPC. It is an
    intentional refusal like every ToolRefusedError, which the bare Refused carrier is not:
    Core also builds Refused(INTERNAL_ERROR, ...) for real failures."""

    def __init__(self, message: str) -> None:
        super().__init__(self.INVALID_PARAMS, message)


class UnclaimedError(RelationViolationError):
    """The requested bus sequence has no committed claim transition."""

    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"Bus row {seq} has no committed claim transition")


class UnreceivedAnswerError(ToolRefusedError, RelationViolationError):
    """A message answers only messages its sender was delivered."""


class ThreadNotStoppedError(ToolRefusedError, RelationViolationError):
    """A stopped-thread precondition failed, not an unknown write outcome."""


class StoredHistoryUnreadable(RelationViolationError):  # noqa: N818 - nominal read failure
    """Stored wire bytes cannot be decoded as their declared history type."""


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


class GoalAttemptRefused(GoalRefused, GoalAttemptError):
    """An expected attempt refusal keeps its ledger identity and RPC refusal contract."""


class UnresolvedAttemptError(GoalAttemptRefused):
    """A blocked or in-flight attempt needs an explicit resolution."""

    diagnostic_text = "Goal attempt unresolved; inspect diagnostics before Retry."


class StaleAttemptError(GoalAttemptRefused):
    """A late outcome cannot modify the current goal generation."""

