"""Annotation operations borrow the original coordinator connection and clock."""
from __future__ import annotations

from .coordination_tables.annotations import AnnotationRequestsRow, SpanAnnotationsRow
from .working_memory_labels import CalibrationReport, ClassifierVersion, HumanLabel, ModelLabel, QuestionVersion
from .working_memory_requests import CompletedAnnotationOutcome, DisclosureRequest, SubmittedAnnotationOutcome


class PreviouslyRequestedAnnotation(ValueError):
    """A recorded external request is never automatically replayed."""


class WorkingMemoryAnnotations:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def address(span, question: QuestionVersion, classifier: ClassifierVersion):
        return dict(zip(SpanAnnotationsRow.address, (
            span.segment_sha256, span.coordinates.offset, span.coordinates.length,
            question.question, question.sha256, classifier.classifier, classifier.pin), strict=True))

    def labels(self, span, question, classifier):
        key = self.address(span, question, classifier)
        with self.session.read():
            return tuple(SpanAnnotationsRow.select(self.session._connection,
                order_by=("id",), **key))

    def calibration(self, question: QuestionVersion, classifier: ClassifierVersion) -> CalibrationReport:
        with self.session.read():
            db = self.session._connection
            originals = SpanAnnotationsRow.select(db, order_by=("id",),
                question=question.question, question_version=question.sha256,
                classifier=classifier.classifier, classifier_pin=classifier.pin, label_kind=ModelLabel)
            cases = []
            for row in originals:
                original = row.label
                rows = SpanAnnotationsRow.select(db, order_by=("id",),
                    **self.address(original.span, question, classifier))
                effective = SpanAnnotationsRow.effective(tuple(rows))
                cases.extend(effective.evaluate_original(original))
            return CalibrationReport(question, classifier, tuple(cases))

    def for_segment(self, segment, classifier: ClassifierVersion) -> tuple[ModelLabel, ...]:
        """Read the complete effective answer family at its original addresses.

        A kind answer does not replace its obligation, scope or fulfillment
        evidence. Different question versions remain different recorded facts;
        reading a segment never substitutes today's question definition.
        """
        with self.session.read():
            rows = SpanAnnotationsRow.select(self.session._connection, order_by=("id",),
                segment_digest=segment.sha256, classifier=classifier.classifier,
                classifier_pin=classifier.pin)
            grouped = {}
            for row in rows:
                label = row.label
                key = tuple(self.address(label.span, label.question, label.classifier).values())
                grouped.setdefault(key, []).append(row)
            return tuple(SpanAnnotationsRow.effective(tuple(originals))
                         for originals in grouped.values())

    def reserve(self, request: DisclosureRequest, grant) -> AnnotationRequestsRow:
        key = self.address(request.span, request.question, request.classifier)
        with self.session.transaction() as db:
            if AnnotationRequestsRow.one(db, **key) is not None:
                raise PreviouslyRequestedAnnotation("Original classifier request already recorded; inspect its disposition")
            now = self.session.now()
            grant.require_budget(AnnotationRequestsRow.requests_since(
                db, request.grant, grant.window_start(now)))
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
            rows = tuple(SpanAnnotationsRow.select(db, **key))
            if original not in (row.label for row in rows):
                raise ValueError("Human correction names no original stored classifier answer")
            corrected = HumanLabel.correct(original, answer, author)
            row = SpanAnnotationsRow(label=corrected, created_at_ms=self.session.now())
            row.insert(db)
            return corrected
