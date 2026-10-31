"""An agent's inbox: delivered messages no harness attempted and no native input took.

Human reads never position it. A message a bridge attempted is consumed or UNKNOWN by its
attempt's outcome, and an uncertain attempt is never paged again or replayed. The consumption
facts live in the coordinator, attached read-only to the bus snapshot the page is read from.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .bus_tables import MessageIdentitySource
from .coordination_database import CoordinationStore
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.harness_deliveries import HarnessDelivery
from .nominal.field_codec import RecordMember
from .message_reference import AfterMessageStart, InboxStart, MessageReference, StreamBeginning
from .private_registry_guard import RegistryPrivacy
from .read_basis import ReadCursor

if TYPE_CHECKING:
    from .read_ledger import ReadDocument, ReadLedger
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class TakenInputCursor(ReadCursor):
    """No human read positions an agent; an input its harness or native runtime took is not new.

    It knows no store until it is attached to the snapshot it reads."""

    def unread_rule(self) -> str:
        raise TypeError("A taken-input cursor reads only once attached to its root's coordinator")

    def attached(self, db: sqlite3.Connection, root: Path, privacy: RegistryPrivacy) -> AttachedTakenInputs:
        """The coordinator joins the held bus snapshot read-only, once per snapshot."""
        db.execute(f'ATTACH DATABASE ? AS "{AttachedTakenInputs.database}"',
                   (CoordinationStore.path_in(root).resolve().as_uri() + "?mode=ro",))
        return AttachedTakenInputs(self.conversations, privacy=privacy)


@dataclass(frozen=True, slots=True)
class AttachedTakenInputs(ReadCursor):
    """The taken-input rule over one attached coordinator; the root's privacy says whether native
    inputs exist there at all."""

    privacy: RegistryPrivacy = field(kw_only=True)
    database: ClassVar[str] = "coordination"

    def unread_rule(self) -> str:
        """Pending: no harness attempt and no native context entry for this exact message."""
        actor = "(SELECT value FROM recipients WHERE key=request.actor)"
        message = f'''(SELECT message_id FROM "{MessageIdentitySource.declared_name}" AS identity
            WHERE identity.seq=request.seq)'''
        return f"""{super().unread_rule()}
            AND NOT EXISTS (SELECT 1 FROM "{self.database}"."{HarnessDelivery.declared_name}" AS attempt
                WHERE attempt.recipient_lookup={actor} AND attempt.seq=request.seq
                    AND attempt.message_id={message})
            AND NOT EXISTS (SELECT 1 FROM "{self.database}"."{WakeAssignment.declared_name}" AS claim
                WHERE claim.recipient_lookup={actor} AND claim.wire_seq=request.seq
                    AND claim.message_id={message}
                    AND {self.privacy.context_entered_sql("claim.assignment_id")})"""


class TakenInputPosition(InboxStart, declared_name=None):
    """Taken inputs own an agent's position; human reads do not, before or after a message."""

    def advanced(self, reference: MessageReference) -> TakenInputAfter:
        return TakenInputAfter(reference)

    def read_basis(self, reads: ReadLedger, actor: str, snapshot: RegistrySnapshot
                   ) -> tuple[ReadDocument, ReadCursor]:
        return reads.empty(), TakenInputCursor()


@dataclass(frozen=True, slots=True)
class TakenInputStart(RecordMember, StreamBeginning, TakenInputPosition):
    """The agent inbox begins at its oldest delivered message nothing took."""


@dataclass(frozen=True, slots=True)
class TakenInputAfter(RecordMember, AfterMessageStart, TakenInputPosition):
    """A stream that already passed this message continues on taken inputs."""
