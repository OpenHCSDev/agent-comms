"""Guarded message publication and durable human sender identity."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Type

from .active_route import guard_original_root_write
from .registration import Registration

if TYPE_CHECKING:
    pass
from .bus_publication import AgentOrigin, HumanOrigin
from .envelope_claim_transitions import FileClaimPath
from .errors import RelationViolationError, UnregisteredThreadError
from .message_bus import MessageBus
from .messages import InfoKind, Message, MessageKind, MessageRole, ConversationRole, NoticeRole
from .publisher import InitialWrites, OwnerPublisher
from .change_report import ChangeReport
from .store_files import SharedLock, _store_lock
from .thread_identity import UserThreadRole
from .threads import Thread
from .task_sources import TaskAttachment, NoTaskAttachment
from .task_sources import HumanConstraintPin, NativeInputConstraintPin, TaskScopeSelection, CurrentTaskScopeSelection
from .task_sources import TaskChange, OriginalTaskChange
from .message_reference import MessageReference



class Messaging:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"

    def send(
        self,
        sender: str,
        target: str,
        body: str,
        type: Type[MessageKind] = InfoKind,
        *,
        role: MessageRole = ConversationRole(),
        claims: Sequence[FileClaimPath] = (),
        releases: Sequence[str | Path] = (),
        report: ChangeReport | None = None,
    ) -> str:
        """Declare a message and route it through the bus."""
        return self.send_message(
            sender, target, body, type, role=role, claims=claims, releases=releases,
            report=report,
        ).message_id

    def send_message(
        self,
        sender: str,
        target: str,
        body: str,
        type: Type[MessageKind] = InfoKind,
        *,
        role: MessageRole = ConversationRole(),
        claims: Sequence[FileClaimPath] = (),
        releases: Sequence[str | Path] = (),
        task: TaskAttachment = NoTaskAttachment(),
        report: ChangeReport | None = None,
    ) -> Message:
        """Return one committed envelope, including optional guarded claims."""
        type.require_sendable()
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            if sender not in self.registry:
                raise UnregisteredThreadError(f"Sender {sender!r} is not registered.")
            owner = self.registry.require(sender)
            owner.role.require_executable(RelationViolationError(
                "Human messages require the explicit user-message operation."
            ))
            message = Message(
                sender=owner.name,
                target=target,
                body=body,
                type=type,
                role=role,
                task=task,
                report=report,
                answers=self._answered_by(owner, target),
            )
            publisher: OwnerPublisher = self.bus.publisher
            if claims or releases:
                committed = publisher.publish_claim_envelope(
                    message,
                    worktree=Path(owner.worktree),
                    incarnation=str(owner.created_at),
                    claims=claims,
                    releases=releases,
                )
            else:
                committed = publisher.publish_ordinary(message, origin=AgentOrigin())
        return committed

    def _answered_by(self, owner: Thread, target: str) -> tuple[MessageReference, ...]:
        """What this owner's message to target answers among its running turn's inputs."""
        lease = owner.turn_lease
        if lease is None:
            return ()
        from .input_disposition import InputDispositions

        inputs = InputDispositions(self.root).in_turn(lease.turn_id)
        with self.bus.log.certified_read() as source:
            return inputs.answered_by(lease, source, self.registry.snapshot(), target)

    def initialize_private_initial_protocol(self) -> str:
        """Initialize the private protocol on a fresh owner-only root."""
        publisher: InitialWrites = self.bus.publisher
        with _store_lock(self._wire_lock_path):
            return publisher.initialize_private_protocol()

    def _user_identity_under_wire_lock(self, worktree: str) -> Thread:
        """Choose the durable USER identity while the caller holds the wire lock."""
        for thread in self._user_identities():
            return thread
        name = self.registry.snapshot().claim_name("user")
        thread = self.registry.new_thread(name, frozenset(), worktree, role=UserThreadRole)
        self.registry.register(thread)
        return thread

    def _user_identities(self):
        """Derive existing humans from the original registered declarations."""
        return (human for thread in self.registry.all_threads().values()
                for human in thread.role.human(thread))

    def user_identity(self, worktree: str) -> Thread:
        """One durable human sender, never an executor or a tag-derived agent role."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            for thread in self._user_identities():
                return thread
        # Creation is a write, with its original exclusive recheck. Observing
        # an existing sender must not serialize every sidebar/page preparation
        # against independent native input or response publication.
        with _store_lock(self._wire_lock_path):
            return self._user_identity_under_wire_lock(worktree)

    def send_user_message(self, target: str, body: str, *, worktree: str,
                          task: TaskAttachment = NoTaskAttachment()) -> Message:
        """Cooperative local UI send, not cryptographic same-UID authentication."""
        # The historical root write fence precedes identity creation and
        # remains held through the actual bus publication on an old root.
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            user = self._user_identity_under_wire_lock(worktree)
            return self._publish_user_under_wire_lock(user, target, body, task)

    def pin_user_constraint(self, recipient: str, subject: MessageReference, *, worktree: str,
                            scope: TaskScopeSelection = CurrentTaskScopeSelection(),
                            change: TaskChange = OriginalTaskChange()) -> Message:
        """Pin a certified original USER message; never replay it or lease a turn."""
        return self._pin_constraint(recipient, subject, HumanConstraintPin,
                                    worktree=worktree, scope=scope, change=change)

    def pin_input_constraint(self, recipient: str, subject, *, worktree: str,
                             scope: TaskScopeSelection = CurrentTaskScopeSelection(),
                             change: TaskChange = OriginalTaskChange()) -> Message:
        """Pin the original input provenance, not a reconstruction of its text."""
        return self._pin_constraint(recipient, subject, NativeInputConstraintPin,
                                    worktree=worktree, scope=scope, change=change)

    def approve_annotations(self, recipient, subject, *, worktree, per_hour, segments):
        from .working_memory_policy import AnnotationDisclosureGrant

        return self._pin_constraint(recipient, subject, AnnotationDisclosureGrant,
            worktree=worktree, scope=CurrentTaskScopeSelection(), change=OriginalTaskChange(),
            per_hour=per_hour, segments=segments)

    def _pin_constraint(self, recipient, subject, declaration, *, worktree, scope, change,
                        **declaration_options):
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            user = self._user_identity_under_wire_lock(worktree)
            owner = self.registry.require(recipient)
            task = declaration(scope=scope.select_human(owner), subject=subject,
                               source_user=user.incarnation, recipient=owner.incarnation,
                               change=change, **declaration_options)
            return self._publish_user_under_wire_lock(
                user, owner.name, "Pinned constraint from its original source",
                task, role=NoticeRole())

    def _publish_user_under_wire_lock(self, user, target, body, task, *, role=ConversationRole()):
        return self.bus.publisher.publish_ordinary(
            Message(user.name, target, body, InfoKind, task=task, role=role),
            origin=HumanOrigin(user.name, user.created_at, user.worktree),
        )

    def acknowledge(
        self, name: str, references: Sequence[MessageReference]
    ) -> int:
        """Mark only the returned message references delivered.

        Reading the named messages takes no wire lock, like every inbox read.
        The shared lock covers only the read-ledger update: it keeps exclusive
        wire writers such as a maintenance cutover out of that one write, while
        page readers proceed. The ledger merges sequences under its own lock and
        rejects a replaced bus, so another reader or acknowledgment loses nothing.
        """
        basis = self.bus.delivered(name, references)
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            self.bus.reads.mark_displayed(basis.viewer, basis)
        return basis.message_count

    def acknowledge_through(self, name: str, sequence: int) -> None:
        with _store_lock(self._wire_lock_path):
            self.bus.mark_delivered_through(self.registry.require(name).name, sequence)
