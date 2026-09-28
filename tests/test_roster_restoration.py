"""Restoration changes visibility without replay or transferring live ownership."""

import os
from dataclasses import replace

import pytest

from agent_comms.comms import Comms
from agent_comms.display_order import ThreadSort
from agent_comms.errors import RelationViolationError
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.turn_lease import ActiveTurn


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
    old.threads.register(live)
    old.threads.register(missing)
    old.messaging.send("live", "missing", "Old pending work must not be replayed")
    current.threads.register(replace(live, title="Current live title"))
    current.messaging.initialize_private_initial_protocol()
    current.messaging.initialize_private_claim_protocol()
    current.messaging.send_user_message("#comms", "Current message", worktree=str(tmp_path))
    before = current.registry.snapshot()
    bus = current.bus.log.path.read_bytes()
    old_bus = old.bus.log.path.read_bytes()
    source = replace(old.registry.snapshot(), aliases={"former-name": "missing"})

    assert current.registry.restore_stopped(source, ("live", "missing")) == ("missing",)
    after = current.registry.snapshot()
    assert after.threads["live"] == before.threads["live"]
    assert after.owner_generations["live"] == before.owner_generations["live"]
    assert after.admission_generations["live"] == before.admission_generations["live"]
    assert after.threads["missing"] == replace(missing, pid=0, active_turn=None)
    assert after.statuses["missing"] == StoppedThreadStatus()
    assert after.aliases["former-name"] == "missing"
    assert current.bus.log.path.read_bytes() == bus
    assert old.bus.log.path.read_bytes() == old_bus
    assert not current.bus.inbox("missing")
    first = current.registry.store.path.read_bytes()
    assert current.registry.restore_stopped(source, ("live", "missing")) == ()
    assert current.registry.store.path.read_bytes() == first


@pytest.mark.parametrize("collision", ["name", "incarnation", "alias"])
def test_restore_conflict_has_no_partial_registry_write(tmp_path, collision):
    old = Comms(tmp_path / "old")
    current = Comms(tmp_path / "current")
    original = Thread("owner", frozenset(), str(tmp_path))
    current.threads.register(original)
    good = Thread("good", frozenset(), str(tmp_path))
    conflicting = Thread("owner" if collision == "name" else "other", frozenset(), str(tmp_path))
    if collision == "incarnation":
        conflicting = replace(conflicting, created_at=original.created_at)
    old.threads.register(good)
    old.threads.register(conflicting)
    if collision == "alias":
        current.registry.rename("owner", "renamed")
        conflicting = replace(conflicting, name="owner")
    source = old.registry.snapshot()
    source = replace(source, threads={"good": good, conflicting.name: conflicting})
    before = current.registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError, match="identity conflicts"):
        current.registry.restore_stopped(source, ("good", conflicting.name))
    assert current.registry.store.path.read_bytes() == before


def test_restore_keeps_archived_status(tmp_path):
    old = Comms(tmp_path / "old")
    new = Comms(tmp_path / "new")
    thread = Thread("archived", frozenset(), str(tmp_path))
    old.registry.register(thread, ArchivedThreadStatus())
    new.registry.restore_stopped(old.registry.snapshot(), (thread.name,))
    assert new.registry.status(thread.name) == ArchivedThreadStatus()
    assert new.registry.require(thread.name).pid == 0


