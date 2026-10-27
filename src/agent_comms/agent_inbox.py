"""An agent's inbox: delivered messages no harness attempted and no native input took.

Human reads never position it. A message a bridge attempted is consumed or UNKNOWN by its
attempt's outcome, and an uncertain attempt is never paged again or replayed. The consumption
facts live in the coordinator, attached read-only to the bus snapshot the page is read from.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .bus_tables import MessageIdentitySource
from .coordination_database import CoordinationStore
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.harness_deliveries import HarnessDelivery
from .field_codec import RecordMember
from .message_reference import StreamBeginning
from .native_runtime_input import NativeRuntimeInput
from .read_basis import ReadCursor

if TYPE_CHECKING:
    from .read_ledger import ReadDocument, ReadLedger
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class TakenInputCursor(ReadCursor):
    """No human read positions an agent; an input its harness or native runtime took is not new."""

    database: ClassVar[str] = "coordination"

    @classmethod
    def unread_rule(cls) -> str:
        """Pending: no harness attempt and no native context entry for this exact message."""
        actor = "(SELECT value FROM recipients WHERE key=request.actor)"
        message = f'''(SELECT message_id FROM "{MessageIdentitySource.declared_name}" AS identity
            WHERE identity.seq=request.seq)'''
        return f"""{ReadCursor.unread_rule()}
            AND NOT EXISTS (SELECT 1 FROM "{cls.database}"."{HarnessDelivery.declared_name}" AS attempt
                WHERE attempt.recipient_lookup={actor} AND attempt.seq=request.seq
                    AND attempt.message_id={message})
            AND NOT EXISTS (SELECT 1 FROM "{cls.database}"."{WakeAssignment.declared_name}" AS claim
                WHERE claim.recipient_lookup={actor} AND claim.wire_seq=request.seq
                    AND claim.message_id={message}
                    AND EXISTS (SELECT 1 {NativeRuntimeInput.context_entered_from(
                        f"({NativeRuntimeInput.source_membership_sql()})", "claim.assignment_id")}))"""

    def attach(self, db: sqlite3.Connection, root: Path) -> None:
        """The coordinator joins the held bus snapshot read-only, once per snapshot."""
        db.execute(f'ATTACH DATABASE ? AS "{self.database}"',
                   (CoordinationStore.path_in(root).resolve().as_uri() + "?mode=ro",))


@dataclass(frozen=True, slots=True)
class TakenInputStart(RecordMember, StreamBeginning):
    """The agent inbox begins at its oldest delivered message nothing took."""

    def read_basis(self, reads: ReadLedger, actor: str, snapshot: RegistrySnapshot
                   ) -> tuple[ReadDocument, ReadCursor]:
        return reads.empty(), TakenInputCursor()
