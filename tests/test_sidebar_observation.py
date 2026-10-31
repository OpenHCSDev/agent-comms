"""Sidebar states answer the same questions before and after a real read."""

from agent_comms.repo import Checkout
import gc
import hashlib
import pickle
from collections.abc import Callable, Mapping
from dataclasses import fields, replace

import pytest

from agent_comms.comms import Comms
from agent_comms.registry_document import ThreadRows

from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import MalformedRecord
from agent_comms.display_references import (
    ChannelDisplayReference, CurrentDisplayReferences, DisplayReference, DisplayReferences,
    HistoricalDisplayReference, ThreadDisplayReference,
)
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.historical_views import HistorySource
from agent_comms.messages import InfoKind, Message, PublicRecord
from agent_comms.registry_document import HistoricalRootPurpose
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.ui_model.sidebar import SidebarModel
from agent_comms.ui_model.unread import ExactUnread, UnobservedUnread
from agent_comms.threads import Thread




def test_sidebar_apply_and_failure_publish_their_original_answers(comms):
    scheduled = []
    model = SidebarModel(scheduled.append)
    assert model.observation.channel_unread("#any") == UnobservedUnread()
    snapshot = comms.views.viewer_snapshot(Checkout(comms.root.parent))
    model.apply(snapshot, SidebarModel.derive(snapshot))
    assert model.observation.require_snapshot() is snapshot
    assert model.observation.channel_order() is snapshot.channel_order
    assert model.observation.channel_unread("#any") == ExactUnread(0)
    with pytest.raises(KeyError):
        model.observation.bus_unread("missing")
    assert set(model.channels.rows) == set(snapshot.channels)
    failure = ValueError("original sidebar read failure")
    model.clear(failure)
    assert not model.channels.rows and not model.threads.rows
    assert model.observation.channel_unread("#any") == UnobservedUnread()
    with pytest.raises(ValueError) as caught:
        model.observation.require_snapshot()
    assert caught.value is failure


@pytest.fixture
def declare_reference(comms):
    def declare(name="reference", status=StoppedThreadStatus()):
        return comms.registry.declare(Thread(
            name=name, tags=frozenset(), worktree=Checkout(comms.root.parent),
        ), status=status)
    return declare


def test_registry_snapshot_constructor_and_pickle_own_immutable_maps(comms, declare_reference):
    original = declare_reference()
    captured = comms.registry.snapshot()
    supplied = dict(captured.threads)
    snapshot = replace(captured, threads=ThreadRows.from_threads(supplied))
    supplied.clear()
    for restored in (snapshot, pickle.loads(pickle.dumps(snapshot, protocol=5))):
        assert restored.purpose == captured.purpose
        assert restored.threads[original.name] == original
        assert restored.born(original.incarnation.require_recorded().lookup) == original
        for declaration in fields(restored):
            value = getattr(restored, declaration.name)
            if isinstance(value, Mapping):
                with pytest.raises(TypeError):
                    value["unowned"] = None


def test_snapshot_restoration_preserves_historical_write_refusal(comms):
    archived = replace(comms.registry.snapshot(), purpose=HistoricalRootPurpose())
    restored = pickle.loads(pickle.dumps(archived, protocol=5))
    with pytest.raises(RelationViolationError, match="read-only"):
        restored.purpose.require_write()


def test_snapshot_constructor_retains_namespace_validation_and_derives_birth_index(
    comms, declare_reference,
):
    original = declare_reference()
    captured = comms.registry.snapshot()
    with pytest.raises(MalformedRecord, match="name differs"):
        replace(captured, threads=ThreadRows.from_threads({"misnamed": original}))
    with pytest.raises(RelationViolationError, match="alias"):
        replace(captured, aliases={"unowned": "absent"})
    retired = replace(captured, threads=ThreadRows.from_threads({}))
    assert not retired.lookup_names
    assert captured.born(original.incarnation.lookup) == original


def test_current_reference_resolves_original_birth_after_registry_rename(comms, declare_reference):
    original = declare_reference(status=RunningThreadStatus())
    comms.registry.rename(original.name, "renamed")
    snapshot = comms.views.viewer_snapshot(Checkout(comms.root.parent))
    references = pickle.loads(pickle.dumps(snapshot, protocol=5)).references
    current = references.thread(original.incarnation)
    assert current.name == "renamed"
    assert current.incarnation == original.incarnation
    assert current.current_in(references)
    assert references.find(f"@{original.name}")[0].reference == current


def test_recreated_thread_reference_resolves_new_birth_not_same_name(comms, declare_reference):
    original = declare_reference()
    before = comms.views.viewer_snapshot(Checkout(comms.root.parent)).references
    comms.threads.delete(original.name)
    recreated = declare_reference(original.name)
    snapshot = comms.views.viewer_snapshot(Checkout(comms.root.parent))
    references = pickle.loads(pickle.dumps(snapshot, protocol=5)).references
    assert recreated.incarnation != original.incarnation
    assert references.thread(recreated.incarnation).incarnation == recreated.incarnation
    assert before.thread(original.incarnation).incarnation == original.incarnation
    with pytest.raises(KeyError):
        references.thread(original.incarnation)


