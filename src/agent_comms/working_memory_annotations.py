"""Annotation operations borrow the original coordinator connection and clock."""
from __future__ import annotations

from .coordination_tables.annotations import AnnotationRequestsRow, SpanAnnotationsRow
from .field_codec import FieldCodec
from .working_memory_labels import ClassifierVersion, HumanLabel, ModelLabel, QuestionVersion
from .working_memory_requests import CompletedAnnotationOutcome, DisclosureRequest, SubmittedAnnotationOutcome


class PreviouslyRequestedAnnotation(ValueError):
    """A recorded external request is never automatically replayed."""


class AnnotationBudgetExhausted(ValueError):
    """The configured rolling-hour request budget has no remaining admission."""


class WorkingMemoryAnnotations:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def address(span, question: QuestionVersion, classifier: ClassifierVersion):
        return dict(zip(SpanAnnotationsRow.address, (
            span.segment_sha256, span.coordinates.offset, span.coordinates.length,
            question.question.declared_name, question.sha256, classifier.pin), strict=True))

    def labels(self, span, question, classifier):
        key = self.address(span, question, classifier)
        with self.session.read():
            return tuple(SpanAnnotationsRow.select(self.session._connection,
                where=" AND ".join(f'{name}=?' for name in key),
                parameters=tuple(key.values()), order_by=("id",)))

    def reserve(self, request: DisclosureRequest, per_hour: int) -> AnnotationRequestsRow:
        if type(per_hour) is not int or per_hour < 1:
            raise ValueError("External annotation requires a positive configured hourly budget")
        key = self.address(request.span, request.question, request.classifier)
        with self.session.transaction() as db:
            if AnnotationRequestsRow.one(db, **key) is not None:
                raise PreviouslyRequestedAnnotation("Original classifier request already recorded; inspect its disposition")
            now = self.session.now()
            count = FieldCodec.decode(int, db.execute(
                f'SELECT COUNT(*) FROM "{AnnotationRequestsRow.declared_name}" WHERE created_at_ms>?',
                (now - 3_600_000,)).fetchone()[0])
            if count >= per_hour:
                raise AnnotationBudgetExhausted("Configured hourly annotation budget is exhausted")
            original = AnnotationRequestsRow(request=request, outcome=SubmittedAnnotationOutcome(), created_at_ms=now)
            sequence = original.insert(db).lastrowid
            return AnnotationRequestsRow.one(db, id=sequence)

    def complete(self, original: AnnotationRequestsRow, response, label: ModelLabel):
        if (label.span, label.question, label.classifier) != (
                original.request.span, original.request.question, original.request.classifier):
            raise ValueError("Classifier label is not an answer to its original disclosed request")
        with self.session.transaction() as db:
            current = AnnotationRequestsRow.one(db, id=original.id)
            if current != original:
                raise ValueError("Classifier request disposition changed before publication")
            SpanAnnotationsRow(label=label, created_at_ms=self.session.now()).insert(db)
            AnnotationRequestsRow.update(db, where="id=?", parameters=(original.id,),
                outcome=CompletedAnnotationOutcome(response.id, response.model, response.usage))

    def fail(self, original: AnnotationRequestsRow, outcome):
        with self.session.transaction() as db:
            current = AnnotationRequestsRow.one(db, id=original.id)
            if current != original:
                raise ValueError("Classifier request disposition changed before refusal")
            AnnotationRequestsRow.update(db, where="id=?", parameters=(original.id,), outcome=outcome)

    def correct(self, original: ModelLabel, answer, author, registry):
        registry.require(author.resolved(registry).name).role.require_user()
        with self.session.transaction() as db:
            key = self.address(original.span, original.question, original.classifier)
            rows = tuple(SpanAnnotationsRow.select(db,
                where=" AND ".join(f'{name}=?' for name in key), parameters=tuple(key.values())))
            if original not in (row.label for row in rows):
                raise ValueError("Human correction names no original stored classifier answer")
            corrected = HumanLabel.correct(original, answer, author)
            row = SpanAnnotationsRow(label=corrected, created_at_ms=self.session.now())
            row.insert(db)
            return corrected
