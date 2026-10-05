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
from .working_memory_questions import OtherSpan, SpanAnswer, SpanQuestion


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

    @property
    def working_memory_section(self) -> str:
        # No empirical calibration/threshold has been admitted for this pin.
        # A probability alone does not classify the human working-memory view.
        return OtherSpan.section

    def evaluate_original(self, original: ModelLabel) -> tuple[CalibrationCase, ...]:
        # An unreviewed prediction supplies no human evaluation evidence.
        return ()


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

    @property
    def working_memory_section(self) -> str:
        return self.question.question.working_memory_section(self.answer)

    def evaluate_original(self, original: ModelLabel) -> tuple[CalibrationCase, ...]:
        return (CalibrationCase(original, self),)


@dataclass(frozen=True)
class CalibrationCase:
    """One stored prediction and its effective original human review."""
    original: ModelLabel
    review: HumanLabel

    def __post_init__(self):
        # A review refines this exact answer; it cannot evaluate another model
        # release, source, question, response or probability distribution.
        corrected = HumanLabel.correct(self.original, self.review.answer, self.review.author)
        if corrected != self.review:
            raise ValueError("Calibration review differs from its original classifier answer")

    @property
    def correct(self) -> bool:
        return self.original.answer is self.review.answer


@dataclass(frozen=True)
class ProbabilityFrequency:
    """Observed outcomes at an exact reported probability, without thresholds."""
    answer: type[SpanAnswer]
    probability: float
    reviewed: int
    observed: int

    @property
    def frequency(self) -> float:
        return self.observed / self.reviewed


@dataclass(frozen=True)
class CalibrationReport:
    question: QuestionVersion
    classifier: ClassifierVersion
    cases: tuple[CalibrationCase, ...]

    def __post_init__(self):
        if not self.cases:
            raise ValueError("No human-reviewed spans exist for this question and classifier version")
        for case in self.cases:
            if (case.original.question, case.original.classifier) != (self.question, self.classifier):
                raise ValueError("Calibration cannot combine different question or classifier versions")

    @property
    def accuracy(self) -> float:
        return sum(case.correct for case in self.cases) / len(self.cases)

    def frequencies(self) -> tuple[ProbabilityFrequency, ...]:
        counts = {}
        for case in self.cases:
            for prediction in case.original.probabilities:
                key = prediction.answer, prediction.probability
                reviewed, observed = counts.get(key, (0, 0))
                counts[key] = reviewed + 1, observed + (prediction.answer is case.review.answer)
        return tuple(ProbabilityFrequency(answer, probability, reviewed, observed)
            for (answer, probability), (reviewed, observed) in counts.items())

    def public_report(self) -> dict[str, object]:
        """The CLI boundary renders metrics derived from these original reviews."""
        return {"question": FieldCodec.encode(self.question),
                "classifier": FieldCodec.encode(self.classifier),
                "reviewed": len(self.cases), "accuracy": self.accuracy,
                "probability_frequencies": tuple(
                    {**FieldCodec.encode(point), "frequency": point.frequency}
                    for point in self.frequencies()),
                "cases": FieldCodec.encode(self.cases)}


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