def test_current_reference_membership_and_lookup_share_the_retained_registry(comms, declare_reference):
    original = declare_reference()
    references = CurrentDisplayReferences.from_registry(str(comms.root), comms.registry.snapshot())
    for restored in (references, pickle.loads(pickle.dumps(references, protocol=5))):
        reference = restored.thread(original.incarnation)
        assert reference.current_in(restored)
        assert restored.find(f"@{original.name}")[0].reference == reference
        with pytest.raises(TypeError):
            restored.targets["unowned"] = reference


def _references(count: int) -> CurrentDisplayReferences:
    members = {f"thread-{index}": RecordedThreadIncarnation(f"thread-{index}", 1000.0 + index) for index in range(count)}
    return CurrentDisplayReferences("/root", members, ("#team", "thread-1"))


def _built_references() -> int:
    return sum(isinstance(item, DisplayReference) for item in gc.get_objects())


def test_unpickling_current_references_hashes_and_builds_nothing(monkeypatch):
    packet = pickle.dumps(_references(2000), protocol=5)
    digests = []
    sha256 = hashlib.sha256
    monkeypatch.setattr(hashlib, "sha256", lambda *data: digests.append(data) or sha256(*data))
    built = _built_references()
    restored = pickle.loads(packet)
    assert digests == []
    assert _built_references() == built
    assert restored == pickle.loads(packet)
    assert _built_references() == built


def test_current_targets_answer_as_the_eager_map_did(comms, declare_reference):
    references = _references(5)
    eager: dict[str, DisplayReference] = {
        name: ThreadDisplayReference("/root", incarnation) for name, incarnation in references.members.items()}
    eager.update((name, ChannelDisplayReference("/root", name)) for name in references.channels)
    assert dict(references.targets) == eager
    assert list(references.targets) == list(eager)
    assert len(references.targets) == len(eager)
    assert references.targets == eager and eager == references.targets
    assert references.targets != {**eager, "#other": eager["#team"]}
    assert isinstance(references.targets["thread-1"], ChannelDisplayReference)
    with pytest.raises(KeyError):
        references.targets["missing"]

    plain = DisplayReferences("/root", eager)
    text = "@thread-0 and #team, @thread-1, @thread-22 and @missing"
    assert references.find(text) == plain.find(text)
    assert references.name_end(text, 1) == plain.name_end(text, 1)
    assert all(references.contains(reference) for reference in eager.values())
    assert references.thread(references.members["thread-3"]).incarnation == references.members["thread-3"]
    with pytest.raises(KeyError):
        references.thread(RecordedThreadIncarnation("thread-3", 9000.0))

    declare_reference()
    current = comms.views.viewer_snapshot(Checkout(comms.root.parent)).references
    source = HistorySource.capture_original(comms.root)
    assert current.for_source(source) == DisplayReferences(current.root, dict(current.targets)).for_source(source)


def test_current_reference_source_projection_keeps_historical_identity(comms, declare_reference):
    original = declare_reference()
    references = comms.views.viewer_snapshot(Checkout(comms.root.parent)).references
    source = HistorySource.capture_original(comms.root)
    restored = pickle.loads(pickle.dumps(references, protocol=5))
    recorded = restored.for_source(source)
    assert not isinstance(recorded, CurrentDisplayReferences)
    reference = recorded.find(f"@{original.name}")[0].reference
    assert isinstance(reference, HistoricalDisplayReference)
    assert reference.source == source.key
    assert reference.incarnation == original.incarnation


def test_source_projection_reuses_real_captures_without_merging_revisions(
    comms: Comms, declare_reference: Callable[[], Thread],
) -> None:
    original = declare_reference()
    references = CurrentDisplayReferences.from_registry(
        str(comms.root), comms.registry.snapshot(), ("#team",))
    earlier = HistorySource.capture_original(comms.root)
    recorded = references.for_source(earlier)
    assert references.for_source(None) is references
    assert references.for_source(HistorySource.capture_original(comms.root)) is recorded
    assert recorded.targets["#team"] == references.targets["#team"]
    assert recorded.find(f"@{original.name}")[0].reference.incarnation == original.incarnation

    comms.registry.rename(original.name, "renamed-reference")
    later = HistorySource.capture_original(comms.root)
    assert later.key == earlier.key
    assert later.snapshot_bus_revision == earlier.snapshot_bus_revision
    assert later.snapshot_registry_revision != earlier.snapshot_registry_revision
    renamed = references.for_source(later)
    assert renamed is not recorded
    assert references.for_source(later) is renamed
    assert recorded.find("@renamed-reference") == ()
    assert renamed.find("@renamed-reference")[0].reference.name == "renamed-reference"
    assert renamed.targets["#team"] == recorded.targets["#team"]
    assert references.for_source(earlier) is recorded

    comms.bus.bus.append(lambda seq, db: PublicRecord.from_message(
        Message("renamed-reference", "#team", "another bus revision", InfoKind, seq=seq)))
    appended = HistorySource.capture_original(comms.root)
    assert appended.key == later.key
    assert appended.snapshot_registry_revision == later.snapshot_registry_revision
    assert appended.snapshot_bus_revision != later.snapshot_bus_revision
    after_append = references.for_source(appended)
    assert after_append is not renamed
    assert after_append == renamed
    assert references.for_source(appended) is after_append

    restored = pickle.loads(pickle.dumps(references, protocol=5))
    assert restored == references
    restored_recorded = restored.for_source(earlier)
    assert restored_recorded == recorded
    assert restored_recorded is not recorded
    assert restored.for_source(earlier) is restored_recorded
