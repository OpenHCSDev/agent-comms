"""Guarded message publication and durable human sender identity."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .active_route import guard_original_root_write
from .candidate_maintenance import schedule_candidate_catchup
from .registration import Registration

if TYPE_CHECKING:
    pass
from .envelope_claim_transitions import FileClaimPath
from .errors import RelationViolationError, UnregisteredThreadError
from .message_bus import MessageBus
from .messages import Message, MessageType
from .store_files import _store_lock
from .thread_identity import ThreadRole
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
        type: MessageType = MessageType.INFO,
        *,
        notice: bool = False,
        claims: Sequence[FileClaimPath] = (),
        releases: Sequence[str | Path] = (),
    ) -> str:
        """Declare a message and route it through the bus."""
        return self.send_message(
            sender, target, body, type, notice=notice, claims=claims, releases=releases
        ).message_id

    def send_message(
        self,
        sender: str,
        target: str,
        body: str,
        type: MessageType = MessageType.INFO,
        *,
        notice: bool = False,
        claims: Sequence[FileClaimPath] = (),
        releases: Sequence[str | Path] = (),
        task: TaskAttachment = NoTaskAttachment(),
    ) -> Message:
        """Return one committed envelope, including optional guarded claims."""
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            if sender not in self.registry:
                raise UnregisteredThreadError(f"Sender {sender!r} is not registered.")
            owner = self.registry.require(sender)
            if not owner.role.executable:
                raise RelationViolationError(
                    "Human messages require the explicit user-message operation."
                )
            message = Message(
                sender=owner.name, target=target, body=body, type=type,
                notice=notice, task=task,
            )
            if claims or releases:
                committed = self.bus.publisher.publish_claim_envelope(
                    message,
                    worktree=Path(owner.worktree),
                    incarnation=str(owner.created_at),
                    claims=claims,
                    releases=releases,
                )
            else:
                committed = self.bus.publisher.publish_ordinary(message)
        # Pure memory notification and daemon scheduling occur only AFTER the
        # canonical wire/bus publication locks are released. The scheduler owns
        # its own worker-unavailable state; anything it raises is a defect.
        schedule_candidate_catchup(self.bus, committed.seq)
        return committed

    def initialize_private_initial_protocol(self) -> str:
        """Initialize the private protocol on a fresh owner-only root."""
        with _store_lock(self._wire_lock_path):
            return self.bus.publisher.initialize_private_protocol()

    def send_initial_cohort(
        self,
        sender: str,
        target: str,
        body: str,
        type: MessageType = MessageType.INFO,
        *,
        notice: bool = False,
    ) -> Message:
        """Test-gated full-N send. No automatic SQL acceptance or live wake."""
        with _store_lock(self._wire_lock_path):
            if sender not in self.registry or not self.registry.require(sender).role.executable:
                raise RelationViolationError("Initial sender must be a registered executable.")
            committed = self.bus.publisher.publish_initial_cohort(
                Message(sender=sender, target=target, body=body, type=type, notice=notice)
            )
        schedule_candidate_catchup(self.bus, committed.seq)
        return committed

    def _user_identity_under_wire_lock(self, worktree: str) -> Thread:
        """Choose the durable USER identity while the caller holds the wire lock."""
        for thread in self._user_identities():
            return thread
        name, suffix = "user", 2
        while self.registry.name_reserved(name):
            name, suffix = f"user-{suffix}", suffix + 1
        thread = Thread(name, frozenset(), worktree, role=ThreadRole.USER)
        self.registry.register(thread)
        return thread

    def _user_identities(self):
        """Derive existing humans from the original registered declarations."""
        return (thread for thread in self.registry.all_threads().values()
                if self.bus.reads.human(thread.role))

    def user_identity(self, worktree: str) -> Thread:
        """One durable human sender, never an executor or a tag-derived agent role."""
        with _store_lock(self._wire_lock_path, shared=True):
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
            committed = self._publish_user_under_wire_lock(user, target, body, task)
        return self._notify_user_commit(committed)

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
            committed = self._publish_user_under_wire_lock(
                user, owner.name, "Pinned constraint from its original source",
                task, notice=True)
        return self._notify_user_commit(committed)

    def _publish_user_under_wire_lock(self, user, target, body, task, *, notice=False):
        from .bus_publication import HumanOrigin

        return self.bus.publisher.publish_ordinary(
            Message(user.name, target, body, MessageType.INFO, task=task, notice=notice),
            _human_origin=HumanOrigin(user.name, user.created_at, user.worktree),
        )

    def _notify_user_commit(self, committed):
        # No notification runs on UNKNOWN; the scheduler owns its own
        # worker-unavailable state, so anything it raises is a defect.
        schedule_candidate_catchup(self.bus, committed.seq)
        return committed

    def acknowledge(self, name: str, target: str | None = None) -> int:
        """Mark an inbox or one conversation delivered. Returns count acknowledged."""
        with _store_lock(self._wire_lock_path):
            return self.bus.mark_delivered(name, target)

    def acknowledge_through(self, name: str, sequence: int) -> None:
        with _store_lock(self._wire_lock_path):
            self.bus.mark_delivered_through(self.registry.require(name).name, sequence)
