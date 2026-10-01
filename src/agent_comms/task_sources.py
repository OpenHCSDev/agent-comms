"""Authored task statements live on their original wire message, never a ledger."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .goals import GoalRevision
from .message_reference import MessageReference
from .thread_identity import ThreadIncarnation, ThreadRole, TurnId, TurnIdentity
from .turn_context import Provenance, WireProvenance

if TYPE_CHECKING:
    from .input_attempt import StoredInput
    from .input_disposition import InputDocument
    from .messages import Message
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .private_bus_checkpoint import CertifiedSourceRead


@dataclass(frozen=True, kw_only=True)
class TaskScope(DeclaredFamily, affix="TaskScope"):
    project: str

    def require_current(self, owner: Thread) -> None:
        if self.project != owner.worktree:
            raise RelationViolationError("Task project differs from its declared owner")
        self.require_context(owner)

    def require_human_context(self, owner: Thread) -> None:
        self.require_current(owner)

    def for_human(self) -> TaskScope:
        return self

    @abstractmethod
    def require_context(self, owner: Thread) -> None: ...

    @abstractmethod
    def applies(self, owner: Thread, declaration: ScopedTaskDeclaration, registry: RegistrySnapshot) -> bool: ...

    def require_correction(self, previous: ScopedTaskDeclaration, correction: ScopedTaskDeclaration,
                           registry: RegistrySnapshot) -> None:
        if previous.scope != self:
            raise RelationViolationError("Decision correction must preserve its original scope")

@dataclass(frozen=True, kw_only=True)
class GoalTaskScope(TaskScope):
    goal: GoalRevision

    def require_context(self, owner: Thread) -> None:
        owner.require_active_goal(self.goal.id)
        owner.require_goal_checkpoint(self.goal)

    def applies(self, owner: Thread, declaration: ScopedTaskDeclaration, registry: RegistrySnapshot) -> bool:
        return owner.task_scope == self


@dataclass(frozen=True, kw_only=True)
class ProjectTaskScope(TaskScope):
    def require_context(self, owner: Thread) -> None:
        pass

    def applies(self, owner: Thread, declaration: ScopedTaskDeclaration, registry: RegistrySnapshot) -> bool:
        return owner.worktree == self.project


@dataclass(frozen=True, kw_only=True)
class TurnTaskScope(TaskScope):
    """The enclosing declaration's original turn determines this scope."""

    def require_context(self, owner: Thread) -> None:
        if owner.task_scope != self:
            raise RelationViolationError("An active goal requires its revision scope")

    def applies(self, owner: Thread, declaration: ScopedTaskDeclaration, registry: RegistrySnapshot) -> bool:
        return owner.worktree == self.project and declaration.turn_scope_matches(owner, registry)

    def require_human_context(self, owner: Thread) -> None:
        raise RelationViolationError("A USER constraint cannot borrow a model turn scope")

    def for_human(self) -> TaskScope:
        return ProjectTaskScope(project=self.project)

    def require_correction(self, previous: ScopedTaskDeclaration, correction: ScopedTaskDeclaration,
                           registry: RegistrySnapshot) -> None:
        super().require_correction(previous, correction, registry)
        previous.require_same_turn(correction, registry)


class TaskScopeSelection(DeclaredFamily, affix="ScopeSelection"):
    @abstractmethod
    def select(self, owner: Thread) -> TaskScope: ...

    @abstractmethod
    def select_human(self, owner: Thread) -> TaskScope: ...


@dataclass(frozen=True)
class CurrentTaskScopeSelection(TaskScopeSelection, declared_name="current"):
    def select(self, owner: Thread) -> TaskScope:
        return owner.task_scope

    def select_human(self, owner: Thread) -> TaskScope:
        return owner.task_scope.for_human()


@dataclass(frozen=True)
class ExplicitTaskScopeSelection(TaskScopeSelection, declared_name="explicit"):
    scope: TaskScope

    def select(self, owner: Thread) -> TaskScope:
        self.scope.require_current(owner)
        return self.scope

    def select_human(self, owner: Thread) -> TaskScope:
        self.scope.require_human_context(owner)
        return self.scope


class TaskChange(DeclaredFamily, affix="TaskChange"):
    @abstractmethod
    def require_publication(self, declaration: ScopedTaskDeclaration, registry: RegistrySnapshot,
                            original_source: CertifiedSourceRead | None) -> None: ...

    @abstractmethod
    def root_source(self, message: Message,
                    originals: dict[MessageReference, Message]) -> Message: ...


