"""Answers address original spans; the label hierarchy owns correction authority."""
from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Literal

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .thread_identity import ThreadIncarnation
from .turn_context import ContextSpan
from .working_memory_questions import SpanAnswer, SpanQuestion


class Classifier(DeclaredFamily, affix="Classifier"):
    @classmethod
    @abstractmethod
    def version(cls) -> ClassifierVersion: ...

    @classmethod
    @abstractmethod
    async def classify(cls, request, api_key: str): ...


@dataclass(frozen=True)
class ClassifierVersion:
    classifier: type[Classifier]
    pin: str

    def require_returned_model(self, model: str) -> None:
        if model != self.pin and not model.startswith(self.pin + "-"):
            raise ValueError("Classifier response did not use the pinned release")


@dataclass(frozen=True)
class QuestionVersion:
    question: type[SpanQuestion]
    sha256: str

    @classmethod
    def current(cls, question: type[SpanQuestion], owner_rules: tuple[str, ...] = ()):
        # A relation to authored rules is a different question when that original
        # rule set changes. Other questions discard that unrelated input.
        raw = json.dumps({"definition": question.declaration(),
                          "owner_rules": question.disclosed_owner_rules(owner_rules)},
                         sort_keys=True, separators=(",", ":")).encode()
        return cls(question, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class AnswerProbability:
    answer: type[SpanAnswer]
    probability: float

    def __post_init__(self):
        if not 0 <= self.probability <= 1:
            raise ValueError("Answer probability must lie between zero and one")


@dataclass(frozen=True)
class SpanLabel(DeclaredFamily, affix="Label"):
    span: ContextSpan
    question: QuestionVersion
    answer: type[SpanAnswer]

    def __post_init__(self):
        self.question.question.require_answer(self.answer)

    @abstractmethod
    def public_description(self) -> str: ...

    @classmethod
    def effective(cls, labels: tuple[SpanLabel, ...]) -> SpanLabel:
        if not labels:
            raise ValueError("This span has no answer")
        originals = {(label.span, label.question) for label in labels}
        if len(originals) != 1:
            raise ValueError("Correction resolution requires the same original span and question")
        # Refinement is nominal. Incomparable authorities are not settled by a
        # priority number or by whichever row happened to be read last.
        candidates = tuple(label for label in labels
            if all(issubclass(type(label), type(other)) for other in labels))
        if len(candidates) != 1:
            raise ValueError("Original label authority is ambiguous")
        return candidates[0]


@dataclass(frozen=True)
class ModelLabel(SpanLabel):
    classifier: ClassifierVersion
    probabilities: tuple[AnswerProbability, ...]
    confidence: float
    request_id: str
    returned_model: str

    def __post_init__(self):
        super().__post_init__()
        answers = tuple(item.answer for item in self.probabilities)
        if set(answers) != set(self.question.question.answer_family.members_with(SpanAnswer)):
            raise ValueError("Classifier distribution differs from the declared answer family")
        if len(answers) != len(set(answers)) or abs(sum(item.probability for item in self.probabilities) - 1) > 1e-6:
            raise ValueError("Classifier distribution must name every answer once and sum to one")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Classifier confidence must lie between zero and one")

    @property
    def probability(self) -> float:
        return next(item.probability for item in self.probabilities if item.answer is self.answer)

    def public_description(self) -> str:
        return f"{self.classifier.pin} · probability {self.probability:.3f} · confidence {self.confidence:.3f}"


@dataclass(frozen=True)
class HumanLabel(ModelLabel):
    author: ThreadIncarnation

    @classmethod
    def correct(cls, original: ModelLabel, answer: type[SpanAnswer], author: ThreadIncarnation):
        return cls(original.span, original.question, answer, original.classifier,
                   original.probabilities, original.confidence, original.request_id,
                   original.returned_model, author)

    def public_description(self) -> str:
        return f"Human correction by {self.author.name} · original classifier {self.classifier.pin}"


@dataclass(frozen=True)
class JevChoice:
    """External Decisions API choice decoded once at ingress."""
    type: Literal["choice"]
    choice: str
    confidence: float
    probabilities: dict[str, float]

    def label(self, span: ContextSpan, question: QuestionVersion,
              classifier: ClassifierVersion, response: JevResponse) -> ModelLabel:
        family = question.question.answer_family
        return ModelLabel(span, question, family.decode(self.choice),
            classifier, tuple(AnswerProbability(family.decode(name), probability)
                for name, probability in self.probabilities.items()), self.confidence,
            response.id, response.model)


@dataclass(frozen=True)
class JevUsage:
    cost: float
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class JevResponse:
    id: str
    model: str
    provider: str
    answers: dict[str, JevChoice]
    usage: JevUsage


class JevClassifier(Classifier):
    endpoint: ClassVar[str] = "https://openrouter.ai/api/alpha/decisions"

    @classmethod
    def version(cls) -> ClassifierVersion:
        return ClassifierVersion(cls, "typesafe/jev-1.13")

    @classmethod
    def decode(cls, original: object, request):
        response = FieldCodec.decode(JevResponse, original)
        question = request.question.question.declared_name
        if set(response.answers) != {question}:
            raise ValueError("Classifier response differs from the original requested questions")
        request.classifier.require_returned_model(response.model)
        return response, response.answers[question].label(
            request.span, request.question, request.classifier, response)

    @classmethod
    async def classify(cls, request, api_key: str):
        from urllib.request import Request, urlopen
        from .coordinator import Coordination

        def send():
            external = Request(cls.endpoint, data=json.dumps(request.payload()).encode(),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST")
            with urlopen(external, timeout=30) as response:
                return cls.decode(json.load(response), request)

        # The existing resource worker joins cancellation through HTTP close.
        return await Coordination.run_worker(send)
