"""Guarded message publication and durable human sender identity."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .active_route import guard_original_root_write
from .bus_publication import AgentOrigin, HumanOrigin, InitialOrigin
from .change_report import NoChangeReport, ReportEvidence
from .channel_targets import CommandTarget, IncarnationThreadTarget, NamedTarget
from .coordination_session import NativeBoundary
from .errors import (
    CommittedMessageError,
    MessageAcknowledgmentError,
    RelationViolationError,
    UnregisteredThreadError,
)
from .input_disposition import InputDispositions
from .message_bus import MessageBus
from .message_reference import MessageReference
from .messages import ConversationRole, InfoKind, Message, MessageKind, MessageRole, NoticeRole
from .registration import Registration
from .send_result import CorrectionComplete, CorrectionFailed, CorrectionResult, SendResult
from .task_sources import (
    CurrentTaskScopeSelection,
    HumanConstraintPin,
    NativeInputConstraintPin,
    NoTaskAttachment,
    OriginalTaskChange,
    TaskAttachment,
    TaskChange,
    TaskScopeSelection,
)
from .thread_identity import UserThreadRole
from .threads import Thread

if TYPE_CHECKING:
    from .goal_management import Goals
from .todos import TodoError, TodoStore


class Messaging:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, goals: Goals):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.goals = goals

    def _publish(self, message: Message, origin: InitialOrigin,
                 *, destination: NamedTarget) -> Message:
        """Record the meaning of a committed ordinary send beyond its bus transaction."""
        try:
            committed = self.bus.publisher.publish_ordinary(
                message, origin=origin, destination=destination)
        except CommittedMessageError as error:
            origin.complete(self.goals, error.record)
            raise
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
        origin: AgentOrigin = AgentOrigin(),
    ) -> SendResult:
        """Return the committed envelope and its separate wording-correction outcome."""
        type.require_sendable()
        with guard_original_root_write(self.root):
            if sender not in self.registry:
                raise UnregisteredThreadError(f"Sender {sender!r} is not registered.")
            owner = self.registry.require(sender)
            owner.role.require_executable(RelationViolationError(
                "Human messages require the explicit user-message operation."
            ))
            todos = TodoStore(self.root)
            with todos.snapshot():
                pass  # The store validates its schema before any envelope is committed.
            self.bus.bus.messages_for_references(answers)
            message = Message(
                sender=owner.name,
                target=target,
                body=body,
                type=type,
                role=role,
                task=task,
                report=report,
                answers=tuple(dict.fromkeys((*answers, *self._answered_by(owner, target)))),
            )
            committed = self._publish(message, origin, destination=CommandTarget.named(target))
        correction: CorrectionResult
        try:
            todos.correct(owner.incarnation, committed.message_id, body)
        except TodoError as error:
            correction = CorrectionFailed(f'{error.__class__.__name__}: {error}')
        else:
            correction = CorrectionComplete()
        return SendResult(committed, correction)

    def _answered_by(self, owner: Thread, target: str) -> tuple[MessageReference, ...]:
        """What this owner's message to target answers among its running turn's inputs."""
        for lease in owner.turn_lease.held():
            inputs = InputDispositions(self.root).in_turn(lease.turn_id)
            with NativeBoundary(self.bus) as boundary, self.bus.bus.reading() as source:
                return inputs.answered_by(
                    lease, source, boundary.registry, target, privacy=boundary.privacy,
                )
        return ()

    def _register_user(self, worktree: str) -> Thread:
        name = self.registry.snapshot().claim_name("user")
        thread = Thread(name, frozenset(), worktree, role=UserThreadRole)
        self.registry.register(thread)
        return thread

    def user_identity(self, worktree: str) -> Thread:
        """One durable human sender, never an executor or a tag-derived agent role."""
        for thread in self.registry.user_thread().existing():
            return thread
        # The registry owns namespace allocation. Recheck under its canonical
        # writer lock; an existing sender needs only the ordinary registry read.
        with guard_original_root_write(self.root), self.registry.store.locked():
            return self.registry.user_thread().or_create(lambda: self._register_user(worktree))

    def send_user_message(self, target: NamedTarget, body: str, *, worktree: str,
                          task: TaskAttachment = NoTaskAttachment()) -> Message:
        """Cooperative local UI send, not cryptographic same-UID authentication."""
        # The historical root write fence precedes identity creation and
        # remains held through the actual bus publication on an old root.
        with guard_original_root_write(self.root):
            user = self.user_identity(worktree)
            return self._publish_user(user, target, body, task)

    def pin_user_constraint(self, recipient: str, subject: MessageReference, *, worktree: str,
                            scope: TaskScopeSelection = CurrentTaskScopeSelection(),
                            change: TaskChange = OriginalTaskChange()) -> Message:
        """Pin a checked message as sent by the user, without replaying it or starting a turn."""
        return self._pin_constraint(recipient, subject, HumanConstraintPin,
                                    worktree=worktree, scope=scope, change=change)

    def pin_input_constraint(self, recipient: str, subject, *, worktree: str,
                             scope: TaskScopeSelection = CurrentTaskScopeSelection(),
                             change: TaskChange = OriginalTaskChange()) -> Message:
        """Pin the recorded input and its author, rather than a copy of its text."""
        return self._pin_constraint(recipient, subject, NativeInputConstraintPin,
                                    worktree=worktree, scope=scope, change=change)

    def approve_annotations(self, recipient, subject, *, worktree, per_hour, segments):
        from .working_memory_policy import AnnotationDisclosureGrant

        return self._pin_constraint(recipient, subject, AnnotationDisclosureGrant,
            worktree=worktree, scope=CurrentTaskScopeSelection(), change=OriginalTaskChange(),
            per_hour=per_hour, segments=segments)

    def _pin_constraint(self, recipient, subject, declaration, *, worktree, scope, change,
                        **declaration_options) -> Message:
        with guard_original_root_write(self.root):
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
                      task: TaskAttachment, *, role: MessageRole = ConversationRole()) -> Message:
        return self._publish(
            Message(user.name, target.name, body, InfoKind, task=task, role=role),
            HumanOrigin.of(user), destination=target,
        )

    def acknowledge(
        self, name: str, references: Sequence[MessageReference]
    ) -> int:
        """Mark only the returned message references delivered.

        The ledger owns its update lock and rejects a replaced bus; reading
        the named messages uses the bus snapshot, not another exclusion path.
        Every distinct reference must be newly deliverable; an incomplete basis
        raises MessageAcknowledgmentError before any read fact is committed.
        """
        references = tuple(dict.fromkeys(references))
        try:
            basis = self.bus.delivered(name, references)
        except RelationViolationError as error:
            raise MessageAcknowledgmentError(self.registry.require(name).name, references) from error
        if basis.message_count != len(references):
            raise MessageAcknowledgmentError(basis.viewer, basis.uncovered(references))
        self.bus.reads.mark_displayed(basis.viewer, basis)
        return basis.message_count

    def acknowledge_through(self, name: str, sequence: int) -> None:
        # The bus owns snapshot capture and the ledger validates that source.
        self.bus.mark_delivered_through(self.registry.require(name).name, sequence)
