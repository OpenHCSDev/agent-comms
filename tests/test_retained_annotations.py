"""Selected original annotation data/refusals, not external disclosure/native proof."""
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from cutover.carry_annotation_failure_kinds import AnnotationFailureKinds

from agent_comms.comms import wire
from agent_comms.context_store import (
    CapturedContext,
    ContextManifestReference,
    ContextStore,
)
from agent_comms.execution.context_prior import UNREAD_CONTEXT
from agent_comms.coordination.coordinator import StandaloneCoordination
from agent_comms.coordination.database import CoordinationStore, ReadableStore
from agent_comms.coordination.tables.annotations import AnnotationRequestsRow, SpanAnnotationsRow
from agent_comms.coordination.working_memory_annotations import (
    PreviouslyRequestedAnnotation,
    WorkingMemoryAnnotations,
)
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.turn_context import (
    ContextManifest,
    ContextSpan,
    ContributionCoordinates,
    ObservedDispatchRequest,
    RecordedContextTurn,
    SystemLayerSegment,
    ToolCatalogSegment,
    UnattributedProvenance,
)
from agent_comms.execution.turn_item import PiRequestKey
from agent_comms.execution.working_memory_disclosure import DisclosureState
from agent_comms.errors import RelationViolationError
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.practice import PracticeCopy, PracticeMeasurement
from agent_comms.installer.probes.carry import Carry
from agent_comms.installer.probes.inputs import SelectedInputsProbe
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.task_sources import AnnotationDisclosureGrant
from agent_comms.nominal.field_codec import FieldCodec, MessageId, TurnId
from agent_comms.planning.task_scope import ProjectTaskScope
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation, TurnIdentity
from agent_comms.storage.locked_store import LiveFilesRequest
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.working_memory.working_memory_labels import (
    HumanLabel,
    JevChoice,
    JevClassifier,
    JevResponse,
    JevUsage,
    ModelLabel,
    QuestionVersion,
)
from agent_comms.working_memory.working_memory_questions import KindQuestion, OtherSpan, SpanAnswer
from agent_comms.working_memory.working_memory_requests import (
    AnnotationBudgetExhausted,
    ClassifierAnswerRejectedAnnotationOutcome,
    ClassifierUnreachableAnnotationOutcome,
    DisclosureRequest,
    SubmittedAnnotationOutcome,
)
from working_memory_fixture import local_system


@dataclass(frozen=True)
class AnnotationSource:
    store: StandaloneCoordination
    context: CapturedContext
    grant: AnnotationDisclosureGrant
    selected_requests: tuple[DisclosureRequest, ...]

    @property
    def path(self) -> Path:
        return self.store.session.path

    @property
    def reference(self) -> ContextManifestReference:
        return ContextManifestReference(self.context.source, self.context.record_id)


def completed_label(
    store: StandaloneCoordination, request: DisclosureRequest,
    grant: AnnotationDisclosureGrant, identifier: str,
) -> ModelLabel:
    original = store.annotations.reserve(request, grant)
    answers = request.question.question.answer_family.members_with(SpanAnswer)
    choice = JevChoice('choice', OtherSpan.declared_name, 0.5,
                       {answer.declared_name: 1 / len(answers) for answer in answers})
    response = JevResponse(identifier, request.classifier.pin, 'fixture',
                           {request.question.question.declared_name: choice}, JevUsage(0, 3, 1))
    label = choice.label(request.span, request.question, request.classifier, response)
    store.annotations.complete(original, response, label)
    return label


def disclosed_request(
    segment: SystemLayerSegment, span: ContextSpan, grant: MessageReference,
) -> DisclosureRequest:
    return DisclosureRequest.capture(
        span, JevClassifier.version(), KindQuestion,
        DisclosureState.capture(segment, span, KindQuestion), grant,
    )