@dataclass(frozen=True)
class OriginalTaskChange(TaskChange, declared_name="original"):
    def require_publication(self, declaration, registry, original_source) -> None:
        pass

    def root_source(self, message, originals):
        return message


@dataclass(frozen=True)
class CorrectionTaskChange(TaskChange, declared_name="correction"):
    original: MessageReference

    def require_original(self, original_source: CertifiedSourceRead | None) -> Message:
        if original_source is None:
            raise RelationViolationError("Decision correction requires the original publication read")
        from .private_bus_checkpoint import delivery_references_unlocked

        delivery, = delivery_references_unlocked(original_source, (self.original,))
        original = delivery.message
        if original.reference != self.original:
            raise RelationViolationError("Decision correction requires its original wire reference")
        return original

    def require_publication(self, declaration, registry, original_source) -> None:
        original = self.require_original(original_source)
        declaration.require_correction(original, registry)

    def root_source(self, message, originals):
        try:
            return originals[self.original]
        except KeyError as error:
            raise RelationViolationError("Retained correction lacks its original source") from error


class TaskAttachment(DeclaredFamily, affix="TaskAttachment"):
    """Absent attachments have no publication, fact or author effects."""

    retains_authored_task = False
    permits_agent_revision = False

    def require_scoped_task(self) -> ScopedTaskDeclaration:
        raise RelationViolationError("Original wire message has no declared scoped task")

    def require_constraint(self) -> Constraint:
        raise RelationViolationError("Original wire message has no declared constraint")

    def require_decision(self) -> Decision:
        raise RelationViolationError("Original wire message has no declared decision")

    def require_user_supersession(self) -> UserTaskSupersession:
        raise RelationViolationError("Original wire message has no declared user supersession")

    def require_human_constraint(self) -> HumanConstraintPin:
        raise RelationViolationError("Original wire message has no USER constraint pin")

    def original_text_source(self, message: Message, originals: dict[MessageReference, Message]) -> Message:
        return message

    def original_wording(self, original: Message) -> str:
        return original.body

    def original_wording_context_source(self, original: Message) -> Provenance:
        return WireProvenance(original.reference)

    def original_input_sources(self, inputs: InputDocument) -> tuple[StoredInput, ...]:
        return ()

    def original_wording_provenance(self, original: Message) -> dict[str, object]:
        return dict(wording=FieldCodec.encode(original.reference),
                    author=original.sender, author_role=original.sender_role.value)

    def selected_sources(self, message: Message) -> tuple[Message, ...]:
        return (message,)

    def lineage_reference(self, root: Message) -> MessageReference:
        return root.reference

    def root_source(self, message: Message, originals: dict[MessageReference, Message]) -> Message:
        raise RelationViolationError("Ordinary message is not a decision lineage event")

    def current_roots(self, message: Message, originals: dict[MessageReference, Message],
                      owner: Thread, registry: RegistrySnapshot) -> tuple[Message, ...]:
        return ()

    def revises_after(self, previous: Message) -> bool:
        raise RelationViolationError("Ordinary message cannot revise a declared choice")

    def require_sender(self, sender: str) -> None:
        pass

    def require_target(self, target: str, registry: RegistrySnapshot) -> None:
        pass

    def require_publication(self, sender, registry, original_source) -> None:
        pass

    def retained_task_facts(self, message: Message):
        return ()

    def user_task_facts(self, message: Message):
        from .retained_task_facts import UserSourceTaskFact

        return (UserSourceTaskFact(message),)


@dataclass(frozen=True)
class NoTaskAttachment(TaskAttachment, declared_name="absent"):
    pass


@dataclass(frozen=True)
class UserTaskSupersession(TaskAttachment, declared_name="user_supersession"):
    """The human's exact correction stays on its original enclosing Message.

    This event retires a declared choice; it does not infer replacement valid
    alternatives, mint a model turn, or rewrite the original author's record.
    """

    change: CorrectionTaskChange

    def require_user_supersession(self) -> UserTaskSupersession:
        return self

    def root_source(self, message: Message, originals: dict[MessageReference, Message]) -> Message:
        return self.change.root_source(message, originals)

    def current_roots(self, message, originals, owner, registry):
        # This exact user row may address a recipient who never received the
        # private original. It only revises an eligible captured owned lineage.
        if self.change.original not in originals:
            return ()
        return (self.root_source(message, originals),)

    def revises_after(self, previous: Message) -> bool:
        return True

    def require_publication(self, sender, registry, original_source) -> None:
        registry.require(sender).role.require_user()
        previous = self.change.require_original(original_source).task.require_scoped_task()
        previous.require_user_revision(sender, registry)

    def user_task_facts(self, message: Message):
        from .retained_task_facts import UserTaskCorrectionFact

        return (UserTaskCorrectionFact(message),)


