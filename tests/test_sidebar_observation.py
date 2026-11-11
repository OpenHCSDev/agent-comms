"""Sidebar states answer the same questions before and after a real read."""

import gc
import hashlib
import pickle
from collections.abc import Mapping
from dataclasses import fields, replace

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.messaging.channel_targets import IncarnationThreadTarget, ThreadTarget
from agent_comms.messaging.display_references import (
    ChannelDisplayReference,
    CurrentDisplayReferences,
    DisplayReference,
    DisplayReferences,
    HistoricalDisplayReference,
    RetiredThreadReference,
    ThreadDisplayReference,
)
from agent_comms.messaging.historical_views import HistorySource
from agent_comms.nominal.field_codec import MalformedRecord
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import HistoricalRootPurpose, ThreadRows
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.thread_status import (
    RunningThreadStatus,
    StoppedThreadStatus,
)
from agent_comms.registry.threads import NativeThread
from agent_comms.ui_model.sidebar import ChannelRowModel, SidebarModel
from agent_comms.ui_model.unread import ExactUnread, UnobservedUnread


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
        return comms.registry.declare(NativeThread(
            name=name, tags=frozenset(), worktree=Checkout(comms.root.parent)), status=status)
    return declare


def test_channel_counts_working_online_and_registered_members(comms, declare_reference):
    busy = declare_reference("busy", RunningThreadStatus())
    declare_reference("idle", RunningThreadStatus())
    declare_reference("offline")
    snapshot = comms.views.viewer_snapshot(Checkout(comms.root.parent))
    channel = snapshot.channels["#all"]
    row = ChannelRowModel.of(channel, snapshot, frozenset((busy.name, "not-a-member")))
    assert (row.working_agents, row.active_agents, row.registered_agents) == (1, 2, 3)
    assert row.active
    assert row.summary() == "1/2/3 agents · tags: all"
    assert row.tooltip() == "1 in turn / 2 online / 3 total · tags: all"
    idle = ChannelRowModel.of(channel, snapshot, frozenset())
    assert idle.working_agents == 0
    assert not idle.active


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
    retired = references.thread(original.incarnation)
    assert not retired.current_in(references)
    assert references.thread(recreated.incarnation).current_in(references)
    # The old birth never answers with the name its successor now carries.
    assert retired.target() == IncarnationThreadTarget(original.incarnation)
    assert references.thread(recreated.incarnation).target() == ThreadTarget(recreated.name)


def test_retired_dm_peer_has_no_unread_to_paint(comms, declare_reference):
    """A tab for a deleted-and-redeclared peer can paint once before retirement closes it."""
    original = declare_reference()
    comms.threads.delete(original.name)
    recreated = declare_reference(original.name)
    model = SidebarModel([].append)
    snapshot = comms.views.viewer_snapshot(Checkout(comms.root.parent))
    model.apply(snapshot, SidebarModel.derive(snapshot))
    assert model.observation.peer_unread(original.incarnation) == UnobservedUnread()
    assert model.observation.peer_unread(recreated.incarnation) == ExactUnread(0)


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
    # Another birth under a current name is that birth, retired, never the current thread.
    other_birth = RecordedThreadIncarnation("thread-3", 9000.0)
    assert references.thread(other_birth) == RetiredThreadReference("/root", other_birth)
    assert not references.thread(other_birth).current_in(references)

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


def test_a_dm_peer_unread_is_read_by_birth_from_the_sidebars_own_cut(comms, declare_reference):
    """A DM tab keeps the peer's name from when it opened. After a rename the sidebar's unread
    map is keyed by the new name, so a lookup by the tab's name raised KeyError (b-ui, renamed_dm).
    By birth, the sidebar resolves the name through its own snapshot."""
    peer = declare_reference("peer", RunningThreadStatus()).incarnation
    model = SidebarModel(lambda flush: None)
    assert model.observation.peer_unread(peer) == UnobservedUnread()
    comms.registry.rename(peer.name, "renamed-peer")
    snapshot = comms.views.viewer_snapshot(Checkout(comms.root.parent))
    model.apply(snapshot, SidebarModel.derive(snapshot))
    with pytest.raises(KeyError):
        model.observation.bus_unread(peer.name)
    assert model.observation.peer_unread(peer) == model.observation.bus_unread("renamed-peer")