@pytest.fixture
def annotation_source(tmp_path: Path) -> Iterator[AnnotationSource]:
    root = tmp_path / 'annotation-source'
    comms = wire(root)
    # The ordinary bus owner supplies the original empty bus, not a reminted
    # destination source. Nothing here sends a message or contacts a provider.
    with comms.bus.bus.reading(), InputDispositions(root).snapshot():
        pass
    owner = RecordedThreadIncarnation('Fixture', 17001.0)
    author = RecordedThreadIncarnation('FixtureHuman', 17002.0)
    reference = MessageReference(1, MessageId.mint('stored-annotation-grant'))
    grant = AnnotationDisclosureGrant(
        scope=ProjectTaskScope(project=Checkout(tmp_path)), subject=reference,
        source_user=author, recipient=owner, per_hour=8, segments=(SystemLayerSegment,),
    )
    texts = ('Still submitted.\n', 'Recorded failure.\n', 'Original answer.')
    positions = []
    offset = 0
    for text in texts:
        positions.append(ContributionCoordinates.capture(
            SystemLayerSegment, (UnattributedProvenance(),), offset, text,
        ))
        offset += len(text.encode())
    segment = local_system(''.join(texts), tuple(positions))
    spans = segment.public_spans()
    manifest = ContextManifest(
        thread=owner, turn=RecordedContextTurn(TurnId('1' * 32), TurnIdentity(owner, 1)),
        segments=(segment.measured_manifest(),), counter='fixture-counter',
        request_id=ObservedDispatchRequest(PiRequestKey('fixture-annotation-context')),
    )
    captured = ContextStore(root).record_after(UNREAD_CONTEXT, manifest)
    with StandaloneCoordination(str(CoordinationStore.path_in(root)), clock_ms=lambda: 1000) as store:
        requests = tuple(disclosed_request(segment, span, reference) for span in spans)
        store.annotations.reserve(requests[0], grant)
        failed = store.annotations.reserve(requests[1], grant)
        store.annotations.fail(failed, ClassifierUnreachableAnnotationOutcome())
        model = completed_label(store, requests[2], grant, 'fixture-answer')
        # Stored correction data tests precedence, not human/publication authority.
        correction = HumanLabel.correct(model, OtherSpan, author)
        old_request = replace(requests[2], question=QuestionVersion(KindQuestion, 'b' * 64))
        completed_label(store, old_request, grant, 'fixture-old-question')
        other_version = replace(model.classifier, pin=model.classifier.pin + '-fixture-other')
        other_reference = MessageReference(2, MessageId.mint('stored-other-classifier-grant'))
        other_grant = replace(grant, subject=other_reference, classifier=other_version)
        other_request = replace(requests[2], classifier=other_version, grant=other_reference)
        completed_label(store, other_request, other_grant, 'fixture-other-classifier')
        other_segment = local_system('Not in the selected context.')
        other_span, = other_segment.public_spans()
        other_usage = disclosed_request(other_segment, other_span, reference)
        completed_label(store, other_usage, grant, 'fixture-same-grant-other-context')
        unused_reference = MessageReference(3, MessageId.mint('stored-unused-grant'))
        unused_grant = replace(grant, subject=unused_reference)
        store.annotations.reserve(replace(other_usage,
            question=QuestionVersion(KindQuestion, 'c' * 64), grant=unused_reference), unused_grant)
        foreign_span = ContextSpan(segment.sha256, ContributionCoordinates.capture(
            ToolCatalogSegment, (UnattributedProvenance(),), 1, 't',
        ))
        assert not segment.measured_manifest().contains_span(foreign_span)
        with store.session.writing() as db:
            SpanAnnotationsRow(label=correction, created_at_ms=1000).insert(db)
            SpanAnnotationsRow(label=replace(model, span=foreign_span), created_at_ms=1000).insert(db)
        yield AnnotationSource(store, captured, grant, (*requests, old_request, other_request))


def test_selected_context_copy_preserves_answers_versions_outcomes_and_grant_usage(
    annotation_source: AnnotationSource, tmp_path: Path,
) -> None:
    source = annotation_source
    before = CoordinationStore.digest(source.path)
    contexts_before = ContextStore(source.path.parent).path.read_bytes()
    expected = WorkingMemoryAnnotations.for_context(
        ReadableStore(source.path), (source.context.value,), JevClassifier.version(),
    )
    assert len(expected) == 2
    copy = PracticeCopy(source.path.parent, tmp_path / 'selected-annotations')
    copy.include((Bus.path_in(source.path.parent), InputDispositions.path_in(source.path.parent)))
    copy.include_contexts((), requests=(source.reference,), maximum_bytes=8 * 2**20)
    copy.include_inputs((), maximum_bytes=8 * 2**20)
    retained_path = CoordinationStore.path_in(copy.path)
    assert WorkingMemoryAnnotations.for_context(
        ReadableStore(retained_path), (source.context.value,), JevClassifier.version(),
    ) == expected
    with source.store.session.read():
        db = source.store.session._connection
        originals = tuple(AnnotationRequestsRow.select(db, order_by=('id',)))
        labels = tuple(SpanAnnotationsRow.select(db, order_by=('id',)))
        usage = AnnotationRequestsRow.requests_since(db, source.grant.subject, 0)
    with StandaloneCoordination(str(retained_path)) as retained:
        with retained.session.read():
            db = retained.session._connection
            rows = tuple(AnnotationRequestsRow.select(db, order_by=('id',)))
            assert rows == tuple(row for row in originals
                if row.request.grant in {request.grant for request in source.selected_requests})
            assert tuple(SpanAnnotationsRow.select(db, order_by=('id',))) == tuple(
                row for row in labels if row.label.span.coordinates.kind is SystemLayerSegment)
            assert AnnotationRequestsRow.requests_since(db, source.grant.subject, 0) == usage == 5
        for request in source.selected_requests[:3]:
            with pytest.raises(PreviouslyRequestedAnnotation):
                retained.annotations.reserve(request, source.grant)
        # The exact grant-wide count, including the unselected context, decides
        # the same budget refusal on source and retained data; no request is sent.
        exhausted = replace(source.grant, per_hour=usage)
        with pytest.raises(AnnotationBudgetExhausted):
            exhausted.require_budget(usage)
    assert CoordinationStore.digest(source.path) == before
    assert ContextStore(source.path.parent).path.read_bytes() == contexts_before


