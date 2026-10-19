"""Errors: declaration and persistence owners."""

from __future__ import annotations


class ToolRefusedError(ValueError):
    """An intentional refusal safe to return as a tool answer, not a transport failure."""


class UnregisteredThreadError(ToolRefusedError):
    """A reference does not resolve to a registered thread."""


class UnknownChannelError(ToolRefusedError):
    """A target is absent from the catalog and current thread tags."""


class RelationViolationError(ValueError):
    """A required relation between declarations cannot be proved."""


class StoredHistoryUnreadable(RelationViolationError):  # noqa: N818 - nominal read failure
    """Stored wire bytes cannot be decoded as their declared history type."""


class ClaimEnvelopeUnknownError(RelationViolationError):
    """A claim row may be visible after failed durability; never replay automatically."""


class HumanInitialUnknownError(RelationViolationError):
    """A private USER initial may be committed; inspect its ID, never retry."""

    def __init__(self, wire_root_id: str, wire_seq: int, message_id: str) -> None:
        self.wire_root_id = wire_root_id
        self.wire_seq = wire_seq
        self.message_id = message_id
        super().__init__(
            "Private human send outcome UNKNOWN; inspect the committed bus by "
            f"root/seq/id ({wire_root_id}/{wire_seq}/{message_id}); do not retry."
        )


class HumanClaimBlockedError(RelationViolationError):
    """A prior private reservation cannot be safely retried."""
