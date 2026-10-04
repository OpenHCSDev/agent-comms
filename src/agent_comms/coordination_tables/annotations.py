"""Durable label/request declarations; the existing coordinator owns storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.typed_table import Column, Index, TypedTable, sql_literal
from agent_comms.working_memory_questions import SpanQuestion
from agent_comms.message_reference import MessageReference
from agent_comms.field_codec import FieldCodec
from agent_comms.working_memory_labels import Classifier, ModelLabel, SpanLabel
from agent_comms.working_memory_requests import AnnotationOutcome, DisclosureRequest


@dataclass(frozen=True, kw_only=True)
class SpanAnnotationsRow(CoordinatorTable, TypedTable):
    label: ModelLabel
    created_at_ms: int
    # SQLite NULL requests its original rowid allocation, not a lifecycle state.
    id: int | None = field(default=None, metadata={"sql": Column(primary_key=True, auto_increment=True)})
    segment_digest: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.span.segment_sha256')")})
    offset: int = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.span.coordinates.offset')")})
    length: int = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.span.coordinates.length')")})
    question: type[SpanQuestion] = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.question.question')")})
    question_version: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.question.sha256')")})
    classifier: type[Classifier] = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.classifier.classifier')")})
    classifier_pin: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.classifier.pin')")})
    label_kind: type[SpanLabel] = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.kind')")})

    address: ClassVar[tuple[str, ...]] = (
        "segment_digest", "offset", "length", "question", "question_version", "classifier", "classifier_pin")
    indexes = (Index(address, unique=True, where=f"label_kind={sql_literal(ModelLabel)}"), Index(address))

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
    id: int | None = field(default=None, metadata={"sql": Column(primary_key=True, auto_increment=True)})
    segment_digest: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.span.segment_sha256')")})
    offset: int = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.span.coordinates.offset')")})
    length: int = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.span.coordinates.length')")})
    question: type[SpanQuestion] = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.question.question')")})
    question_version: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.question.sha256')")})
    classifier: type[Classifier] = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.classifier.classifier')")})
    classifier_pin: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.classifier.pin')")})

    grant_reference: MessageReference = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.grant')")})

    unique = (SpanAnnotationsRow.address,)
    indexes = (Index(("grant_reference", "created_at_ms")),)

    @classmethod
    def requests_since(cls, db, reference: MessageReference, since: int) -> int:
        # The row declaration owns SQLite binding, including the reference DTO.
        grant_field = next(item for item in cls._fields() if item.name == "grant_reference")
        return FieldCodec.decode(int, db.execute(
            f'SELECT COUNT(*) FROM "{cls.declared_name}" WHERE created_at_ms>? AND grant_reference=?',
            (since, grant_field.encode(reference))).fetchone()[0])
