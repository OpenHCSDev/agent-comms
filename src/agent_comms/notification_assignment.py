"""Read-only assignment display; never executes or acknowledges work."""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, NoReturn

from .frozen_recipient import FrozenRecipient
from .bus import Bus
from .coordination_database import CoordinationStore
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.executions import CurrentExecutions
from .coordination_tables.harness_deliveries import HarnessDelivery
from .coordination_tables.publications import PublicationEvidence
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, ThreadLookup, boundary_decoder
from .input_disposition import InputDispositions
from .message_reference import MessageReference
from agent_comms.notification_progress import AssignmentResponse, UnansweredResponse
from .notification_evidence import ConsumptionEvidence
from .notification_progress import (
    AssignmentProgress,
    QueuedProgress,
    SentProgress,
    ShownToModelProgress,
)
from .private_registry_guard import RegistryPrivacy
from .registry_store import RegistryStore
from .typed_table import BooleanStorage, TypedRow
from .wake_policy import RecipientDecision

from dataclasses import field
from functools import cached_property
from .thread_identity import ThreadIncarnation
from agent_comms.notification_evidence import FirstRecipient, OutcomeViewer, RecipientCursor, RecipientLimit
from .bus_publication import Delivered
if TYPE_CHECKING:

    from .agent_activity import RecipientActivity
    from .coordination_database import InvalidStore, MissingStore
    from .message_notification import MessageNotification
    from .private_registry_guard import RegistryPrivacy
    from .notification_progress import DeliveryProgress


@dataclass(frozen=True)
class RecipientSelection:
    """A bounded slice of original frozen positions, never a replacement audience."""

    source: Delivered
    eligible: range
    limit: RecipientLimit

    @property
    def positions(self) -> range:
        return self.eligible[: self.limit.value]

    @property
    def remaining(self) -> int:
        return len(self.eligible) - len(self.positions)

    @property
    def recipients(self) -> tuple[FrozenRecipient, ...]:
        return tuple(self.source.recipients[position] for position in self.positions)

    @property
    def entries(
        self,
    ) -> tuple[tuple[tuple[ThreadLookup, MessageReference], FrozenRecipient], ...]:
        return tuple(
            ((recipient.recipient_lookup, self.source.message.reference), recipient)
            for recipient in self.recipients
        )


@dataclass(frozen=True)
class NotificationSelection:
    """The exact per-original recipient cuts used by every expensive evidence reader."""

    window_limit: ClassVar[int] = 120
    viewer: OutcomeViewer
    pages: tuple[RecipientSelection, ...]

    @classmethod
    def of(
        cls,
        viewer: OutcomeViewer,
        sources: Sequence[Delivered],
        *,
        after: RecipientCursor = FirstRecipient(),
        limit: RecipientLimit = RecipientLimit(),
    ) -> NotificationSelection:
        pages = []
        for source in sources:
            eligible, following = viewer.positions(source), after.positions(source)
            pages.append(
                RecipientSelection(
                    source,
                    range(max(eligible.start, following.start), min(eligible.stop, following.stop)),
                    limit,
                )
            )
        return cls(viewer, tuple(pages))

    def read_without_assignments(self) -> NotificationAssignmentRead:
        return NotificationAssignmentRead(self, ())

    @property
    def sources(self) -> tuple[Delivered, ...]:
        return tuple(page.source for page in self.pages)

    @property
    def keys(self) -> tuple[tuple[ThreadLookup, MessageReference], ...]:
        return tuple(key for page in self.pages for key, _recipient in page.entries)