@dataclass(frozen=True)
class UserTaskDrop(UserTaskSupersession):
    """A human explicitly retires the original declaration without a new wording."""

    def selected_sources(self, message):
        return ()


@dataclass(frozen=True, kw_only=True)
class ScopedTaskDeclaration(TaskAttachment):
    """The original source scope and lineage; admission belongs to each author kind."""
    scope: TaskScope
    change: TaskChange = field(default_factory=OriginalTaskChange,
                              metadata={"wire_omit_default": True})
    retains_authored_task = True

    def require_scoped_task(self) -> ScopedTaskDeclaration:
        return self

    @abstractmethod
    def require_previous(self, original: Message) -> ScopedTaskDeclaration: ...

    @abstractmethod
    def retained_task_facts(self, message: Message): ...

    @abstractmethod
    def applies(self, owner, registry): ...

    @property
    @abstractmethod
    def author(self): ...

    def turn_scope_matches(self, owner, registry):
        return False

    def require_same_turn(self, correction, registry):
        raise RelationViolationError("This declaration owns no model turn")

    def turn_scope_matches_original(self, previous, registry):
        return False

    def require_user_revision(self, sender, registry):
        pass

    def root_source(self, message, originals):
        return self.change.root_source(message, originals)

    def current_roots(self, message, originals, owner, registry):
        if not self.applies(owner, registry):
            return ()
        return (self.root_source(message, originals),)

    def revises_after(self, previous):
        return previous.task.permits_agent_revision

    def require_sender(self, sender):
        if self.author.name != sender:
            raise RelationViolationError("Task declaration does not belong to this message author")

    def require_correction(self, original, registry):
        previous = self.require_previous(original)
        if previous.author.resolved(registry) != self.author.resolved(registry):
            raise RelationViolationError("An agent cannot correct another author's task declaration")
        self.scope.require_correction(previous, self, registry)


@dataclass(frozen=True, kw_only=True)
class ModelTaskDeclaration(ScopedTaskDeclaration):
    source_turn: TurnIdentity
    source_turn_id: TurnId
    permits_agent_revision = True

    @classmethod
    def from_admission(cls, owner, scope, change, **content):
        lease = owner.require_turn_lease()
        return cls(scope=scope.select(owner), source_turn=lease.identity,
                   source_turn_id=TurnId(lease.turn_id), change=change, **content)

    def __post_init__(self):
        self.author.require_recorded()
        if self.source_turn.generation <= 0:
            raise ValueError("A task declaration requires its original admitted turn")

    @property
    def author(self):
        return self.source_turn.incarnation

    def applies(self, owner, registry):
        return (self.author.current(registry)
                and self.author.resolved(registry) == owner.incarnation
                and self.scope.applies(owner, self, registry))

    def turn_scope_matches(self, owner, registry):
        return owner.has_authored_turn(self.source_turn.resolved(registry), self.source_turn_id)

    def require_same_turn(self, correction, registry):
        if not correction.turn_scope_matches_original(self, registry):
            raise RelationViolationError("Task correction belongs to a different turn scope")

    def turn_scope_matches_original(self, previous, registry):
        return (previous.source_turn.resolved(registry), previous.source_turn_id) == (
            self.source_turn.resolved(registry), self.source_turn_id)

    def require_emission(self, owner):
        self.require_sender(owner.name)
        lease = owner.require_turn_lease()
        if (lease.identity, lease.turn_id) != (self.source_turn, self.source_turn_id.value):
            raise RelationViolationError("Task declaration's original turn is no longer admitted")
        self.scope.require_current(owner)

    def require_publication(self, sender, registry, original_source):
        author = registry.require(sender)
        author.require_turn(self.source_turn_id, registry.admission_generations[sender])
        self.require_emission(author)
        self.change.require_publication(self, registry, original_source)


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

    def require_previous(self, original):
        return original.task.require_human_constraint()

    def require_correction(self, original, registry):
        super().require_correction(original, registry)
        if self.require_previous(original).recipient.resolved(registry) != self.recipient.resolved(registry):
            raise RelationViolationError("USER pin correction must preserve its recipient")

    def require_user_revision(self, sender, registry):
        if self.author.resolved(registry) != registry.require(sender).incarnation:
            raise RelationViolationError("A USER cannot revise another human's constraint")

    def revises_after(self, previous):
        return True

    def lineage_reference(self, root):
        return self.subject

    def applies(self, owner, registry):
        return (self.recipient.current(registry)
                and self.recipient.resolved(registry) == owner.incarnation
                and self.scope.applies(owner, self, registry))

    def original_text_source(self, message, originals):
        try:
            return originals[self.subject]
        except KeyError as error:
            raise RelationViolationError("USER pin lacks its original captured wording") from error

    def require_target(self, target, registry):
        if not self.recipient.matches_recorded_name(target, registry):
            raise RelationViolationError("USER pin must address its original recipient")

    def require_publication(self, sender, registry, original_source):
        registry.require(sender).role.require_user()
        self.require_user_revision(sender, registry)
        if original_source is None:
            raise RelationViolationError("USER pin requires the certified original message")
        self.require_wording_publication(registry, original_source)
        if not self.recipient.current(registry):
            raise RelationViolationError("USER pin recipient was replaced")
        owner = registry.require(self.recipient.resolved(registry).name)
        self.scope.require_human_context(owner)
        self.change.require_publication(self, registry, original_source)

    def require_wording_publication(self, registry, original_source):
        from .private_bus_checkpoint import delivery_references_unlocked
        from .bus_publication import stable_thread_lookup

        delivery, = delivery_references_unlocked(original_source, (self.subject,))
        delivery.message.sender_role.require_user()
        if delivery.audience.sender_lookup != stable_thread_lookup(self.source_user.created_at):
            raise RelationViolationError("USER pin differs from its original human author")
        if not delivery.audience.includes_lookup(stable_thread_lookup(self.recipient.created_at)):
            raise RelationViolationError("USER pin recipient did not receive its original message")

    def user_task_facts(self, message):
        from .retained_task_facts import HumanConstraintTaskFact

        return (HumanConstraintTaskFact(message),)

    def retained_task_facts(self, message):
        return ()


