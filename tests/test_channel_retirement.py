"""Saved audience retirement retains addressed history, never routing authority."""

import json
from dataclasses import replace

import pytest

from agent_comms.catalog_store import ChannelCatalog
from agent_comms.channels import AnyOfMatch, SavedView, ViewKind, ViewPredicate
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread
from agent_comms.wire_metadata import WireMetadata
from test_private_human_ingress import _root


def saved_catalog(root):
    from agent_comms.catalog_document import CatalogDocument, ChannelPreferences
    from agent_comms.display_order import ChannelSort, ThreadSort

    document = CatalogDocument(
        tags=frozenset({"api", "ui"}),
        preferences={
            "#engineering": ChannelPreferences(
                created_at=17,
                pinned=True,
                order=ThreadSort.LAST_ACTIVITY,
                pinned_threads=frozenset({"a"}),
                parent="#ui",
            ),
            "#api": ChannelPreferences(parent="#engineering", any_mode=True),
        },
        saved_views={
            "engineering": SavedView(
                "engineering",
                ViewKind.ACTIVITY,
                ViewPredicate(AnyOfMatch, frozenset({"api", "ui"})),
                17,
                frozenset({"#engineering"}),
            )
        },
        list_order=ChannelSort.LAST_USER_INPUT,
    )
    ChannelCatalog(root / "catalog.json").replace(document)
    return FieldCodec.encode(document)


def test_saved_projection_preserves_preferences_history_and_has_no_writable_alias(tmp_path):
    comms = wire(tmp_path)
    for name, tags in (("a", {"api"}), ("b", {"ui"}), ("other", set())):
        comms.threads.register(Thread(name, frozenset(tags), str(tmp_path)))
    original = saved_catalog(tmp_path)
    root_id = comms.messaging.initialize_private_initial_protocol()
    rows = [
        Message("other", target, str(i), MessageType.INFO, timestamp=i, seq=i)
        for i, target in enumerate(("#engineering", "#api", "#ui", "#engineering"), 1)
    ]
    before = "".join(json.dumps(row.to_wire()) + "\n" for row in rows)
    comms.bus.log.path.write_text(before)
    comms.bus.log.path.chmod(0o600)
    comms.bus.log.write_metadata_unlocked(WireMetadata(
        last_seq=4, admission_after_seq=4, writer_protocol_version=1,
        wire_root_id=root_id, claim_envelopes_version=1,
    ))
    catalog = comms.channels.catalog
    imported = catalog.read()
    assert json.loads(catalog.path.read_text()) == original  # read is not activation
    projection = imported.resolve("#engineering")
    assert projection.view.original_targets == {"#engineering"}
    assert projection.pinned and projection.parent == "#ui"
    assert projection.created_at == 17 and imported.pinned_threads("#engineering") == {"a"}
    assert imported.resolve("#api").exact and imported.resolve("#api").any_mode
    assert "#engineering" not in imported.targets_for(frozenset({"api"}))
    assert set(
        next(v.members for v in comms.views.channel_views() if v.channel.name == "#engineering")
    ) == {"a", "b"}
    tail = comms.views.channel_display_page("#engineering", limit=2)
    assert [m.seq for m in tail.messages] == [3, 4]
    older = comms.views.channel_display_page("#engineering", before=3, limit=2)
    assert [m.seq for m in older.messages] == [1, 2]
    assert [m.target for m in comms.views.channel_history("#engineering")] == [
        m.target for m in rows
    ]
    for send in (
        lambda: comms.messaging.send("other", "#engineering", "blocked"),
        lambda: comms.messaging.send_user_message(
            "#engineering", "blocked", worktree=str(tmp_path)
        ),
    ):
        with pytest.raises(ValueError, match="not a routable target"):
            send()
    assert comms.bus.log.path.read_text() == before
    assert "audiences" not in json.loads(catalog.path.read_text())
    assert catalog.read() == imported
    assert FieldCodec.decode(type(imported), json.loads(catalog.path.read_text())) == imported
    with pytest.raises(ValueError, match="archive"):
        comms.channels.delete_saved_view("engineering")
    with pytest.raises(ValueError, match="reserved"):
        comms.channels.create_tag("engineering")
    comms.channels.set_channel_metadata("engineering", parent="#ui", archived=True)
    assert catalog.read().resolve("#engineering").archived
    comms.channels.set_saved_view(
        replace(imported.saved_views["engineering"], original_targets=frozenset())
    )
    assert catalog.read().saved_views["engineering"].original_targets == {"#engineering"}
    assert comms.bus.log.path.read_text() == before


def test_private_initial_rejects_view_but_routes_exact_tags_and_membership(tmp_path):
    comms, _store, _identity, _lookups = _root(tmp_path)
    comms.channels.set_saved_view(
        SavedView("team-view", ViewKind.ACTIVITY, ViewPredicate(AnyOfMatch, frozenset({"team"})))
    )
    before = comms.bus.log.latest_sequence()
    with pytest.raises(ValueError, match="not a routable target"):
        comms.messaging.send_user_message("#team-view", "blocked", worktree=str(comms.root))
    assert comms.bus.log.latest_sequence() == before
    comms.messaging.send_user_message("#team", "exact", worktree=str(comms.root))
    comms.channels.update_tags("bob", remove=frozenset({"team"}))
    rows = comms.views.channel_history("#team")
    assert [row.body for row in rows if row.membership is None] == ["exact"]
    assert any(row.membership is not None for row in rows)
    assert not any(row.target == "#team-view" for row in rows)


def test_restoring_same_named_view_keeps_current_predicate_and_original_target_history(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    saved_catalog(source_root)
    source = ChannelCatalog(source_root / "catalog.json").read()
    comms = wire(tmp_path / "destination")
    comms.channels.create_tag("api")
    current = SavedView(
        "engineering",
        ViewKind.PARTICIPANTS,
        ViewPredicate(AnyOfMatch, frozenset({"api"})),
        created_at=19,
    )
    comms.channels.set_saved_view(current)
    with comms.channels.catalog.editing() as document:
        document.restore_missing(source, {}, existing=True)
    restored = comms.channels.catalog.read().saved_views["engineering"]
    assert restored.predicate == current.predicate
    assert restored.created_at == 19
    assert restored.original_targets == {"#engineering"}
