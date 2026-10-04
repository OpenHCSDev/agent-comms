"""Durable label/request declarations; the existing coordinator owns storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.typed_table import Column, Index, TypedTable
from agent_comms.working_memory_labels import ModelLabel, SpanLabel
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
    question: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.question.question')")})
    question_version: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.question.sha256')")})
    classifier_pin: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.classifier.pin')")})
    label_kind: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(label, '$.kind')")})

    address: ClassVar[tuple[str, ...]] = (
        "segment_digest", "offset", "length", "question", "question_version", "classifier_pin")
    indexes = (Index(address, unique=True, where="label_kind='model'"), Index(address))

    @classmethod
    def effective(cls, rows: tuple[SpanAnnotationsRow, ...]):
        # Row ids order successive corrections from the same authority. Nominal
        # inheritance, not sequence, decides human versus model authority.
        latest = {}
        for row in sorted(rows, key=lambda value: value.id):
            latest[type(row.label)] = row.label
        return SpanLabel.effective(tuple(latest.values()))


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
    question: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.question.question')")})
    question_version: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.question.sha256')")})
    classifier_pin: str = field(init=False, compare=False, metadata={"sql": Column(
        generated="json_extract(request, '$.classifier.pin')")})

    unique = (SpanAnnotationsRow.address,)
    indexes = (Index(("created_at_ms",)),)
