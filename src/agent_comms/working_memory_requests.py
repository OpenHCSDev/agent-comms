"""The disclosed request owns exact sent values and its external disposition."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .turn_context import ContextSpan
from .working_memory_disclosure import DisclosureState
from .working_memory_labels import ClassifierVersion, JevUsage, QuestionVersion
from .working_memory_questions import SpanQuestion


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

    @classmethod
    def capture(cls, span, classifier, question: type[SpanQuestion], state):
        return cls(span, classifier, QuestionVersion.current(question),
                   FieldCodec.decode(ChoiceQuestionDefinition, question.declaration()), state)

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
class FailedAnnotationOutcome(AnnotationOutcome):
    error: str

    def public_description(self) -> str:
        return f"Classifier did not produce an admitted answer: {self.error}. Not automatically resent."
