"""Named destinations share counting, change attribution and human read contracts."""
from pathlib import Path

from agent_comms.repo import Checkout
import pytest

from agent_comms.channel_targets import CommandTarget, IncarnationThreadTarget, NamedTarget
from agent_comms.errors import HumanSendRefusedError, UnregisteredThreadError
from agent_comms.coordination_errors import DMScopeChanged
from agent_comms.historical_views import ConversationObservation, DMDisplayHistory
from agent_comms.message_page import ConversationMessagePageRequest
from agent_comms.read_basis import NamedViewer
from agent_comms.thread_execution import ExternalThreadExecution
from bus_record_fixture import context_comms
from agent_comms.threads import Thread


@pytest.fixture
def named_target_journey(tmp_path):
    comms = context_comms(tmp_path)
    worktree = str(tmp_path)
    comms.registry.declare(Thread(
        "cli", frozenset({"team"}), Checkout(Path(worktree)), execution=ExternalThreadExecution,
    ))
    viewer = comms.messaging.user_identity(worktree).name
    comms.messaging.send("cli", "Alice", "Agent direct message")
    comms.messaging.send("cli", viewer, "Human direct message")
    comms.messaging.send("cli", "#team", "Channel message")
    return comms


def test_named_targets_share_count_changes_and_human_read_contracts(named_target_journey):
    comms = named_target_journey
    worktree = str(comms.registry.require("cli").worktree)
    direct: NamedTarget = CommandTarget.named("cli")
    channel: NamedTarget = CommandTarget.named("#team")
    for target in (direct, channel):
        assert comms.bus.pending_count("Alice", target) == 1

    snapshot = comms.registry.snapshot()
    catalog = comms.channels.catalog.read()
    activity = comms.bus.bus.activity_clocks()
    assert direct.changed_threads(snapshot.threads, snapshot, catalog, activity) == ("cli",)
    assert frozenset(channel.changed_threads(snapshot.threads, snapshot, catalog, activity)) == {
        "sender", "Alice", "Bob", "cli",
    }

    before = comms.views.viewer_snapshot(worktree)
    assert before.unread["cli"].count == 1
    assert before.channel_unread["#team"].count == 1
    comms.views.mark_user_view_read(direct.name, worktree=worktree)
    after_direct = comms.views.viewer_snapshot(worktree)
    assert after_direct.unread["cli"].count == 0
    assert after_direct.channel_unread["#team"].count == 1
    comms.views.mark_user_view_read(channel.name, worktree=worktree)
    after_channel = comms.views.viewer_snapshot(worktree)
    assert after_channel.unread["cli"].count == 0
    assert after_channel.channel_unread["#team"].count == 0
    for target in (direct, channel):
        assert comms.bus.pending_count("Alice", target) == 1


def test_retained_dm_target_follows_rename_through_send_page_and_revision(tmp_path):
    comms = context_comms(tmp_path)
    worktree = str(tmp_path)
    target = IncarnationThreadTarget(comms.registry.require("Alice").incarnation)
    original = comms.messaging.send_user_message(target, "before rename", worktree=worktree)
    comms.registry.rename("Alice", "renamed")
    sent = comms.messaging.send_user_message(target, "after rename", worktree=worktree)
    assert sent.target == "renamed"
    delivery = comms.bus.bus.delivery(sent.seq)
    assert [(row.canonical_thread, row.recipient_lookup) for row in delivery.recipients] == [
        ("renamed", target.incarnation.require_recorded().lookup)]
    page = comms.views.dm_display_page(target, worktree=worktree)
    assert page.messages == (original, sent)
    assert page.selection.basis.peer_identity == target.incarnation
    generic_page = comms.views.history_page(target, viewer=NamedViewer(sent.sender))
    assert generic_page.messages == page.messages
    observed = DMDisplayHistory.revision(
        comms.views, target, ConversationObservation(), worktree=worktree)
    assert observed.selection.peer == target.incarnation
    assert observed.selection.peer.name == "renamed"


@pytest.mark.parametrize("recreate", [False, True])
def test_retained_dm_refuses_a_removed_birth_at_every_selection_boundary(tmp_path, recreate):
    comms = context_comms(tmp_path)
    worktree = str(tmp_path)
    target = IncarnationThreadTarget(comms.registry.require("Alice").incarnation)
    viewer = comms.messaging.user_identity(worktree)
    comms.registry.unregister("Alice")
    comms.registry.remove("Alice")
    if recreate:
        comms.registry.declare(Thread("Alice", frozenset({"team"}), Checkout(Path(worktree))))
    before = comms.bus.bus.revision()
    with pytest.raises(HumanSendRefusedError):
        comms.messaging.send_user_message(target, "must not reach successor", worktree=worktree)
    assert comms.bus.bus.revision() == before
    with pytest.raises(UnregisteredThreadError):
        comms.views.dm_display_page(target, worktree=worktree)
    with pytest.raises(UnregisteredThreadError):
        DMDisplayHistory.revision(comms.views, target, ConversationObservation(), worktree=worktree)
    with pytest.raises(UnregisteredThreadError):
        comms.views.history_page(target, viewer=NamedViewer(viewer.name))


def test_dm_page_refuses_replacement_between_registry_capture_and_bus_read(tmp_path, monkeypatch):
    comms = context_comms(tmp_path)
    worktree = str(tmp_path)
    target = IncarnationThreadTarget(comms.registry.require("Alice").incarnation)
    viewer = comms.messaging.user_identity(worktree)
    read_opened = ConversationMessagePageRequest.read_opened
    successor_messages = []

    def replace_before_read(request, source):
        comms.registry.unregister("Alice")
        comms.registry.remove("Alice")
        comms.registry.declare(Thread("Alice", frozenset({"team"}), Checkout(Path(worktree))))
        successor_messages.append(comms.messaging.send("Alice", viewer.name, "successor reply").message)
        return read_opened(request, source)

    monkeypatch.setattr(ConversationMessagePageRequest, "read_opened", replace_before_read)
    with pytest.raises(DMScopeChanged):
        comms.views.dm_display_page(target, worktree=worktree)
    assert len(successor_messages) == 1
    assert comms.bus.bus.lookup(successor_messages[0].seq) == successor_messages[0]
