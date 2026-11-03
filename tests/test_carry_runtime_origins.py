"""The explicit runtime carry preserves historical rows without binding them to a birth."""
from pathlib import Path

import pytest

from agent_comms.agent_events import ContextUsageObservation
from agent_comms.errors import RelationViolationError
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.pi_counts import PiCount
from agent_comms.reading_attribution import LastKnownReadingAttribution
from agent_comms.runtime_field import UnattributedRuntimeOrigin
from agent_comms.repo import Checkout
from agent_comms.runtime_info import RuntimeInfoStore, RuntimeReadScope
from agent_comms.thread_creation import RecordedCreationTime
from agent_comms.threads import Thread
from agent_comms.thread_identity import UnrecordedThreadIncarnation
from agent_comms.thread_model import DeclaredModel, UndeclaredModel
from cutover.carry_runtime_origins import PreviousRuntimeInfo, PreviousRuntimeInfoDocument, RuntimeOrigins


@pytest.fixture
def runtime_carry(tmp_path: Path) -> RuntimeOrigins:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    return RuntimeOrigins(LiveFilesRequest(root), tmp_path / "spill")


@pytest.fixture
def previous_runtime_rows() -> PreviousRuntimeInfoDocument:
    return PreviousRuntimeInfoDocument({
        "original-map-key": PreviousRuntimeInfo(
            thread="stored-row-spelling", timestamp=19.75, model=DeclaredModel("old/model"),
            context_used=PiCount.from_wire(25), context_size=PiCount.from_wire(100),
        ),
        # This was a valid old thread name, not a document-version marker.
        "version": PreviousRuntimeInfo(thread="version", timestamp=0.0),
    })


def write_previous(carry: RuntimeOrigins, previous: PreviousRuntimeInfoDocument) -> Path:
    path = RuntimeInfoStore.path_in(carry.root)
    path.write_text(FieldCodec.compact(FieldCodec.encode_document(previous, PreviousRuntimeInfoDocument)))
    return path


def test_runtime_carry_preserves_scopes_values_clocks_and_repeat_bytes(
    runtime_carry: RuntimeOrigins, previous_runtime_rows: PreviousRuntimeInfoDocument,
) -> None:
    path = write_previous(runtime_carry, previous_runtime_rows)
    original = path.read_bytes()
    with pytest.raises(MalformedRecord):
        RuntimeInfoStore(path).read()
    proposed = runtime_carry.carry(apply=False)
    assert proposed.changed == 1 and proposed.detail["rows"] == 2
    assert not proposed.rewritten and path.read_bytes() == original
    applied = runtime_carry.carry(apply=True)
    assert applied.changed == 1 and applied.rewritten == (path.relative_to(runtime_carry.root),)
    document = RuntimeInfoStore(path).read()
    key = UnrecordedThreadIncarnation("original-map-key")
    saved = document.observations[key]
    assert saved.model == DeclaredModel("old/model")
    assert saved.context_used == PiCount.from_wire(25)
    assert saved.context_size == PiCount.from_wire(100)
    expected = UnattributedRuntimeOrigin(key.name, "stored-row-spelling", 19.75)
    assert saved.model_observation == expected.capture(DeclaredModel("old/model"))
    assert saved.context_used_observation == expected.capture(PiCount.from_wire(25))
    assert saved.context_size_observation == expected.capture(PiCount.from_wire(100))
    assert UnrecordedThreadIncarnation("version") in document.observations
    assert document.observations[UnrecordedThreadIncarnation("version")].timestamp == 0.0
    successor = path.read_bytes()
    for apply in (False, True):
        assert runtime_carry.carry(apply=apply).changed == 0
        assert path.read_bytes() == successor


def test_carried_history_survives_partial_current_writes_and_removal(
    runtime_carry: RuntimeOrigins, previous_runtime_rows: PreviousRuntimeInfoDocument,
) -> None:
    path = write_previous(runtime_carry, previous_runtime_rows)
    runtime_carry.carry(apply=True)
    store = RuntimeInfoStore(path)
    historical = store.read().observations.copy()
    # Being older than the row does not establish ownership of that old name.
    thread = Thread("original-map-key", frozenset(), Checkout(runtime_carry.root), created_at=RecordedCreationTime(1.0))
    owner = thread.incarnation.require_recorded()
    scope = RuntimeReadScope(owner, (owner.name,))
    before = store.for_thread(scope)
    assert before.model == DeclaredModel("old/model")
    assert before.model_attribution() == LastKnownReadingAttribution((owner.name, "stored-row-spelling"))
    assert before.current_model(owner) == UndeclaredModel()
    store.observe(owner, 30.0, ContextUsageObservation(context_used=PiCount.from_wire(75)))
    after = store.for_thread(scope)
    assert after.model == DeclaredModel("old/model")
    assert after.context_used == PiCount.from_wire(75)
    assert after.context_size == PiCount.from_wire(100)
    assert after.current_model(owner) == UndeclaredModel()
    assert {key: store.read().observations[key] for key in historical} == historical
    # Removing an attested birth must not retire an unknown historical owner.
    store.remove_threads((thread,))
    assert store.read().observations == historical


@pytest.mark.parametrize("apply", [False, True])
def test_malformed_old_runtime_rows_refuse_before_any_publication(runtime_carry: RuntimeOrigins, apply: bool) -> None:
    path = RuntimeInfoStore.path_in(runtime_carry.root)
    path.write_text('{"good":{"thread":"A","ts":1.0},"bad":{"thread":"B","ts":"not-a-clock"}}')
    original = path.read_bytes()
    with pytest.raises(MalformedRecord):
        runtime_carry.carry(apply=apply)
    assert path.read_bytes() == original


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("document", [
    '{"version":3,"observations":[]}',
    '{"version":2,"observations":[["not-an-incarnation",{}]]}',
])
def test_bad_current_runtime_format_is_not_reinterpreted_as_old_rows(runtime_carry: RuntimeOrigins, apply: bool, document: str) -> None:
    path = RuntimeInfoStore.path_in(runtime_carry.root)
    path.write_text(document)
    original = path.read_bytes()
    with pytest.raises(MalformedRecord):
        runtime_carry.carry(apply=apply)
    assert path.read_bytes() == original


def test_empty_old_runtime_document_is_carried_once(runtime_carry: RuntimeOrigins) -> None:
    path = RuntimeInfoStore.path_in(runtime_carry.root)
    path.write_text('{}')
    assert runtime_carry.carry(apply=True).changed == 1
    document = RuntimeInfoStore(path).read()
    assert document.observations == {} and document.version == 2
    assert runtime_carry.carry(apply=True).changed == 0


def test_absent_runtime_document_is_not_created_by_the_carry(runtime_carry: RuntimeOrigins) -> None:
    path = RuntimeInfoStore.path_in(runtime_carry.root)
    for apply in (False, True):
        assert runtime_carry.carry(apply=apply).changed == 0
        assert not path.exists()


def test_runtime_carry_refuses_an_aliased_file(runtime_carry: RuntimeOrigins, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{}')
    path = RuntimeInfoStore.path_in(runtime_carry.root)
    path.symlink_to(outside)
    with pytest.raises(RelationViolationError, match="unconfined"):
        runtime_carry.carry(apply=True)
    assert outside.read_text() == '{}'
