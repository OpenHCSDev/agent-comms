"""Sidebar states answer the same questions before and after a real read."""

from agent_comms.repo import Checkout
import pickle
from collections.abc import Mapping
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_comms.registry_document import InvalidRegistryDocument, ThreadRows

from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.field_codec import FieldCodec, MalformedRecord, ThreadLookup
from agent_comms.session_file import BusSessionLocation
from agent_comms.display_references import CurrentDisplayReferences, HistoricalDisplayReference
from agent_comms.historical_views import HistorySource
from agent_comms.registry_document import HistoricalRootPurpose
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.thread_text import GivenThreadText
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
    assert snapshot.lookup_names  # Derived cached views must not enter the pickle.
    for restored in (snapshot, pickle.loads(pickle.dumps(snapshot, protocol=5))):
        assert restored.purpose == captured.purpose
        assert restored.threads[original.name] == original
        lookup = original.incarnation.require_recorded().lookup
        assert restored.born(lookup) == original
        assert dict(restored.lookup_names) == {lookup: original.name}
        assert list(restored.lookup_names.items()) == [(lookup, original.name)]
        assert list(restored.lookup_names.values()) == [original.name]
        assert restored.lookup_names == {lookup: original.name}
        with pytest.raises(TypeError):
            restored.lookup_names[lookup] = original.name
        with pytest.raises(TypeError):
            del restored.lookup_names[lookup]
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
    captured = comms.registry.snapshot()
    before = captured.lookup_names
    lookup = original.incarnation.lookup
    comms.registry.rename(original.name, "renamed")
    assert comms.registry.snapshot().born(lookup).name == "renamed"
    assert before[lookup] == original.name
    assert captured.born(lookup) == original
    snapshot = comms.views.viewer_snapshot(Checkout(comms.root.parent))
    references = pickle.loads(pickle.dumps(snapshot, protocol=5)).references
    current = references.thread(original.incarnation)
    assert current.name == "renamed"
    assert current.incarnation == original.incarnation
    assert current.current_in(references)
    assert references.find(f"@{original.name}")[0].reference == current


def test_recreated_thread_reference_resolves_new_birth_not_same_name(comms, declare_reference):
    original = declare_reference()
    captured = comms.registry.snapshot()
    before = comms.views.viewer_snapshot(Checkout(comms.root.parent)).references
    comms.threads.delete(original.name)
    deleted = comms.registry.snapshot().lookup_names
    recreated = declare_reference(original.name)
    current = comms.registry.snapshot()
    assert original.incarnation.lookup not in deleted
    assert original.incarnation.lookup not in current.lookup_names
    assert current.born(recreated.incarnation.lookup) == recreated
    assert captured.born(original.incarnation.lookup) == original
    with pytest.raises(UnregisteredThreadError):
        current.born(original.incarnation.lookup)
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


