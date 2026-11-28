from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
import time

from agent_comms.cli_bridges.harness_delivery_states import (
    AcknowledgedReceipt,
    DeliveredOutcome,
    FilteredOutcome,
    IgnoredMonitor,
    UnconfirmedOutcome,
)
from agent_comms.coordination.assignment_store import AssignmentStore
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.session import NativeCapture, NativeCoordinationSession
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
from agent_comms.installer.harness_install import HarnessSession
from agent_comms.errors import (
    CommittedMessageError,
    RelationViolationError,
    TodoError,
    UnreceivedAnswerError,
    UnregisteredThreadError,
)
from agent_comms.execution.input_ingress import InputUserIdentity
from agent_comms.execution.transcript_sources import ExternalTranscript
from agent_comms.execution.turn_context import ContextSegment
from agent_comms.execution.turn_item import NoSourceTurn, SourceTurn
from agent_comms.input_disposition import InputDispositions
from agent_comms.messaging.bus_publication import AgentOrigin, AutomatedOrigin
from agent_comms.messaging.channel_targets import (
    CommandTarget,
    IncarnationThreadTarget,
    NamedTarget,
)
from agent_comms.messaging.initial_origin import HumanOrigin, InitialOrigin
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.message_files import FileUpload, MessageFiles
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.message_send_reads import PresenceMessageSender
from agent_comms.messaging.messages import (
    ConversationRole,
    InfoKind,
    Message,
    MessageKind,
    MessageRole,
    NoticeRole,
)
from agent_comms.messaging.send_result import (
    CorrectionComplete,
    CorrectionFailed,
    CorrectionResult,
    SendResult,
)
from agent_comms.messaging.task_sources import (
    AnnotationDisclosureGrant,
    HumanConstraintPin,
    NativeInputConstraintPin,
    NoTaskAttachment,
    OriginalTaskChange,
    TaskAttachment,
    TaskChange,
)
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.planning.goal_management import Goals
from agent_comms.planning.orders import TodoStore
from agent_comms.planning.task_scope import (
    CurrentTaskScopeSelection,
    TaskScopeSelection,
)
from agent_comms.projects.change_report import NoChangeReport, ReportEvidence
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread


class AnswerAuthority(ABC):
    """Whose receipts a send may answer."""

    @abstractmethod
    def lookups(self, owner: Thread) -> tuple[ThreadLookup, ...]: ...


class SenderReceipts(AnswerAuthority):
    """An ordinary send answers only what its sender received."""

    def lookups(self, owner: Thread) -> tuple[ThreadLookup, ...]:
        return owner.incarnation.require_recorded().answerable(())


@dataclass(frozen=True)
class TeamReceipts(AnswerAuthority):
    """A teammate verb answers what its actor received, and what each teammate it acts
    for received; ``ThreadIncarnation.acts_for`` decides which, as it does for orders."""

    actor: ThreadIncarnation
    teammates: tuple[ThreadIncarnation, ...]

    def lookups(self, owner: Thread) -> tuple[ThreadLookup, ...]:
        return self.actor.answerable(self.teammates)


