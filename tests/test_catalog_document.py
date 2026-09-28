"""Lossless one-way catalog migration and one atomic publication boundary."""

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from agent_comms import locked_store
from agent_comms.catalog_store import CatalogMigration, ChannelCatalog
from agent_comms.comms import wire
from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType


@pytest.fixture
def historical_catalog(tmp_path):
    rows = {
        "channels.json": {
            "tags": ["api", "ui"],
            "channels": {"#engineering": ["api", "ui"]},
            "orders": {"#api": "last_message_sent"},
            "created_at": {"#api": 3.0, "#engineering": 5.0},
            "list_order": "last_user_input",
        },
        "channel_pins.json": {"channels": ["#api", "#all"], "threads": {"#api": ["alpha"]}},
        "channel_metadata.json": {
            "channels": {"#api": {"parent": "#engineering", "archived": True, "any_mode": True}}
        },
        "saved_views.json": {
            "views": {
                "work": {
                    "kind": "participants",
                    "predicate": {"match": "any_of", "tags": ["api", "ui"]},
                }
            }
        },
    }
    for name, data in rows.items():
        (tmp_path / name).write_text(json.dumps(data))
    return tmp_path


def test_read_is_nonmutating_then_complete_atomic_migration(historical_catalog):
    root = historical_catalog
    catalog = ChannelCatalog(root / "catalog.json")
    before_bytes = {p: p.read_bytes() for p in CatalogMigration.paths(root)}
    before = catalog.read()
    assert not catalog.path.exists()
    channel = before.resolve("#api")
    assert (channel.order, channel.created_at, channel.parent) == (
        ThreadSort.LAST_MESSAGE,
        3.0,
        "#engineering",
    )
    assert channel.pinned and channel.archived and channel.any_mode
    assert before.pinned_threads("#api") == {"alpha"}
    assert before.resolve("#all").pinned
    assert before.resolve("#engineering").tags == {"api", "ui"}
    assert before.saved_views["work"].created_at == 0.0
    assert before.list_order is ChannelSort.LAST_USER_INPUT
    comms = wire(root)
    comms.channels.create_tag("later")
    migrated = catalog.read()
    assert migrated.tags == before.tags | {"later"}
    assert migrated.audiences == before.audiences
    assert migrated.saved_views == before.saved_views
    assert migrated.list_order == before.list_order
    assert all(migrated.preferences[name] == value for name, value in before.preferences.items())
    assert before_bytes == {p: p.read_bytes() for p in before_bytes}
    assert catalog.source_paths() == (catalog.path,)
    assert FieldCodec.decode(type(migrated), json.loads(catalog.path.read_text())) == migrated
    assert wire(root).channels.catalog.read() == migrated
    # Retired files cannot overwrite current preferences or restore deleted views.
    comms.channels.delete_saved_view("work")
    (root / "saved_views.json").write_text('{"views":{"broken":{}}}')
    assert not catalog.read().saved_views


def test_malformed_migration_keeps_every_original_and_no_partial_publication(historical_catalog):
    root = historical_catalog
    path = root / "channel_metadata.json"
    path.write_text('{"channels":{"#api":{"any_mode":"true"}}}')
    before = {p: p.read_bytes() for p in CatalogMigration.paths(root)}
    with pytest.raises(ValueError):
        wire(root).channels.create_tag("later")
    assert not (root / "catalog.json").exists()
    assert before == {p: p.read_bytes() for p in before}


def test_failed_migration_directory_sync_keeps_sources_and_retryable_absence(historical_catalog):
    root = historical_catalog
    before = {p: p.read_bytes() for p in CatalogMigration.paths(root)}
    catalog = ChannelCatalog(root / "catalog.json")
    real_sync = os.fsync
    failed = False

    def fail_directory(fd):
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("directory publication failed")
        real_sync(fd)

    with patch.object(locked_store.os, "fsync", side_effect=fail_directory):
        with pytest.raises(OSError, match="directory publication failed"):
            with catalog.editing() as document:
                document.list_order = ChannelSort.CREATED
    assert not catalog.path.exists()
    assert before == {p: p.read_bytes() for p in before}
    assert catalog.read().list_order is ChannelSort.LAST_USER_INPUT
    with catalog.editing() as document:
        document.list_order = ChannelSort.CREATED
    assert catalog.read().list_order is ChannelSort.CREATED


