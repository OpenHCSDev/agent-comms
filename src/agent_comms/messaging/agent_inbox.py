"""An agent's inbox: delivered messages no harness attempted and no native input took.

Human reads never position it. A message a bridge attempted is consumed or UNKNOWN by its
attempt's outcome, and an uncertain attempt is never paged again or replayed. The consumption
facts live in the coordinator, attached read-only to the bus snapshot the page is read from.
"""

from __future__ import annotations

import sqlite3
import time
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
from agent_comms.messaging.bus_tables import MessageIdentitySource
from agent_comms.messaging.message_page import DisplayedMessagePage
from agent_comms.messaging.message_reference import (
    AfterMessageStart,
    InboxStart,
    MessageReference,
    StreamBeginning,
)
from agent_comms.messaging.read_basis import ReadCursor
from agent_comms.messaging.watched import Watched
from agent_comms.nominal.field_codec import (
    NullMember,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
)
from agent_comms.registry.private_registry_guard import RegistryPrivacy

if TYPE_CHECKING:
    from agent_comms.messaging.read_ledger import ReadDocument, ReadLedger
    from agent_comms.registry.registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class TakenInputCursor(ReadCursor):
    """No human read positions an agent; an input its harness or native runtime took is not new.

    It knows no store until it is attached to the snapshot it reads."""

    def unread_rule(self) -> str:
        raise TypeError("A taken-input cursor reads only once attached to its root's coordinator")

    def attached(self, db: sqlite3.Connection, root: Path, privacy: RegistryPrivacy) -> AttachedTakenInputs:
        """The coordinator joins the bus reader read-only, once per reader: readers are reused
        per thread (OpenSQLiteReadConnection.acquire), and a root's coordinator never moves."""
        if AttachedTakenInputs.database not in {row[1] for row in db.execute("PRAGMA database_list")}:
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


class InboxWait(SubclassUnion, affix="Wait"):
    """How long an inbox read may wait for its first message. A bridge waits on the bus's
    own commit signal instead of polling, through the same request locally and over the
    connector."""

    @abstractmethod
    def read[Page: DisplayedMessagePage](self, bus: Watched, page: Callable[[], Page]) -> Page: ...


@dataclass(frozen=True, slots=True)
class NoWait(NullMember, InboxWait):
    """Answer with what is there now."""

    def read[Page: DisplayedMessagePage](self, bus: Watched, page: Callable[[], Page]) -> Page:
        return page()


@dataclass(frozen=True, slots=True)
class SecondsWait(SingleFieldValue, InboxWait):
    """Wait up to `seconds` for a message, waking on each bus commit; an HTTP request
    must answer before its client gives up, so the bound stays under a minute."""

    seconds: int
    longest: ClassVar[int] = 50

    def __post_init__(self) -> None:
        if not 0 < self.seconds <= self.longest:
            raise ValueError(f"An inbox wait lasts 1 to {self.longest} seconds")

    def read[Page: DisplayedMessagePage](self, bus: Watched, page: Callable[[], Page]) -> Page:
        deadline = time.monotonic() + self.seconds
        # Only the bus's own commits can bring a message: a goal, attempt or guard write in the
        # same root re-ran this whole read for every waiting socket (P1200).
        # The kernel wakes on any event in a watched directory; only a changed bus source re-reads.
        with bus.register_sources(*bus.commit_sources()) as changes:
            current = page()
            while not current.page.messages and (left := deadline - time.monotonic()) > 0:
                changes.block_until_change(left)
                if changes.changed_paths():
                    current = page()
            return current