@pytest.mark.parametrize("decoded", (False, True))
def test_captured_lookup_acquires_and_binds_only_requested_complete_rows(
    comms, declare_reference, decoded,
):
    originals = tuple(declare_reference(f"lookup-{number}") for number in range(12))
    selected = replace(
        originals[0], session_file=BusSessionLocation(Path("lookup-selected.jsonl")),
    )
    invalid = originals[-1]
    captured = comms.registry.snapshot()
    raw = captured.threads.to_wire()
    raw[selected.name] = FieldCodec.encode_fields(selected)
    raw[invalid.name][FieldCodec.field_plan(Thread, "model").key] = 123
    rows = ThreadRows.from_wire(raw)
    rows.bind(comms.registry.store._bind_thread)
    if decoded:
        rows[selected.name] = selected  # A genuine write removes this row from _raw.
    snapshot = replace(captured, threads=rows)
    decoded_before = frozenset(snapshot.threads._decoded)
    expected = {thread.name: thread.incarnation.lookup for thread in originals}

    lookup = snapshot.lookup_names
    assert lookup
    assert len(lookup) == len(originals)
    assert tuple(lookup) == tuple(expected[name] for name in snapshot.threads)
    assert set(lookup.keys()) == set(expected.values())
    assert all(key in lookup and key in lookup.keys() for key in expected.values())
    missing = ThreadLookup.mint()
    assert missing not in lookup and missing not in lookup.keys()
    assert lookup.get(missing) is None
    with pytest.raises(KeyError) as caught:
        lookup[missing]
    assert caught.value.args == (missing,)
    assert frozenset(snapshot.threads._decoded) == decoded_before

    acquired = snapshot.born(selected.incarnation.lookup)
    assert acquired is snapshot.require(selected.name)
    assert acquired.session_file == selected.session_file
    assert acquired.session_file.bound_to(comms.root) is acquired.session_file
    assert lookup.get(selected.incarnation.lookup) == selected.name
    assert snapshot.born(selected.incarnation.lookup) is acquired
    assert frozenset(snapshot.threads._decoded) == decoded_before | {selected.name}
    with pytest.raises(InvalidRegistryDocument):
        snapshot.born(invalid.incarnation.lookup)
    with pytest.raises(InvalidRegistryDocument):
        {key: snapshot.born(key) for key in lookup}
    with pytest.raises(UnregisteredThreadError):
        comms.registry.require_lookup(missing)


@pytest.mark.parametrize("change", ("add", "delete", "rebirth"))
@pytest.mark.parametrize("new_first", (False, True))
def test_lookup_membership_stays_with_empty_and_unprimed_cuts(
    comms, declare_reference, change, new_first,
):
    original = declare_reference()
    template = comms.registry.snapshot()
    rows = ThreadRows.from_wire(
        {} if change == "add" else {original.name: FieldCodec.encode_fields(original)}
    )
    before = replace(template, threads=rows)
    # Even an obtained empty view must remain empty after the shared rows change.
    empty_view = before.lookup_names if change == "add" else None
    if change == "delete":
        del rows[original.name]
        expected_after = ()
    else:
        replacement = original
        if change == "rebirth":
            comms.threads.delete(original.name)
            replacement = declare_reference(original.name)
            assert replacement.incarnation != original.incarnation
        rows[replacement.name] = replacement
        expected_after = (replacement,)
    after = replace(template, threads=rows)
    expected_before = () if change == "add" else (original,)
    cuts = ((before, expected_before), (after, expected_after))
    candidates = {original.incarnation.lookup, ThreadLookup.mint()}
    candidates.update(thread.incarnation.lookup for thread in expected_after)
    for snapshot, expected in reversed(cuts) if new_first else cuts:
        lookup = snapshot.lookup_names
        names = {thread.incarnation.lookup: thread.name for thread in expected}
        assert tuple(lookup) == tuple(names)
        assert tuple(lookup.keys()) == tuple(names)
        assert len(lookup) == len(snapshot.threads) == len(names)
        assert dict(lookup) == names
        for key in candidates:
            assert (key in lookup) == (key in names)
            assert lookup.get(key) == names.get(key)
            if key in names:
                assert lookup[key] == names[key]
                assert snapshot.born(key) is snapshot.threads[names[key]]
            else:
                with pytest.raises(KeyError):
                    lookup[key]
                with pytest.raises(UnregisteredThreadError):
                    snapshot.born(key)
    if empty_view is not None:
        assert not empty_view and not tuple(empty_view)