def test_catalog_restore_preserves_current_preferences_and_historical_channels(tmp_path):
    old = Comms(tmp_path / "old")
    new = Comms(tmp_path / "new")
    for c in (old, new):
        c.channels.set_channel("comms", frozenset({"comms"}))
    old.channels.set_channel("nra", frozenset({"nra"}))
    old.channels.set_channel("openhcs", frozenset({"openhcs"}))
    old.channels.set_channel_metadata("#openhcs", parent="#nra", archived=False)
    old.channels.set_channel_any_mode("#nra", True)
    old.channels.set_channel_pinned("#nra", True)
    old.channels.set_channel_pinned("#all", True)
    new.channels.set_channel_sort("#comms", ThreadSort.LAST_ACTIVITY)
    before = new.channels.catalog.resolve("#comms")
    old_files = {p: p.read_bytes() for p in old.root.glob("*.json")}

    new.channels.catalog.restore_missing(old.channels.catalog)
    assert new.channels.catalog.resolve("#comms") == before
    assert not new.channels.catalog.resolve("#all").pinned
    for name in ("#nra", "#openhcs"):
        assert new.channels.catalog.resolve(name) == old.channels.catalog.resolve(name)
    assert old_files == {p: p.read_bytes() for p in old.root.glob("*.json")}
    new.channels.catalog.restore_missing(old.channels.catalog)
    assert new.channels.catalog.resolve("#comms") == before
    assert new.channels.catalog.resolve("#nra").any_mode


@pytest.mark.parametrize("repair_existing", [False, True])
def test_private_restoration_allows_new_cohort_without_starting_old_subscribers(
    tmp_path, repair_existing
):
    from agent_comms.bus_publication import stable_thread_lookup
    from agent_comms.cohort_schema import install_private_cohort_schema
    from agent_comms.coordination_cohort import accept_initial_cohort
    from agent_comms.coordination_store import MutationStore

    old, current = Comms(tmp_path / "old"), Comms(tmp_path / "current")
    live = Thread("live", frozenset({"comms"}), str(tmp_path), pid=os.getpid())
    missing = Thread("missing", frozenset({"comms"}), str(tmp_path), pid=os.getpid())
    old.threads.register(missing)
    current.threads.register(live)
    root_id = current.messaging.initialize_private_initial_protocol()
    source = old.registry.snapshot()
    with MutationStore(str(current.root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        live_before = store.register_participant(
            stable_thread_lookup(live.created_at), live.name, live.name, committed=True
        ).value
        if repair_existing:
            current.registry.restore_stopped(source, (missing.name,))
        message = (
            current.messaging.send_user_message("#comms", "@live Reply once", worktree=str(tmp_path))
            if repair_existing
            else None
        )
        bus_before = current.bus.log.path.read_bytes() if message else None
        current.threads.restore_stopped(source, (missing.name,))
        snapshot = current.registry.snapshot()
        assert snapshot.statuses[missing.name] == StoppedThreadStatus()
        assert snapshot.threads[missing.name].pid == 0
        assert snapshot.threads[live.name] == live
        assert store.participant(stable_thread_lookup(live.created_at)) == live_before
        assert (
            store.participant(stable_thread_lookup(missing.created_at)).pointer.execution_id is None
        )
        if bus_before is not None:
            assert current.bus.log.path.read_bytes() == bus_before
        else:
            message = current.messaging.send_user_message(
                "#comms", "@live Reply once", worktree=str(tmp_path)
            )
        accept_initial_cohort(current.bus, root_id, message.seq, store)
        assert tuple(
            store._connection.execute(
                "SELECT member_count, claim_count, sealed FROM claim_batch_receipts "
                "WHERE wire_seq=?",
                (message.seq,),
            ).fetchone()
        ) == (2, 1, 1)
        assert [
            tuple(row)
            for row in store._connection.execute(
                "SELECT recipient, disposition FROM wake_claims WHERE wire_seq=?", (message.seq,)
            ).fetchall()
        ] == [(live.name, "full_pending")]
        snapshot = current.registry.snapshot()
        assert current.threads.restore_stopped(source, (missing.name,)) == ()
        assert current.registry.snapshot() == snapshot


def test_public_restoration_does_not_create_coordinator(tmp_path):
    old, current = Comms(tmp_path / "old"), Comms(tmp_path / "current")
    old.threads.register(Thread("missing", frozenset({"comms"}), str(tmp_path)))
    assert current.threads.restore_stopped(old.registry.snapshot(), ("missing",)) == ("missing",)
    assert not (current.root / "coordination.sqlite3").exists()
