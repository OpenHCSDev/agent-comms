"""An old pin stays visible but never becomes a guessed current birth."""
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_comms.activity_clocks import ActivityClocks, ChannelActivity
from agent_comms.catalog_document import CatalogDocument, ChannelOptions
from agent_comms.catalog_store import ChannelCatalog
from agent_comms.channels import AnyOfMatch, ParticipantsView, ViewPredicate
from agent_comms.display_order import LastActivityThreadSort, LastUserInputChannelSort
from agent_comms.errors import RelationViolationError
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.presentation import ChannelView
from agent_comms.registration import Registration
from agent_comms.repo import Checkout
from agent_comms.registry_document import DEFAULT_ROSTER
from agent_comms.thread_creation import RecordedCreationTime
from agent_comms.thread_pins import ThreadPin
from agent_comms.threads import Thread
from cutover.carry_catalog_pin_origins import (
    CatalogPinOrigins, PreviousCatalogDocument, PreviousChannelPreferences,
)


@pytest.fixture
def catalog_carry(tmp_path: Path) -> CatalogPinOrigins:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    return CatalogPinOrigins(LiveFilesRequest(root), tmp_path / "spill")


@pytest.fixture
def previous_catalog() -> PreviousCatalogDocument:
    return PreviousCatalogDocument(
        preferences={
            "#team": PreviousChannelPreferences(
                order=LastActivityThreadSort, created_at=17, pinned=True,
                parent="#all", any_mode=True,
                pinned_threads=frozenset({"original-pin", "a" * 32}),
            ),
            "#all": PreviousChannelPreferences(archived=True),
        },
        tags=frozenset({"team"}),
        saved_views={"saved": ParticipantsView(
            "saved", ViewPredicate(AnyOfMatch, frozenset({"team"})), 23)},
        list_order=LastUserInputChannelSort,
    )


def write_previous(carry: CatalogPinOrigins, document: PreviousCatalogDocument) -> Path:
    path = ChannelCatalog.path_in(carry.root)
    path.write_text(FieldCodec.compact(FieldCodec.encode_document(document, PreviousCatalogDocument)))
    return path


def test_catalog_carry_preserves_all_options_names_and_repeat_bytes(
    catalog_carry: CatalogPinOrigins, previous_catalog: PreviousCatalogDocument,
) -> None:
    path = write_previous(catalog_carry, previous_catalog)
    original = path.read_bytes()
    with pytest.raises(MalformedRecord):
        ChannelCatalog(path).read()
    proposed = catalog_carry.carry(apply=False)
    assert proposed.changed == 1 and proposed.detail["pins"] == 2
    assert not proposed.rewritten and path.read_bytes() == original
    applied = catalog_carry.carry(apply=True)
    assert applied.changed == 1 and applied.rewritten == (path.relative_to(catalog_carry.root),)
    document = ChannelCatalog(path).read()
    assert document.tags == previous_catalog.tags
    assert document.saved_views == previous_catalog.saved_views
    assert document.list_order is previous_catalog.list_order
    for name, before in previous_catalog.preferences.items():
        after = document.preferences[name]
        for plan in FieldCodec.field_plans(ChannelOptions):
            assert plan.current(after) == plan.current(before)
        assert after.pinned_threads == frozenset(ThreadPin.unattributed(pin) for pin in before.pinned_threads)
    successor = path.read_bytes()
    for apply in (False, True):
        assert catalog_carry.carry(apply=apply).changed == 0
        assert path.read_bytes() == successor


def test_carried_pin_is_visible_without_pinning_a_same_name_replacement(
    catalog_carry: CatalogPinOrigins, previous_catalog: PreviousCatalogDocument,
) -> None:
    path = write_previous(catalog_carry, previous_catalog)
    catalog_carry.carry(apply=True)
    store = ChannelCatalog(path)
    registry = Registration(catalog_carry.root / "registry.json")
    thread = registry.declare(Thread(
        "original-pin", frozenset({"team"}), Checkout(catalog_carry.root),
        created_at=RecordedCreationTime(4.0)))
    document = store.read()
    assert not document.thread_pinned("#team", thread.incarnation.require_recorded())
    view = ChannelView.capture(
        document.require_channel("#team", ActivityClocks()), (thread,), registry.snapshot(),
        document.pinned_threads("#team"), {}, {}, ChannelActivity(), DEFAULT_ROSTER,
    )
    assert view.members == ("original-pin",) and not view.pinned_members
    assert set(view.pin_notices) == {
        "last known pin: original-pin (owner unrecorded)",
        f"last known pin: {'a' * 32} (owner unrecorded)",
    }
    pins = document.pinned_threads("#team")
    store.remove_threads((thread,))
    assert store.read().pinned_threads("#team") == pins


def test_catalog_restore_keeps_unattributed_pins_and_current_preferences(
    previous_catalog: PreviousCatalogDocument,
) -> None:
    source = previous_catalog.carried()
    original = FieldCodec.encode_document(source, CatalogDocument)
    destination = CatalogDocument()
    activity = ActivityClocks()
    restored = destination.restore_missing(source, activity, existing=True)
    assert "#team" in restored
    assert destination.pinned_threads("#team") == source.pinned_threads("#team")
    destination.set_thread_pinned(
        "#team", Thread("current", frozenset(), Checkout(Path.cwd()),
                        created_at=RecordedCreationTime(9.0)).incarnation.require_recorded(), True)
    kept = destination.preferences["#team"]
    destination.restore_missing(source, activity, existing=True)
    assert destination.preferences["#team"] == kept
    assert FieldCodec.encode_document(source, CatalogDocument) == original


@pytest.mark.parametrize("data", (
    {"version": 1}, {"version": 3}, {"version": None},
    {"version": 2, "preferences": {"#all": {"pinned_threads": ["a" * 32]}}},
    {"preferences": {"#all": {"pinned_threads": [7]}}},
))
def test_catalog_carry_refuses_malformed_or_future_inputs_without_rewriting(
    catalog_carry: CatalogPinOrigins, data: dict[str, JsonValue],
) -> None:
    path = ChannelCatalog.path_in(catalog_carry.root)
    path.write_text(FieldCodec.compact(data))
    original = path.read_bytes()
    for apply in (False, True):
        with pytest.raises((MalformedRecord, ValueError, TypeError)):
            catalog_carry.carry(apply=apply)
        assert path.read_bytes() == original


def test_absent_catalog_needs_no_carry(catalog_carry: CatalogPinOrigins) -> None:
    assert catalog_carry.carry(apply=False).changed == 0
    assert catalog_carry.carry(apply=True).changed == 0


def test_catalog_carry_refuses_an_external_symlink(catalog_carry: CatalogPinOrigins, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    ChannelCatalog.path_in(catalog_carry.root).symlink_to(outside)
    with pytest.raises(RelationViolationError):
        catalog_carry.carry(apply=True)
    assert outside.read_text() == "{}"
