"""Durable label/request declarations; the existing coordinator owns storage."""
from __future__ import annotations

import sqlite3
from abc import abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Self

from agent_comms.coordination.schema import CoordinatorRow
from agent_comms.execution.turn_context import ContextSpan, SegmentManifest
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.storage.typed_table import (
    LOCAL_TABLE_SOURCE,
    AttachedPracticeRebuild,
    Column,
    Generated,
    Index,
    RowId,
    TableSchema,
    TableSource,
    TypedTable,
    UnallocatedRowId,
    sql_literal,
)
from agent_comms.working_memory.working_memory_labels import Classifier, ModelLabel, SpanLabel
from agent_comms.working_memory.working_memory_questions import SpanQuestion
from agent_comms.working_memory.working_memory_requests import AnnotationOutcome, DisclosureRequest


class ContextSpanRows(TypedTable, declared_name=None):
    """Stored answers and request dispositions share original-span eligibility."""

    __slots__ = ()

    @property
    @abstractmethod
    def context_span(self) -> ContextSpan: ...

    @classmethod
    def for_segments(
        cls, db: sqlite3.Connection, segments: Iterable[SegmentManifest], *,
        table_source: TableSource = LOCAL_TABLE_SOURCE, **key: object,
    ) -> Iterator[Self]:
        originals: dict[str, list[SegmentManifest]] = {}
        for segment in segments:
            originals.setdefault(segment.sha256, []).append(segment)
        for row in cls.stream_keys(db, tuple(
            dict(key, segment_digest=digest) for digest in originals
        ), table_source=table_source):
            span = row.context_span
            if any(segment.contains_span(span) for segment in originals[span.segment_sha256]):
                yield row

    @classmethod
    def retain_segments(
        cls, original: AttachedPracticeRebuild, segments: Iterable[SegmentManifest],
    ) -> int:
        # Restored requests may repeat large original prefixes. Consume one
        # segment at a time, not a second materialized context corpus.
        members = cls.members_with(cls)
        return sum(row.retain_original(original)
                   for segment in segments
                   for member in members
                   for row in member.for_segments(original.destination, (segment,),
                                                  table_source=original.source))


@dataclass(frozen=True, kw_only=True)
class SpanAnnotationsRow(ContextSpanRows, CoordinatorRow):
    label: ModelLabel
    created_at_ms: int
    id: Annotated[RowId, Column(primary_key=True, auto_increment=True)] = field(
        default_factory=UnallocatedRowId
    )
    segment_digest: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(label, '$.span.segment_sha256')"),
    )
    offset: ClassVar[Generated] = Generated(
        int,
        Column(generated="json_extract(label, '$.span.coordinates.offset')"),
    )
    length: ClassVar[Generated] = Generated(
        int,
        Column(generated="json_extract(label, '$.span.coordinates.length')"),
    )
    question: ClassVar[Generated] = Generated(
        type[SpanQuestion],
        Column(generated="json_extract(label, '$.question.question')"),
    )
    question_version: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(label, '$.question.sha256')"),
    )
    classifier: ClassVar[Generated] = Generated(
        type[Classifier],
        Column(generated="json_extract(label, '$.classifier.classifier')"),
    )
    classifier_pin: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(label, '$.classifier.pin')"),
    )
    label_kind: ClassVar[Generated] = Generated(
        type[SpanLabel],
        Column(generated="json_extract(label, '$.kind')"),
    )

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
        for row in sorted(rows, key=lambda value: int(value.id)):
            latest[type(row.label)] = row.label
        return SpanLabel.effective(tuple(latest.values()))

    @property
    def context_span(self) -> ContextSpan:
        return self.label.span


@dataclass(frozen=True, kw_only=True)
class AnnotationRequestsRow(ContextSpanRows, CoordinatorRow):
    request: DisclosureRequest
    outcome: AnnotationOutcome
    created_at_ms: int
    id: Annotated[RowId, Column(primary_key=True, auto_increment=True)] = field(
        default_factory=UnallocatedRowId
    )
    segment_digest: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(request, '$.span.segment_sha256')"),
    )
    offset: ClassVar[Generated] = Generated(
        int,
        Column(generated="json_extract(request, '$.span.coordinates.offset')"),
    )
    length: ClassVar[Generated] = Generated(
        int,
        Column(generated="json_extract(request, '$.span.coordinates.length')"),
    )
    question: ClassVar[Generated] = Generated(
        type[SpanQuestion],
        Column(generated="json_extract(request, '$.question.question')"),
    )
    question_version: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(request, '$.question.sha256')"),
    )
    classifier: ClassVar[Generated] = Generated(
        type[Classifier],
        Column(generated="json_extract(request, '$.classifier.classifier')"),
    )
    classifier_pin: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract(request, '$.classifier.pin')"),
    )

    grant_reference: ClassVar[Generated] = Generated(
        MessageReference,
        Column(generated="json_extract(request, '$.grant')"),
    )

    unique = (SpanAnnotationsRow.address,)
    indexes = (Index(("grant_reference", "created_at_ms")),)

    @property
    def context_span(self) -> ContextSpan:
        return self.request.span

    @classmethod
    def retain_required_rows(
        cls, original: AttachedPracticeRebuild, schema: type[TableSchema],
    ) -> int:
        """Keep original grant usage and each request's complete answer/correction group."""
        changes = super().retain_required_rows(original, schema)
        changes += original.retain_keys(cls, tuple(
            {"grant_reference": row.request.grant} for row in cls.stream(original.destination)
        ))
        # One natural address can name multiple correction rows; it is not a
        # foreign key. Compare the actual declared generated columns, without
        # making storage import a reader to reconstruct their domain values.
        related = " AND ".join(
            f"{SpanAnnotationsRow.qualified(name)} IS {cls.qualified(name)}"
            for name in SpanAnnotationsRow.address
        )
        changes += original.retain_rows(
            SpanAnnotationsRow,
            where=f"EXISTS (SELECT 1 FROM main.{LOCAL_TABLE_SOURCE.qualified(cls.declared_name)} "
                  f"WHERE {related})",
        )
        return changes

    @classmethod
    def requests_since(cls, db, reference: MessageReference, since: int) -> int:
        return cls.count(db, where='created_at_ms>?', parameters=(since,), grant_reference=reference)
