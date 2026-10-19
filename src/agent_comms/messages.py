"""Messages: declaration and persistence owners."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Type

from .channel_targets import CommandTarget
from .change_report import FileLine, ChangeReport
from .declared_family import DeclaredFamily
from .envelope_claim_transitions import ClaimTransition
from .errors import RelationViolationError
from .field_codec import FieldCodec, InlineRecord, MalformedRecord, NullMember, projected
from .mentions import ThreadMention
from .message_reference import MessageReference
from .response_policy import InformationalPolicy, ResponseEligibility
from .thread_identity import AgentThreadRole, ThreadRole
from .task_sources import Decision, TaskAttachment, NoTaskAttachment
from .wake_policy import BoundedTriageWake, PassiveWake

from .page_traversal import LiveCursor, PageCursor

if TYPE_CHECKING:
    from .threads import Thread


class SendableMessageKind:
    """Capability for kinds accepted by generic message commands."""


class MessageKind(DeclaredFamily, affix="Kind"):
    """A declared message kind; each member owns the validation it requires.

    Members are the kind themselves, never instances: the wire carries the
    plain declared name (``"type": "info"``), exactly as the previous enum
    stored it. Publication-time checks run once, from the single commit path.
    """

    conversation_message: ClassVar[bool] = True

    @classmethod
    def sendable_names(cls) -> tuple[str, ...]:
        return tuple(member.declared_name for member in cls.members_with(SendableMessageKind))

    @classmethod
    def require_sendable(cls) -> None:
        if not issubclass(cls, SendableMessageKind):
            raise ValueError(f"Message kind {cls.declared_name!r} is not generically sendable")

    @classmethod
    def require_publishable(cls, message: Message, worktree: Path) -> None:
        """Checks a send must pass before publication; base kinds declare none."""


class ClaimNoticeKind(MessageKind, declared_name="claim-notice"):
    """A claim-owner record, not a delivery to a reader."""

    conversation_message = False

    @classmethod
    def claimed(cls, owner: Thread) -> Message:
        return Message(
            owner.name, owner.name, "Claimed", cls,
        )


class InfoKind(SendableMessageKind, MessageKind):
    pass


class QuestionKind(SendableMessageKind, MessageKind):
    pass


class AckKind(SendableMessageKind, MessageKind):
    pass


class HandoffKind(SendableMessageKind, MessageKind):
    """A handoff declares its evidence; completion claims carry references."""

    CLAIMS = ("fixed", "single owner", "no other callers")
    # Evidence command: a command literal inside backticks, not a bare word.
    COMMANDS = re.compile(r"`[^`]*\b(grep|rg|git|sed|awk|find|python|ast)\b[^`]*`")

    @classmethod
    def require_publishable(cls, message: Message, worktree: Path) -> None:
        claims = [
            claim for claim in cls.CLAIMS
            if re.search(rf"\b{re.escape(claim)}\b", message.body, re.IGNORECASE)
        ]
        if not claims or FileLine.scan(message.body) or cls.COMMANDS.search(message.body):
            return
        raise ValueError(
            "Handoff message claims " + ", ".join(claims)
            + " without a file:line reference or a command in backticks as evidence."
        )


class AlertKind(SendableMessageKind, MessageKind):
    pass


class ChangeReportKind(SendableMessageKind, MessageKind, declared_name="change-report"):
    """A review request declares each touched fact with its single owner."""

    @classmethod
    def require_publishable(cls, message: Message, worktree: Path) -> None:
        if message.report is None:
            raise ValueError(
                "A change report requires its report: the branch, each touched fact, "
                "its one owner and every consumer as file:line, the search command "
                "showing the declaration is single, and the new-case edit counts."
            )
        message.report.require_references_exist(worktree)


class MembershipChange(StrEnum):
    JOINED = "joined"
    LEFT = "left"

    def conversation_role(self):
        return MembershipConversationRole(self)


@dataclass(frozen=True, slots=True)
class NoMembershipChange(NullMember):
    def conversation_role(self):
        return ConversationRole()


@dataclass(frozen=True, slots=True)
class MessageRoleWire:
    """The unchanged external fields, decoded once into a message role."""

    change: MembershipChange | NoMembershipChange = field(default_factory=NoMembershipChange, metadata={"wire_name": "membership", "wire_omit_default": True})
    notice: bool = field(default=False, metadata={"wire_omit_default": True})

    def role(self):
        role = self.change.conversation_role()
        return role.as_notice() if self.notice else role


class MessageRole(InlineRecord):
    @classmethod
    def wire_record(cls):
        return MessageRoleWire

    @classmethod
    def from_wire(cls, data):
        return FieldCodec.decode(MessageRoleWire, data).role()

    def to_wire(self):
        return FieldCodec.encode_record(self.wire_fields())

    def notification_references(self, message):
        return ()

    def response_wake_mode(self):
        return BoundedTriageWake()

    def as_notice(self):
        return NoticeRole()

    def require_recipients(self, target, audience):
        if len(audience.recipients) != len(target.direct_names):
            raise ValueError("a direct message must have exactly one frozen eligible recipient.")
        if tuple(member.canonical_thread for member in audience.recipients) != target.direct_names:
            raise ValueError("direct frozen recipient must match the exact stored target.")


class InformationalMessage:
    def response_policy(self, message):
        return InformationalPolicy.instance()

    def counts_as_conversation(self):
        return False

    def wake_mode(self, target, recipient, mentions, control):
        return PassiveWake()


@dataclass(frozen=True, slots=True)
class ConversationRole(MessageRole):
    def wire_fields(self):
        return MessageRoleWire()

    def export_attributes(self):
        return ()

    def counts_as_conversation(self):
        return True

    def as_historical(self):
        return HistoricalConversationRole()

    def response_policy(self, message):
        return (CommandTarget.named(message.target).response_policy(message)
                if message.type.conversation_message else InformationalPolicy.instance())

    def notification_references(self, message):
        return (message.reference,) if message.type.conversation_message else ()

    def wake_mode(self, target, recipient, mentions, control):
        if control.passive:
            return PassiveWake()
        return target.wake_policy(recipient, mentions)


@dataclass(frozen=True, slots=True)
class NoticeRole(InformationalMessage, MessageRole):
    def wire_fields(self):
        return MessageRoleWire(notice=True)

    def export_attributes(self):
        return ("notice=true",)

    def as_historical(self):
        return HistoricalNoticeRole()

    def response_wake_mode(self):
        return PassiveWake()

    def require_recipients(self, target, audience):
        if audience.recipients:
            super(NoticeRole, self).require_recipients(target, audience)


@dataclass(frozen=True)
class Membership(InformationalMessage):
    change: MembershipChange

    def wire_fields(self):
        return replace(super().wire_fields(), change=self.change)

    def export_attributes(self):
        return (*super().export_attributes(), f"membership={self.change.value}")

    def as_notice(self):
        return MembershipNoticeRole(self.change)


@dataclass(frozen=True, slots=True)
class MembershipConversationRole(Membership, ConversationRole):
    def as_historical(self):
        return HistoricalMembershipConversationRole(self.change)


@dataclass(frozen=True, slots=True)
class MembershipNoticeRole(Membership, NoticeRole):
    def as_historical(self):
        return HistoricalMembershipNoticeRole(self.change)


class HistoricalRole:
    """Historical rows cannot create current reply or notification work."""

    def notification_references(self, message):
        return ()

    def response_policy(self, message):
        return InformationalPolicy.instance()

    def as_historical(self):
        return self

    def as_notice(self):
        return super().as_notice().as_historical()


class HistoricalConversationRole(HistoricalRole, ConversationRole):
    pass


class HistoricalNoticeRole(HistoricalRole, NoticeRole):
    pass


class HistoricalMembershipConversationRole(HistoricalRole, MembershipConversationRole):
    pass


class HistoricalMembershipNoticeRole(HistoricalRole, MembershipNoticeRole):
    pass


@dataclass(frozen=True, slots=True)
class Message:
    """Declares one inter-thread message.

    The bus assigns ``seq`` (a monotonically increasing log position) at send
    time; the hash-derived ``message_id`` is identity only and is not ordered.
    """

    sender: str = field(metadata={"wire_name": "from", "wire_order": 2})
    target: str = field(metadata={"wire_name": "to", "wire_order": 3})
    body: str = field(metadata={"wire_name": "text", "wire_order": 6})
    type: Type[MessageKind] = field(metadata={"wire_order": 5})
    timestamp: float = field(
        default_factory=time.time, metadata={"wire_name": "ts", "wire_order": 4}
    )
    seq: int = field(default=0, metadata={"wire_order": 0, "publication_exclude": True})
    sender_role: Type[ThreadRole] = field(  # noqa: UP006 - the `type` field shadows the builtin
        default=AgentThreadRole, metadata={"wire_order": 7, "publication_exclude": True}
    )
    role: MessageRole = field(default_factory=ConversationRole, metadata={"wire_order": 8})
    mentions: tuple[ThreadMention, ...] = field(
        default=(),
        metadata={"wire_omit_default": True, "wire_order": 10, "publication_exclude": True},
    )
    claim_transition: ClaimTransition | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_order": 11, "publication_exclude": True},
    )
    task: TaskAttachment = field(
        default_factory=NoTaskAttachment, metadata={"wire_omit_default": True, "wire_order": 12}
    )
    report: ChangeReport | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_order": 13}
    )
    answers: tuple[MessageReference, ...] = field(
        default=(), metadata={"wire_omit_default": True, "wire_order": 14}
    )

    @property
    def text_size(self) -> int:
        return len(self.body)

    @property
    def publication_snapshot(self) -> Message:
        """Frozen publication content before independently owned delivery enrichment.

        Sequence, role, resolved mentions and claim evidence are assigned by the
        bus. Their declarations exclude them from publication content identity;
        all other declared fields participate in the Message value comparison.
        This is an in-memory snapshot, never a different wire representation.
        """
        return replace(
            self,
            **{
                declared.name: declared.default
                for declared in fields(self)
                if declared.metadata.get("publication_exclude")
            },
        )

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    def require_decision(self) -> Decision:
        return self.task.require_decision()

    @property
    def retains_authored_task(self) -> bool:
        return self.task.retains_authored_task

    def require_task_publication(self, sender, snapshot, original_source) -> None:
        self.task.require_target(self.target, snapshot)
        self.task.require_publication(sender, snapshot, original_source)

    def checkpoint_rows(self, previous_sequence: int):
        """The envelope declares both publication and optional claim facts."""
        from .private_bus_checkpoint import ClaimCommits, PublicMessages

        yield PublicMessages(self.seq, previous_sequence, self.sender, self.message_id)
        if self.claim_transition is not None:
            yield ClaimCommits(self.claim_transition)

    def require_claim_transition(self) -> ClaimTransition:
        if self.claim_transition is None:
            raise RelationViolationError("Original wire message has no claim transition")
        return self.claim_transition

    def retained_task_facts(self):
        from .retained_task_facts import (
            ClaimTaskFact,
        )

        yield from self.sender_role.user_task_facts(self)
        yield from self.task.retained_task_facts(self)
        if self.claim_transition is not None:
            yield ClaimTaskFact(self)

    def __post_init__(self) -> None:
        if not self.sender:
            raise RelationViolationError("Message sender cannot be empty.")
        if not self.target:
            raise RelationViolationError("Message target cannot be empty.")
        if not self.body:
            raise ValueError("Message body cannot be empty.")
        self.task.require_sender(self.sender)
        if self.claim_transition is not None:
            transition = self.claim_transition
            if self.seq > 0 and (
                transition.owner != self.sender
                or transition.seq != self.seq
                or transition.message_id != self.message_id
            ):
                raise RelationViolationError("Claim transition does not bind this message.")
        if any(
            not (0 <= mention.start < mention.end <= len(self.body))
            or self.body[mention.start] != "@"
            for mention in self.mentions
        ):
            raise ValueError("Mention ranges must identify text in the message body.")
        CommandTarget.named(self.target).validate(self)

    @property
    def view_cursor(self) -> PageCursor:
        return LiveCursor(self.seq)

    @property
    def view_key(self) -> tuple[str, int]:
        return "", self.seq

    @property
    def view_order(self) -> tuple[int, int, int]:
        return 1, 0, self.seq

    @projected(view="wire", name="id")
    def message_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.sender}:{self.target}:{self.timestamp}:{self.body}".encode()
        ).hexdigest()[:12]
        return digest

    history_source = None

    @property
    def display_metadata(self) -> dict:
        """Live messages carry no historical provenance decoration."""
        return {}

    def to_wire(self) -> dict:
        return {"seq": self.seq, "id": self.message_id, **FieldCodec.encode(self)}

    @classmethod
    def from_wire(cls, data: Mapping) -> Message:
        from .bus_publication import PRIVATE_WIRE_FIELD

        # ID is derived; private authority is independently validated by the bus.
        public = {
            key: value for key, value in data.items() if key not in ("id", PRIVATE_WIRE_FIELD)
        }
        return FieldCodec.decode(cls, {"ts": 0.0, **public})

    @classmethod
    def from_committed_wire(cls, record: Mapping) -> Message:
        """One strict public-envelope boundary for full, indexed and cohort readers."""
        from .audience_manifest import MAX_WIRE_SEQ
        from .bus_publication import PRIVATE_WIRE_FIELD, _canonical, public_envelope_digest

        if not isinstance(record, Mapping):
            raise MalformedRecord("Bus row is not an object.")
        public = {key: value for key, value in record.items() if key != PRIVATE_WIRE_FIELD}
        message = cls.from_wire(public)
        if _canonical(public) != _canonical(message.to_wire()):
            raise MalformedRecord("Noncanonical public bus envelope.")
        if not 0 < message.seq <= MAX_WIRE_SEQ:
            raise MalformedRecord("Bus sequence is outside the durable range.")
        public_envelope_digest(public)
        return message

    def require_retained_claim(self, claimed_after_seq: int) -> None:
        if self.claim_transition is None and self.seq > claimed_after_seq:
            raise RelationViolationError(
                "Unattested public initial exceeds the retained history boundary."
            )

    def response_eligibility(
        self, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> ResponseEligibility:
        """Resolve channel responders from canonical audience identities.

        Mention identities were canonicalized when the message was committed.
        The audience remains authoritative for membership, so a valid mention
        outside the channel cannot acquire delivery by being named.
        """
        policy = self.role.response_policy(self)
        return ResponseEligibility(policy, policy.recipients(self, audience, aliases=aliases))

    @property
    def exact_reply_target(self) -> str:
        """The exact response destination owned by this opening message."""
        return CommandTarget.named(self.target).reply_target(self.sender)

    @property
    def reply_target(self) -> str | None:
        """An external sender may receive an automatic live reply."""
        return self.sender_role.reply_target(self)
