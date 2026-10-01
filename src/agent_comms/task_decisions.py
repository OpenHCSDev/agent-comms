"""Authored choices live on their original wire message, never a decision ledger."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .goals import GoalRevision
from .message_reference import MessageReference
from .thread_identity import ThreadIncarnation, TurnId, TurnIdentity

if TYPE_CHECKING:
    from .messages import Message
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .private_bus_checkpoint import CertifiedSourceRead


@dataclass(frozen=True, kw_only=True)
class DecisionScope(DeclaredFamily, affix="DecisionScope"):
    project: str

    def require_current(self, owner: Thread) -> None:
        if self.project != owner.worktree:
            raise RelationViolationError("Decision project differs from its author")
        self.require_context(owner)

    @abstractmethod
    def require_context(self, owner: Thread) -> None: ...

@dataclass(frozen=True, kw_only=True)
class GoalDecisionScope(DecisionScope):
    goal: GoalRevision

    def require_context(self, owner: Thread) -> None:
        owner.require_active_goal(self.goal.id)
        owner.require_goal_checkpoint(self.goal)


@dataclass(frozen=True, kw_only=True)
class TurnDecisionScope(DecisionScope):
    """The enclosing Decision's original turn determines this scope."""

    def require_context(self, owner: Thread) -> None:
        if owner.decision_scope != self:
            raise RelationViolationError("An active goal requires its revision scope")


class DecisionScopeSelection(DeclaredFamily, affix="ScopeSelection"):
    @abstractmethod
    def select(self, owner: Thread) -> DecisionScope: ...


@dataclass(frozen=True)
class CurrentDecisionScopeSelection(DecisionScopeSelection, declared_name="current"):
    def select(self, owner: Thread) -> DecisionScope:
        return owner.decision_scope


@dataclass(frozen=True)
class ExplicitDecisionScopeSelection(DecisionScopeSelection, declared_name="explicit"):
    scope: DecisionScope

    def select(self, owner: Thread) -> DecisionScope:
        self.scope.require_current(owner)
        return self.scope


class DecisionChange(DeclaredFamily, affix="DecisionChange"):
    @abstractmethod
    def require_publication(self, decision: Decision, registry: RegistrySnapshot,
                            original_source: CertifiedSourceRead | None) -> None: ...


@dataclass(frozen=True)
class OriginalDecisionChange(DecisionChange, declared_name="original"):
    def require_publication(self, decision, registry, original_source) -> None:
        pass


@dataclass(frozen=True)
class CorrectionDecisionChange(DecisionChange, declared_name="correction"):
    original: MessageReference

    def require_publication(self, decision, registry, original_source) -> None:
        if original_source is None:
            raise RelationViolationError("Decision correction requires the original publication read")
        from .private_bus_checkpoint import source_references_unlocked

        original, = source_references_unlocked(original_source, (self.original,))
        if original.reference != self.original:
            raise RelationViolationError("Decision correction requires its original wire reference")
        decision.require_correction(original, registry)


class DecisionAttachment(DeclaredFamily, affix="DecisionAttachment"):
    """Absent attachments have no publication, fact or author effects."""

    retains_authored_choice = False

    def require_decision(self) -> Decision:
        raise RelationViolationError("Original wire message has no declared decision")

    def require_sender(self, sender: str) -> None:
        pass

    def require_publication(self, sender, registry, original_source) -> None:
        pass

    def retained_task_facts(self, message: Message):
        return ()


@dataclass(frozen=True)
class NoDecision(DecisionAttachment, declared_name="absent"):
    pass


@dataclass(frozen=True, kw_only=True)
class Decision(DecisionAttachment, declared_name="choice"):
    chosen: str
    rejected: tuple[str, ...]
    scope: DecisionScope
    source_turn: TurnIdentity
    source_turn_id: TurnId
    change: DecisionChange = field(default_factory=OriginalDecisionChange,
                                   metadata={"wire_omit_default": True})
    retains_authored_choice = True

    def require_decision(self) -> Decision:
        return self

    def retained_task_facts(self, message: Message):
        from .retained_task_facts import DecisionTaskFact

        return (DecisionTaskFact(message),)

    def __post_init__(self) -> None:
        if not self.chosen.strip():
            raise ValueError("A decision requires a nonempty chosen alternative")
        if not self.rejected or any(not value.strip() for value in self.rejected):
            raise ValueError("A decision requires nonempty valid rejected alternatives")
        if len(set(self.rejected)) != len(self.rejected) or self.chosen in self.rejected:
            raise ValueError("Decision alternatives must be unique and distinct")
        self.author.require_recorded()
        if self.source_turn.generation <= 0:
            raise ValueError("A decision requires its original admitted turn")

    @property
    def author(self) -> ThreadIncarnation:
        return self.source_turn.incarnation

    @property
    def text(self) -> str:
        return "Decision: " + self.chosen + "\nValid rejected alternatives:\n" + "\n".join(
            "- " + value for value in self.rejected
        )

    def require_sender(self, sender: str) -> None:
        if self.author.name != sender:
            raise RelationViolationError("Decision does not belong to this message author")

    def require_emission(self, owner: Thread) -> None:
        self.require_sender(owner.name)
        lease = owner.require_turn_lease()
        if (lease.identity, lease.turn_id) != (
            self.source_turn, self.source_turn_id.value
        ):
            raise RelationViolationError("Decision's original turn is no longer admitted")
        self.scope.require_current(owner)

    def require_correction(self, original: Message, registry: RegistrySnapshot) -> None:
        previous = original.require_decision()
        if previous.author.resolved(registry) != self.author.resolved(registry):
            raise RelationViolationError("An agent cannot correct another author's decision")
        if previous.scope != self.scope:
            raise RelationViolationError("Decision correction must preserve its original scope")

    def require_publication(
        self, sender: str, registry: RegistrySnapshot,
        original_source: CertifiedSourceRead | None,
    ) -> None:
        author = registry.require(sender)
        author.require_turn(self.source_turn_id, registry.admission_generations[sender])
        self.require_emission(author)
        self.change.require_publication(self, registry, original_source)