class Messaging(InputUserIdentity[Thread], PresenceMessageSender):
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, goals: Goals):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.goals = goals

    @property
    def files(self) -> MessageFiles:
        return MessageFiles(self.root)

    def _publish(self, message: Message, origin: InitialOrigin,
                 *, destination: NamedTarget, files: tuple[FileUpload, ...] = ()) -> Message:
        """Record the meaning of a committed ordinary send beyond its bus transaction.

        Its files' bytes are kept before the message is published and named as its files once
        it has a reference, so a message is never published over bytes that failed to write."""
        self.files.keep(files)
        try:
            committed = self.bus.publisher.publish_ordinary(
                message, origin=origin, destination=destination)
        except CommittedMessageError as error:
            self.files.attach(error.record.reference, files)
            origin.complete(self.goals, error.record)
            raise
        self.files.attach(committed.reference, files)
        return origin.complete(self.goals, committed)

    def send(
        self,
        sender: str,
        target: str,
        body: str,
        type: type[MessageKind] = InfoKind,
        *,
        role: MessageRole = ConversationRole(),
        task: TaskAttachment = NoTaskAttachment(),
        report: ReportEvidence = NoChangeReport(),
        answers: Sequence[MessageReference] = (),
        origin: AutomatedOrigin = AgentOrigin(),
        source_turn: SourceTurn = NoSourceTurn(),
        answerer: AnswerAuthority = SenderReceipts(),
        files: tuple[FileUpload, ...] = (),
    ) -> SendResult:
        """Return the committed envelope and its separate wording-correction outcome."""
        type.require_sendable()
        if sender not in self.registry:
            raise UnregisteredThreadError(f"Sender {sender!r} is not registered.")
        owner = self.registry.require(sender)
        owner.role.require_automated_publication(RelationViolationError(
            "Human messages require the explicit user-message operation."
        ))
        todos = TodoStore(self.root)
        with todos.snapshot():
            pass  # The store validates its schema before any envelope is committed.
        self.bus.bus.messages_for_references(answers)
        unreceived = self.bus.bus.unreceived(answerer.lookups(owner), answers)
        if unreceived:
            raise UnreceivedAnswerError(
                f"{owner.name} never received {', '.join(r.message_id for r in unreceived)}"
                " and acts for no teammate who did, so it cannot answer it")
        message = Message(
            sender=owner.name,
            target=target,
            body=body,
            type=type,
            role=role,
            task=task,
            report=report,
            answers=tuple(dict.fromkeys((*answers, *self._answered_by(owner, target, source_turn)))),
        )
        committed = self._publish(
            message, origin, destination=CommandTarget.named(target), files=files)
        if answers:
            owner.settle_answered_remote_deliveries(answers, self, committed)
        correction: CorrectionResult
        try:
            todos.correct(owner.incarnation, committed.message_id, body)
        except TodoError as error:
            correction = CorrectionFailed(f'{error.__class__.__name__}: {error}')
        else:
            correction = CorrectionComplete()
        return SendResult(committed, correction)

    def acknowledge_deliveries(
        self,
        recipient: str,
        references: Sequence[MessageReference],
        *,
        bridge: type[HarnessSession] = HarnessSession,
        timestamp: float | None = None,
    ) -> tuple[MessageReference, ...]:
        """Confirm that the named received messages entered this recipient's context."""
        unique = tuple(dict.fromkeys(references))
        if not unique:
            return ()
        if recipient not in self.registry:
            raise UnregisteredThreadError(f"Recipient {recipient!r} is not registered.")
        owner = self.registry.require(recipient)
        self.bus.bus.messages_for_references(unique)
        unreceived = self.bus.bus.unreceived(SenderReceipts().lookups(owner), unique)
        if unreceived:
            raise UnreceivedAnswerError(
                f"{owner.name} never received {', '.join(r.message_id for r in unreceived)}"
                ", so it cannot acknowledge it")
        self._record_acknowledged_deliveries(
            owner, unique, time.time() if timestamp is None else timestamp, bridge=bridge,
        )
        return unique

    def acknowledge_direct_turn_inputs(
        self,
        owner: Thread,
        origins: tuple[Message, ...],
        *,
        bridge: type[HarnessSession],
    ) -> tuple[MessageReference, ...]:
        """Record durable model-context delivery acknowledgements, then advance read markers."""
        refs = tuple(dict.fromkeys(origin.reference for origin in origins if origin.seq > 0))
        if not refs:
            return ()
        acknowledged = self.acknowledge_deliveries(owner.name, refs, bridge=bridge)
        self.bus.mark_messages_displayed(owner.name, origins)
        return acknowledged

    def ignore_direct_turn_inputs(
        self,
        owner: Thread,
        origins: tuple[Message, ...],
        *,
        bridge: type[HarnessSession],
    ) -> tuple[MessageReference, ...]:
        """Settle non-waking or unanswered direct-inbox origins as ignored (Not answering)."""
        refs = tuple(dict.fromkeys(origin.reference for origin in origins if origin.seq > 0))
        if not refs:
            return ()
        lookup = owner.incarnation.require_recorded().lookup
        evidence = IgnoredMonitor()
        path = str(CoordinationStore.path_in(self.root))
        with NativeCoordinationSession(path) as session, session.writing() as db:
            assignments = AssignmentStore(session)
            for ref in refs:
                existing = HarnessDelivery.for_recipients(db, ((lookup, ref),))
                if existing:
                    for row in existing:
                        replace(
                            row, outcome=FilteredOutcome, monitor=evidence,
                        ).record(assignments, db)
                else:
                    HarnessDelivery(
                        bridge, lookup, ref.message_id, ref.seq,
                        FilteredOutcome, monitor=evidence,
                    ).record(assignments, db)
        return refs

    def _settle_answered_remote_deliveries(
        self,
        owner: Thread,
        answers: Sequence[MessageReference],
        timestamp: float,
    ) -> None:
        """Settle open unconfirmed harness attempts when an external thread explicitly answers them."""
        unique = tuple(dict.fromkeys(answers))
        own_lookups = SenderReceipts().lookups(owner)
        unreceived = frozenset(self.bus.bus.unreceived(own_lookups, unique))
        own_answers = tuple(ref for ref in unique if ref not in unreceived)
        if not own_answers:
            return
        lookup = owner.incarnation.require_recorded().lookup
        keys = tuple((lookup, ref) for ref in own_answers)
        with self.registry.store.coordinator_reader.reader_snapshot() as db:
            pending = tuple(dict.fromkeys(
                row.reference
                for row in HarnessDelivery.for_recipients(db, keys)
                if issubclass(row.outcome, UnconfirmedOutcome)
            ))
        if pending:
            self._record_acknowledged_deliveries(owner, pending, timestamp)

    def _record_acknowledged_deliveries(
        self,
        owner: Thread,
        references: tuple[MessageReference, ...],
        timestamp: float,
        *,
        bridge: type[HarnessSession] = HarnessSession,
    ) -> None:
        lookup = owner.incarnation.require_recorded().lookup
        evidence = AcknowledgedReceipt(timestamp)
        path = str(CoordinationStore.path_in(self.root))
        with NativeCoordinationSession(path) as session, session.writing() as db:
            assignments = AssignmentStore(session)
            for ref in references:
                existing = HarnessDelivery.for_recipients(db, ((lookup, ref),))
                if existing:
                    for row in existing:
                        row.apply(
                            partial(replace, outcome=DeliveredOutcome, monitor=evidence),
                            assignments, db,
                        )
                else:
                    HarnessDelivery(
                        bridge, lookup, ref.message_id, ref.seq,
                        DeliveredOutcome, monitor=evidence,
                    ).record(assignments, db)

    def _answered_by(self, owner: Thread, target: str,
                     source_turn: SourceTurn) -> tuple[MessageReference, ...]:
        """Native lease or explicitly named external source; never a newest-row guess."""
        references: list[MessageReference] = []
        for lease in owner.turn_lease.held():
            inputs = InputDispositions(self.root).in_turn(lease.turn_id)
            with NativeCapture(self.bus) as boundary, self.bus.bus.reading() as source:
                references.extend(inputs.answered_by(
                    lease, source, boundary.registry, target, privacy=boundary.privacy,
                ))
        references.extend(source_turn.external_answers(
            NativeCapture(self.bus), ExternalTranscript(self.root, owner.incarnation),
            CommandTarget.named(target),
        ))
        return tuple(dict.fromkeys(references))

    def user_identity(self, worktree: Checkout) -> Thread:
        """One durable human sender, never an executor or a tag-derived agent role."""
        return self.registry.user_thread().or_create(lambda: self.registry.claim_user(worktree))

    def send_user_message(self, target: NamedTarget, body: str, *, worktree: Checkout,
                          task: TaskAttachment = NoTaskAttachment(),
                          files: tuple[FileUpload, ...] = ()) -> Message:
        """Cooperative local UI send, not cryptographic same-UID authentication."""
        user = self.user_identity(worktree)
        return self._publish_user(user, target, body, task, files=files)

    def send_owner_notice(self, target: NamedTarget, body: str, notice: TaskAttachment, *,
                          role: MessageRole) -> Message:
        """Core speaks in the owner's voice, under his own thread; the notice task, not the sender,
        says nobody typed it, so it directs nothing and adds no owner words to any task."""
        return self._publish_user(self.user_identity(Checkout(self.root)), target, body, notice, role=role)

    def pin_user_constraint(self, recipient: str, subject: MessageReference, *, worktree: Checkout,
                            scope: TaskScopeSelection = CurrentTaskScopeSelection(),
                            change: TaskChange = OriginalTaskChange()) -> Message:
        """Pin a checked message as sent by the user, without replaying it or starting a turn."""
        return self._pin_constraint(recipient, subject, HumanConstraintPin,
                                    worktree=worktree, scope=scope, change=change)

    def pin_input_constraint(self, recipient: str, subject, *, worktree: Checkout,
                             scope: TaskScopeSelection = CurrentTaskScopeSelection(),
                             change: TaskChange = OriginalTaskChange()) -> Message:
        """Pin the recorded input and its author, rather than a copy of its text."""
        return self._pin_constraint(recipient, subject, NativeInputConstraintPin,
                                    worktree=worktree, scope=scope, change=change)

    def approve_annotations(self, recipient: str, subject: MessageReference, *, worktree: Checkout,
                            per_hour: int, segments: tuple[type[ContextSegment], ...]) -> Message:
        return self._pin_constraint(recipient, subject, AnnotationDisclosureGrant,
            worktree=worktree, scope=CurrentTaskScopeSelection(), change=OriginalTaskChange(),
            per_hour=per_hour, segments=segments)

    def _pin_constraint(self, recipient, subject, declaration, *, worktree: Checkout, scope, change,
                        **declaration_options) -> Message:
        user = self.user_identity(worktree)
        owner = self.registry.require(recipient)
        task = declaration(scope=scope.select_human(owner), subject=subject,
                           source_user=user.incarnation, recipient=owner.incarnation,
                           change=change, **declaration_options)
        return self._publish_user(
            user, IncarnationThreadTarget(owner.incarnation),
            "Pinned constraint from its original source",
            task, role=NoticeRole())

    def _publish_user(self, user: Thread, target: NamedTarget, body: str,
                      task: TaskAttachment, *, role: MessageRole = ConversationRole(),
                      files: tuple[FileUpload, ...] = ()) -> Message:
        return self._publish(
            Message(user.name, target.name, body, InfoKind, task=task, role=role),
            HumanOrigin.of(user), destination=target, files=files,
        )