def test_update_failure_restores_whole_document_not_just_mode(tmp_path):
    comms = wire(tmp_path)
    comms.channels.create_tag("api")
    comms.channels.set_channel_pinned("api", True)
    before = comms.channels.catalog.path.read_bytes()
    with patch.object(ChannelCatalog, "_write_unlocked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            comms.channels.set_channel_any_mode("api", True)
    assert comms.channels.catalog.path.read_bytes() == before
    assert wire(tmp_path).channels.catalog.read().resolve("#api").pinned
    assert not comms.channels.catalog.read().resolve("#api").any_mode


def test_independent_writers_merge_under_wire_then_document_lock(tmp_path):
    def create(index):
        client = wire(tmp_path)
        name = f"tag-{index}"
        client.channels.create_tag(name)
        client.channels.set_channel_pinned(name, True)
        client.channels.set_channel_any_mode(name, True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(create, range(8)))
    document = wire(tmp_path).channels.catalog.read()
    assert document.tags == {f"tag-{i}" for i in range(8)}
    assert all(
        document.resolve(f"#tag-{i}").pinned and document.resolve(f"#tag-{i}").any_mode
        for i in range(8)
    )


def test_observer_revision_covers_migration_and_subsequent_preferences(historical_catalog):
    observer = wire(historical_catalog)
    first = observer.views.revision()
    writer = wire(historical_catalog)
    writer.channels.set_channel_any_mode("api", False)
    second = observer.views.revision()
    assert first.files != second.files
    writer.channels.set_channel_pinned("api", False)
    third = observer.views.revision()
    assert second.files != third.files
    channel = observer.channels.catalog.read().resolve("#api")
    assert not channel.any_mode and not channel.pinned


def test_message_decode_fields_are_declared_not_a_second_roster():
    @dataclass(frozen=True, slots=True)
    class AnnotatedMessage(Message):
        annotation: str = ""

    original = AnnotatedMessage("alpha", "#api", "hello", MessageType.INFO, annotation="retained")
    assert AnnotatedMessage.from_wire(original.to_wire()) == original
    with pytest.raises(ValueError, match="Unknown fields"):
        Message.from_wire({**original.to_wire(), "extension": "not supported"})
    with pytest.raises(ValueError):
        Message.from_wire(
            {**Message("alpha", "#api", "hello", MessageType.INFO).to_wire(), "notice": "false"}
        )
    historical = Message.from_wire({"from": "alpha", "to": "#api", "text": "old", "type": "info"})
    assert historical.timestamp == 0.0
    assert historical.display_metadata == {}


def test_initial_publication_rechecks_current_catalog_revision(tmp_path, monkeypatch):
    from agent_comms import audience_manifest
    from agent_comms.errors import RelationViolationError
    from test_private_human_ingress import _root

    comms, _store, _root_id, _lookups = _root(tmp_path)
    comms.channels.create_tag("team")
    sequence = comms.bus.log.latest_sequence()
    original = audience_manifest.freeze_audience

    def capture_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        # A noncooperating writer changes the source during admission. The
        # already-held wire lock is not itself evidence of unchanged bytes.
        with comms.channels.catalog.editing() as document:
            document.list_order = ChannelSort.CREATED
        return result

    monkeypatch.setattr(audience_manifest, "freeze_audience", capture_then_change)
    with pytest.raises(RelationViolationError, match="registry/catalog revision changed"):
        comms.messaging.send_user_message("#team", "must not append", worktree=str(tmp_path))
    assert comms.bus.log.latest_sequence() == sequence


def test_message_integer_timestamp_keeps_original_identity():
    original = Message("alpha", "#api", "old", MessageType.INFO, timestamp=123, seq=7)
    encoded = original.to_wire()
    restored = Message.from_wire(encoded)
    assert restored.to_wire() == encoded
    assert type(restored.timestamp) is int
    assert restored.message_id == original.message_id
