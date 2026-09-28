"""Guarded message publication and durable human sender identity."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .active_route import guard_original_root_write
from .candidate_maintenance import schedule_private_candidate_after_commit
from .registration import Registration

if TYPE_CHECKING:
    pass
from .channel_targets import BuiltinChannel
from .envelope_claim_transitions import FileClaimPath
from .errors import RelationViolationError, UnregisteredThreadError
from .message_bus import MessageBus
from .messages import Message, MessageType
from .store_files import _store_lock
from .thread_identity import ThreadRole
from .threads import Thread

_LOG = logging.getLogger(__name__)


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
            message = Message(sender=owner.name, target=target, body=body, type=type, notice=notice)
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
        # canonical wire/bus publication locks are released. Projection errors
        # can never turn a committed original into an apparent failed send.
        try:
            schedule_private_candidate_after_commit(self.bus, committed.seq)
        except Exception as error:
            _LOG.warning(
                "Candidate notification omitted after committed send (%s)", error.__class__.__name__
            )
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
        try:
            schedule_private_candidate_after_commit(self.bus, committed.seq)
        except Exception as error:
            _LOG.warning(
                "Candidate notification omitted after committed initial (%s)",
                error.__class__.__name__,
            )
        return committed

    def sent_tool_message(self, name: str, output: str, ok: bool) -> Message | None:
        """Resolve a successful send receipt, including older ID-only tool results."""
        if name != "comms_send" or not ok:
            return None
        try:
            receipt = json.loads(output)
            if not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str):
                return None
            if isinstance(receipt.get("message"), dict):
                message = Message.from_wire(receipt["message"])
                return message if message.message_id == receipt["id"] else None
            return self.bus.log.message_by_id(receipt["id"])
        except (ValueError, KeyError, TypeError):
            return None

    def broadcast(self, sender: str, body: str) -> str:
        """Declare a message addressed to every peer."""
        return self.send(sender, BuiltinChannel.ALL.value, body)

    def _user_identity_under_wire_lock(self, worktree: str) -> Thread:
        """Choose the durable USER identity while the caller holds the wire lock."""
        for thread in self.registry.all_threads().values():
            if self.bus.reads.human(thread.role):
                return thread
        name, suffix = "user", 2
        while self.registry.name_reserved(name):
            name, suffix = f"user-{suffix}", suffix + 1
        thread = Thread(name, frozenset(), worktree, role=ThreadRole.USER)
        self.registry.register(thread)
        return thread

    def user_identity(self, worktree: str) -> Thread:
        """One durable human sender, never an executor or a tag-derived agent role."""
        with _store_lock(self._wire_lock_path):
            return self._user_identity_under_wire_lock(worktree)

    def send_user_message(self, target: str, body: str, *, worktree: str) -> Message:
        """Cooperative local UI send, not cryptographic same-UID authentication."""
        from .bus_publication import HumanOrigin

        # The PR116 legacy retirement fence precedes identity creation and
        # remains held through the actual bus publication on an old root.
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            user = self._user_identity_under_wire_lock(worktree)
            committed = self.bus.publisher.publish_ordinary(
                Message(user.name, target, body, MessageType.INFO),
                _human_origin=HumanOrigin(user.name, user.created_at, user.worktree),
            )
        # Never turn a committed row into an apparent failed send because a
        # best-effort notification failed. No notification runs on UNKNOWN.
        try:
            schedule_private_candidate_after_commit(self.bus, committed.seq)
        except Exception as error:
            _LOG.warning(
                "Candidate notification omitted after committed human send (%s)",
                error.__class__.__name__,
            )
        return committed

    def acknowledge(self, name: str, target: str | None = None) -> int:
        """Mark an inbox or one conversation delivered. Returns count acknowledged."""
        with _store_lock(self._wire_lock_path):
            return self.bus.mark_delivered(name, target)

    def acknowledge_through(self, name: str, sequence: int) -> None:
        with _store_lock(self._wire_lock_path):
            self.bus.mark_delivered_through(self.registry.require(name).name, sequence)
