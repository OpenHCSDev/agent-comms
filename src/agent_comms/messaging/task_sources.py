"""Authored task statements live on their original wire message, never a ledger."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated, Self

from agent_comms import input_attempt
from agent_comms.cli_bridges.harness_delivery_states import Outcome
from agent_comms.errors import RelationViolationError
from agent_comms.execution.delivers_input import DeliversInput
from agent_comms.execution.input_lookup import InputLookup
from agent_comms.execution.input_publication_read import InputPublicationRead
from agent_comms.execution.turn_context import ContextSegment, Provenance, WireProvenance
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_directions import MessageDirections
from agent_comms.messaging.message_reads import MessageReferencesRead
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.projection_source import ProjectionSource
from agent_comms.messaging.task_callbacks import DeliveredContext, TeamObservation
import agent_comms.messaging.messages as messages
import agent_comms.messaging.retained_task_facts as task_facts
from agent_comms.messaging.task_attachment import NoTaskAttachment, TaskAttachment
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup, TurnId, WireOmitDefault
from agent_comms.planning.owner_requests import OwnerRequestId, RequestDirective
from agent_comms.planning.task_scope import TaskScope, TaskScopeSelection
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry
from agent_comms.registry.thread_identity import (
    RecordedThreadIncarnation,
    ThreadIncarnation,
    TurnIdentity,
    UserThreadRole,
)
from agent_comms.registry.thread_identity_source import ThreadIdentitySource
from agent_comms.storage.typed_table import TypedTable
from agent_comms.ui_model.message_category import MessageCategory
from agent_comms.working_memory.working_memory_requests import AnnotationBudgetExhausted


class TaskChange(DeclaredFamily, affix="TaskChange"):
    @abstractmethod
    def require_publication(self, declaration: ScopedTaskDeclaration, registry: TaskRegistry[TaskOwner],
                            original_source: MessageReferencesRead[messages.Message]) -> None: ...

    @abstractmethod
    def root_source(self, message: messages.Message,
                    originals: dict[MessageReference, messages.Message]) -> messages.Message: ...


@dataclass(frozen=True)
class OriginalTaskChange(TaskChange, declared_name="original"):
    def require_publication(self, declaration: ScopedTaskDeclaration, registry: TaskRegistry[TaskOwner], original_source: MessageReferencesRead[messages.Message]) -> None:
        pass

    def root_source(self, message: messages.Message, originals: dict[MessageReference, messages.Message]) -> messages.Message:
        return message


@dataclass(frozen=True)
class CorrectionTaskChange(TaskChange, declared_name="correction"):
    original: MessageReference

    def require_original(self, original_source: MessageReferencesRead[messages.Message]) -> messages.Message:
        delivery, = original_source.messages_for_references((self.original,))
        original = delivery.message
        if original.reference != self.original:
            raise RelationViolationError("Decision correction requires its original wire reference")
        return original

    def require_publication(self, declaration: ScopedTaskDeclaration, registry: TaskRegistry[TaskOwner], original_source: MessageReferencesRead[messages.Message]) -> None:
        original = self.require_original(original_source)
        declaration.require_correction(original, registry)

    def root_source(self, message: messages.Message, originals: dict[MessageReference, messages.Message]) -> messages.Message:
        try:
            return originals[self.original]
        except KeyError as error:
            raise RelationViolationError("Retained correction lacks its original source") from error


__all__ = ["TaskAttachment", "NoTaskAttachment"]  # re-exported from task_attachment


@dataclass(frozen=True)
class OwnerRequestAttachment(TaskAttachment):
    """The selected requests stay bound to their original committed directive."""

    requests: tuple[OwnerRequestId, ...]

    def direct(self, goals: MessageDirections[messages.Message], message: messages.Message) -> None:
        directive = RequestDirective.from_message(message)
        for request in self.requests:
            goals.todos.record_directive(request, directive)


@dataclass(frozen=True)
class UserTaskSupersession(TaskAttachment, declared_name="user_supersession"):
    """The human's exact correction stays on its original enclosing messages.Message.

    This event retires a declared choice; it does not infer replacement valid
    alternatives, mint a model turn, or rewrite the original author's record.
    """

    change: CorrectionTaskChange

    def require_user_supersession(self) -> UserTaskSupersession:
        return self

    def root_source(self, message: messages.Message, originals: dict[MessageReference, messages.Message]) -> messages.Message:
        return self.change.root_source(message, originals)

    def current_roots(
        self,
        message: messages.Message,
        originals: dict[MessageReference, messages.Message],
        owner: TaskOwner,
        registry: TaskRegistry[TaskOwner],
    ):
        # This exact user row may address a recipient who never received the
        # private original. It only revises an eligible captured owned lineage.
        if self.change.original not in originals:
            return None
        return self.root_source(message, originals)

    def revises_after(self, previous: messages.Message) -> bool:
        return True

    def require_publication(self, sender: str, registry: TaskRegistry[TaskOwner], original_source: MessageReferencesRead[messages.Message]) -> None:
        registry.require(sender).role.require_user(
            RelationViolationError("A task revision requires the original human sender")
        )
        previous = self.change.require_original(original_source).task.require_scoped_task()
        previous.require_user_revision(sender, registry)

    def user_task_facts(self, message: messages.Message):
        return (task_facts.UserTaskCorrectionFact(message),)


@dataclass(frozen=True)
class UserTaskDrop(UserTaskSupersession):
    """A human explicitly retires the original declaration without a new wording."""

    def selected_sources(self, message: messages.Message) -> tuple[messages.Message, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class ScopedTaskDeclaration(TaskAttachment):
    """The source and its history; each author type decides whether work may start."""

    scope: TaskScope
    change: Annotated[TaskChange, WireOmitDefault()] = field(
        default_factory=OriginalTaskChange
    )

    def require_scoped_task(self) -> ScopedTaskDeclaration:
        return self

    @abstractmethod
    def require_previous(self, original: messages.Message) -> ScopedTaskDeclaration: ...

    @abstractmethod
    def retained_task_facts(self, message: messages.Message): ...

    @abstractmethod
    def applies(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> bool: ...

    @property
    @abstractmethod
    def author(self): ...

    def turn_scope_matches(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> bool:
        return False

    def require_same_turn(self, correction: ScopedTaskDeclaration, registry: TaskRegistry[TaskOwner]) -> None:
        raise RelationViolationError("This declaration owns no model turn")

    def turn_scope_matches_original(self, previous: ModelTaskDeclaration, registry: RegistryNames[ThreadIdentitySource]) -> bool:
        return False

    def require_user_revision(self, sender: str, registry: RegistryNames[ThreadIdentitySource]) -> None:
        pass

    def root_source(self, message: messages.Message, originals: dict[MessageReference, messages.Message]) -> messages.Message:
        return self.change.root_source(message, originals)

    def current_roots(self, message: messages.Message, originals: dict[MessageReference, messages.Message], owner: TaskOwner, registry: TaskRegistry[TaskOwner]):
        if not self.applies(owner, registry):
            return None
        return self.root_source(message, originals)

    def revises_after(self, previous: messages.Message) -> bool:
        return previous.task.permits_agent_revision

    def require_sender(self, sender: str):
        if self.author.name != sender:
            raise RelationViolationError("Task declaration does not belong to this message author")

    def require_correction(self, original: messages.Message, registry: TaskRegistry[TaskOwner]) -> None:
        previous = self.require_previous(original)
        if previous.author.resolved(registry) != self.author.resolved(registry):
            raise RelationViolationError("An agent cannot correct another author's task declaration")
        self.scope.require_correction(
            previous.scope, partial(previous.require_same_turn, self, registry),
        )


@dataclass(frozen=True, kw_only=True)
class ModelTaskDeclaration(ScopedTaskDeclaration):
    source_turn: TurnIdentity
    source_turn_id: TurnId
    permits_agent_revision = True

    @classmethod
    def from_claim(cls, owner: TaskOwner, scope: TaskScopeSelection, change: TaskChange, **content) -> Self:
        lease = owner.require_turn_lease()
        return cls(scope=scope.select(owner), source_turn=lease.identity,
                   source_turn_id=lease.turn_id, change=change, **content)

    def __post_init__(self):
        self.author.require_recorded()
        if self.source_turn.generation <= 0:
            raise ValueError("A task declaration requires its original authorized turn")

    @property
    def author(self):
        return self.source_turn.incarnation

    def applies(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> bool:
        return (self.author.current(registry)
                and self.author.resolved(registry) == owner.incarnation
                and self.scope.applies(owner, partial(self.turn_scope_matches, owner, registry)))

    def turn_scope_matches(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> bool:
        return owner.has_authored_turn(self.source_turn.resolved(registry), self.source_turn_id)

    def require_same_turn(self, correction: ScopedTaskDeclaration, registry: TaskRegistry[TaskOwner]) -> None:
        if not correction.turn_scope_matches_original(self, registry):
            raise RelationViolationError("Task correction belongs to a different turn scope")

    def turn_scope_matches_original(self, previous: ModelTaskDeclaration, registry: RegistryNames[ThreadIdentitySource]) -> bool:
        return (previous.source_turn.resolved(registry), previous.source_turn_id) == (
            self.source_turn.resolved(registry), self.source_turn_id)

    def require_emission(self, owner: TaskOwner) -> None:
        self.require_sender(owner.name)
        lease = owner.require_turn_lease()
        if (lease.identity, lease.turn_id) != (self.source_turn, self.source_turn_id):
            raise RelationViolationError("Task declaration's original turn is no longer authorized")
        self.scope.require_current(owner)

    def require_publication(self, sender: str, registry: TaskRegistry[TaskOwner], original_source: MessageReferencesRead[messages.Message]):
        author = registry.require(sender)
        author.require_publication_turn(partial(registry.claim_generation, sender), self.source_turn_id)
        self.require_emission(author)
        self.change.require_publication(self, registry, original_source)


@dataclass(frozen=True, kw_only=True)
class Subtask(ModelTaskDeclaration):
    """Explicit completed/unfinished observation, not a turn or goal terminal.

    The boolean is the original author's observation at the tool boundary. It
    has no independently updated lifecycle: corrections replace the observation
    through the same original wire lineage as decisions and constraints.
    """
    completed: bool
    observes_subtask = True

    def require_subtask(self):
        return self

    def require_previous(self, original: messages.Message) -> Subtask:
        return original.task.require_subtask()

    def turn_scope_matches(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> bool:
        return owner.has_observed_task_turn(self.source_turn.resolved(registry), self.source_turn_id)

    def applies(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> bool:
        return super().applies(owner, registry) and self.turn_scope_matches(owner, registry)

    def retained_task_facts(self, message: messages.Message):
        return (task_facts.SubtaskTaskFact(message),)


@dataclass(frozen=True, kw_only=True)
class HumanConstraintPin(ScopedTaskDeclaration):
    """Original human wording is referenced, never copied into a pin's body."""
    subject: MessageReference
    source_user: ThreadIncarnation
    recipient: ThreadIncarnation

    def __post_init__(self):
        self.source_user.require_recorded()
        self.recipient.require_recorded()

    @property
    def author(self):
        return self.source_user

    def require_human_constraint(self):
        return self

    def annotation_rules(self, message: messages.Message, facts: task_facts.RetainedTaskFacts) -> tuple[str, ...]:
        return (self.original_wording(facts.original_text_source(message.reference)),)

    def require_previous(self, original: messages.Message) -> HumanConstraintPin:
        return original.task.require_human_constraint()

    def require_correction(self, original: messages.Message, registry: TaskRegistry[TaskOwner]) -> None:
        super().require_correction(original, registry)
        if self.require_previous(original).recipient.resolved(registry) != self.recipient.resolved(registry):
            raise RelationViolationError("USER pin correction must preserve its recipient")

    def require_user_revision(self, sender: str, registry: RegistryNames[ThreadIdentitySource]) -> None:
        if self.author.resolved(registry) != registry.require(sender).incarnation:
            raise RelationViolationError("A USER cannot revise another human's constraint")

    def revises_after(self, previous: messages.Message) -> bool:
        return True

    def lineage_reference(self, root: messages.Message) -> MessageReference:
        return self.subject

    def applies(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> bool:
        return (self.recipient.current(registry)
                and self.recipient.resolved(registry) == owner.incarnation
                and self.scope.applies(owner, partial(self.turn_scope_matches, owner, registry)))

    def original_text_source(self, message: messages.Message, originals: dict[MessageReference, messages.Message]) -> messages.Message:
        try:
            return originals[self.subject]
        except KeyError as error:
            raise RelationViolationError("USER pin lacks its original captured wording") from error

    def require_target(self, target: str, registry: TaskRegistry[TaskOwner]):
        if not self.recipient.matches_recorded_name(target, registry):
            raise RelationViolationError("USER pin must address its original recipient")

    def require_publication(self, sender: str, registry: TaskRegistry[TaskOwner], original_source: MessageReferencesRead[messages.Message]):
        registry.require(sender).role.require_user(
            RelationViolationError("A USER pin requires the original human sender")
        )
        self.require_user_revision(sender, registry)
        self.require_wording_publication(registry, original_source)
        if not self.recipient.current(registry):
            raise RelationViolationError("USER pin recipient was replaced")
        owner = registry.require(self.recipient.resolved(registry).name)
        self.scope.require_human_context(owner)
        self.change.require_publication(self, registry, original_source)

    def require_wording_publication(self, registry: TaskRegistry[TaskOwner], original_source: MessageReferencesRead[messages.Message]) -> None:
        delivery, = original_source.deliveries_for_references((self.subject,))
        delivery.message.sender_role.require_user(
            RelationViolationError("Original wording must come from a human sender")
        )
        if delivery.sender_lookup != self.source_user.lookup:
            raise RelationViolationError("USER pin differs from its original human author")
        if not delivery.includes_lookup(self.recipient.lookup):
            raise RelationViolationError("USER pin recipient did not receive its original message")

    def user_task_facts(self, message: messages.Message):
        return (task_facts.HumanConstraintTaskFact(message),)

    def retained_task_facts(self, message: messages.Message):
        return ()


@dataclass(frozen=True, kw_only=True)
class NativeInputConstraintPin(HumanConstraintPin):
    """The same USER pin relation, addressed to an original direct ACP input."""

    subject: Provenance

    def __post_init__(self):
        super().__post_init__()
        self.subject.require_human_input()

    def original_wording(self, original: input_attempt.StoredInput) -> str:
        return original.source_text

    def original_wording_context_source(self, original: input_attempt.StoredInput) -> Provenance:
        return original.context_provenance()

    def original_input_sources(self, inputs: InputLookup[input_attempt.InputAttempt]) -> tuple[input_attempt.StoredInput, ...]:
        provenance = self.subject.require_human_input()
        return (inputs.lookup(provenance.key).require_original_provenance(provenance),)

    def original_wording_provenance(self, original: input_attempt.StoredInput) -> dict[str, object]:
        origin = original.origin.require_human()
        return dict(wording=FieldCodec.encode(original.context_provenance()),
                    author=origin.author.sender, author_role=FieldCodec.encode(UserThreadRole))

    def require_wording_publication(self, registry: TaskRegistry[TaskOwner], original_source: InputPublicationRead[Provenance, input_attempt.StoredInput]) -> None:
        privacy, row = original_source.input_publication(self.subject)
        origin = row.origin.require_human()
        if origin.root_id != privacy.namespace:
            raise RelationViolationError("USER input pin belongs to another wire root")
        original_author = RecordedThreadIncarnation(origin.author.sender, origin.author.created_at)
        if original_author.resolved(registry) != self.source_user.resolved(registry):
            raise RelationViolationError("USER input pin differs from its original human author")
        if origin.claimed_identity.incarnation.resolved(registry) != self.recipient.resolved(registry):
            raise RelationViolationError("USER input pin recipient did not receive its original input")
        if origin.project != self.scope.project:
            raise RelationViolationError("USER input pin belongs to another project")


@dataclass(frozen=True, kw_only=True)
class Decision(ModelTaskDeclaration, declared_name="choice"):
    chosen: str
    rejected: tuple[str, ...]

    def require_decision(self):
        return self

    def require_previous(self, original: messages.Message) -> Decision:
        return original.task.require_decision()

    def retained_task_facts(self, message: messages.Message):
        return (task_facts.DecisionTaskFact(message),)

    def __post_init__(self):
        super().__post_init__()
        if not self.chosen.strip():
            raise ValueError("A decision requires a nonempty chosen alternative")
        if not self.rejected or any(not value.strip() for value in self.rejected):
            raise ValueError("A decision requires nonempty valid rejected alternatives")
        if len(set(self.rejected)) != len(self.rejected) or self.chosen in self.rejected:
            raise ValueError("Decision alternatives must be unique and distinct")

    @property
    def text(self):
        return "Decision: " + self.chosen + "\nValid rejected alternatives:\n" + "\n".join(
            "- " + value for value in self.rejected)


@dataclass(frozen=True, kw_only=True)
class Constraint(ModelTaskDeclaration):
    """Explicit constraint membership; exact wording belongs to messages.Message.body only."""

    def require_constraint(self):
        return self

    def require_previous(self, original: messages.Message) -> Constraint:
        return original.task.require_constraint()

    def retained_task_facts(self, message: messages.Message):
        return (task_facts.ConstraintTaskFact(message),)


from agent_comms.execution.turn_context import ContextSegment
from agent_comms.working_memory.working_memory_labels import ClassifierVersion, JevClassifier


@dataclass(frozen=True, kw_only=True)
class AnnotationDisclosureGrant(HumanConstraintPin):
    """Borrow human authorship, recipient, scope and correction from the pin."""
    per_hour: int
    segments: tuple[type[ContextSegment], ...]
    classifier: ClassifierVersion = field(default_factory=JevClassifier.version)

    def __post_init__(self):
        super().__post_init__()
        if self.per_hour < 1:
            raise ValueError("Disclosure grant requires a positive hourly request budget")
        if not self.segments:
            raise ValueError("Disclosure grant requires permitted segment kinds")

    def require_annotation_grant(self):
        return self

    def annotation_rules(self, message: messages.Message, facts: task_facts.RetainedTaskFacts) -> tuple[str, ...]:
        # Permission to spend/disclose is not a rule to classify as instructions.
        return ()

    def require_segment(self, segment: ContextSegment):
        if type(segment) not in self.segments:
            raise RelationViolationError("Human grant does not disclose this segment kind")


    def window_start(self, now: int) -> int:
        return now - 3_600_000

    def require_budget(self, submitted: int) -> None:
        if submitted >= self.per_hour:
            raise AnnotationBudgetExhausted("Original disclosure grant's hourly request budget is exhausted")