@dataclass(frozen=True)
class SourceEvidence:
    """One original's claims, harness attempts, stored inputs and independent responses,
    grouped by frozen recipient. Equality over all of them is the window's change test."""

    claims: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...] = ()
    consumption: Mapping[ThreadLookup, tuple[ConsumptionEvidence, ...]] = field(
        default_factory=dict
    )
    responses: Mapping[ThreadLookup, AssignmentResponse] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def answered(
        self, recipient: ThreadLookup, notification: MessageNotification
    ) -> MessageNotification:
        return self.responses.get(recipient, UnansweredResponse()).answered(notification)

    def consumed_progress(self, recipient: ThreadLookup,
                          progress: DeliveryProgress) -> DeliveryProgress:
        """The recipient's progress after each of its own harness attempts and stored inputs
        of this original."""
        for attempt in self.consumption.get(recipient, ()):
            progress = attempt.consumed_progress(progress)
        return progress


@dataclass(frozen=True)
class NotificationAssignmentRead:
    """A window's claims and its harness attempts, both keyed by the exact original, as the
    viewer captured with them may see them: every later projection and change check of this
    read answers for that same viewer.

    A harness attempt is its own consumption evidence: a no-wake or passive recipient has no
    claim for the attempt to move, and its message still entered the session."""

    selection: NotificationSelection
    rows: tuple[tuple[WakeAssignment, AssignmentActivityEvidence], ...]
    consumption: Mapping[tuple[ThreadLookup, MessageReference], tuple[ConsumptionEvidence, ...]] = (
        field(default_factory=lambda: MappingProxyType({}))
    )
    responses: Mapping[tuple[ThreadLookup, MessageReference], AssignmentResponse] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def sources(self) -> tuple[Delivered, ...]:
        return self.selection.sources

    @cached_property
    def evidence(self) -> Mapping[MessageReference, SourceEvidence]:
        """Each original's evidence, grouped once per captured window."""
        claims: dict[MessageReference, list[tuple[WakeAssignment, AssignmentActivityEvidence]]] = {
            source.message.reference: [] for source in self.sources
        }
        consumption: dict[MessageReference, dict[ThreadLookup, tuple[ConsumptionEvidence, ...]]] = {
            source.message.reference: {} for source in self.sources
        }
        responses: dict[MessageReference, dict[ThreadLookup, AssignmentResponse]] = {
            source.message.reference: {} for source in self.sources
        }
        for (lookup, source), response in self.responses.items():
            responses[source][lookup] = response
        for (lookup, source), facts in self.consumption.items():
            consumption[source][lookup] = facts
        for row in self.rows:
            claims.setdefault(row[0].source, []).append(row)
        return {
            source: SourceEvidence(
                tuple(claims[source]),
                MappingProxyType(consumption[source]),
                MappingProxyType(responses[source]),
            )
            for source in claims
        }

    def differs_from(self, fresh: Mapping[MessageReference, SourceEvidence]) -> bool:
        return any(previous != fresh.get(source, SourceEvidence())
                   for source, previous in self.evidence.items())


@dataclass(frozen=True)
class NotificationReadMark:
    """The exact display evidence used for a source/recipient, including no display.

    A mark is identified by its source's message reference, not the source's value:
    bus rows are append-only (typed_table's abort-on-UPDATE/DELETE triggers), so a
    reference names one frozen delivery and its audience. Comparing the delivery
    itself walked its whole audience once per mark, so a window of N marks on an
    N-recipient channel message compared N squared recipients on every revision.
    """

    source: Delivered = field(compare=False)
    recipient: FrozenRecipient
    displayed_to: tuple[ThreadIncarnation, ...]
    reference: MessageReference = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", self.source.message.reference)


@dataclass(frozen=True)
class AssignmentContextEvidence(DeclaredFamily, affix="ContextEvidence"):
    @classmethod
    @boundary_decoder
    def from_columns(cls, committed_at_ms: object) -> AssignmentContextEvidence:
        """SQL NULL is the one foreign fact tested; a present value decodes as its own type."""
        if committed_at_ms is None:
            return NotShownContextEvidence()
        return ShownContextEvidence(FieldCodec.decode(int, committed_at_ms))

    @abstractmethod
    def progress(self, *, pending_at_ms: int, queued: bool) -> AssignmentProgress: ...


