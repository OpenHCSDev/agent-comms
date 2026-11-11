"""The disclosed request owns exact sent values and its external disposition."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from http.client import HTTPException
from typing import ClassVar, Literal
from urllib.error import HTTPError

from agent_comms.execution.turn_context import ContextSpan
from agent_comms.execution.working_memory_disclosure import DisclosureState
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.working_memory.working_memory_labels import (
    ClassifierVersion,
    JevUsage,
    QuestionVersion,
)
from agent_comms.working_memory.working_memory_questions import SpanQuestion


@dataclass(frozen=True)
class ChoiceQuestionDefinition:
    instructions: str
    criteria: dict[str, str]
    type: Literal["choice"] = "choice"


@dataclass(frozen=True)
class DisclosureRequest:
    span: ContextSpan
    classifier: ClassifierVersion
    question: QuestionVersion
    definition: ChoiceQuestionDefinition
    state: DisclosureState
    grant: MessageReference

    @classmethod
    def capture(cls, span, classifier, question: type[SpanQuestion], state, grant: MessageReference):
        return cls(span=span, classifier=classifier, question=QuestionVersion.current(question, state.owner_rules),
                   definition=FieldCodec.decode(ChoiceQuestionDefinition, question.declaration()), state=state, grant=grant)

    def payload(self):
        return {"model": self.classifier.pin,
                "questions": {self.question.question.declared_name: FieldCodec.encode(self.definition)},
                "state": FieldCodec.encode(self.state)}


@dataclass(frozen=True)
class AnnotationOutcome(DeclaredFamily, affix="AnnotationOutcome"):
    @abstractmethod
    def public_description(self) -> str: ...


@dataclass(frozen=True)
class SubmittedAnnotationOutcome(AnnotationOutcome):
    def public_description(self) -> str:
        return "Submitted; no settled classifier response is recorded. Never resend automatically."


@dataclass(frozen=True)
class CompletedAnnotationOutcome(AnnotationOutcome):
    request_id: str
    returned_model: str
    usage: JevUsage

    def public_description(self) -> str:
        return f"Completed by {self.returned_model} · request {self.request_id} · cost ${self.usage.cost}"


@dataclass(frozen=True)
class FailedAnnotationOutcome(AnnotationOutcome, declared_name=None):
    """The classifier was asked and gave no authorized answer. Each member is one kind of
    error that stops it, and nothing resends the request."""

    caught: ClassVar[type[Exception]]
    said: ClassVar[str]

    def public_description(self) -> str:
        return f"Classifier did not produce an authorized answer: {self.said}. Not automatically resent."

    @classmethod
    def caught_errors(cls) -> tuple[type[Exception], ...]:
        return tuple(member.caught for member in cls.members_with(FailedAnnotationOutcome))

    @classmethod
    def for_error(cls, error: type[BaseException]) -> FailedAnnotationOutcome:
        """The member whose caught error comes first in the raised error's MRO."""
        caught = {member.caught: member for member in cls.members_with(FailedAnnotationOutcome)}
        return next(caught[kind] for kind in error.__mro__ if kind in caught)()


@dataclass(frozen=True)
class ClassifierUnreachableAnnotationOutcome(FailedAnnotationOutcome):
    caught = OSError
    said = "the classifier could not be reached"


@dataclass(frozen=True)
class ClassifierHttpStatusAnnotationOutcome(FailedAnnotationOutcome):
    caught = HTTPError
    said = "the classifier answered with an HTTP error status"


@dataclass(frozen=True)
class ClassifierProtocolAnnotationOutcome(FailedAnnotationOutcome):
    caught = HTTPException
    said = "the classifier's HTTP response was malformed"


@dataclass(frozen=True)
class ClassifierAnswerRejectedAnnotationOutcome(FailedAnnotationOutcome):
    caught = ValueError
    said = "the classifier's answer was refused as invalid"


class AnnotationBudgetExhausted(ValueError):
    """The disclosure grant has used up its hourly request allowance."""