@dataclass(frozen=True, kw_only=True)
class NativeInputConstraintPin(HumanConstraintPin):
    """The same USER pin relation, addressed to an original direct ACP input."""

    subject: Provenance

    def __post_init__(self):
        super().__post_init__()
        self.subject.require_human_input()

    def original_wording(self, original: StoredInput) -> str:
        return original.source_text

    def original_wording_context_source(self, original: StoredInput) -> Provenance:
        return original.context_provenance()

    def original_input_sources(self, inputs: InputDocument) -> tuple[StoredInput, ...]:
        return (self.subject.require_human_input().require_original(inputs),)

    def original_wording_provenance(self, original: StoredInput) -> dict[str, object]:
        origin = original.origin.require_human()
        return dict(wording=FieldCodec.encode(original.context_provenance()),
                    author=origin.author.sender, author_role=ThreadRole.USER.value)

    def require_wording_publication(self, registry, original_source):
        from .input_disposition import InputDispositions

        original_source.require_current()
        with InputDispositions(original_source.path.parent / InputDispositions.filename).reading() as inputs:
            row = self.subject.require_human_input().require_original(inputs)
        origin = row.origin.require_human()
        if origin.root_id != original_source.witness.root_id:
            raise RelationViolationError("USER input pin belongs to another wire root")
        original_author = ThreadIncarnation(origin.author.sender, origin.author.created_at)
        if original_author.resolved(registry) != self.source_user.resolved(registry):
            raise RelationViolationError("USER input pin differs from its original human author")
        if origin.admission.incarnation.resolved(registry) != self.recipient.resolved(registry):
            raise RelationViolationError("USER input pin recipient did not receive its original input")
        if origin.project != self.scope.project:
            raise RelationViolationError("USER input pin belongs to another project")


@dataclass(frozen=True, kw_only=True)
class Decision(ModelTaskDeclaration, declared_name="choice"):
    chosen: str
    rejected: tuple[str, ...]

    def require_decision(self):
        return self

    def require_previous(self, original):
        return original.require_decision()

    def retained_task_facts(self, message):
        from .retained_task_facts import DecisionTaskFact

        return (DecisionTaskFact(message),)

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
    """Explicit constraint membership; exact wording belongs to Message.body only."""

    def require_constraint(self):
        return self

    def require_previous(self, original):
        return original.task.require_constraint()

    def retained_task_facts(self, message):
        from .retained_task_facts import ConstraintTaskFact

        return (ConstraintTaskFact(message),)
