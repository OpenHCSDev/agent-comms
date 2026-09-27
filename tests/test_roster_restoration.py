"""Restoration changes visibility without replay or transferring live ownership."""

import os
from dataclasses import replace

import pytest

from agent_comms import Thread
from agent_comms.declarations import ActiveTurn, RelationViolationError, ThreadSort, ThreadStatus
from agent_comms.operations import Comms


def test_restore_keeps_live_owner_and_bus_while_importing_saved_stopped_identity(tmp_path):
    old = Comms(tmp_path / "old")
    current = Comms(tmp_path / "current")
    live = Thread("live", frozenset({"comms"}), str(tmp_path), pid=os.getpid())
    missing = Thread(
        "missing",
        frozenset({"nra", "openhcs"}),
        str(tmp_path),
        pid=os.getpid(),
        session_file=str(tmp_path / "saved.jsonl"),
        title="Preserved title",
        task="Original task",
        active_turn=ActiveTurn("old-turn", os.getpid()),
    )
    old.register(live)
    old.register(missing)
    old.send("live", "missing", "Old pending work must not be replayed")
    current.register(replace(live, title="Current live title"))
    current.initialize_private_initial_protocol()
    current.initialize_private_claim_protocol()
    current.send_user_message("#comms", "Current message", worktree=str(tmp_path))
    before = current.registry.snapshot()
    bus = current.bus._path.read_bytes()
    old_bus = old.bus._path.read_bytes()
    source = replace(old.registry.snapshot(), aliases={"former-name": "missing"})

    assert current.registry.restore_stopped(source, ("live", "missing")) == ("missing",)
    after = current.registry.snapshot()
    assert after.threads["live"] == before.threads["live"]
    assert after.owner_epochs["live"] == before.owner_epochs["live"]
    assert after.admission_generations["live"] == before.admission_generations["live"]
    assert after.threads["missing"] == replace(missing, pid=0, active_turn=None)
    assert after.statuses["missing"] is ThreadStatus.STOPPED
    assert after.aliases["former-name"] == "missing"
    assert current.bus._path.read_bytes() == bus
    assert old.bus._path.read_bytes() == old_bus
    assert not current.inbox("missing")
    first = current.registry._path.read_bytes()
    assert current.registry.restore_stopped(source, ("live", "missing")) == ()
    assert current.registry._path.read_bytes() == first


@pytest.mark.parametrize("collision", ["name", "incarnation", "alias"])
def test_restore_conflict_has_no_partial_registry_write(tmp_path, collision):
    old = Comms(tmp_path / "old")
    current = Comms(tmp_path / "current")
    original = Thread("owner", frozenset(), str(tmp_path))
    current.register(original)
    good = Thread("good", frozenset(), str(tmp_path))
    conflicting = Thread("owner" if collision == "name" else "other", frozenset(), str(tmp_path))
    if collision == "incarnation":
        conflicting = replace(conflicting, created_at=original.created_at)
    old.register(good)
    old.register(conflicting)
    if collision == "alias":
        current.registry.rename("owner", "renamed")
        conflicting = replace(conflicting, name="owner")
    source = old.registry.snapshot()
    source = replace(source, threads={"good": good, conflicting.name: conflicting})
    before = current.registry._path.read_bytes()
    with pytest.raises(RelationViolationError, match="identity conflicts"):
        current.registry.restore_stopped(source, ("good", conflicting.name))
    assert current.registry._path.read_bytes() == before


def test_restore_keeps_archived_status(tmp_path):
    old = Comms(tmp_path / "old")
    new = Comms(tmp_path / "new")
    thread = Thread("archived", frozenset(), str(tmp_path))
    old.registry.register(thread, ThreadStatus.ARCHIVED)
    new.registry.restore_stopped(old.registry.snapshot(), (thread.name,))
    assert new.registry.status(thread.name) is ThreadStatus.ARCHIVED
    assert new.registry.require(thread.name).pid == 0


def test_catalog_restore_preserves_current_preferences_and_historical_channels(tmp_path):
    old = Comms(tmp_path / "old")
    new = Comms(tmp_path / "new")
    for c in (old, new):
        c.set_channel("comms", frozenset({"comms"}))
    old.set_channel("nra", frozenset({"nra"}))
    old.set_channel("openhcs", frozenset({"openhcs"}))
    old.set_channel_metadata("#openhcs", parent="#nra", archived=False)
    old.set_channel_any_mode("#nra", True)
    old.set_channel_pinned("#nra", True)
    old.set_channel_pinned("#all", True)
    new.set_channel_sort("#comms", ThreadSort.LAST_ACTIVITY)
    before = new.channel_catalog.resolve("#comms")
    old_files = {p: p.read_bytes() for p in old.root.glob("*.json")}

    new.channel_catalog.restore_missing(old.channel_catalog)
    assert new.channel_catalog.resolve("#comms") == before
    assert not new.channel_catalog.resolve("#all").pinned
    for name in ("#nra", "#openhcs"):
        assert new.channel_catalog.resolve(name) == old.channel_catalog.resolve(name)
    assert old_files == {p: p.read_bytes() for p in old.root.glob("*.json")}
    new.channel_catalog.restore_missing(old.channel_catalog)
    assert new.channel_catalog.resolve("#comms") == before
    assert new.channel_catalog.resolve("#nra").any_mode