@pytest.mark.parametrize("decoded", (False, True))
def test_current_reference_acquires_only_captured_incarnations(comms, declare_reference, decoded):
    original = declare_reference(status=RunningThreadStatus())
    unread = declare_reference("unread")
    comms.registry.rename(original.name, "renamed")
    captured = comms.registry.snapshot()
    raw = captured.threads.to_wire()
    raw[unread.name][FieldCodec.field_plan(Thread, "model").key] = 123
    snapshot = replace(captured, threads=ThreadRows.from_wire(raw))
    if decoded:
        snapshot.require("renamed")

    references = CurrentDisplayReferences.from_registry(str(comms.root), snapshot, ("#comms",))
    for _ in range(100):
        repeated = CurrentDisplayReferences.from_registry(str(comms.root), snapshot, ("#comms",))
        assert repeated.members["renamed"] is references.members["renamed"]
        assert repeated.members["unread"] is references.members["unread"]
    with pytest.raises(InvalidRegistryDocument):
        snapshot.require(unread.name)
    assert references.members[original.name].name == "renamed"
    assert references.members["renamed"].wire_birth == original.incarnation.wire_birth
    assert references.find(f"@{original.name}")[0].reference.name == "renamed"
    assert references.find("#comms")[0].reference.name == "#comms"


@pytest.mark.parametrize("entry", (
    FieldCodec.field_plan(Thread, "goal"), FieldCodec.field_plan(Thread, "role"),
))
def test_cold_shape_and_birth_capture_do_not_acquire_unrequested_goal_fields(
    comms, declare_reference, entry,
):
    original = declare_reference()
    captured = comms.registry.snapshot()
    raw = captured.threads.to_wire()
    raw[original.name][entry.key] = []
    rows = ThreadRows.from_wire(raw)
    assert not rows._captured
    assert not rows._decoded
    snapshot = replace(captured, threads=rows)
    assert snapshot.incarnation(original.name) == original.incarnation
    assert original.incarnation.lookup in snapshot.lookup_names
    assert not snapshot.threads._decoded
    with pytest.raises(MalformedRecord):
        snapshot.threads.determining(original.name)
    with pytest.raises(InvalidRegistryDocument):
        snapshot.born(original.incarnation.lookup)


def test_lookup_metadata_is_carried_from_snapshot_through_data_only_revision(
    comms, declare_reference,
):
    original = declare_reference(status=RunningThreadStatus())
    declare_reference("unrelated")
    before = comms.registry.snapshot()
    lookup = original.incarnation.lookup
    original_index = before.lookup_names
    metadata = before.threads._lookup_names
    changed = comms.registry.update_thread(
        original.name, lambda thread: replace(thread, task=GivenThreadText("data-only edit")),
    )
    after = comms.registry.snapshot()
    assert after.born(lookup) == changed
    assert after.threads._lookup_names is metadata
    assert original_index[lookup] == original.name
    assert before.born(lookup) == original
    assert tuple(after.lookup_names) == tuple(after.incarnation(name).require_recorded().lookup
                                            for name in after.threads)
    comms.registry.rename(original.name, "renamed")
    renamed = comms.registry.snapshot()
    assert renamed.born(lookup).name == "renamed"
    assert original_index[lookup] == original.name
    assert after.born(lookup).name == original.name
    assert tuple(renamed.lookup_names) == tuple(renamed.incarnation(name).require_recorded().lookup
                                              for name in renamed.threads)


@pytest.mark.parametrize("entry,value", (
    (FieldCodec.field_plan(Thread, "name"), "invalid name"),
    (FieldCodec.field_plan(Thread, "created_at"), []),
    (FieldCodec.field_plan(Thread, "goal"), []),
    (FieldCodec.field_plan(Thread, "role"), []),
))
def test_captured_reference_rows_refuse_malformed_determining_fields(declare_reference, entry, value):
    original = declare_reference()
    raw = FieldCodec.encode_fields(original)
    raw[entry.key] = value
    with pytest.raises(MalformedRecord):
        ThreadRows.from_wire({original.name: raw}).determining(original.name)


def test_captured_reference_rows_refuse_unknown_or_absent_required_fields(declare_reference):
    original = declare_reference()
    raw = FieldCodec.encode_fields(original)
    missing_birth = dict(raw)
    del missing_birth[FieldCodec.field_plan(Thread, "created_at").key]
    for malformed in ({**raw, "undeclared_field": "refuse"}, missing_birth):
        with pytest.raises(MalformedRecord):
            ThreadRows.from_wire({original.name: malformed}).determining(original.name)


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