def test_effective_annotations_require_an_original_allocated_row_id(
    annotation_source: AnnotationSource,
) -> None:
    source = annotation_source
    with source.store.session.read():
        stored = SpanAnnotationsRow.select(
            source.store.session._connection, order_by=('id',),
        )[0]
    assert SpanAnnotationsRow.effective((stored,)) == stored.label
    unstored = SpanAnnotationsRow(label=stored.label, created_at_ms=stored.created_at_ms)
    with pytest.raises(TypeError):
        SpanAnnotationsRow.effective((unstored,))


def test_context_selection_cannot_follow_finalized_coordinator_evidence(
    annotation_source: AnnotationSource, tmp_path: Path,
) -> None:
    source = annotation_source
    before = CoordinationStore.digest(source.path)
    copy = PracticeCopy(source.path.parent, tmp_path / 'wrong-selection-order')
    copy.include((Bus.path_in(source.path.parent), InputDispositions.path_in(source.path.parent)))
    copy.include_inputs((), maximum_bytes=8 * 2**20)
    with pytest.raises(UpgradeRefused):
        copy.include_contexts((), requests=(source.reference,), maximum_bytes=8 * 2**20)
    assert not ContextStore(copy.path).path.exists()
    assert CoordinationStore.digest(source.path) == before


def test_selected_annotation_materialization_writes_only_the_private_target(
    annotation_source: AnnotationSource, tmp_path: Path,
) -> None:
    source = annotation_source
    root = source.path.parent
    before = CoordinationStore.digest(source.path)
    contexts_before = ContextStore.digest(ContextStore.path_in(root))
    expected = WorkingMemoryAnnotations.for_context(
        ReadableStore(source.path), (source.context.value,), JevClassifier.version(),
    )
    comms = wire(root)
    gate = MaintenanceBarrier(comms.registry.store.path)
    held = gate.retarget(gate.begin(MaintenanceOperator.current()), LaunchRuntime.running())
    copy = PracticeCopy(root, tmp_path / 'captured-annotations')
    try:
        probe = SelectedInputsProbe(
            root, copy.path, 8 * 2**20, requests=(source.reference,),
        )
        with held.holding(), InstallCommands() as commands:
            measured = commands.probe(
                probe, LaunchRuntime.running().interpreter, cwd=tmp_path,
            )
        assert measured == PracticeMeasurement.capture(copy.path)
        contexts = ContextStore(copy.path).original_segments()
        retained = WorkingMemoryAnnotations.for_segments(
            ReadableStore(CoordinationStore.path_in(copy.path)), contexts, JevClassifier.version(),
        )
        assert retained == expected
        assert CoordinationStore.digest(source.path) == before
        assert ContextStore.digest(ContextStore.path_in(root)) == contexts_before
    finally:
        gate.clear(held)


def test_a_failure_stored_as_an_error_name_carries_to_the_member_that_caught_it(
    annotation_source: AnnotationSource, tmp_path: Path,
):
    """Before the failure family a failed request stored the caught error's class name. The
    carry places each name under the clause that caught it, also when several rows share one
    name, and refuses a name it cannot place."""
    table = AnnotationRequestsRow.declared_name
    outcome = FieldCodec.field_plan(AnnotationRequestsRow, 'outcome')
    step = AnnotationFailureKinds(LiveFilesRequest(annotation_source.path.parent), tmp_path / 'spill')
    with annotation_source.store.session.writing() as db:
        row_id, other_id = (db.execute(
            f"SELECT id FROM \"{table}\" WHERE json_extract(outcome, '$.kind') = ? ORDER BY id LIMIT 1",
            (kind.declared_name,)).fetchone()[0]
            for kind in (ClassifierUnreachableAnnotationOutcome, SubmittedAnnotationOutcome))
        Carry.rewrite_stored(db, AnnotationRequestsRow, outcome, [
            ('{"kind": "failed", "error": "JSONDecodeError"}', stored) for stored in (row_id, other_id)])

    assert step.carry(apply=False).changed == 2
    step.carry(apply=True)

    with annotation_source.store.session.read():
        db = annotation_source.store.session._connection
        carried = tuple(AnnotationRequestsRow.one(db, id=stored).outcome for stored in (row_id, other_id))
    assert carried == (ClassifierAnswerRejectedAnnotationOutcome(),) * 2
    assert step.carry(apply=False).changed == 0
    with annotation_source.store.session.writing() as db:
        Carry.rewrite_stored(db, AnnotationRequestsRow, outcome,
                             [('{"kind": "failed", "error": "NoSuchError"}', row_id)])
    with pytest.raises(RelationViolationError, match="NoSuchError"):
        step.carry(apply=False)
