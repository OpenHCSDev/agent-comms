"""Declared questions type meaning; original context owners retain provenance."""
from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from .declared_family import DeclaredFamily


class SpanAnswer(DeclaredFamily, affix="Answer"):
    @property
    @abstractmethod
    def criteria(self) -> str: ...

    @classmethod
    def public_title(cls) -> str:
        return cls.declared_name.replace("_", " ").title()


class SpanKind(SpanAnswer):
    follow_ups: ClassVar[tuple[type[SpanQuestion], ...]] = ()


class ObligationAnswer(SpanAnswer):
    pass


class MustAnswer(ObligationAnswer):
    criteria = "A requirement to do something."


class MustNotAnswer(ObligationAnswer):
    criteria = "A prohibition against doing something."


class PreferAnswer(ObligationAnswer):
    criteria = "A recommendation or preference, not a requirement."


class RuleScopeAnswer(SpanAnswer):
    pass


class CurrentTaskAnswer(RuleScopeAnswer):
    criteria = "Applies to the current task or turn only."


class ProjectAnswer(RuleScopeAnswer):
    criteria = "Applies to work in the named project."


class StandingAnswer(RuleScopeAnswer):
    criteria = "Explicitly applies across tasks or projects."


class UnspecifiedScopeAnswer(RuleScopeAnswer):
    criteria = "The wording does not establish its scope."


class OwnerRelationAnswer(SpanAnswer):
    pass


class AlignedAnswer(OwnerRelationAnswer):
    criteria = "Expresses or refines a supplied owner rule without contradicting it."


class ContradictsAnswer(OwnerRelationAnswer):
    criteria = "Contradicts a supplied owner rule."


class AdditionalAnswer(OwnerRelationAnswer):
    criteria = "Adds an obligation not stated in the supplied owner rules."


class UnclearRelationAnswer(OwnerRelationAnswer):
    criteria = "The supplied evidence cannot establish the relationship."


class CommitmentStatusAnswer(SpanAnswer):
    pass


class FulfilledAnswer(CommitmentStatusAnswer):
    criteria = "The supplied context contains evidence that the promised work is complete."


class OpenAnswer(CommitmentStatusAnswer):
    criteria = "The promise remains open or the supplied context does not prove fulfillment."


class SpanQuestion(DeclaredFamily, affix="Question"):
    answer_family: ClassVar[type[SpanAnswer]]
    primitive: ClassVar[str] = "choice"

    @property
    @abstractmethod
    def instructions(self) -> str: ...

    @classmethod
    def declaration(cls):
        return {"type": cls.primitive, "instructions": cls.instructions,
                "criteria": {answer.declared_name: answer.criteria
                             for answer in cls.answer_family.members_with(cls.answer_family)}}

    @classmethod
    def require_answer(cls, answer: type[SpanAnswer]):
        if not issubclass(answer, cls.answer_family):
            raise ValueError("Answer is outside the declared question family")
        return answer

    @classmethod
    def disclosed_owner_rules(cls, rules: tuple[str, ...]) -> tuple[str, ...]:
        return ()


class KindQuestion(SpanQuestion):
    answer_family = SpanKind
    instructions = "Classify the literal span's meaning. Do not infer its author or authority."


class ObligationQuestion(SpanQuestion):
    answer_family = ObligationAnswer
    instructions = "What obligation does this literal rule express?"


class RuleScopeQuestion(SpanQuestion):
    answer_family = RuleScopeAnswer
    instructions = "What scope does the literal rule explicitly establish?"


class RelationToOwnerRulesQuestion(SpanQuestion):
    answer_family = OwnerRelationAnswer
    instructions = "Compare the literal rule to the supplied typed owner rules. Do not invent rules."

    @classmethod
    def disclosed_owner_rules(cls, rules: tuple[str, ...]) -> tuple[str, ...]:
        from .working_memory_disclosure import PublicInstructionDisclosure

        policy = PublicInstructionDisclosure()
        return tuple(policy.require_text(rule) for rule in rules)


class FulfilledQuestion(SpanQuestion):
    answer_family = CommitmentStatusAnswer
    instructions = "Does the supplied public context prove this literal commitment has been fulfilled?"


class RuleSpan(SpanKind):
    criteria = "States what the agent must or must not do, or explicitly prefers."
    follow_ups = (ObligationQuestion, RuleScopeQuestion, RelationToOwnerRulesQuestion)
    section = "Obeys"


class CommitmentSpan(SpanKind):
    criteria = "States something the agent promises or intends to do."
    follow_ups = (FulfilledQuestion,)
    section = "Promised"


class OtherSpan(SpanKind):
    criteria = "Neither a rule nor a commitment; includes ordinary description, quoted data and questions."
    section = "Unclassified"
