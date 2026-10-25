"""Restoration changes visibility without replay or transferring live ownership."""

from agent_comms.repo import Checkout
from agent_comms.channel_targets import CommandTarget


from bus_record_fixture import prepare_native_comms
from conftest import installed_comms

import os
from dataclasses import replace

import pytest

from agent_comms.field_codec import TurnId
from agent_comms.registry_document import ThreadRows

from agent_comms.session_file import RegistrySessionLocation
from agent_comms.coordination_tables.executions import NoPointedExecution
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import wire
from agent_comms.display_order import LastActivityThreadSort
from agent_comms.errors import RelationViolationError
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from agent_comms.turn_lease import NoTurnRecord
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread


def test_restore_keeps_live_owner_and_bus_while_importing_saved_stopped_identity(tmp_path):
    old = installed_comms(tmp_path / "old")
    current = installed_comms(tmp_path / "current")
    live = Thread('live', frozenset({'comms'}), Checkout.from_wire(str(tmp_path)), process_identity=ProcessIdentity.capture(os.getpid()))
    missing = Thread('missing', frozenset({'nra', 'openhcs'}), Checkout.from_wire(str(tmp_path)), process_identity=ProcessIdentity.capture(os.getpid()), session_file=RegistrySessionLocation.from_path(str(tmp_path / 'saved.jsonl'), old.root), title=GivenThreadText('Preserved title'), task=GivenThreadText('Original task'))
    old.registry.declare(live)
    old.registry.declare(missing)
    missing = old.agents.begin_turn("missing", TurnId('310c74d7ac243239a928d4021c709acb')).thread
    old.messaging.send("live", "missing", "Old pending work must not be replayed").message.message_id
    current.registry.declare(replace(live, title=GivenThreadText("Current live title")))
    current.messaging.send_user_message(CommandTarget.named('#comms'), "Current message", worktree=str(tmp_path))
    before = current.registry.snapshot()
    bus = current.bus.bus.revision()
    old_bus = old.bus.bus.revision()
    source = replace(old.registry.snapshot(), aliases={"former-name": "missing"})

    assert current.registry.restore_stopped(source, ("live", "missing")) == ("missing",)
    after = current.registry.snapshot()
    assert after.threads["live"] == before.threads["live"]
    assert after.owner_generations["live"] == before.owner_generations["live"]
    assert after.claim_generation("live") == before.claim_generation("live")
    assert after.threads["missing"] == replace(missing, process_identity=NoProcess(), turn_record=NoTurnRecord())
    assert after.statuses["missing"] == StoppedThreadStatus()
    assert after.aliases["former-name"] == "missing"
    assert current.bus.bus.revision() == bus
    assert old.bus.bus.revision() == old_bus
    assert not current.bus.inbox("missing").page.messages
    first = current.registry.store.path.read_bytes()
    assert current.registry.restore_stopped(source, ("live", "missing")) == ()
    assert current.registry.store.path.read_bytes() == first


@pytest.mark.parametrize("collision", ["name", "incarnation", "alias"])
def test_restore_conflict_has_no_partial_registry_write(tmp_path, collision):
    old = wire(tmp_path / "old")
    current = wire(tmp_path / "current")
    original = Thread('owner', frozenset(), Checkout.from_wire(str(tmp_path)))
    current.registry.declare(original)
    good = Thread('good', frozenset(), Checkout.from_wire(str(tmp_path)))
    conflicting = Thread('owner' if collision == 'name' else 'other', frozenset(), Checkout.from_wire(str(tmp_path)))
    if collision == "incarnation":
        conflicting = replace(conflicting, created_at=original.created_at)
    old.registry.declare(good)
    old.registry.declare(conflicting)
    if collision == "alias":
        current.registry.rename("owner", "renamed")
        conflicting = replace(conflicting, name="owner")
    source = old.registry.snapshot()
    source = replace(source, threads=ThreadRows.from_threads({"good": good, conflicting.name: conflicting}))
    before = current.registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError):
        current.registry.restore_stopped(source, ("good", conflicting.name))
    assert current.registry.store.path.read_bytes() == before


def test_restore_keeps_archived_status(tmp_path):
    old = wire(tmp_path / "old")
    new = wire(tmp_path / "new")
    thread = Thread('archived', frozenset(), Checkout.from_wire(str(tmp_path)))
    old.registry.register(thread, ArchivedThreadStatus())
    new.registry.restore_stopped(old.registry.snapshot(), (thread.name,))
    assert new.registry.status(thread.name) == ArchivedThreadStatus()
    assert new.registry.require(thread.name).process_identity == NoProcess()


