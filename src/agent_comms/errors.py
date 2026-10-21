"""Errors: declaration and persistence owners."""

from __future__ import annotations

from .message_reference import MessageReference


class ToolRefusedError(ValueError):
    """An intentional refusal safe to return as a tool answer, not a transport failure."""


class UnregisteredThreadError(ToolRefusedError):
    """A reference does not resolve to a registered thread."""


class UnknownChannelError(ToolRefusedError):
    """A target is absent from the catalog and current thread tags."""


class RelationViolationError(ValueError):
    """A required relation between declarations cannot be proved."""


class MessageAcknowledgmentError(RelationViolationError):
    """Core could not confirm every requested message; no partial read is committed."""

    def __init__(self, recipient: str, references: tuple[MessageReference, ...]) -> None:
        self.recipient = recipient
        self.references = references
        named = ", ".join(f"{reference.seq}/{reference.message_id}" for reference in references)
        super().__init__(f"Core cannot acknowledge messages for {recipient!r}: {named}. "
                         "Delivery remains unconfirmed; no reads were changed.")


class ThreadNotStoppedError(ToolRefusedError, RelationViolationError):
    """A stopped-thread precondition failed, not an unknown write outcome."""


class StoredHistoryUnreadable(RelationViolationError):  # noqa: N818 - nominal read failure
    """Stored wire bytes cannot be decoded as their declared history type."""


class ClaimEnvelopeUnknownError(RelationViolationError):
    """A claim row may be visible after failed durability; never replay automatically."""


class HumanSendError(RelationViolationError):
    """A human send Core did not complete; delivery_failure decodes each member for the sender."""


class HumanInitialUnknownError(HumanSendError):
    """A private USER initial may be committed; inspect its ID, never retry."""

    def __init__(self, wire_root_id: str, wire_seq: int, message_id: str) -> None:
        self.wire_root_id = wire_root_id
        self.wire_seq = wire_seq
        self.message_id = message_id
        super().__init__(
            "Private human send outcome UNKNOWN; inspect the committed bus by "
            f"root/seq/id ({wire_root_id}/{wire_seq}/{message_id}); do not retry."
        )


class HumanClaimBlockedError(HumanSendError):
    """A prior private reservation cannot be safely retried."""


class CheckpointNeedsRepairError(Exception):
    """A shared source requires exclusive checkpoint repair before it can open."""