class NotShownContextEvidence(AssignmentContextEvidence):
    def progress(self, *, pending_at_ms: int, queued: bool) -> AssignmentProgress:
        progress = QueuedProgress if queued else SentProgress
        return progress(pending_at_ms)


@dataclass(frozen=True)
class ShownContextEvidence(AssignmentContextEvidence):
    """The native context commit recorded on the input row when it was proved."""

    committed_at_ms: int

    def progress(self, *, pending_at_ms: int, queued: bool) -> AssignmentProgress:
        return ShownToModelProgress(self.committed_at_ms)


@dataclass(frozen=True)
class AssignmentActivity:
    triage_inflight: bool
    progress: AssignmentProgress
    response: AssignmentResponse

    def pending_notification(self, notification: MessageNotification, queued_at_ms: int) -> MessageNotification:
        return self.progress.selected_notification(notification, busy=False)

    def completed_notification(self, notification: MessageNotification) -> MessageNotification:
        return self.response.completed_notification(self.progress, notification)

    def answered(self, notification: MessageNotification) -> MessageNotification:
        return self.response.answered(notification)


@dataclass(frozen=True)
class AssignmentActivityEvidence(TypedRow):
    """Independent context and response facts decoded once from one SQL row."""

    triage_inflight: bool
    # Whether the recipient has a current execution: SQL answers it, no id is decoded.
    queued: bool
    context: AssignmentContextEvidence
    response: AssignmentResponse

    @classmethod
    def column_projection(cls) -> dict[str, str]:
        return {
            "triage_inflight": "triage_inflight",
            "queued": "queued",
            "context_committed_at_ms": "context_committed_at_ms",
            "response_seq": "response_seq",
            "response_message_id": "response_message_id",
            "response_received_at_ms": "response_received_at_ms",
        }

    @classmethod
    def _decode_row(cls, row, positions: Mapping[str, int]) -> AssignmentActivityEvidence:
        def column(name: str):
            return row[positions[name]]

        return cls(
            BooleanStorage.from_sql(column("triage_inflight")),
            BooleanStorage.from_sql(column("queued")),
            AssignmentContextEvidence.from_columns(column("context_committed_at_ms")),
            AssignmentResponse.from_columns(
                column("response_seq"),
                column("response_message_id"),
                column("response_received_at_ms"),
            ),
        )

    def resolve(self, *, pending_at_ms: int) -> AssignmentActivity:
        return AssignmentActivity(
            self.triage_inflight,
            self.context.progress(
                pending_at_ms=pending_at_ms,
                queued=self.queued,
            ),
            self.response,
        )

class NotificationSource(DeclaredFamily, affix="NotificationSource"):
    """Original frozen delivery or handling acknowledgement, never an execution authority."""

    @abstractmethod
    def project(self, observation: RecipientActivity,
                sender_lookup: ThreadLookup) -> MessageNotification: ...

    @abstractmethod
    def recipient(self) -> FrozenRecipient:
        """The frozen publication recipient this source answers for."""


@dataclass(frozen=True)
class UnrecordedNotificationSource(NotificationSource):
    published: FrozenRecipient
    decision: RecipientDecision

    def recipient(self) -> FrozenRecipient:
        return self.published

    def project(
        self, observation: RecipientActivity, sender_lookup: ThreadLookup
    ) -> MessageNotification:
        return self.decision.initial_notification(
            self.published, sender_lookup, observation=observation)