def test_catalog_restore_preserves_current_preferences_and_historical_channels(tmp_path):
    old = installed_comms(tmp_path / "old")
    new = installed_comms(tmp_path / "new")
    for c in (old, new):
        c.channels.create_tag("comms")
    old.channels.create_tag("nra")
    old.channels.create_tag("openhcs")
    old.channels.set_channel_metadata("#openhcs", parent="#nra", archived=False)
    old.channels.set_channel_any_mode("#nra", True)
    old.channels.set_channel_pinned("#nra", True)
    old.channels.set_channel_pinned("#all", True)
    new.channels.set_channel_sort("#comms", LastActivityThreadSort)
    before = new.bus.require_channel("#comms")
    old_files = {p: p.read_bytes() for p in old.root.glob("*.json")}

    with new.bus.opened_activity() as activity, new.channels.catalog.editing() as document:
        document.restore_missing(
            old.channels.catalog.read(), activity, existing=True
        )
    assert new.bus.require_channel("#comms") == before
    assert not new.bus.require_channel("#all").pinned
    for name in ("#nra", "#openhcs"):
        assert new.bus.require_channel(name) == old.bus.require_channel(
            name
        )
    assert old_files == {p: p.read_bytes() for p in old.root.glob("*.json")}
    with new.bus.opened_activity() as activity, new.channels.catalog.editing() as document:
        document.restore_missing(
            old.channels.catalog.read(), activity, existing=True
        )
    assert new.bus.require_channel("#comms") == before
    assert new.bus.require_channel("#nra").any_mode


@pytest.mark.parametrize("repair_existing", [False, True])
def test_private_restoration_allows_new_cohort_without_starting_old_subscribers(
    tmp_path, repair_existing
):
    from conftest import accept_initial
    from agent_comms.coordinator import Coordination

    old, current = wire(tmp_path / "old"), prepare_native_comms(wire(tmp_path / "current"))
    live = Thread('live', frozenset({'comms'}), Checkout.from_wire(str(tmp_path)), process_identity=ProcessIdentity.capture(os.getpid()))
    missing = Thread('missing', frozenset({'comms'}), Checkout.from_wire(str(tmp_path)), process_identity=ProcessIdentity.capture(os.getpid()))
    old.registry.declare(missing)
    # The registry assigns storage roots at declaration; restoration must leave that thread alone.
    live = current.registry.declare(live)
    root_id = current.registry.prepare_native_root().namespace
    source = old.registry.snapshot()
    with Coordination(str(current.root / "coordination.sqlite3")) as store:
        live_before = store.participants.register(
            live.incarnation.lookup, committed=True
        ).value
        if repair_existing:
            current.registry.restore_stopped(source, (missing.name,))
        message = (
            current.messaging.send_user_message(
                CommandTarget.named('#comms'), "@live Reply once", worktree=str(tmp_path)
            )
            if repair_existing
            else None
        )
        bus_before = current.bus.bus.revision() if message else None
        current.registry.restore_stopped(source, (missing.name,))
        snapshot = current.registry.snapshot()
        assert snapshot.statuses[missing.name] == StoppedThreadStatus()
        assert snapshot.threads[missing.name].process_identity == NoProcess()
        assert snapshot.threads[live.name] == live
        assert store.participants.get(live.incarnation.lookup) == live_before
        assert (
            store.participants.get(missing.incarnation.lookup).pointer.execution_id == NoPointedExecution()
        )
        if bus_before is not None:
            assert current.bus.bus.revision() == bus_before
        else:
            message = current.messaging.send_user_message(
                CommandTarget.named('#comms'), "@live Reply once", worktree=str(tmp_path)
            )
        accept_initial(current.bus, root_id, message.seq, store)
        assert tuple(
            store.session._connection.execute(
                "SELECT member_count, claim_count, sealed FROM claim_batch_receipts "
                "WHERE wire_seq=?",
                (message.seq,),
            ).fetchone()
        ) == (2, 1, 1)
        assert [
            tuple(row)
            for row in store.session._connection.execute(
                "SELECT recipient, disposition FROM wake_claims WHERE wire_seq=?", (message.seq,)
            ).fetchall()
        ] == [(live.name, "full_pending")]
        snapshot = current.registry.snapshot()
        assert current.registry.restore_stopped(source, (missing.name,)) == ()
        assert current.registry.snapshot() == snapshot


def test_public_restoration_declares_participant_without_native_authority(tmp_path):
    old, current = wire(tmp_path / "old"), wire(tmp_path / "current")
    old.registry.declare(Thread('missing', frozenset({'comms'}), Checkout.from_wire(str(tmp_path))))
    assert current.registry.restore_stopped(old.registry.snapshot(), ("missing",)) == ("missing",)
    from agent_comms.coordinator import Coordination
    with Coordination(str(current.root / "coordination.sqlite3")) as store:
        assert store.participants.get(current.registry.require("missing").incarnation.lookup).committed
    assert not current.registry.store.guard_path.exists()
