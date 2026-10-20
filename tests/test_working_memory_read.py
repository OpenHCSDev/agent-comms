"""Read/correct original annotation addresses without reader-side installation."""
from dataclasses import replace
import hashlib
import sqlite3

import pytest

from agent_comms.coordinator import Coordination
from agent_comms.coordination_tables.annotations import SpanAnnotationsRow
from agent_comms.field_codec import FieldCodec
from agent_comms.registration import Registration
from agent_comms.thread_identity import UserThreadRole
from agent_comms.threads import Thread
from agent_comms.turn_context import (
    ContextSpan, ContributionCoordinates, FileProvenance, SystemLayerSegment,
    UnattributedProvenance,
)
from agent_comms.working_memory_annotations import WorkingMemoryAnnotations
from agent_comms.working_memory_labels import (
    AnswerProbability, HumanLabel, JevClassifier, ModelLabel, QuestionVersion,
)
from agent_comms.working_memory_questions import (
    CommitmentSpan, KindQuestion, OtherSpan, RuleSpan,
)


def local_system(text, sources=()):
    """Unattested local content; no SDK/provider observation is claimed."""
    raw = text.encode()
    return SystemLayerSegment(provenance=(UnattributedProvenance(),), content=text,
        tokens=0, sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw),
        source_spans=sources)


def test_readers_do_not_create_a_missing_store(tmp_path):
    path = tmp_path / "missing" / "coordination.sqlite3"
    segment = local_system("Keep the original instructions.")
    with pytest.raises(sqlite3.OperationalError):
        WorkingMemoryAnnotations.for_segment(path, segment.measured_manifest(), JevClassifier.version())
    assert not path.parent.exists()


def test_file_attribution_is_bounded_by_original_assembly_range():
    first = FileProvenance("first-source", "1" * 64)
    second = FileProvenance("second-source", "2" * 64)
    a = "Keep source A.\n"
    b = "Keep source B."
    ranges = (ContributionCoordinates.capture(SystemLayerSegment, (first,), 0, a),
              ContributionCoordinates.capture(SystemLayerSegment, (second,), len(a.encode()), b))
    segment = local_system(a + b, ranges)
    manifest = segment.measured_manifest()
    span = ContextSpan(manifest.sha256, ContributionCoordinates.capture(
        SystemLayerSegment, (second,), len(a.encode()), b))
    assert manifest.contains_span(span)
    assert not manifest.contains_span(replace(span,
        coordinates=replace(span.coordinates, provenance=(first,))))
    assert not manifest.contains_span(replace(span,
        coordinates=replace(span.coordinates, offset=0)))
    historical = replace(local_system(a + b), provenance=(first, second))
    assert all(span.coordinates.provenance == (UnattributedProvenance(),)
               for span in historical.public_spans())
