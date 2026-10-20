"""Durable label/request declarations; the existing coordinator owns storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.message_reference import MessageReference
from agent_comms.typed_table import Column, Index, TypedTable, sql_literal
from agent_comms.working_memory_labels import Classifier, ModelLabel, SpanLabel
from agent_comms.working_memory_questions import SpanQuestion
from agent_comms.working_memory_requests import AnnotationOutcome, DisclosureRequest


@dataclass(frozen=True, kw_only=True)
class SpanAnnotationsRow(CoordinatorTable, TypedTable):
    label: ModelLabel
    created_at_ms: int
    # SQLite NULL requests its original rowid allocation, not a lifecycle state.
    id: Annotated[int | None, Column(primary_key=True, auto_increment=True)] = None
    segment_digest: Annotated[
        str, Column(generated="json_extract(label, '$.span.segment_sha256')")
    ] = field(init=False, compare=False)
    offset: Annotated[
        int, Column(generated="json_extract(label, '$.span.coordinates.offset')")
    ] = field(init=False, compare=False)
    length: Annotated[
        int, Column(generated="json_extract(label, '$.span.coordinates.length')")
    ] = field(init=False, compare=False)
    question: Annotated[
        type[SpanQuestion], Column(generated="json_extract(label, '$.question.question')")
    ] = field(init=False, compare=False)
    question_version: Annotated[
        str, Column(generated="json_extract(label, '$.question.sha256')")
    ] = field(init=False, compare=False)
    classifier: Annotated[
        type[Classifier], Column(generated="json_extract(label, '$.classifier.classifier')")
    ] = field(init=False, compare=False)
    classifier_pin: Annotated[
        str, Column(generated="json_extract(label, '$.classifier.pin')")
    ] = field(init=False, compare=False)
    label_kind: Annotated[
        type[SpanLabel], Column(generated="json_extract(label, '$.kind')")
    ] = field(init=False, compare=False)

    address: ClassVar[tuple[str, ...]] = (
        "segment_digest",
        "offset",
        "length",
        "question",
        "question_version",
        "classifier",
        "classifier_pin",
    )
    indexes = (
        Index(address, unique=True, where=f"label_kind={sql_literal(ModelLabel)}"),
        Index(address),
    )

    @classmethod
    def effective(cls, rows: tuple[SpanAnnotationsRow, ...]):
        # Row ids order successive corrections from the same authority. Nominal
        # inheritance, not sequence, decides human versus model authority.
        latest = {}
        for row in sorted(rows, key=lambda value: value.id):
            latest[type(row.label)] = row.label
        return SpanLabel.effective(tuple(latest.values()))

    @classmethod
    def for_digests(cls, db, digests, classifier):
        if not digests:
            return ()
        return cls.select(db,
            where='segment_digest IN (' + ','.join('?' for _ in digests) + ')',
            parameters=tuple(digests), order_by=("id",),
            classifier=classifier.classifier, classifier_pin=classifier.pin)


@dataclass(frozen=True, kw_only=True)
class AnnotationRequestsRow(CoordinatorTable, TypedTable):
    request: DisclosureRequest
    outcome: AnnotationOutcome
    created_at_ms: int
    id: Annotated[int | None, Column(primary_key=True, auto_increment=True)] = None
    segment_digest: Annotated[
        str, Column(generated="json_extract(request, '$.span.segment_sha256')")
    ] = field(init=False, compare=False)
    offset: Annotated[
        int, Column(generated="json_extract(request, '$.span.coordinates.offset')")
    ] = field(init=False, compare=False)
    length: Annotated[
        int, Column(generated="json_extract(request, '$.span.coordinates.length')")
    ] = field(init=False, compare=False)
    question: Annotated[
        type[SpanQuestion], Column(generated="json_extract(request, '$.question.question')")
    ] = field(init=False, compare=False)
    question_version: Annotated[
        str, Column(generated="json_extract(request, '$.question.sha256')")
    ] = field(init=False, compare=False)
    classifier: Annotated[
        type[Classifier], Column(generated="json_extract(request, '$.classifier.classifier')")
    ] = field(init=False, compare=False)
    classifier_pin: Annotated[
        str, Column(generated="json_extract(request, '$.classifier.pin')")
    ] = field(init=False, compare=False)

    grant_reference: Annotated[
        MessageReference, Column(generated="json_extract(request, '$.grant')")
    ] = field(init=False, compare=False)

    unique = (SpanAnnotationsRow.address,)
    indexes = (Index(("grant_reference", "created_at_ms")),)

    @classmethod
    def requests_since(cls, db, reference: MessageReference, since: int) -> int:
        return cls.count(db, where='created_at_ms>?', parameters=(since,), grant_reference=reference)