@dataclass(frozen=True)
class NotificationAssignment(NotificationSource):
    assignment: WakeAssignment
    activity: AssignmentActivity

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        """Commit sources for coordinator, independent replies and stored inputs."""
        return (
            *CoordinationStore.source_paths(root),
            *Bus.source_paths(root),
            *InputDispositions.source_paths(root),
        )

    @classmethod
    def changed(cls, reads: Iterable[NotificationAssignmentRead], root: Path) -> tuple[NotificationAssignmentRead, ...]:
        """Acquire fresh bounded rows once; retained reads own their equality."""
        # A retained read owns its recipient cut. Re-reading a different viewer's
        # superset would both decode it and report a permanent false change.
        return tuple(
            read for read in reads if read.differs_from(cls.read(root, read.selection).evidence)
        )

    @classmethod
    def resolve(cls, read: NotificationAssignmentRead) -> tuple[NotificationAssignment, ...]:
        return tuple(
            cls(assignment, activity.resolve(pending_at_ms=assignment.updated_at_ms))
            for assignment, activity in read.rows
        )

    @classmethod
    def for_deliveries(
        cls,
        selection: NotificationSelection,
        records: Iterable[NotificationAssignment],
    ) -> Iterator[tuple[Delivered, tuple[NotificationSource, ...]]]:
        """Match notification assignments to messages and the recorded recipients this
        viewer may see; an invisible recipient allocates no placeholder."""
        by_recipient = {}
        for record in records:
            key = (record.assignment.source, record.assignment.recipient_lookup)
            by_recipient.setdefault(key, []).append(record)
        for page in selection.pages:
            original = page.source
            reference = original.message.reference
            sources = []
            for position in page.positions:
                recipient, decision = original.recipients[position], original.decisions[position]
                matching = by_recipient.get((reference, recipient.recipient_lookup), ())
                if len(matching) > 1:
                    raise ValueError("Original notification recipient has multiple handling rows")
                sources.append(matching[0] if matching else UnrecordedNotificationSource(recipient, decision))
            yield original, tuple(sources)

    @classmethod
    def capture_responses(
        cls, root: Path, read: NotificationAssignmentRead
    ) -> NotificationAssignmentRead:
        """Capture bounded independent replies after releasing the coordinator snapshot.

        A reply belongs to its original/frozen author, not to a claim lifecycle or
        model-context proof. The bus returns only the first reply per selected key,
        in sequence order, including recipients with no claim at all.
        """
        keys = frozenset(read.selection.keys)
        if not keys:
            return read
        captured = Bus(Bus.path_in(root)).recipient_replies(read.selection.keys)
        responses: dict[tuple[ThreadLookup, MessageReference], AssignmentResponse] = {}
        for record in captured:
            for reply in record.deliveries():
                for answered in reply.message.answers:
                    key = (reply.sender_lookup, answered)
                    if key in keys:
                        responses[key] = responses.get(key, UnansweredResponse()).recorded(reply)
        return replace(read, responses=MappingProxyType(responses))

    @classmethod
    def capture_inputs(
        cls, root: Path, read: NotificationAssignmentRead
    ) -> NotificationAssignmentRead:
        """Capture the selected recipients' stored inputs after releasing the coordinator snapshot.

        An ordinary or following turn that took the original into the model proves it by its
        own input row, keyed as the delivery recorded it; one keyed read covers the window.
        """
        registry = RegistryStore.at(root).snapshot()
        keys = {
            InputDispositions.bus_key(page.source.message, thread): key
            for page in read.selection.pages
            for key, recipient in page.entries
            for thread in registry.recipient(recipient, page.source).input_owners()
        }
        rows = InputDispositions(root).read(tuple(keys)).rows
        consumption = dict(read.consumption)
        for key, row in rows.items():
            selected = keys[key]
            consumption[selected] = (*consumption.get(selected, ()), row)
        return replace(read, consumption=MappingProxyType(consumption))

    @classmethod
    def capture_detached(
        cls, root: Path, read: NotificationAssignmentRead
    ) -> NotificationAssignmentRead:
        """Every bus-side evidence read that follows the coordinator snapshot."""
        return cls.capture_inputs(root, cls.capture_responses(root, read))

    @classmethod
    def capture(
        cls,
        connection: sqlite3.Connection,
        selection: NotificationSelection,
        privacy: RegistryPrivacy,
    ) -> NotificationAssignmentRead:
        """Probe only selected frozen keys before joining any claim/context/proof.

        The existing (recipient_lookup, wire_seq) unique index selects each row;
        message_id corroborates the exact original rather than a current name.
        """
        if len(selection.pages) > NotificationSelection.window_limit:
            raise ValueError("Notification reads require a bounded visible message window")
        keys = tuple(dict.fromkeys(selection.keys))
        if not keys:
            return NotificationAssignmentRead(selection, ())
        privacy.coordinator_schema().require_schema(connection)
        columns = ",".join(f"w.{name}" for name in WakeAssignment.columns())
        context = privacy.notification_context_sql(connection)
        rows = []
        # Each batch's selected frozen keys select their assignments through the
        # recipient/seq index; message_id corroborates the exact original, and native-input
        # membership is selected once per batch by its assignment index.
        for requested in WakeAssignment.requested_keys(
            connection,
            tuple(
                {
                    "recipient_lookup": lookup,
                    "wire_seq": reference.seq,
                    "message_id": reference.message_id,
                }
                for lookup, reference in keys
            ),
        ):
            rows.extend(
                WakeAssignment.joined(
                    connection.execute(
                        f"WITH {requested.values}, "
                        f"w AS MATERIALIZED (SELECT * {requested.join}), "
                        f"context AS MATERIALIZED ({context}) "
                        f"SELECT {columns},context.triage_inflight,"
                        "pointer.execution_id IS NOT NULL AS queued,context.context_committed_at_ms,"
                        "p.seq AS response_seq, p.message_id AS response_message_id, "
                        "p.received_at_ms AS response_received_at_ms "
                        "FROM w "
                        "JOIN context ON context.assignment_id=w.assignment_id "
                        f"LEFT JOIN {CurrentExecutions.declared_name} pointer "
                        "ON pointer.owner_lookup=w.recipient_lookup "
                        f"LEFT JOIN {PublicationEvidence.declared_name} p "
                        "ON p.execution_id=w.execution_id AND p.exact_target=w.exact_target",
                        requested.bindings,
                    ),
                    AssignmentActivityEvidence,
                )
            )
        consumption: dict[
            tuple[ThreadLookup, MessageReference], tuple[ConsumptionEvidence, ...]
        ] = {}
        for attempt in HarnessDelivery.for_recipients(connection, keys):
            key = (attempt.recipient_lookup, attempt.reference)
            consumption[key] = (*consumption.get(key, ()), attempt.monitor)
        return NotificationAssignmentRead(
            selection,
            tuple(sorted(rows, key=lambda row: (-row[0].wire_seq, row[0].recipient))),
            MappingProxyType(consumption),
        )

    @classmethod
    def read(cls, root: Path, selection: NotificationSelection) -> NotificationAssignmentRead:
        """Capture this viewer's assignment evidence through the single observing
        connection owner."""
        privacy = RegistryStore.at(root).privacy()
        with CoordinationStore.observed(CoordinationStore.path_in(root)).observing(
            cls,
            selection,
            privacy,
        ) as result:
            captured = result
        return cls.capture_detached(root, captured)

    @classmethod
    def missing_store(
        cls,
        store: MissingStore,
        selection: NotificationSelection,
        privacy: RegistryPrivacy,
    ) -> NotificationAssignmentRead:
        return privacy.missing_notification_assignments(selection)

    @classmethod
    def invalid_store(
        cls,
        store: InvalidStore,
        selection: NotificationSelection,
        privacy: RegistryPrivacy,
    ) -> NoReturn:
        raise ValueError(f"Channel notification status unavailable: {store.failure_reason}")

    def recipient(self) -> FrozenRecipient:
        return FrozenRecipient(self.assignment.recipient_lookup, self.assignment.recipient)

    def project(
        self, observation: RecipientActivity, sender_lookup: ThreadLookup
    ) -> MessageNotification:
        return self.assignment.lifecycle.notification(
            self.recipient(),
            sender_lookup,
            observation=observation,
            activity=self.activity,
            updated_at_ms=self.assignment.updated_at_ms,
            triage_inflight=self.activity.triage_inflight,
        )
